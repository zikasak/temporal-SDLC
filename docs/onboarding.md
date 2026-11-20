# Onboarding an existing repository

`POST /onboarding/sessions` starts a task flow (`TaskWorkflow` with origin `onboarding`, see
[task flows](task-flows.md)) whose children are the onboarding interview
(`OnboardingWorkflow`, `backend/app/onboarding/`), its requirements review and its delivery.
It runs outside requirements sessions and the intake. Only one unfinished
onboarding per repository and branch is allowed (`app.onboarding_sessions` plus an advisory
lock).

1. **preparing**: the branch is checked out.
2. **awaiting_confirmation**: the branch already has a contract in `contracts/`, contract
   tests in `contract-tests/` or a root `Dockerfile` (`existing_artifacts`). The user calls
   `POST …/contract-decision` with `decision` `replace`, `decline` or
   `architecture_only`. Replacing goes on; declining ends the onboarding as **declined** and
   changes nothing. `architecture_only` is offered only while `architecture_missing` is true
   (the contract lacks `contracts/architecture.md` or `contracts/architecture.json`) and the
   rest of that contract is readable (`inspect_onboarding_contract` activity).
3. **drafting**: the LLM reads the code and drafts the requirements, the architecture, the
   API and the language (`onboarding_turn` activity). An architecture-only onboarding
   drafts only the architecture and records with `record_conflict` where it disagrees with
   the existing `api.json` or `requirements.json`; open conflicts are listed in `problems`.
4. **interviewing / turn_running**: the user reviews and corrects the drafts. Failed turns
   and failed drafting can be retried, and `POST …/checkout` checks the branch out again.
   Forking ends the original as **superseded** and starts a new task flow for the fork,
   with the review state copied; the original's task flow closes as cancelled.
5. **finalize** first has the requirements review check the drafted requirements (unless
   `force=true`, or for an architecture-only onboarding); contradictions are asked in the
   interview, as for a requirements session. Then the drafts become the contract and the
   task flow starts its pending `DeliveryWorkflow` with `TaskSpec.onboarding`. No code is generated. The implementation agent writes only the
   root `Dockerfile` and `.dockerignore` and the repository's checks script
   `.sdlc/checks.sh` (see [the repository's checks](service-implementation.md#the-repositorys-checks)),
   and the host keeps the existing code as it was.
   The test agent writes a new contract-test suite. Verification with the checks script,
   repair rounds (while they make progress, at most 12), code review rounds (at most 5), the push to `onboarding/<branch>-<short id>` and the
   pull request work as for any delivery, and so do mentions on the pull request. The
   contract includes `contracts/architecture.md` and `contracts/architecture.json`.
   An architecture-only onboarding starts a delivery with `TaskSpec.architecture_only`,
   which only pushes those two files (`publish_onboarding_architecture`) and opens the pull
   request: no tests, verification or code review.

When the diagnosis blames the existing code, the delivery asks the user in
`agent_questions` (stage `diagnosis`) whether it is a bug, and waits without a timeout:

- If the user confirms the bug, the implementation agent may change the code.
- If the user says the Dockerfile or setup is at fault, only those files are repaired.
- If the user says the code is right, the tests are repaired instead.

When the existing code changes, `existing_code_changed` is set, and the commit message and
the pull request description say so.

## In the web client and the workflow list

The **Onboarding** tab starts an onboarding from a repository and branch and offers every
action of `/onboarding/sessions`: messages and answers, the contract decision, checkout
updates, finalizing, forking, retries, agent questions (including the confirmation of a bug
in the existing code), open review findings and stage retries. The repository picker of the
**Requirements** tab can start an onboarding of the selected branch and open it there.

`GET /workflows` returns `repository` and `branch` for every execution (null when they do
not apply) and filters on them with the same query parameters; task flows also carry
`origin`, and `origin=onboarding` lists the onboardings' task flows. They are recorded when
an execution starts (`app/temporal/execution_fields.py`): the repository selection in the
memo and the custom keyword search attributes `Repository` (lower-cased `owner/name`),
`Branch` and `TaskOrigin`. The `temporal-namespace` compose job and the main worker register
the attributes; the main worker also deletes, once per instance, the executions started
before they recorded these fields. The **Flows** page's Onboarding filter lists the
onboardings' task flows, whose children are the onboarding interview, its requirements
review and its delivery.
