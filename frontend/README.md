# Async LLM requests

This page starts a Temporal workflow for each message, returns its workflow ID immediately, and retrieves the answer by that ID. Replies include the conversation in the LLM call. A message can include one PDF, DOCX, or TXT file. The original is kept on disk, extracted text is kept in PostgreSQL, and the workflow carries only its document ID. The LLM receives a `document://` link and can read or search the parsed text with tools. It can also create, edit, and delete its own `.txt` and `.md` documents in the conversation. The latest request ID restores the conversation, attachments, and generated document links while Temporal retains it.

## GitHub connection and session repositories

The **GitHub** tab connects one shared GitHub App installation: the app's ID and private key, the key encrypted in PostgreSQL using a stable backend `GITHUB_TOKEN_ENCRYPTION_KEY`. A new Requirements session must choose an accessible repository and branch; Chat can optionally choose one. Replies, retries, forks, and API-design sessions derived from Requirements inherit that fixed selection.

Requirements and API-design sessions check the branch out before the LLM starts, keep all LLM file access inside that checkout, and push the verified service and tests to a new `generated/…` branch; the app therefore needs the Contents read and write permission. Chat only records its selection. The connection is shared by every signed-in user. See [GitHub setup, permissions, security, and API details](../docs/github-connector.md).

## Flows page

The Flows page lists the top-level flows: task flows (by their current name), onboardings and chats. Opening a task flow lists its requirements, architecture, API design and delivery steps with their statuses, including **Pending** for a step that waits for the steps it depends on and **Skipped** for one that will never run; the earlier steps of another task that a forked or fix-round task flow links to are marked *linked from another task*. A flow or step the backend reports as waiting for you is highlighted, with an **Open questions** button that opens the step that asks; a closed flow whose delivery's review findings were neither accepted nor acted on carries an *unresolved findings* marker instead. The **Requirements** filter lists task flows; requirements flows from before task flows existed appear only under **All**. For flows the backend reports no waiting or findings flags for (those started before it did), the page reads each one's session to highlight it and show the marker. A session's view links to all steps of its task flow (`?parent=<task flow>`), and a closed delivery lists the new flows its fix rounds started.

## Requirement IDs in conversations

In the requirements, architecture, API design and onboarding conversations, and in the coding agents' questions in the delivery progress, requirement IDs the LLM mentions in its messages, questions and options (such as `FR-12`, `fr12` or `NFR-3`) are marked. Hovering over one or tapping it shows the requirement's ID, title, description, priority and category, marked "retired" when the task retires it, or "Unknown requirement" when no requirement has that ID. The text comes from the session response's `lookup_requirements` field.

## Sign-in

Every API route except `POST /auth/login`, `POST /auth/logout`, `GET /auth/registration` and `POST /auth/register` requires a signed-in user. A login is an email address or a username, case-insensitive.

While there are no accounts, the sign-in page offers to create the first one (`POST /auth/register`, which also signs it in). Registration then closes: only the first user, the earliest account still present, registers the others, on the Users page (`GET` and `POST /auth/users`). `GET /auth/registration` reports whether the first account is still to be created. Create the first account before exposing a new deployment, since whoever reaches it first can take it.

Accounts can also be created, reset and removed from `backend/`:

```sh
env/bin/python -m app.auth.users add you@example.com   # prompts for the password; also resets one
env/bin/python -m app.auth.users list
env/bin/python -m app.auth.users remove you@example.com
```

Set `AUTH_JWT_SECRET` (at least 32 random characters) in `backend/.env` first; without it the API answers 503. Passwords are stored only as salted scrypt hashes in `app.users`.

`POST /auth/login` accepts `{"login":"you@example.com","password":"..."}` and starts a server-side session. It returns a short-lived access token (an HS256 JWT naming the session, valid for `AUTH_ACCESS_TOKEN_TTL_MINUTES`, default 15) as `access_token` and as an `HttpOnly`, `SameSite=Strict` cookie, and a rotating `refresh_token`, also set as an `HttpOnly`, `SameSite=Strict` cookie scoped to `/api/auth/refresh`. The browser uses the cookies; other clients send `Authorization: Bearer <token>` and renew it by posting `{"refresh_token":"..."}` to `POST /auth/refresh`, which returns a new pair. A replaced refresh token is accepted for 30 more seconds; reused later, it revokes the session. A session can no longer be renewed after `AUTH_SESSION_IDLE_DAYS` (default 7) without activity or `AUTH_SESSION_MAX_DAYS` (default 30) after sign-in. The web app refreshes shortly before the access token expires, one tab at a time, retries a request once after a 401, and shows the sign-in form with a "session expired" notice when the session cannot be renewed. `GET /auth/me` returns the signed-in login, the access token's expiry and `admin`, true for the first user. Every request checks that its session is still active: signing out revokes it at once, and removing a user or resetting a password from the host ends their sessions. Change `AUTH_JWT_SECRET` to invalidate every access token. Sign-in is limited to `AUTH_LOGIN_ATTEMPTS_PER_MINUTE` (default 10) and refresh to `AUTH_REFRESH_ATTEMPTS_PER_MINUTE` (default 30) per client address, answering 429 beyond that, and password checks run two at a time with a short queue, answering 503 when it is full. Behind reverse proxies set `AUTH_TRUSTED_PROXY_HOPS` to their number so the limit applies to the real client address; the limits are per API process. All signed-in users share the same sessions and data; registering accounts is the only thing reserved for the first user.

## Model settings

The **Settings** tab stores the adapter, model, thinking level, and context compaction threshold for each workflow in PostgreSQL, including the requirements reviewer (Requirements review), which checks the requirements of tasks and onboardings for contradictions and starts with the Requirements model. Choices are discovered from the configured providers: Pi's RPC model list and per-model thinking levels (using `PI_CREDENTIALS_DIR`), Claude Agent SDK initialization metadata, or the `/models` list of the OpenAI endpoint saved on this page. The Google endpoint uses its native model list. Discovery sends no model prompts. It also reads each model's context window where the provider reports one (Pi, the Google endpoint, and OpenAI-compatible servers that add OpenRouter's `context_length` or vLLM's `max_model_len`); a compaction threshold above it cannot be saved. The tab also sets the length up to which contracts are embedded whole in coding agents' inputs. Both Docker application images include Pi; a local API needs Pi installed on its PATH, with support for `get_available_thinking_levels`. Claude uses the SDK's bundled CLI.

The **OpenAI endpoint** section, shown to the administrator (the first user) only, sets the base URL, API key and display name of the OpenAI-compatible endpoint that every model run and coding agent of the openai adapter uses; the environment variables `OPENAI_BASE_URL` and `OPENAI_API_KEY` are no longer read for it. Saving a new base URL or key first checks it with `GET {base URL}/models` and saves nothing when that fails. The key is stored encrypted with `SETTINGS_ENCRYPTION_KEY` (a Fernet key in the backend's environment) and never returned; leaving the field empty keeps the saved key. The display name replaces "OpenAI" in the model pickers. Until a base URL is saved, the openai adapter is not offered and workflow types with nothing saved default to Pi. **Remove** clears the endpoint, unless a workflow type still uses the openai adapter.

In Coolify, configure `OPENAI_API_KEY` for Pi's `openai/*` models or `ANTHROPIC_API_KEY` for its `anthropic/*` models. For `openai-codex/*`, sign in through Pi in the API container as user `app`: `pi --no-session`, then `/login openai-codex`. The Compose deployment persists its dedicated credential volume across redeploys and shares it with workers and the sandbox daemon, including token-refresh locks. See [Pi OAuth login and deployment](../docs/pi-oauth.md) for the one-time setup. Rebuild and redeploy the application image after upgrading from an image without Pi.

Model and thinking selectors show only provider-reported choices. Endpoints without thinking capability metadata offer **Model default**, which omits explicit reasoning options. **Refresh provider options** reloads the catalog without discarding edits. Unavailable saved selections stay visible for correction; saves recheck provider availability and the model/level combination before writing any workflow. Changes apply to the next model run.

## Start locally

In Zed, run `debugger: start` and select **Full stack (API + workers + frontend)**.
It starts the Compose services, FastAPI, all three Temporal workers, and Vite.
Python breakpoints work in the API and each worker through Debugpy child sessions.
The profile enables `TEMPORAL_DEBUG` so workflow pauses do not trigger deadlock detection.
For React/TypeScript breakpoints, also start the **Frontend (Chrome)** profile.
Stopping the full-stack debug session stops the app processes; Compose services
remain running. Docker must be running and the backend/frontend dependencies
must already be installed. The profile uses the local Temporal port `7233`.

Start Temporal services from the project root:

```sh
docker compose -f backend/docker-compose.yml up
```

Install backend dependencies and start the worker; set `SETTINGS_ENCRYPTION_KEY` for the API and workers alike, so the OpenAI endpoint's API key can be saved:

```sh
backend/env/bin/pip install -r backend/requirements.txt
export SETTINGS_ENCRYPTION_KEY="$(backend/env/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
cd backend
env/bin/python -m app.temporal.worker
```

To use an OpenAI-compatible chat endpoint that supports tool calling, such as a local MLX endpoint at `http://localhost:1234/v1`, set it up on the Settings page as the administrator; an endpoint that needs no authentication can be saved without a key. If the worker runs in a container, use an endpoint address reachable from that container. `TEMPORAL_ADDRESS` defaults to `localhost:7233` for both the worker and API.

The API and worker both use `APP_DATABASE_URL` (default `postgresql://temporal:temporal@localhost:5433/temporal`) and `DOCUMENT_STORAGE_DIR` (default `backend/data/documents`). They create an `app.documents` table in the existing PostgreSQL service. If the API and worker run on different hosts, mount the same persistent document directory into both. The local Compose file exposes PostgreSQL on port 5433. Uploaded originals are stored on disk; generated text documents are stored in PostgreSQL. Documents expire about one day after the latest request that references them; the API removes expired documents hourly.

Start the API from `backend/` so the `app` package resolves:

```sh
cd backend
env/bin/uvicorn app.api.main:app --reload --port 8000
```

Then start the frontend:

```sh
cd frontend
npm install
npm run dev
```

The Vite development server forwards `/api/*` to `http://127.0.0.1:8000` and removes the `/api` prefix.

## API

`POST /llm/requests` accepts `{"message":"Hello"}` and returns HTTP 202 with `{"request_id":"llm-request-...","status":"running"}`. To reply, send `{"message":"Tell me more","parent_request_id":"llm-request-..."}` using a completed request ID. Each reply receives a new request ID. To attach a file, submit multipart form data with `message`, optional `parent_request_id`, and `file`. One file of up to 20 MiB is accepted per message. PDF parsing reads embedded text only; image-only and encrypted PDFs produce a parsing error. Extracted text is limited to 2 million characters.

`GET /llm/requests/{request_id}` returns `request_id`, `status`, `result`, `error`, `messages`, `parent_request_id`, and `generated_documents`. `messages` contains the full ordered user and assistant conversation, including the pending user message while the latest request runs. An attached user message has an `attachment` object with `document_id`, `filename`, `content_type`, and `size_bytes`; it contains no file bytes or extracted text. `generated_documents` lists the live model-created files in the conversation with the same metadata and a `revision`. `GET /llm/requests/{request_id}/documents/{document_id}` downloads either an uploaded file referenced by that conversation or a model-created file in the same conversation. `POST /llm/requests/{request_id}/retry` retries a failed request with its stored attachment and original conversation session. `result` remains the latest text answer when completed and is `null` otherwise. Unknown or expired IDs return 404. A parent request that is still running, failed, or predates conversation storage returns 409. A conversation above 64 KiB of serialized messages returns 413; earlier turns are never dropped silently.

The model has four document tools: `create_document` creates a `.txt` or `.md` file with UTF-8 content of at most 256 KiB; `edit_document` replaces one exact text match; `replace_document` replaces the full content; and `delete_document` removes the file. Editing, replacing, and deleting require the current revision. The backend permits those actions only for documents marked as model-created in the current conversation, including its branches. Uploaded documents remain read-only to the model. Deletion removes a generated document immediately.

Each model call has a 120 second timeout; the document tool loop runs inside a six minute activity attempt. Temporal runs the LLM activity once, to avoid replaying file changes after an uncertain result, and bounds the workflow to eight minutes. Each request's result and conversation snapshot remain available for the local Temporal namespace's one day retention period. An explicit retry can repeat a billable provider call if an earlier attempt reached the provider but its outcome was lost.
