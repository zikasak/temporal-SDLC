from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from app.delivery.flow.stages import COMPONENT_REPAIRS_PATCH

with workflow.unsafe.imports_passed_through():
    from app.delivery.contract import with_requirements
    from app.delivery.models import open_review_findings
    from app.requirements.models import CHANGE_KINDS
    from app.temporal.activities import (
        open_pull_request, publish_documentation, publish_onboarding,
        publish_onboarding_architecture, publish_repository,
    )

COMPONENT_PUSHES_PATCH = 'component-pushes'


class Publication:
    """The push of the verified sources, or of an onboarding's architecture, and the
    pull request opened from it."""

    async def _publish(self) -> None:
        """Push the sources that passed verification, with their contract, to a new branch
        of the repository. Documentation that no check exists for yet is pushed too, and
        marked as unverified."""
        if not self.spec.checkout:
            return
        components = bool(self.progress.component_tests)
        if components:
            workflow.deprecate_patch(COMPONENT_REPAIRS_PATCH)
            workflow.deprecate_patch(COMPONENT_PUSHES_PATCH)
        # Tests that pass with review findings left after the last round are pushed too,
        # marked as unverified, with the findings in the commit message.
        open_findings = self.progress.verification_status == 'findings_open'
        result = (self.progress.verification
                  if self.progress.verification_status == 'completed' or open_findings
                  else None)
        documentation = self.spec.kind == 'docs'
        # Only a documentation task may go on without a check, and only when
        # verification says it has none: a check that ran and did not pass stops it too.
        unverified = bool(documentation and result and result['status'] == 'not_implemented')
        skip = not (unverified or result and result['status'] == 'passed')
        if components:
            # Both suites, the contract tests being none when the repository has none.
            skip = not self._components_passed(findings_open=True)
        elif not skip and self.spec.kind in CHANGE_KINDS and (
                self.progress.verification_error
                or self.spec.kind == 'bugfix' and self.progress.baseline_status != 'reproduced'):
            # Green alone is not enough for a change: all of its tests must have run, a
            # bugfix's must have been red without the fix, and an API change must have
            # kept the repository's tests its service still passes.
            skip = True
        if skip:
            if ((self.progress.open_findings or self.state.mention is not None
                 or self.state.ci_fix is not None) and self.progress.publication):
                # A run started by the user's instructions that did not pass leaves the
                # earlier unverified push on the branch, and the flag raised. So does a
                # fix round a pull request comment asked for, with the earlier push, and
                # an automatic fix round of failed CI.
                self.progress.publication_status = 'completed'
                if self.progress.pull_request_status in ('waiting', 'running'):
                    workflow.deprecate_patch('kept-push-pull-request')
                    # A retried repository stage, such as after a failed pull request
                    # step, still opens or updates the pull request of the earlier push;
                    # a step that was not attempted is never reported as done.
                    await self._pull_request()
            else:
                self.progress.publication_status = 'skipped'
                self.progress.pull_request_status = 'skipped'
            return
        self.progress.publication_status = 'running'
        self.progress.publication_error = None
        findings = open_review_findings(self.progress) if open_findings else []
        if documentation:
            # The documentation alone: the service, its tests and its contract stay.
            push, args = publish_documentation, [self.spec.language, not unverified]
        else:
            contract = {
                'language': self.spec.language,
                'requirements': self.spec.requirements,
                'requirements_document': self.spec.requirements_document,
                'api': self.spec.api,
                # Committed as contracts/architecture.md and contracts/architecture.json;
                # without one the repository's files are kept.
                'architecture': self.spec.architecture,
                'architecture_structure': self.spec.architecture_structure,
            }
            if self.spec.brief:
                # The repository's contract grows by what the change required of the service.
                contract = with_requirements(contract, self.spec.brief['requirements'],
                                             self.spec.brief['retired'])
            if self.spec.onboarding:
                push, args = publish_onboarding, [
                    contract, findings, self.progress.existing_code_changed]
            else:
                push, args = publish_repository, [contract, self.spec.kind, findings]
                if components:
                    # With its component tests and contract; its contract tests are
                    # pushed only when an author of the change wrote them.
                    args.append({'contract_tests': bool(self.progress.tests)})
        try:
            self.progress.publication = await workflow.execute_activity(
                push,
                args=[self.spec.session_id, self.spec.repository, *args],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Repository push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.publication_error = (
                f'The {"documentation" if documentation else "verified sources"} could not be '
                'pushed to the repository.' + (f' {reason}' if reason else ''))
            self.progress.publication_status = 'error'
            # Nothing new was pushed, so no pull request step runs for it.
            self.progress.pull_request_status = 'skipped'
        else:
            workflow.logger.info('Sources pushed to %s', self.progress.publication['branch'])
            self.progress.publication_status = 'completed'
            self._pushed = True
            if not documentation:
                # A repeated push of a delivery whose findings the user accepted keeps
                # the acceptance; any other push with open findings asks the user again.
                accepted = (open_findings and self.state.retry_stage == 'repository'
                            and self.progress.findings_acceptance is not None
                            and not self.progress.open_findings)
                # Raised for the user to accept the delivery or give instructions.
                self.progress.open_findings = open_findings and not accepted
                if open_findings and not accepted:
                    self.progress.findings_acceptance = None
            workflow.deprecate_patch('pull-request')
            await self._pull_request()

    async def _revise_architecture(self, instructions: str) -> bool:
        """Change the architecture of an architecture-only onboarding as a comment on its
        pull request asks; true when it changed and is to be pushed again."""
        try:
            revised = await workflow.execute_activity(
                'revise_onboarding_architecture',
                args=[self.spec.session_id, self.spec.architecture_structure, instructions],
                result_type=dict, start_to_close_timeout=timedelta(minutes=30),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Architecture fix failed: %s', exc.cause or exc)
            reason = (str(getattr(exc.cause, 'message', '') or '')[:2500]
                      if isinstance(exc.cause, ApplicationError) else '')
            self._fix_error = reason or 'The architecture could not be changed.'
            return False
        self.spec.architecture = revised['architecture']
        self.spec.architecture_structure = revised['architecture_structure']
        self._fix_summary = revised.get('summary') or None
        return True

    async def _publish_architecture(self) -> None:
        """Push the architecture an onboarding added to the repository's contract, then
        open its pull request."""
        if not self.spec.checkout:
            return
        self.progress.publication_status = 'running'
        self.progress.publication_error = None
        try:
            self.progress.publication = await workflow.execute_activity(
                publish_onboarding_architecture,
                args=[self.spec.session_id, self.spec.repository, self.spec.architecture,
                      self.spec.architecture_structure],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except ActivityError as exc:
            workflow.logger.warning('Architecture push failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.publication_error = (
                'The architecture could not be pushed to the repository.'
                + (f' {reason}' if reason else ''))
            self.progress.publication_status = 'error'
            self.progress.pull_request_status = 'skipped'
            return
        workflow.logger.info('Architecture pushed to %s', self.progress.publication['branch'])
        self.progress.publication_status = 'completed'
        self._pushed = True
        await self._pull_request()

    async def _pull_request(self) -> None:
        """Open the pull request of the pushed branch, or update the open one. Its failure
        never fails the push or the delivery: it is reported, and retrying the repository
        stage pushes again and tries again. A failed update keeps the pull request."""
        self.progress.pull_request_status = 'running'
        self.progress.pull_request_error = None
        brief = self.spec.brief
        change = {
            'kind': ('onboarding_architecture' if self.spec.architecture_only
                     else 'onboarding' if self.spec.onboarding else self.spec.kind),
            'requirements': brief['requirements'] if brief else self.spec.requirements,
            'retired': brief['retired'] if brief else [],
        }
        if self.spec.onboarding and self.progress.existing_code_changed:
            change['existing_code_changed'] = True
        proofs = self._proofs()
        if proofs:
            change['proofs'] = proofs
        args = [self.spec.session_id, self.spec.repository, self.progress.publication, change]
        text = None
        workflow.deprecate_patch('pull-request-text')
        # The LLM writes the title and description; without them the generic text is
        # used, which is reported but fails neither the step nor the delivery.
        try:
            text = await workflow.execute_activity(
                'write_pull_request_text',
                args=[self.spec.session_id, self.progress.publication,
                      {**change, 'task': (brief['requirements_document'] if brief
                                          else self.spec.requirements_document)}],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(maximum_attempts=2),
                result_type=dict,
            )
        except ActivityError as exc:
            workflow.logger.warning('The pull request text could not be written: %s',
                                    exc.cause or exc)
            # Only the activity's own explanations are shown: a provider's error
            # message may carry credentials (NFR-2).
            reason = (str(getattr(exc.cause, 'message', '') or '')[:500]
                      if isinstance(exc.cause, ApplicationError)
                      and exc.cause.type == 'PullRequestTextError' else '')
            self.progress.pull_request_text_error = (
                'The LLM could not write the pull request title and description, so the '
                'generic text was used.' + (f' {reason}' if reason else ''))
        args.append(text)
        try:
            self.progress.pull_request = await workflow.execute_activity(
                open_pull_request,
                args=args,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError as exc:
            workflow.logger.warning('Pull request step failed: %s', exc.cause or exc)
            reason = str(getattr(exc.cause, 'message', '') or '')[-500:]
            self.progress.pull_request_error = (
                reason or 'The pull request could not be opened or updated.')
            self.progress.pull_request_status = 'error'
        else:
            workflow.logger.info('Pull request #%d is open', self.progress.pull_request['number'])
            self.progress.pull_request_status = 'completed'
            if text:
                # The pull request now carries LLM-written text.
                self.progress.pull_request_text_error = None

    def _proofs(self) -> dict[str, list[dict[str, Any]]]:
        """Which suite proves each criterion of a routed change, by suite, for its pull
        request: each criterion's ID, title and, at a seam, the seam. Empty for a change that
        was not routed, or with no criterion a suite proves."""
        titles = {item['id']: item.get('title') or ''
                  for item in (self.spec.brief or {}).get('requirements') or []}
        proofs: dict[str, list[dict[str, Any]]] = {}
        for requirement, route in (self.progress.routing or {}).items():
            if route['route'] not in ('contract', 'component'):
                continue
            item: dict[str, Any] = {'id': requirement, 'title': titles.get(requirement, '')}
            if route.get('seam'):
                item['seam'] = route['seam']
            proofs.setdefault(route['route'], []).append(item)
        return proofs
