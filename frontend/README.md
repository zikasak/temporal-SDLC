# Async LLM requests

This page starts a Temporal workflow for each message, returns its workflow ID immediately, and retrieves the answer by that ID. Replies include the conversation in the LLM call. A message can include one PDF, DOCX, or TXT file. The original is kept on disk, extracted text is kept in PostgreSQL, and the workflow carries only its document ID. The LLM receives a `document://` link and can read or search the parsed text with tools. It can also create, edit, and delete its own `.txt` and `.md` documents in the conversation. The latest request ID restores the conversation, attachments, and generated document links while Temporal retains it. The existing greeting workflow and `POST /start` endpoint are still available.

## GitHub connection and session repositories

The **GitHub** tab connects one shared GitHub App installation: the app's ID and private key, the key encrypted in PostgreSQL using a stable backend `GITHUB_TOKEN_ENCRYPTION_KEY`. A new Requirements session must choose an accessible repository and branch; Chat can optionally choose one. Replies, retries, forks, and API-design sessions derived from Requirements inherit that fixed selection.

Requirements and API-design sessions check the branch out before the LLM starts, keep all LLM file access inside that checkout, and push the verified service and tests to a new `generated/…` branch; the app therefore needs the Contents read and write permission. Chat only records its selection. The connection is shared by every signed-in user. See [GitHub setup, permissions, security, and API details](../docs/github-connector.md).

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

`POST /auth/login` accepts `{"login":"you@example.com","password":"..."}` and returns a JWT (HS256, valid for `AUTH_TOKEN_TTL_HOURS`, default 12) as `access_token` and as an `HttpOnly`, `SameSite=Strict` cookie. The browser uses the cookie; other clients send `Authorization: Bearer <token>`. `GET /auth/me` returns the signed-in login, the token's expiry and `admin`, true for the first user. Tokens are stateless: signing out clears the cookie, and removing a user or changing a password does not revoke tokens already issued. Change `AUTH_JWT_SECRET` to sign everyone out. Sign-in is limited to `AUTH_LOGIN_ATTEMPTS_PER_MINUTE` (default 10) per client address, answering 429 beyond that, and password checks run two at a time with a short queue, answering 503 when it is full. Behind reverse proxies set `AUTH_TRUSTED_PROXY_HOPS` to their number so the limit applies to the real client address; the limits are per API process. All signed-in users share the same sessions and data; registering accounts is the only thing reserved for the first user.

## Model settings

The **Settings** tab stores the adapter, model, and thinking level for each workflow in PostgreSQL. Choices are discovered from the configured providers: Pi's RPC model list and per-model thinking levels (using `PI_CREDENTIALS_DIR`), Claude Agent SDK initialization metadata, or the configured `OPENAI_BASE_URL` model endpoint. The Google endpoint uses its native model list. Discovery sends no model prompts. Pi must be installed on the API host and support `get_available_thinking_levels`; Claude uses the SDK's bundled CLI.

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

Install backend dependencies and configure the worker with an OpenAI compatible chat endpoint that supports tool calling:

```sh
backend/env/bin/pip install -r backend/requirements.txt
export OPENAI_API_KEY="your-api-key"
# Optional for a compatible provider; the default is the local MLX endpoint.
# export OPENAI_BASE_URL="http://localhost:1234/v1"
cd backend
env/bin/python -m app.temporal.worker
```

For a local provider that does not require authentication, set `OPENAI_API_KEY` to the placeholder accepted by that provider. If the worker runs in a container, use an endpoint address reachable from that container. The defaults target the local MLX endpoint at `http://localhost:1234/v1`. `TEMPORAL_ADDRESS` defaults to `localhost:7233` for both the worker and API.

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
