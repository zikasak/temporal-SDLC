"""A change too large for one reviewer's context is reviewed in parts."""
import json
import shutil
import uuid
from typing import Any

from temporalio import activity
from temporalio.worker import Worker

from app.languages import get_language
from app.review import activities as review
from app.review.workflows import ReviewWorkflow


def test_each_part_gets_only_its_files_diff_and_the_others_listed(tmp_path, monkeypatch):
    language = get_language('python')
    session = tmp_path / 'session'
    service = session / 'implementation/service/app'
    service.mkdir(parents=True)
    for name in ('accounts.py', 'orders.py', 'stock.py'):
        (service / name).write_text(f'# {name}\n' + 'value = 1\n' * 300)
    shutil.copytree(language.test_suite.template, session / 'tests/suite')
    # Two of the files' diffs fit one part; the third needs another.
    monkeypatch.setattr(review, 'PART_DIFF_CHARS', 7000)

    def prepared(part):
        workspace = tmp_path / f'workspace-{uuid.uuid4().hex}'
        workspace.mkdir()
        changes, count = review.prepare_review(language, session, {}, workspace, part)
        inputs = json.loads((workspace / 'input.json').read_text())
        return changes, count, inputs, (workspace / 'review/changes.diff').read_text()

    first, count, inputs, diff = prepared(0)
    assert count == 2
    assert first == inputs['changes'] == {
        'service': ['service/app/accounts.py', 'service/app/orders.py'], 'tests': []}
    assert inputs['review_part'] == {'part': 1, 'of': 2, 'other_changes': {
        'service': ['service/app/stock.py'], 'tests': []}}
    assert '# orders.py' in diff and '# stock.py' not in diff

    second, _, inputs, diff = prepared(1)
    assert second['service'] == ['service/app/stock.py']
    assert inputs['review_part']['part'] == 2
    assert '# stock.py' in diff and '# accounts.py' not in diff
    # Every file of the service, not only the part's, is there to read.
    assert len(list(tmp_path.glob('workspace-*/review/service/app/accounts.py'))) == 2

    monkeypatch.setattr(review, 'PART_DIFF_CHARS', 1_000_000)
    _, count, inputs, diff = prepared(0)
    assert count == 1 and 'review_part' not in inputs and '# stock.py' in diff


async def test_review_workflow_reviews_every_part_and_adds_up_their_findings(
        time_skipping_server):
    contexts: list[dict[str, Any]] = []

    @activity.defn(name='review_changes')
    async def review_changes(session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        contexts.append(context)
        part = context.get(review.PART_KEY, 0)
        finding = {'summary': f'Problem in part {part}', 'target': 'implementation',
                   'reason': 'service'}
        return {'session_id': session_id, 'findings': [finding], 'path': '/review',
                **({'next_part': part + 1} if part < 2 else {})}

    env = time_skipping_server
    async with Worker(
            env.client, task_queue='review-test', workflows=[ReviewWorkflow],
            activities=[review_changes]):
        result = await env.client.execute_workflow(
            ReviewWorkflow.run, args=['session', {'instructions': 'Keep it'}],
            id=f'review-{uuid.uuid4().hex}', task_queue='review-test')

    assert [finding['summary'] for finding in result['findings']] == [
        'Problem in part 0', 'Problem in part 1', 'Problem in part 2']
    assert contexts == [{'instructions': 'Keep it'},
                        {'instructions': 'Keep it', 'review_part': 1},
                        {'instructions': 'Keep it', 'review_part': 2}]
