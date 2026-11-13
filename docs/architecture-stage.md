# The architecture stage

A new service or an API change goes from its requirements to an architecture session and
only then to its API design (`backend/app/architecture/`).

1. Finalizing the requirements session of such a task on a repository has its task flow
   ([task-flow.md](task-flow.md)) begin its pending `ArchitectureWorkflow` child
   (`architecture_session_id`). The session checks the
   repository out and the LLM drafts the architecture at once: components with their
   responsibilities, the data flow between them and the key technology choices, recorded
   with the tools of `app/tools/architecture.py`. An API change starts from the
   repository's `contracts/architecture.json`.
2. For an API change the LLM may propose skipping the stage instead of drafting
   (`skip_proposal`). `POST /architecture/sessions/{id}/skip` confirms it (also without a
   proposal); sending a message declines it (`skip_declined`) and the LLM drafts from it,
   never proposing again. A new service cannot skip (409).
3. The user reviews the draft in the conversation (messages, answers, retries and forks,
   which are refused once the API design started). Finalizing writes `architecture.md` and
   `architecture.json` (`write_architecture_document`), downloadable with
   `GET …/document?format=md|json`.
4. Finalized or skipped, the session ends and hands an `ArchitectureResult` to its task
   flow, which begins its pending API design child, next to the architecture session
   (`api_design_session_id`); a confirmed skip lists the session as skipped. A session from
   before task flows existed hands it to its requirements session instead, or starts the
   design itself once that has closed.

The API design receives the architecture (`architecture`, `architecture_structure`) in its
prompt and passes it to the delivery's `TaskSpec`: the coding agents get it as context, and
the push commits it as `contracts/architecture.md` and `contracts/architecture.json`. A
skipped stage hands on the repository's existing files, which stay unchanged. Tasks on an
existing service need both files: `read_contract` refuses a contract without them, and a
bugfix or documentation delivery gives them to its agents read-only.

The architecture is drafted with the model configured for the API design.
