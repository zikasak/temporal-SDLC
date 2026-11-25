"""One-shot LLM calls through the claude adapter write the prompt cache for 5 minutes only:
the Claude CLI they start gets FORCE_PROMPT_CACHING_5M=1, whatever the Claude login."""
from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from temporalio.testing import ActivityEnvironment

from app.llm import claude, model as llm_model
from app.settings.models import ModelSettings


@pytest.fixture
def claude_options(monkeypatch):
    """The claude adapter for every role. Returns `started`, the ClaudeAgentOptions each
    CLI started with, and `reply`, a one-item list holding what Claude answers."""
    started = []
    reply = ['fix']

    async def settings(workflow_type):
        return ModelSettings(adapter='claude', model='claude-opus-5-5')

    monkeypatch.setattr(llm_model, 'get_workflow_settings', settings)

    class FakeClient:
        def __init__(self, options):
            started.append(options)
            self._reply = reply[0]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def query(self, prompt):
            pass

        async def interrupt(self):
            pass

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock(text=self._reply)],
                                   model='claude-opus-5-5', message_id='msg_1')
            yield ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1,
                                is_error=False, num_turns=1, session_id='session-1',
                                result=self._reply)

    monkeypatch.setattr(claude, 'ClaudeSDKClient', FakeClient)
    return SimpleNamespace(started=started, reply=reply)


def assert_5m_cache(started):
    assert started
    for options in started:
        assert options.env.get('FORCE_PROMPT_CACHING_5M') == '1'


async def test_commit_message_uses_5_minute_cache(claude_options):
    from app.agent_workflows.host import _work_message

    assert await _work_message('implementation', 'implementation', 'verified', 'diff')
    assert_5m_cache(claude_options.started)


async def test_pull_request_text_uses_5_minute_cache(claude_options, monkeypatch):
    from app.delivery import pull_request_activities
    from app.github import checkout

    async def diff(*args):
        return '+ change'

    monkeypatch.setattr(checkout, 'delivery_diff', diff)
    claude_options.reply[0] = 'Add rate limiting\n\nLimits sign-in attempts.'
    await ActivityEnvironment().run(pull_request_activities.write_pull_request_text,
                                    'session-1', {'commit': 'abc'}, {'task': 'Task'})
    assert_5m_cache(claude_options.started)


async def test_mention_classification_and_answer_use_5_minute_cache(claude_options,
                                                                   monkeypatch, tmp_path):
    from app.delivery import mention_activities

    monkeypatch.setattr(mention_activities, 'GENERATED_CODE_DIR', tmp_path)

    environment = ActivityEnvironment()
    assert await environment.run(mention_activities.classify_mention,
                                 'session-1', '@app fix it') == 'fix'
    assert await environment.run(mention_activities.answer_mention,
                                 'session-1', '@app why?', {})
    assert len(claude_options.started) == 2
    assert_5m_cache(claude_options.started)


async def test_task_flow_naming_uses_5_minute_cache(claude_options):
    from app.tasks.activities import name_task_flow

    assert await ActivityEnvironment().run(name_task_flow, 'Add rate limiting', [])
    assert_5m_cache(claude_options.started)


async def test_failure_diagnosis_uses_5_minute_cache(claude_options):
    from app.verification.checks import analyze_checks_failure

    await ActivityEnvironment().run(analyze_checks_failure, 'session-1', 'FAILED test_a')
    assert_5m_cache(claude_options.started)


async def test_other_calls_keep_the_default_cache(claude_options):
    from langchain_core.messages import HumanMessage

    model = await llm_model.get_model('requirements')
    await model.ainvoke([HumanMessage('Hi')])
    assert 'FORCE_PROMPT_CACHING_5M' not in claude_options.started[0].env
