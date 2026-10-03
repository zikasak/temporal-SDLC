# Reusable implementation and test-generation workflows

## Context
The two coding workflows duplicate model/tool loops, filesystem copying, prepare → run → build → publish orchestration, Dockerfiles, and worker setup. Extract that machinery so later implementation/test-generation stages do not require copying whole packages.

**Agreed scope:** reusable stages now; feedback loops later. The core is toolchain-agnostic, with today's Java 21/Maven image as the default. No new toolchains, providers, dependencies, service-test execution, or deployment topology changes.

## Approach
### 1. Shared host lifecycle, explicit role policies
Create `app/agent_workflows/host.py` with a small frozen `StageSpec` and `run_stage`. The spec declares the stage identifier, artifact/workspace/output names, heartbeat text, explicit agent-image build definition, and hooks for input preflight/preparation and output sanitization/validation. These are trusted Python definitions, not configuration accepted from workflow inputs.

The runner owns session-ID validation, temporary workspaces, heartbeat management, isolated agent execution, output-image build, SHA-256 image-ID validation, and publication of the existing `{image_id, path, session_id}` result and `image.json`. Preserve the current publish-on-success/manifest-replacement protocol; failures before publication must not replace prior successful artifacts. Do not introduce a new transactional storage system.

Share regular-file traversal/copying through a role-supplied selection predicate. Keep domain policies in each existing `activities.py`:

| Stage | Inputs/workspace | Sanitization and validation | Published directory |
| --- | --- | --- | --- |
| Implementation | Generated `server` sources without tests/links/build output; existing context; `service` workspace | Same source exclusions; require production Dockerfile | `implementation/service` |
| Test generation | Contract-field allowlist, rendered OpenAPI, static Maven template; `tests` workspace | Allow only test sources + Maven/README config; overlay trusted runner Dockerfile/ignore file; require Maven test suite | `tests/suite` |

The generic runner never discovers or forwards sibling artifacts automatically. Test authoring must still receive no implementation/codegen sources, images, logs, or extra context.

### 2. Shared container runtime and pure model factory
Create `app/agent_workflows/runtime.py` with an explicit agent profile: workspace root, prompt, initial messages, turn-limit environment key/default, and completion validation/messages. Share root-bound read/write tools, shell execution, and the synchronous tool-call loop. Preserve tool names, 30,000-character reads, 120-second shell timeout/process-group cleanup, 16,000-byte shell output tail, tool-call IDs/error responses, and both existing turn-limit keys/default of 80.

Keep each `agent.py` as a small entrypoint containing its current prompt, input-message assembly, and distinct completion check. No Java/Maven/file-name assumptions belong in the runtime. Extract the existing provider constructor into `app/llm/factory.py` with explicit settings; the host `get_model()` retains its config wrapper/cache, and containers use environment settings without importing `app.config`. Retain the current SDK options. Do not reuse the host-dependent LangGraph runtime.

### 3. Reusable Temporal/bootstrap helpers, stable public contracts
Add a deterministic `execute_agent_activity` helper and a parameterized worker bootstrap. Keep the existing explicitly decorated workflow/activity names, `(session_id, context)` arguments, worker module entrypoints, separate queues, startup labels, and concurrency of one. Keep activity options at 100 minutes, 2-minute heartbeat timeout, and one attempt.

Leave `ApiDesignWorkflow` unchanged: parallel children, independent failures/results, child IDs, and existing replay patch markers remain intact. Stage specs stay host-side and are not serialized through Temporal.

### 4. One default image, replaceable build definitions
Extend `docker_agents.run_agent` to consume an explicit image-build definition (context, Dockerfile, build arguments), instead of deriving the context from a role directory. Keep its existing naming, non-root/container resource restrictions, copy-in/out, bounded redacted logs, and cancellation cleanup.

Replace the two agent Dockerfiles with one Java/Maven default Dockerfile, parameterized by `AGENT_ROLE` and `WORKSPACE_NAME`. Build from `backend/` with a strict `.dockerignore` allowlist; COPY only the shared runtime, pure factory, minimal empty package initializers, and selected role's `agent.py`. Host config/orchestration, another role's code, generated data, environment files, and templates must not enter the image. Keep current dependency pins and UID 10001. Custom future toolchains supply a different explicit build definition while following the same workspace/output and non-root execution contract.

Adding a future stage requires its role policies/profile and a thin activity/workflow registration, not edits to the shared loop/lifecycle. Its artifact namespace and input visibility must be explicit; no new feedback stage is added now.

## Files to modify
- **Add:** `backend/app/agent_workflows/{__init__.py,host.py,runtime.py,workflows.py,worker.py,Dockerfile}`; keep the package initializer empty to prevent container imports of host modules.
- **Add:** `backend/app/llm/factory.py`, `backend/.dockerignore`, `backend/tests/test_agent_workflows.py`.
- **Modify:** `backend/app/docker_agents.py`, `backend/app/llm/model.py`.
- **Modify:** `backend/app/implementation/{agent.py,activities.py,workflows.py,worker.py}` and `backend/app/test_generation/{agent.py,activities.py,workflows.py,worker.py}`.
- **Remove:** `backend/app/implementation/Dockerfile`, `backend/app/test_generation/Dockerfile`, after migrating their callers/docs to the default image.
- **Extend selectively:** `backend/tests/test_implementation.py`, `backend/tests/test_test_generation.py`; keep existing integration coverage in `backend/tests/test_api_design_workflow.py` as the compatibility owner.
- **Document:** `docs/service-implementation.md` (shared architecture, image builds, future-stage example), `docs/interaction-schema.md` (source-map/provider references).
- **Keep unchanged:** parent Temporal workflows, queue/config values, launch scripts/Zed profiles, and test-runner template.

## Reuse
- `backend/app/docker_agents.py`: `docker`, `run_agent`, `keep_activity_alive`, `container_base_url`.
- `backend/app/implementation/activities.py`: `copy_service` source filtering and implementation validation.
- `backend/app/test_generation/activities.py`: contracts allowlist, `copy_test_suite`, trusted runner Dockerfile/template.
- `backend/app/temporal/workflows.py`: existing parallel child orchestration and replay patch markers; leave unchanged.
- Existing dedicated worker entrypoints and task-queue settings; preserve their deployment contracts.
- `backend/app/llm/model.py`: existing Gemini/OpenAI-compatible constructor options; extract once for host/container reuse without changing host caching.
- `backend/tests/test_api_design_workflow.py`: real Temporal workers already protect parallel start, independent branch errors, and public result shapes.
- Existing role prompts and distinct completion requirements in each `agent.py`.

## Steps
- [x] Confirm reusable-only scope, toolchain independence, existing reuse opportunities, and compatibility boundaries.
- [x] After approval, run baseline focused tests and capture representative child-workflow histories before changing workflow wrappers.
- [x] Extract the pure model constructor and shared container runtime; migrate both agent entrypoints without changing prompts/policies.
- [x] Add the shared host stage specification, copy utility, and lifecycle; migrate both activities while retaining role-local input/output safeguards.
- [x] Introduce explicit image builds and the allowlisted shared default Dockerfile; retire duplicated Dockerfiles.
- [x] Extract deterministic Temporal execution and worker-bootstrap helpers; retain all public registrations/options.
- [x] Add only missing boundary/extension/runtime coverage; update developer documentation with a future-stage recipe.
- [x] Run focused/full tests, historical replay, Docker packaging checks, and available end-to-end smoke verification.

## Verification
### Automated behavior coverage
- Preserve existing `ActivityEnvironment` tests with Docker mocked at the external command boundary: contracts-only tar contents, exclusions, trusted runner template, redaction, cleanup on failures/cancellation, and successful publication.
- Extend the existing lifecycle owner with invalid image-ID/output validation cases and retention of prior successful artifacts after agent/validation/build failures; avoid duplicating generic failure coverage across both roles.
- In `test_agent_workflows.py`, exercise a minimal third stage with a different image-build context and workspace/artifact names, without Maven files. This guards against hardcoded role/toolchain assumptions rather than repeating the publication suite.
- Add runtime tests using a scripted model at the existing model-construction boundary: real file writes/tool results, traversal/symlink escape rejection, recoverable tool failures, completion validation, and turn exhaustion. No live provider calls, source greps, or test-only production injection APIs.
- Existing real-worker tests remain the owner of parallel starts, independent branch failures, and returned session state. Replay captured pre-refactor child histories with `temporalio.worker.Replayer` to verify unchanged commands.

Use the existing Python environment (confirmed Python 3.13.14); container code remains Python 3.12-compatible:

```sh
cd backend
env/bin/python -m pytest tests/test_implementation.py tests/test_test_generation.py tests/test_agent_workflows.py tests/test_api_design_workflow.py
env/bin/python -m pytest
```

### Docker and end-to-end checks
Build both default variants after approval:

```sh
# From backend/
docker build -f app/agent_workflows/Dockerfile --build-arg AGENT_ROLE=implementation --build-arg WORKSPACE_NAME=service -t temporal-implementation-agent:refactor .
docker build -f app/agent_workflows/Dockerfile --build-arg AGENT_ROLE=test_generation --build-arg WORKSPACE_NAME=tests -t temporal-test-generation-agent:refactor .
```

Run credential-free import/packaging smoke checks via a Python entrypoint override. Confirm shared runtime/factory imports, UID/workspace, and absence of host config, Temporal orchestration, and the other role's module.

With Docker, Temporal, and model access available, finalize a small API-design session using the existing workers; verify parallel independent results and unchanged manifests/source directories. Then run the published service and HTTP test-runner images using `docs/service-implementation.md`. Test execution remains a manual verification step, not a new workflow feature. Report unavailable integration checks explicitly.

## Execution results
- Baseline focused suite: **22 passed**; captured success/failure histories for both original child workflows before changing wrappers.
- Expanded activity/runtime/extension coverage: **29 passed**; parent integration suite: **13 passed**.
- Full backend suite: **92 passed**.
- Replayed all **4 pre-refactor histories** against the migrated workflows successfully.
- Built both default agent images. Credential-free smoke checks passed on Python **3.12.15**: UID **10001**, correct role workspace, real read/write tools, pure runtime/factory imports, and absence of host configuration/orchestration/Temporal and role-package modules.
- Python syntax/whitespace checks passed; old role Dockerfile references were removed from source/developer docs.
- Live model-to-service/test-runner verification was **not run**: configured provider `localhost:1234` is unavailable. No feedback loop or service-test execution was added.
- No commits or image pushes were made.
