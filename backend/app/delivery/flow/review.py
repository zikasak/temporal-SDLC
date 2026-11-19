import asyncio
from typing import Any, cast

from temporalio import workflow
from temporalio.exceptions import ChildWorkflowError

from app.delivery.flow.stages import FINDING_CONFLICTS_PATCH, restore_finding

from app.delivery.flow.base import FlowBase

with workflow.unsafe.imports_passed_through():
    from app.delivery.causes import with_cause
    from app.config import REVIEW_TASK_QUEUE
    from app.delivery.models import (
        kept_tests, named_tests, open_review_findings, reproduce_request, touched_tests,
        unproven_green,
    )
    from app.requirements.models import CHANGE_KINDS
    from app.review.models import (
        MAX_FINDING_CORRECTIONS, MAX_REVIEW_ROUNDS, UNRESOLVED_CHARS, ReviewFinding,
        ReviewRound, correctable, earlier_findings, for_worker, review_request, same_finding,
    )

# Marks the workflow code that also sends a bugfix's tests corrected for review findings
# back to their authors when they pass without the fix, and that, when such a correction is
# retried, sends them back again instead of correcting the same findings again; deliveries
# recorded before it failed the correction at that baseline.
CORRECTION_REPRODUCE_PATCH = 'reproduce-corrected-tests'


class ReviewRounds(FlowBase):
    """Code review of the verified delivery: its rounds of findings and corrections,
    and the fix rounds started by the user's instructions or a mention."""

    async def _review_rounds(self, instructions: str | None = None,
                             corrected: list[ReviewFinding] | None = None) -> None:
        """Review the verified delivery's changes; while findings to correct remain, send
        them to the worker they concern, verify again and review again, at most
        MAX_REVIEW_ROUNDS times. Each round knows the findings already `corrected` in this
        run. A finding reported again goes for another correction, each at most
        MAX_FINDING_CORRECTIONS times in the run, unless failing tests no author may change
        contradict it: a round with nothing to correct ends the run. Findings left are
        open, each saying why when no author could settle it: the delivery is pushed as
        unverified and the user decides about them."""
        earlier = [cast(ReviewFinding, {key: value for key, value in item.items()
                                        if key not in ('repeats', 'unresolved')})
                   for item in corrected or []]
        # How often each finding of `earlier` was sent for correction in this run.
        sent = [1] * len(earlier)
        for count in range(1, MAX_REVIEW_ROUNDS + 1):
            # The user's instructions hold for every round of the run they started.
            findings = await self._review(instructions, earlier, record=count == 1)
            if findings is None:
                return
            if not findings:
                self.progress.verification_status = 'completed'
                return
            if workflow.patched(FINDING_CONFLICTS_PATCH):
                self._release_kept(findings)
                again = self._correct_again(findings, earlier, sent)
                new = [item for item in findings if 'repeats' not in item]
                batch = [*new, *(item for _, item in again)]
                if not any(correctable(item) for item in batch):
                    workflow.logger.info('The review found nothing an author can correct')
                    break
                if count == MAX_REVIEW_ROUNDS:
                    workflow.logger.info('Review findings remain after %d rounds',
                                         MAX_REVIEW_ROUNDS)
                    break
                if not await self._correct(batch, self.progress.reviews[-1]['round']):
                    return
                for index, item in again:
                    earlier[index] = self._as_earlier(item)
                    sent[index] += 1
                earlier += [self._as_earlier(item) for item in new]
                sent += [1] * len(new)
                continue
            new = [item for item in findings if 'repeats' not in item]
            if not new:
                workflow.logger.info('The review only repeats findings already corrected')
                break
            if not any(correctable(item) for item in new):
                # Such as findings about the stubs of a component contract: the user decides.
                workflow.logger.info('The review found nothing an author can correct')
                break
            if count == MAX_REVIEW_ROUNDS:
                workflow.logger.info('Review findings remain after %d rounds',
                                     MAX_REVIEW_ROUNDS)
                break
            if not await self._correct(new, self.progress.reviews[-1]['round']):
                return
            earlier += new
            sent += [1] * len(new)
        if workflow.patched(FINDING_CONFLICTS_PATCH):
            self._explain_open(earlier, sent)
        self.progress.verification_status = 'findings_open'

    @staticmethod
    def _as_earlier(item: ReviewFinding) -> ReviewFinding:
        return cast(ReviewFinding, {key: value for key, value in item.items()
                                    if key not in ('repeats', 'unresolved')})

    def _correct_again(self, findings: list[ReviewFinding], earlier: list[ReviewFinding],
                       sent: list[int]) -> list[tuple[int, ReviewFinding]]:
        """The findings of a round that report an earlier one again and go for another
        correction, each with the index of the earlier one: those corrected fewer than
        MAX_FINDING_CORRECTIONS times that no failing test an author may not change
        contradicts."""
        again: list[tuple[int, ReviewFinding]] = []
        for item in findings:
            index = item.get('repeats', 0) - 1
            if (0 <= index < len(earlier) and sent[index] < MAX_FINDING_CORRECTIONS
                    and self._conflict(earlier[index]) is None
                    and all(index != taken for taken, _ in again)):
                again.append((index, item))
        return again

    def _conflict(self, finding: ReviewFinding) -> dict[str, Any] | None:
        """The conflict recorded between the finding and failing tests no author may
        change, if any."""
        return next((item for item in reversed(self.state.finding_conflicts)
                     if same_finding(item, finding)), None)

    def _explain_open(self, earlier: list[ReviewFinding], sent: list[int]) -> None:
        """Say, on each finding left open that reports an earlier one again, why no author
        settled it: failing tests no author may change contradict it, or its corrections
        did not settle it."""
        for item in open_review_findings(self.progress):
            index = item.get('repeats', 0) - 1
            if not 0 <= index < len(earlier):
                continue
            conflict = self._conflict(earlier[index]) or self._conflict(item)
            if conflict:
                # Known by its words as reported last too, for a later run to find it.
                self._note_conflict(item, conflict)
                tests = ', '.join(conflict['tests'][:5])
                reason = conflict['reason'] or 'They assert what the finding requires to change.'
                item['unresolved'] = (
                    f'Tests of the repository that no author of this delivery may change '
                    f'fail when it is corrected: {tests}. {reason} Change those tests, or '
                    f'accept the finding.')[:UNRESOLVED_CHARS]
            elif sent[index] >= MAX_FINDING_CORRECTIONS:
                item['unresolved'] = (
                    f'Corrected {sent[index]} times in this run, and the review still finds '
                    f'it.')

    def _release_kept(self, findings: list[ReviewFinding]) -> None:
        """Release the repository's kept tests that findings about the tests name: a test
        the changed service passes as the repository holds it, such as one whose
        assertions never ran, may still be wrong against the contract, and the review is
        the authority that says so."""
        kept = [case for case in self.state.kept_tests
                if case not in self.state.released_tests]
        released = sorted({case for item in findings for case in named_tests(item, kept)})
        if released:
            workflow.logger.info('Review findings release kept tests: %s', ', '.join(released))
            self.state.released_tests = sorted({*self.state.released_tests, *released})

    async def _review(self, instructions: str | None, earlier: list[ReviewFinding],
                      record: bool = True) -> list[ReviewFinding] | None:
        """One review round, told the findings already corrected; None when the review
        could not run. `record` keeps the instructions with the round whose reviewed
        fixes were made from them."""
        self.progress.verification_status = 'reviewing'
        number = len(self.progress.reviews) + 1
        context = self._coding_context()
        if 'component_routes' in context:
            # The criteria the HTTP suite proves have contract tests of the change's own.
            context['contract_tests'] = bool(self.progress.tests)
        if self.spec.kind == 'bugfix':
            # Every test a correction adds has to fail without the fix, as the others did.
            context['regression'] = True
        if instructions:
            context['instructions'] = instructions
        if earlier:
            context['previous_findings'] = earlier_findings(earlier)
        try:
            result = await self._child(
                'ReviewWorkflow', REVIEW_TASK_QUEUE, self._child_id('review', number), context)
        except ChildWorkflowError as exc:
            workflow.logger.warning('Code review failed: %s', exc.cause or exc)
            self.progress.review_error = with_cause('The code review could not run.', exc)
            self.progress.verification_status = 'error'
            return None
        self.progress.review_error = None
        findings: list[ReviewFinding] = list(result.get('findings') or [])
        round_: ReviewRound = {'round': number, 'findings': findings}
        if instructions and record and self._mention_ref is not None:
            # A comment's text, up to GitHub's 65536 characters per comment, stays in the
            # mention history; the round refers to it so that the state stays bounded.
            round_['mention'] = dict(self._mention_ref)
        elif instructions and record:
            round_['instructions'] = instructions
        self.progress.reviews.append(round_)
        workflow.logger.info('Review round %d: %d findings', number, len(findings))
        return findings

    async def _correct(self, findings: list[ReviewFinding], number: int,
                       instructions: str | None = None,
                       restore: list[str] | None = None, suite: str | None = None) -> bool:
        """Have each worker the findings concern correct them, or with the user's
        instructions both, then verify again. True when verification passed again.
        A correction of the review's own findings that did not pass is left for the user
        to retry; one from instructions leaves the open findings to decide about again."""
        self.progress.verification_status = 'repairing'
        workers = [worker for worker in ('implementation', 'tests')
                   if instructions or for_worker(findings, worker)]
        self._correcting = [dict(item) for item in findings]
        try:
            await asyncio.gather(*(
                self._implement_service(review_request(findings, worker, number, instructions))
                if worker == 'implementation' else self._generate_tests(
                    review_request(findings, worker, number, instructions, restore), suite)
                for worker in workers))
            if await self._verify_correction(workers, number):
                return True
        finally:
            self._correcting = []
        self.progress.correction_failed = not instructions
        return False

    async def _retry_correction(self) -> None:
        """Go on from a correction of review findings that did not pass, then with the
        review rounds. Corrected tests of a bugfix that passed without the fix go back to
        their authors with the baseline, as correcting the same findings again would write
        them again. A worker that failed, or corrected tests of a bugfix whose baseline
        proved nothing for another reason, correct the findings again. A suite that verification ran
        and rejected goes back to the test author with the reason, as the same suite would
        be rejected again. Otherwise the corrected delivery is only verified again."""
        self.progress.correction_failed = False
        number = self.progress.reviews[-1]['round']
        workflow.deprecate_patch('correct-rejected-suite')
        rejected = self._rejected_suite()
        failed = (self.progress.implementation_error or self.progress.tests_error
                  or self.progress.component_tests_error)
        # Corrected tests that still pass without the fix: correcting the same findings
        # again would write them again, so only their authors go on from the baseline.
        wrong = (self._unreproducing_suites()
                 if not failed and self.spec.kind == 'bugfix'
                 and self.progress.baseline_status == 'not_reproduced' else {})
        if wrong and workflow.patched(CORRECTION_REPRODUCE_PATCH):
            workflow.logger.info('Reproduce round of the corrected tests: %s', ', '.join(
                f'{suite} {sum(map(len, tests.values()))}' for suite, tests in wrong.items()))
            self.progress.verification_status = 'repairing'
            findings = self._test_findings(number)
            await self._rewrite_tests({suite: reproduce_request(tests, number, findings)
                                       for suite, tests in wrong.items()})
            corrected = await self._verify_correction(['tests'], number)
            self.progress.correction_failed = not corrected
        elif (failed
                or self.spec.kind == 'bugfix' and self.progress.baseline_status != 'reproduced'):
            self.progress.implementation_error = None
            self.progress.tests_error = None
            self.progress.component_tests_error = None
            corrected = await self._correct(self._open_to_correct(), number)
        elif rejected:
            finding, restore, suite = rejected
            corrected = await self._correct([finding], number, restore=restore, suite=suite)
        else:
            self.progress.verification_status = 'repairing'
            self._correcting = [dict(item) for item in self._open_to_correct()]
            try:
                corrected = await self._verify_correction([], number)
            finally:
                self._correcting = []
            self.progress.correction_failed = not corrected
        if corrected:
            await self._review_rounds(corrected=open_review_findings(self.progress))

    def _open_to_correct(self) -> list[ReviewFinding]:
        """The open findings of the last round but those that failing tests no author may
        change contradict, which another correction would only undo again."""
        return [item for item in open_review_findings(self.progress)
                if self._conflict(item) is None]

    def _rejected_suite(self) -> tuple[ReviewFinding, list[str], str | None] | None:
        """Why the last verification, which ran and passed, still rejected the suite of a
        change, as a finding for the test author, with the repository's tests it has to
        put back as they were: those it changed or removed that the changed service
        still passes, and the suite whose author it is, for a change with component
        tests. None when the verification proved nothing about the suite."""
        reason = 'Verification of the corrected delivery rejected its tests.'
        if self.progress.component_tests:
            contract, component = (self.progress.verification,
                                   self.progress.component_verification)
            if not component or component['status'] != 'passed' or (
                    contract and contract['status'] != 'passed'):
                return None
            for run, suite, author in ((contract, 'contract', self.progress.tests),
                                       (component, 'component', True)):
                unrun = unproven_green(run) if run and author else None
                if unrun:
                    return {'summary': unrun, 'target': 'tests', 'reason': reason}, [], suite
            return None
        result = self.progress.verification
        if (self.spec.kind not in CHANGE_KINDS or not result
                or result['status'] != 'passed' or not self.progress.tests):
            return None
        unrun = unproven_green(result)
        if unrun:
            return {'summary': unrun, 'target': 'tests', 'reason': reason}, [], None
        existing = self.progress.existing_tests
        if self.spec.kind != 'api_change' or not existing:
            return None
        touched = touched_tests(self.progress.tests, self.state.released_tests)
        kept = kept_tests(existing, touched)
        if not kept:
            return None
        return restore_finding(kept, reason), kept, None

    async def _verify_correction(self, workers: list[str], number: int) -> bool:
        """Verify the delivery as the workers corrected it; true when it passed again."""
        if (self.progress.implementation_error or self.progress.tests_error
                or self.progress.component_tests_error):
            self.progress.verification_status = 'error'
            return False
        if ('tests' in workers and self.spec.kind == 'bugfix'
                and not await self._reproduce_corrected(number)):
            # Corrected added tests that no longer fail without the fix prove nothing.
            self.progress.verification_status = 'error'
            return False
        self.progress.verification = None
        self.progress.verification_error = None
        self.progress.existing_tests = None
        await self._verify_and_repair()
        if self.spec.kind == 'api_change':
            await self._prove_kept()
        if self._passed():
            return True
        if self.progress.verification_status == 'completed':
            # The tests no longer pass: the delivery is not pushed this way.
            self.progress.verification_status = 'error'
            self.progress.verification_error = (
                self.progress.verification_error
                or 'Verification failed after correcting the review findings.')
        return False

    async def _reproduce_corrected(self, number: int) -> bool:
        """The baseline of a bugfix's tests corrected for the findings of review round
        `number`. A finding may ask for a test of what the code already did before the
        fix, which cannot fail there: such tests go back to their authors, who may
        remove them, as tests written wrong do before the fix."""
        if not workflow.patched(CORRECTION_REPRODUCE_PATCH):
            return await self._reproduce(number)
        return await self._reproduce_bounded(number, self._test_findings(number))

    def _test_findings(self, number: int) -> list[dict[str, Any]]:
        """The findings of review round `number` the test authors corrected."""
        findings = next((item['findings'] for item in self.progress.reviews
                         if item['round'] == number), [])
        return review_request(findings, 'tests', number)['review']['findings']

    async def _instructed(self) -> None:
        """The user's instructions about the open review findings: a round of fixes from
        them, verification, and a new run of review rounds. If verification fails for
        good the earlier unverified push stays and the flag stays raised."""
        if self.spec.kind == 'docs':
            # A fix request on a documentation delivery: its author changes the
            # documentation as the comment asks, then the check; documentation is not
            # code-reviewed.
            await self._document(self.state.instructions)
            return
        # A fix request in the pull request, or of failed CI, comes with the open findings
        # only while the delivery has some.
        findings = (open_review_findings(self.progress)
                    if (self.state.mention is None and self.state.ci_fix is None)
                    or self.progress.open_findings else [])
        number = len(self.progress.reviews) + 1
        instructions = self.state.instructions
        # Kept until the correction passes, so that retrying a failed author carries out
        # this fix again instead of the original task.
        self.state.code_fix = {
            'instructions': instructions, 'findings': findings, 'number': number,
            'mention': dict(self._mention_ref) if self._mention_ref else None}
        if await self._correct(findings, number, instructions):
            self.state.code_fix = None
            await self._review_rounds(instructions, findings)

    async def _retry_fix(self) -> None:
        """Retry the failed stage of a fix round from instructions or a mention: the
        failed author makes that fix again on the delivered sources, then verification
        and a new run of review rounds, as the round would have gone on."""
        fix = self.state.code_fix
        assert fix is not None  # a fix retry is only offered for a recorded fix
        worker = 'implementation' if self.state.retry_stage == 'server' else 'tests'
        instructions = fix['instructions']
        number = fix['number']
        self._mention_ref = fix.get('mention')
        self.progress.verification_status = 'repairing'
        try:
            self._correcting = list(fix['findings'])
            await (self._implement_service if worker == 'implementation'
                   else self._generate_tests)(
                review_request(fix['findings'], worker, number, instructions))
            verified = await self._verify_correction([worker], number)
            self._correcting = []
            if not verified:
                return
            self.state.code_fix = None
            await self._review_rounds(instructions, fix['findings'])
        finally:
            self._mention_ref = None
            self._correcting = []
