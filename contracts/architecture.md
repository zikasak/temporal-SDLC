# Architecture

## Components

### Web client

- React and TypeScript single-page app in frontend/, built with Vite and served by nginx in deployment
- Sign-in, registration of the first account and user administration
- Flows page: lists top-level task flows (by their current name), onboardings and chats, and a task flow's requirements, architecture, API design and delivery children with pending, skipped and linked-from-another-task marks; opens chats and sessions in a panel; the Requirements filter lists task flows only, and old-shape requirements flows from before this change are shown only in the All view
- Shows an open task flow as running and highlights it, and each of its children, as waiting for the user and with the unresolved findings marker as GET /workflows reports them, instead of deriving them from the children's sessions; opens the waiting child's questions from the highlight
- Old-shape requirements flows from before this change and their stages are only listed and opened, with no waiting-for-user highlight and no unresolved findings marker
- Opens the next stage from a session's returned IDs: the architecture session, the API design session, or the delivery of a bugfix or documentation change
- Repository and branch picker and GitHub connection settings
- Shows delivery progress, agent questions, review findings and pull request links, and sends the user's answers and decisions

### HTTP API

- FastAPI application (app.api.main) exposing the auth, chat, requirements, architecture, API design, onboarding, workflows and GitHub connector routes
- Requires a signed-in user on every router except auth and the GitHub webhook endpoint
- Validates input, bounds request sizes and maps Temporal and storage failures to 503 and session errors to 404/409/422
- Starts, queries, signals and updates Temporal workflows for each session through the session services
- Creates the database schemas at startup (including the model settings table) and runs background loops: expired-document cleanup, Pi OAuth credential refresh and recovery of interrupted webhook mentions

### Auth module

- Registers the first account as administrator and lets the administrator create and list accounts (app.auth)
- Hashes passwords with salted scrypt and verifies them at sign-in
- Issues and checks HS256 JWT session tokens signed with AUTH_JWT_SECRET, sent as bearer tokens or the HttpOnly access_token cookie
- Rate-limits sign-in and registration attempts per client address

### Session services

- Per-flow services (app.requirements, app.architecture, app.api_design, app.onboarding, app.conversations, app.delivery service modules) that turn API calls into Temporal workflow starts, queries, signals and updates
- Start a TaskWorkflow for every new requirements interview, for every fork of a requirements, architecture or API design session (linked to the original task's earlier stages) and for every fix round on a closed task's delivery; onboardings keep their own top-level OnboardingWorkflow
- Read session state, conversation, drafts, delivery progress and the task flow's ID and next stage's ID (architecture session, API design session, or the delivery of a bugfix or documentation change) from workflow queries for the API responses
- Hand authorised pull request mentions to the delivery or onboarding workflow they belong to
- List workflow executions with their repository, branch, onboarding, task name, pending/skipped state, linked-task, waiting-for-user and unresolved-findings search attributes and the workflow registry (app.temporal.executions, app.temporal.registry): top_level lists task flows, onboardings, chats and the old-shape requirements flows started before this change; parent lists a flow's direct children plus stages linked to it, and an old-shape requirements flow's children as before
- For closed deliveries, onboardings and task flows, clear the listed unresolved findings marker when a findings acceptance record (app.delivery.acceptances) exists for the closed delivery run, since a closed workflow can no longer update its own search attributes
- The workflow_type filter for task flows lists only TaskWorkflow executions, so old-shape RequirementsWorkflow executions appear only in the unfiltered (All) listing

### Orchestration worker

- Temporal worker on the main task queue (app.temporal.worker)
- Runs LLMRequestWorkflow, TaskWorkflow, RequirementsWorkflow, ArchitectureWorkflow, ApiDesignWorkflow, OnboardingWorkflow and DeliveryWorkflow
- TaskWorkflow (new, top-level per task): creates its stage children up front per task kind (requirements, architecture, API design and delivery for a new service or API change; requirements and delivery for a bugfix or documentation), holds each one pending until the stages it depends on have finished, then starts it, and closes skipped stages; it stays running (its Temporal status) for as long as any child is pending or running
- TaskWorkflow starts the pending delivery of a bugfix or documentation change as soon as its requirements are finalized, the architecture of a new service or API change when its requirements are finalized, and the API design when the architecture is finalized or skipped
- TaskWorkflow keeps the task's short name: written by an LLM from the intake at start and rewritten from the finalized requirements; a task flow started by a fork or a fix round on a closed delivery copies the original task's name with a '(fork)' or '(fix)' suffix, and a fork's name is still rewritten when its requirements are finalized
- TaskWorkflow closes with its delivery child's status (cancelled when the delivery was skipped) once every child, including the earlier stages it links to from a fork or fix round, has closed
- Stage and onboarding workflows keep their own waiting-for-user state (unanswered interviewer questions, coding agent questions, open review findings, a skip proposal or a contract decision) and unresolved-findings state as search attributes, cleared when the flow closes so a closed flow is never waiting (FR-110)
- Stages report every change of their waiting-for-user and unresolved-findings state to their TaskWorkflow, which aggregates its children's states into its own WaitingForUser and UnresolvedFindings search attributes
- Stage workflows report their hand-off (requirements finalized or idle-written, architecture finalized or skip confirmed, API design finalized, pull request merged or closed) to their TaskWorkflow instead of starting the next stage themselves, and on closing signal every task flow they are linked into
- Activities for LLM interviewer turns and drafting, writing the requirements, architecture and OpenAPI documents, and naming task flows
- Activities for repository checkout and contract reading, pushing deliveries, documentation and onboardings, and opening or updating pull requests with LLM-written text
- Classifies mentions of the GitHub App, replies to them and answers clarifications
- DeliveryWorkflow runs code generation, implementation, test generation, verification with diagnosis and repair rounds, code review rounds and the push as child workflows on the other task queues
- Executions started before this change keep running in their old shape (stages under a RequirementsWorkflow) and are not migrated

### Codegen worker

- Temporal worker on the codegen task queue running CodegenWorkflow (app.codegen.worker)
- Runs openapi-generator to turn the finalized OpenAPI document into server code for the chosen language
- Writes the generated code to GENERATED_CODE_DIR for the next stages

### Sandbox worker

- One host process with a Temporal worker per sandboxed stage and its own task queue: implementation, test generation, verification, diagnosis, code review and documentation, one activity at a time each (app.agent_workflows.worker)
- Builds agent images and starts, monitors and removes coding agent containers through a Docker daemon (app.docker_agents)
- Passes the workspace, LLM settings and provider credentials into containers and re-logs their output
- Keeps the workspace of an agent that paused with questions and resumes it with the user's answers
- Builds the service from its root Dockerfile and runs the contract test suites against it (Python and Java test-suite images) for verification and baseline checks

### Coding agent containers

- Isolated Docker containers, one per agent run, built from app/agent_workflows/Dockerfile with the role's agent.py
- Implementation, test generation, diagnosis, code review and documentation agents that read and write only their mounted workspace
- Call the LLM through the LangChain, Pi or Claude adapter with the stage's model settings
- Pause with questions for the user by saving their state in the workspace
- Have no access to Temporal, PostgreSQL or the Docker daemon

### LLM adapters

- Build the chat model for a workflow from its model settings (app.llm.factory): OpenAI-compatible or Google Gemini through LangChain, the Pi coding agent CLI, or Claude through the Claude Agent SDK
- Run the LangGraph tool-calling agent with the session tools (requirements, architecture, API design, repository, documents and questions tools in app.tools)
- Normalize the questions the LLM asks the user
- Optionally trace LLM calls to Langfuse

### GitHub connector

- Stores the shared GitHub App connection with its private key and webhook secret encrypted with Fernet (app.github.storage)
- Calls the GitHub REST API as the app: installation tokens, repositories, branches, manifest conversion, webhook configuration, pull requests and comments (app.github.client)
- Checks out and pushes branches with git using installation tokens (app.github.checkout)
- Verifies webhook signatures, deduplicates deliveries and comments, and admits only authorised mentions on the app's pull requests (app.github.webhooks)

### Document storage

- Stores uploaded attachments and generated documents with their metadata in PostgreSQL and their originals on disk under DOCUMENT_STORAGE_DIR (app.documents)
- Parses uploads into searchable session passages
- Deletes expired documents

### Model settings

- Internal storage of the adapter, model and thinking level chosen for each LLM workflow type (app.settings)
- Knows the models and thinking levels each provider offers
- Provides the settings the workers and agent containers use for their LLM calls

### PostgreSQL

- Stores application data in the app schema: users, GitHub connection, pull requests, webhook deliveries and comments, mention admissions, delivery mentions, onboarding sessions, documents and passages, workflow settings and the workflow registry
- Stores Temporal's persistence and visibility databases in deployment

### Temporal server

- Persists workflow state and dispatches workflow and activity tasks to the workers by task queue
- Keeps the Repository, Branch and OnboardingId search attributes used to list executions
- Keeps the new FlowState (pending or skipped, overriding the Temporal status in listings), TaskName and LinkedTaskFlows (keyword list of task flows a stage is linked into by a fork or fix round) search attributes, upserted by the workflows
- Keeps the new WaitingForUser and UnresolvedFindings boolean search attributes, upserted by stages, onboardings and task flows, so listings carry the backend-computed highlight and marker

### GitHub

- External service: the GitHub REST API at api.github.com and git hosting of the repositories the shared GitHub App installation can access
- Issues installation tokens, converts app manifests and holds the app's webhook configuration, pull requests and comments
- Delivers signed webhook events for pull request conversation and inline review comments

### LLM provider

- External service: the model endpoints chosen in the model settings (OpenAI-compatible endpoints, Google Gemini, the providers behind the Pi CLI, or Anthropic's Claude)
- Answers the chat, interviewer, drafting, review, pull request text, mention classification and coding agent model calls

### Langfuse

- External, optional tracing service that receives traces of LLM calls, of the main and codegen queues' activity executions and of sandboxed stage operations
- Used only when LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_BASE_URL are set and LANGFUSE_TRACING_ENABLED is not false

## Data flow

- **Web client → HTTP API**: JSON (and multipart for chat attachments) over HTTP under /api through the frontend's proxy, with the access_token cookie; the client polls sessions and workflow lists for progress.
- **HTTP API → Auth module**: In-process calls: credentials for registration and sign-in, and the bearer token or cookie of every protected request to resolve the signed-in user.
- **Auth module → PostgreSQL**: Users with their scrypt password hashes and creation times, read and written with psycopg.
- **HTTP API → Session services**: In-process calls with validated session actions: messages, answers, forks, finalize, skip, checkout, contract decisions, agent answers, findings decisions, stage retries and workflow list filters.
- **Session services → Temporal server**: Temporal client gRPC calls: start TaskWorkflows (and chats and onboardings) with search attributes, send signals and updates to the task flow and its stages, run queries for session state and list executions by visibility queries over ParentWorkflowId, LinkedTaskFlows, FlowState and the other search attributes.
- **Session services → PostgreSQL**: Workflow registry entries, onboarding sessions and delivery mentions, read and written with psycopg.
- **HTTP API → GitHub connector**: In-process calls: connect, check and remove the app, list repositories and branches, and hand over raw webhook deliveries with their signature for verification and mention admission.
- **GitHub connector → PostgreSQL**: The connection with its Fernet-encrypted private key and webhook secret, the app's pull requests, handled webhook deliveries and comments, and mention admissions.
- **HTTP API → Document storage**: In-process calls: chat attachments to store, and document IDs to stream back as downloads.
- **Document storage → PostgreSQL**: Document metadata and parsed session passages; the originals are written to files on disk.
- **Model settings → PostgreSQL**: The workflow_settings row with the model settings of every LLM workflow type.
- **Temporal server → Orchestration worker**: Workflow and activity tasks of the main task queue over gRPC; the worker returns results, starts TaskWorkflow stage children (pending ones wait for a start signal from their task flow) and starts child workflows on the codegen and sandboxed stage queues; stage workflows signal their task flow at each hand-off and on every change of their waiting-for-user and unresolved-findings state, and, when they close, every task flow in their LinkedTaskFlows; the task flow signals pending stages to start or close as skipped; workflows upsert their search attributes.
- **Temporal server → Codegen worker**: CodegenWorkflow tasks with the OpenAPI document and language over gRPC; the result names the generated code directory.
- **Temporal server → Sandbox worker**: Implementation, test generation, verification, diagnosis, review and documentation workflow tasks over gRPC, each on its own task queue; results carry workspaces, verdicts, findings or agent questions.
- **Orchestration worker → LLM adapters**: In-process calls with the conversation, session tools and the workflow's model settings for interviewer turns, drafting, chat answers, task flow names, pull request text and mention classification and replies.
- **Orchestration worker → GitHub connector**: In-process calls to check out the session's branch and read its contract, push deliveries and onboardings to new branches, open or update pull requests and post mention replies.
- **Orchestration worker → Document storage**: Parses uploads into passages and writes generated requirements, architecture and OpenAPI documents.
- **Orchestration worker → Model settings**: Reads the model settings of the workflow type an LLM activity runs for.
- **Sandbox worker → Coding agent containers**: Docker CLI calls to a dedicated Docker daemon: builds the agent image, copies in the workspace (contract, generated code, tests, review findings, answers) and the stage's model settings file, passes provider credentials as environment variables, and copies the workspace and agent state back out.
- **Sandbox worker → Model settings**: Reads each stage's model settings to pass into its agent container.
- **Coding agent containers → LLM adapters**: In-process calls inside the container: the agent runs the adapter code copied into its image with the stage's model settings file.
- **Session services → Orchestration worker**: Admitted pull request mentions handed to the delivery or onboarding workflow as Temporal signals or updates, delivered through the Temporal server.
- **Codegen worker → Sandbox worker**: Generated server code passed as files under GENERATED_CODE_DIR on the shared /data volume, which the implementation stage copies into its workspace.
- **GitHub connector → GitHub**: REST calls over HTTPS through httpx, authenticated with RS256 app JWTs and installation tokens (installations, repositories, branches, manifest conversion, webhook configuration, pull requests and comments), and git clone and push over HTTPS with installation tokens.
- **GitHub → HTTP API**: Webhook deliveries (issue_comment and pull_request_review_comment events) POSTed to /connectors/github/events with the X-Hub-Signature-256, X-GitHub-Event and X-GitHub-Delivery headers.
- **LLM adapters → LLM provider**: Model calls over HTTPS with the conversation, tools and the workflow's model and thinking level, through LangChain clients, the Pi CLI or the Claude Agent SDK, from the workers and from inside agent containers with the forwarded credentials.
- **LLM adapters → Langfuse**: Traces of LLM calls (LangChain callback handler, Claude plugin hook) exported over HTTPS with the Langfuse keys, with secrets redacted and content capture optional.
- **Sandbox worker → Langfuse**: Spans of sandboxed stage operations (agent runs, diagnosis, review) over HTTPS; the trace context and Langfuse settings are passed into agent containers as environment variables.
- **Orchestration worker → Langfuse**: Spans of the main queue's activity executions (interviewer turns, drafting, document writing, mention handling), tagged with the Temporal workflow, run, activity ID and attempt by the trace_activity decorator (app.temporal.tracing), exported over HTTPS with the Langfuse keys when tracing is enabled; replayed workflow code is never traced.
- **Codegen worker → Langfuse**: Spans of the generate-server-code activity (CodegenWorkflow), tagged with the Temporal workflow, run, activity ID and attempt by the trace_activity decorator (app.temporal.tracing), exported over HTTPS with the Langfuse keys when tracing is enabled.

## Technology choices

| Topic | Choice | Rationale |
| --- | --- | --- |
| Backend language and web framework | Python 3.12 with FastAPI, served by uvicorn | Required by TR-1; async request handling fits the Temporal and httpx clients. |
| Workflow orchestration | Temporal (temporalio Python SDK, server 1.31) with separate task queues for the main, codegen and each sandboxed stage worker | Long-running sessions that wait on the user, retries of single stages and child workflows per stage; separate queues keep each stage to one container at a time. |
| Storage | PostgreSQL 16 through psycopg 3 for application data and Temporal persistence; uploaded originals and generated code on a disk volume | Required by TR-1; one database server serves both the app and Temporal. |
| Web client | React 19 with TypeScript, built with Vite, react-markdown for rendered documents | Required by TR-1. |
| LLM access | LangChain/LangGraph tool-calling agents over OpenAI-compatible endpoints or Google Gemini, plus the Pi coding agent CLI and the Claude Agent SDK as alternative adapters, chosen per workflow type in the model settings | Lets each workflow use a different provider and model without code changes. |
| Coding agent isolation | Docker containers started through a separate Docker-in-Docker daemon (docker:dind under the Sysbox runtime) whose socket only the sandbox worker mounts, on its own network without private addresses | TR-2: agents must not reach Temporal, the database or Docker. |
| Code generation | openapi-generator, run as a subprocess by the codegen worker | Produces server scaffolding from the finalized OpenAPI document before the coding agents fill it in. |
| Repository integration | One shared GitHub App: REST API through httpx with RS256 app JWTs and installation tokens, git CLI for checkout and push, signed webhooks (HMAC-SHA256) for pull request comment mentions | Lets the instance act on repositories without user tokens and receive mentions. |
| Authentication and secrets | HS256 JWTs via PyJWT, scrypt password hashes from hashlib, Fernet (cryptography) encryption of the GitHub App key and webhook secret | NFR-2 and NFR-3. |
| Observability | Optional Langfuse tracing of LLM calls and stage operations, also from inside agent containers | Active only when the Langfuse keys and base URL are set (LANGFUSE_TRACING_ENABLED defaults to true but can turn it off); secrets are redacted from traces. |
| Deployment | Docker Compose (Coolify): frontend with nginx, api, worker, codegen-worker, sandbox-worker, sandbox daemon, PostgreSQL, Temporal and Temporal UI; the root Dockerfile builds the API alone with its own PostgreSQL on port 8080 as a non-root user | TR-3: the root image is a self-contained service for contract tests; deployments use backend/Dockerfile. |
| Task flow hierarchy | A TaskWorkflow per task on the main queue starts every stage as its Temporal child at once; a stage not yet allowed to work waits for a start signal and carries FlowState=pending, a stage that will never run closes with FlowState=skipped; links of forked or fix-round task flows to earlier stages are the LinkedTaskFlows keyword-list search attribute on those stages | FR-125 to FR-140: Temporal has no not-yet-started executions or second parents, so pending, skipped and linked are expressed with search attributes GET /workflows can filter on, keeping NFR-7's existing fields and Temporal statuses underneath. |
| Waiting highlight and findings marker | Computed in the backend: each stage and onboarding upserts WaitingForUser and UnresolvedFindings search attributes as its state changes and clears WaitingForUser when it closes; the TaskWorkflow aggregates its children's states, signalled by them, into its own attributes; for closed flows the listing clears UnresolvedFindings when the existing findings acceptance record exists for the delivery run; GET /workflows returns both as new fields. Executions started before this change carry neither attribute and are listed without highlight or marker (the fields are null for them) | User's choice to compute the highlight in the backend; search attributes let the listing show it without querying every child, the acceptance record already holds acceptances given after a flow closed (FR-142, FR-143), closed flows are never waiting (FR-110), and the listing only gains fields (NFR-7). User chose not to backfill old-shape flows: they are only listed. |
