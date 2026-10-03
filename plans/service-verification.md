# Service verification step

## Context
- API design currently starts implementation and test generation as parallel Temporal child workflows after code generation.
- Each branch publishes a built Docker image; there is no step that executes the tests against the service.
- Add verification only after both branches complete successfully, returning test results.

## Approach
- Add `VerificationWorkflow` and a dedicated host activity/worker. Inputs are session ID and the two successfully published immutable image IDs; do not rebuild images or pass implementation sources into test generation.
- After the existing `asyncio.gather` completes, launch verification only when both results exist and neither branch has an error. Add a `verification-child-workflow` Temporal patch; older histories retain their original command sequence and show verification as not applicable.
- Use a unique Docker bridge network and exactly two constrained containers, with service alias `service`, no published ports, host mounts, Docker socket or inherited credentials. Reuse dropped capabilities, no-new-privileges and resource limits; retain each image's non-root runtime user.
- A trusted test-runner entrypoint waits up to 120 seconds for `service:8080` TCP readiness, then runs the existing Maven command with a 600-second execution bound. This is transport readiness, not an assumed health endpoint or a business assertion. The network retains outbound access needed by Maven.
- Collect runner exit state, bounded logs from both containers and Surefire XML before cleanup. Always remove both containers and the network in bounded `finally` cleanup, including failures/cancellation; use unique names/labels so concurrent sessions cannot collide. Cancellation propagates rather than becoming a test result.
- Return `status: passed | failed | error`, totals (`tests`, `passed`, `failures`, `errors`, `skipped`), duration, runner exit code, bounded failed/error-test details and log tails. Assertions/JUnit errors produce `failed`; Docker/startup/test timeout, OOM, abnormal termination or invalid/missing/zero-executed reports produce `error`. Passing requires successful exit and valid reports with executed tests and no failures/errors; never treat an unexplained nonzero exit as an assertion failure.
- Parse only regular bounded `TEST-*.xml` reports, reject links/DTD/entity declarations, avoid counting nested suite summaries twice, and cap result details/logs with explicit truncation indicators. Keep raw reports and logs under `<GENERATED_CODE_DIR>/<session-id>/verification/`, atomically publish `result.json`, and return a bounded summary suitable for Temporal history/API queries.
- Add `verification`, `verification_error` and `verification_status` (`not_applicable`, `waiting`, `running`, `completed`, `skipped`, `error`) to session state. Set waiting before starting the parallel branches, running before the verification child, skipped on prerequisite failures, completed for returned results, and error for unexpected child failures. Keep the existing API-design workflow return value (OpenAPI document) unchanged; the verification child's return value is test results.
- Frontend continues polling through downstream work and displays generation/verification progress, textual pass/fail/error summary, counts, failed-test details and collapsible logs in the existing results section. Show prerequisite failures/skipped and legacy not-applicable states distinctly; render logs as text, not HTML. Preserve page identity and existing interactions, with accessible live status and responsive wrapping.

## Files to modify
- New `backend/app/verification/{__init__,models,activities,workflows,worker}.py`: result schema, Docker/report lifecycle, Temporal execution and worker entrypoint. Keep orchestration/report parsing local to this stage unless size requires a small dedicated module.
- `backend/app/temporal/workflows.py`: replay-safe gating and session status/result recording.
- `backend/app/api_design/models.py`: backward-compatible defaulted verification fields; the existing session serializer needs no route change.
- `backend/app/config.py`: `TEMPORAL_VERIFICATION_TASK_QUEUE`, default `verification`.
- `backend/app/test_generation/template/Dockerfile` and new trusted `template/run-tests.sh`: readiness/time-bound wrapper around existing Maven execution; readiness applies when verification supplies wait settings, preserving standalone runner use.
- `backend/app/test_generation/activities.py`: always install the trusted wrapper during sanitization, overriding agent output just like the Dockerfile. Update `template/README.md` to document runtime behavior.
- `backend/tools/dev_stack.py`, `.zed/debug.json`: start/register the fifth worker.
- `frontend/src/ApiDesign.tsx`, `frontend/src/styles.css`: downstream result decoding, polling and compact accessible verification presentation.
- `backend/tests/test_api_design_workflow.py`, `backend/tests/test_test_generation.py`, new `backend/tests/test_verification.py` and opt-in Docker integration coverage.
- `docs/service-implementation.md`: pipeline diagram, worker startup, results, failure semantics, limits, shared-Docker requirement and leftover-resource recovery.

## Reuse
- `ApiDesignWorkflow._generate_code`, `_implement_service`, `_generate_tests` and existing replay-patch patterns in `backend/app/temporal/workflows.py`.
- `app.agent_workflows.host.run_stage` result contract (`image_id`, `path`, `session_id`); validate session/image IDs before any Docker/filesystem work.
- `docker` and `keep_activity_alive` in `backend/app/docker_agents.py`: subprocess timeouts, cancellation, bounded logs and heartbeats; no generic Docker engine rewrite.
- `run_agent_worker` in `backend/app/agent_workflows/worker.py`: dedicated single-workflow/activity bootstrap. Use a verification-specific activity policy (approximately 15 minutes, two-minute heartbeat, one attempt), not the authoring activity timeout.
- `api_design.service.get_session` automatically serializes new defaulted state fields; existing polling, generated-document markup and color/spacing tokens in `frontend/src/ApiDesign.tsx` and `styles.css`.
- Existing `SERVICE_BASE_URL` and Maven/Surefire report contracts in `backend/app/test_generation/template/`; existing `ActivityEnvironment` and Temporal test fixtures for behavioral coverage.

## Steps
- [x] Confirm image contracts, worker launch paths, session serialization, existing tests and user runtime/UI decisions.
- [x] Define bounded verification result/state schemas and configuration; implement trusted runner readiness/timeout wrapper and host-controlled publication.
- [x] Implement verification activity: validate inputs, heartbeat, create isolated network/two containers, execute, retrieve/parse artifacts, publish results, and reliably clean up.
- [x] Add dedicated workflow/worker and replay-safe parent join/gating; preserve sibling artifacts and existing parent result semantics.
- [x] Register local worker startup/debug profile; extend frontend parsing, polling, progress and test-result presentation without redesigning the page.
- [x] Extend existing owner-boundary tests, add focused report/lifecycle and real-Docker coverage, and update operational docs.

## Verification
- Extend existing Temporal success, partial-failure and parallel-barrier tests: verification starts only after both branches succeed (exercise either completion order), uses those image IDs, never runs on prerequisite failure, and records assertion failure versus infrastructure error without losing artifacts. Cover unexpected verification child failure and legacy replay when a representative history is available.
- Activity-boundary table-driven coverage using `ActivityEnvironment` and a fake Docker transport: passing/failing/skipped/no-test reports, malformed/oversized/linked reports, missing images, service/runner failure, timeout and cancellation. Assert observable result/publication, no secret/host exposure and cleanup rather than private helper call structure. Extend trusted test-generation publication coverage for the wrapper.
- Opt-in real Docker integration: build a tiny self-contained HTTP service fixture and a runner using the trusted Maven template; verify a passing HTTP assertion, a deliberately failing assertion, delayed/unavailable startup, result reports, and zero remaining per-run containers/networks. This guards networking, executable entrypoint, Surefire format and cleanup independently of the fake transport. Run only with Docker explicitly enabled; no LLM required.
- Run from `backend`: `python -m pytest tests/test_verification.py tests/test_test_generation.py tests/test_api_design_workflow.py`, then relevant sibling/full backend suite. Run the documented opt-in Docker test separately.
- Run from `frontend`: `npm run build` (existing dependencies; no new frontend test framework). Browser-check active generation → verification → pass/fail/error/skipped, retained polling after codegen, old sessions, large failure/log text, and reload persistence; inspect desktop/mobile once and confirm any fixes in one follow-up pass.
- Run `git diff --check`; manually run all five workers against Temporal and confirm the existing session GET endpoint returns verification results and artifacts match the UI. Planning phase executes none of these mutating/runtime checks.

## Execution results
- Full backend suite: **129 passed, 4 opt-in Docker tests skipped**.
- Explicit real-Docker run: **4 passed**, including passing/failing HTTP tests, delayed readiness and unavailable service; no verification containers/networks remained.
- Frontend `npm run build` passed. Headless Brave checked continued polling after codegen, terminal polling stop, pass/fail/error/skipped/legacy states, long messages/logs, and desktop/mobile overflow; screenshots saved under `/tmp/verification-ui-*.png`.
- Temporal coverage includes replay of a history generated without the new verification patch and session API serialization of results/errors.
- Shell syntax, Temporal sandbox loading and changed-file whitespace checks passed. `git diff --check` was unavailable because this checkout has no `.git`; used an explicit whitespace check instead.
- Design detector reported only two pre-existing accent borders outside this feature; left them unchanged.
- Did not run the live LLM-driven five-worker pipeline; orchestration, Docker execution and browser presentation were validated independently with controlled inputs.

## Decisions and discoveries
- User confirmed: failing assertions return completed pass/fail results; display results in the frontend too. Infrastructure failures remain distinct.
- Published branch outputs contain `image_id`, `path`, and `session_id`; images are immutable local `sha256` IDs. Verification must use the same Docker daemon as generation workers.
- `app.docker_agents.docker` already handles bounded subprocesses/cancellation and bounded logs; `keep_activity_alive` provides Temporal heartbeats. Reuse both.
- Agent containers use copied inputs, no host mounts, resource limits, dropped capabilities and cleanup in `finally`; follow that lifecycle for verification.
- `ApiDesignState` already carries generation results/errors. Extend it with verification state/results rather than introducing a separate persistence system.
- Compose only deploys Temporal infrastructure; Python workers run on the host. `backend/tools/dev_stack.py` starts the workers and `.zed/debug.json` provides individual launch profiles; add verification to both.
- Service generation already requires port 8080. The runner is Maven/JUnit, executes `mvn test`, and writes Surefire XML under `/tests/target/surefire-reports`.
- The session GET endpoint serializes `ApiDesignState` with `dataclasses.asdict`; new result fields are exposed automatically without a new endpoint.
- Frontend `frontend/src/ApiDesign.tsx` currently ignores implementation/test fields and stops polling after codegen. Add downstream progress/result parsing and polling until verification is terminal, with legacy sessions explicitly not applicable.
- Preserve the existing UI identity and document/results layout; add a compact verification summary, failed-test details and collapsible bounded logs rather than redesigning the page.

## Runtime contract (confirmed)
- User accepted: generated self-contained service on port 8080, no additional dependencies/containers or environment/credential configuration, two-minute readiness limit and ten-minute test execution limit.
- Wait for service port readiness from the test runner container before Maven runs (exactly two containers; no probe/helper container). Layer readiness around Maven in a trusted runner entrypoint supplied by the host, never an agent-authored script.
- No secret forwarding or automatic repair/regeneration loop in this change.
- Verification uses its own shorter Temporal activity policy, not `execute_agent_activity`'s 100-minute authoring timeout. Reuse the dedicated worker bootstrap; no agent profile or image build is needed.
- Existing tests own parallel ordering and partial failure; extend those cases rather than duplicate them. Add verification coverage at the activity boundary and an opt-in real Docker test. Frontend has no test runner; validate via TypeScript/build and browser scenarios rather than introduce a framework.
