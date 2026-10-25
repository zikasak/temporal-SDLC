# Onboarding an existing repository

`POST /onboarding/sessions` starts an `OnboardingWorkflow` (`backend/app/onboarding/`) on a
repository branch. It runs outside requirements sessions and the intake. Only one unfinished
onboarding per repository and branch is allowed (`app.onboarding_sessions` plus an advisory
lock).

1. **preparing**: the branch is checked out.
2. **awaiting_confirmation**: the branch already has a contract in `contracts/`, contract
   tests in `contract-tests/` or a root `Dockerfile` (`existing_artifacts`). The user calls
   `POST …/contract-decision`. Replacing goes on; declining ends the onboarding as
   **declined** and changes nothing.
3. **drafting**: the LLM reads the code and drafts the requirements, the API and the
   language (`onboarding_turn` activity).
4. **interviewing / turn_running**: the user reviews and corrects the drafts. Failed turns
   and failed drafting can be retried, and `POST …/checkout` checks the branch out again.
   Forking ends the original as **superseded**.
5. **finalize** turns the drafts into the contract and starts a `DeliveryWorkflow` with
   `TaskSpec.onboarding`. No code is generated. The implementation agent writes only the
   root `Dockerfile` and `.dockerignore`, and the host keeps the existing code as it was.
   The test agent writes a new contract-test suite. Verification, repair rounds (at most
   5), code review rounds (at most 5), the push to `onboarding/<branch>-<short id>` and the
   pull request work as for any delivery, and so do mentions on the pull request.

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

`GET /workflows` returns `repository`, `branch` and `onboarding_id` for every execution
(null when they do not apply) and filters on them with the same query parameters. They are
recorded when an execution starts (`app/temporal/execution_fields.py`): the repository
selection in the memo and the custom keyword search attributes `Repository` (lower-cased
`owner/name`), `Branch` and `OnboardingId`. The `temporal-namespace` compose job and the main
worker register the attributes; the main worker also deletes, once per instance, the
executions started before they recorded these fields. The **Workflows** page has an
Onboarding filter, shows each execution's repository and branch, and links an onboarding and
the delivery and code generation it started to the onboarding's page.
