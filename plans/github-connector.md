# GitHub connector and configuration

## Context
Lay the groundwork for working on existing Git projects: connect GitHub, configure access in the UI, and associate repositories with sessions. The app uses React/TypeScript, FastAPI, Temporal, and PostgreSQL, with no existing connector or application authentication layer.

**Confirmed scope:** one shared personal-access-token connection; optional repository/branch selection per Chat or Requirements session; inherited, immutable selections for derived sessions, including API design. No checkout, repository contents in prompts, agent integration, commits, pushes, GitHub App/OAuth, GitHub Enterprise, or per-user connections.

## Approach

### Shared connection and credential storage
- Add a small `app.github` package and thin async routes; use explicit `httpx` and `cryptography` dependencies, not a heavyweight GitHub SDK or new Temporal workflows.
- Store a singleton connection in `app.github_connection`: encrypted token, GitHub user ID/login, validation timestamp, and update timestamp. Initialize its schema idempotently through API lifespan using `app.db.connect`; do not use expiring document storage.
- Encrypt with Fernet using a stable `GITHUB_TOKEN_ENCRYPTION_KEY` supplied through backend environment configuration. Never generate a replacement key silently or store it in PostgreSQL. Missing/invalid keys disable token save/use operations, not unrelated workflows; unreadable saved credentials produce a safe recovery message. Disconnect remains possible without decrypting the stored token. Document backup/key-loss consequences and disconnect/reconnect recovery.
- Validate a submitted token via GitHub `/user` before atomically replacing the saved connection. Failed validation preserves the previous connection. Tokens are write-only: no token, ciphertext, or token fragments in responses, URL parameters, logs, tracing, Temporal state, or browser storage. Clear the password input after submission; redact secret-bearing validation errors, including FastAPI/Pydantic input echoes.
- Recommend a fine-grained PAT restricted to intended repositories with Contents read access (Metadata is implicit). Repository visibility and branch access are validated through actual GitHub calls; never infer permissions solely from token prefixes/scopes. Org approval/SSO may additionally be required.
- This is a trusted local/internal feature with no new authentication system. Keep it off the public internet; remote deployment requires access control and HTTPS before exposure. For connector mutation routes require JSON (send `{}` for Check/Disconnect) and enforce an exact browser-Origin allowlist via `GITHUB_ALLOWED_ORIGINS`, defaulting to localhost/127.0.0.1 development frontend/API origins. Do not compare Origin to the proxy-rewritten Host or trust arbitrary forwarded headers. Reject untrusted/`null` origins, permit Origin-less local CLI calls, and do not add wildcard CORS.

### Connector API
Backend prefix: `/connectors/github`; browser requests use `/api/connectors/github` through the existing proxy.

| Route | Behavior |
| --- | --- |
| `GET /connection` | Stored configuration status, non-secret account identity, validation time, and encryption readiness; no GitHub request on each page load. |
| `PUT /connection` | Validate and save/replace a submitted PAT; return sanitized connection metadata. |
| `POST /connection/check` | Revalidate stored credentials and update validation time; report revoked/expired credentials without silently deleting them. |
| `DELETE /connection` | Delete stored credentials/identity; preserve historical session selections. |
| `GET /repositories?page=1` | Page through repositories accessible to the token, including permitted private/org repositories; return minimal metadata and `next_page`. |
| `GET /repositories/{owner}/{repo}/branches?page=1` | Page through branches for an accessible repository; return names and `next_page`. |

- Use fixed `https://api.github.com`, Bearer authorization, GitHub JSON Accept/API-version headers, bounded async timeouts, and encoded path segments. No user-supplied API hosts, arbitrary URLs, or credential-forwarding redirects.
- Use `/user/repos`, canonical repository lookup, and branch lookup. Page size is bounded (e.g. 50); derive pagination without fetching credential-bearing arbitrary Link URLs. Do not load every repository/branch eagerly.
- Return safe actionable errors: missing connection, invalid token, insufficient permissions/SSO, inaccessible repository/branch, rate limiting with retry guidance, timeout, malformed upstream response, or storage failure. Never relay raw GitHub response bodies or credential-bearing exceptions.
- A shared `resolve_repository_selection` helper validates fresh standalone-session choices against GitHub and returns a canonical snapshot: `provider`, `repository_id`, `full_name`, `html_url`, `private`, and `branch`. Accept only repository identity plus branch as user input, not authoritative metadata. No commit pinning yet: the saved branch name is fixed, but its remote head may move.

### Session metadata and inheritance
- Add optional `repository` metadata defaulting to `None` to `RequirementsState`, `ApiDesignState`, and chat request records/memos. Keep shared metadata types dependency-light so Temporal imports do not initialize HTTP clients, storage, or encryption.
- Extend standalone Chat submission (JSON and multipart) and Requirements creation with optional repository input. Preserve empty-body Requirements creation and all existing no-repository clients. Validate selections before starting workflows; for uploads, reject bad selections before persisting uploaded originals.
- When initializing Requirements with selected metadata, include the existing `OPENING_MESSAGE`: its workflow constructor expects a nonempty transcript. Existing dataclass state already carries metadata across continue-as-new; avoid changing workflow control flow/signatures.
- Copy `source.repository` in Requirements forks, Requirements-to-API-design creation, and API-design forks. Generation retries retain the same state. Chat replies, both fork paths, and retries copy the parent's stored snapshot into each new request memo; use `None` for legacy memos.
- Reject a supplied repository override on parented Chat submissions; derived session APIs inherit server-side and offer no mutation endpoint. Inheritance does not revalidate GitHub or require a live connection, since it only copies metadata and does not read code.
- Preserve selections if credentials are replaced/disconnected or remote access disappears. Fresh standalone selections require validation; existing transcripts and derived sessions remain usable. Do not add a global active repository or session-selection database table.

### UI flow and states
- Add a **GitHub** query-string navigation mode, preserving the existing light neutral/green system, form language, and responsive page shell. This is an operational settings extension, not a visual redesign.
- Settings: concise PAT permission/setup guidance, labeled password input, **Connect GitHub**; connected account and validation timestamp; **Check connection**, token replacement, and confirmed **Disconnect**. Distinguish saved configuration from successful live validation. Show backend key-configuration guidance when storage is unavailable.
- Add one reusable `RepositoryPicker` to new Chat and Requirements session controls: **No repository** by default; paginated repository list labeled `owner/name` with private/default-branch indicators; then paginated branch selection defaulting to the repository's actual default branch. Reset branch state when the repository changes; never silently fall back when a branch is unavailable. Offer Refresh and Load more, not a misleading search over one page.
- Show the selected `owner/name · branch` read-only on active Chat, Requirements, and API-design sessions, including forks and retrieved sessions. New standalone sessions reset the draft choice rather than inheriting a global default. Keep the selected metadata visible during optimistic submission/retry states.
- Handle initial loading, disconnected, no repositories, empty repository/no branches, credential/permission failure, rate limiting, backend offline, validation failure, saving, and success. Repository failures must not block starting without a repository. Disable creation only when a chosen repository lacks a valid branch or is being submitted.
- Use native labeled controls, keyboard-visible focus, polite status/alert feedback, busy/disabled states, request cancellation, and stale-response guards for rapid repository changes. Match existing desktop/mobile breakpoints; no new UI library or frontend test runner.

## Files to modify
- `frontend/src/main.tsx`: add GitHub navigation mode.
- `frontend/src/GitHubSettings.tsx` (new) and `frontend/src/styles.css`: configuration UI, preserving the existing neutral/green visual system.
- Reuse `frontend/src/api.ts` unchanged unless a genuinely shared helper adjustment is needed.
- `backend/app/api/main.py`, `backend/app/api/errors.py`, `backend/app/config.py`: router/schema integration, safe connector error mapping for existing session routes, and encryption/origin configuration.
- `backend/.env.example`, `backend/requirements.txt`, `frontend/README.md`: secret-storage setup, any explicit HTTP/encryption dependencies, and usage.
- `backend/app/github/{__init__,models,client,service,storage,errors}.py` (new): minimal package, types, GitHub HTTP operations, selection resolution, encrypted storage/schema initialization, and safe errors.
- `backend/app/api/routes/github.py` (new): thin connector routes.
- Session integration: `backend/app/requirements/{models,service}.py`, `backend/app/api_design/{models,service}.py`, `backend/app/conversations/{models,service}.py`; `backend/app/api/routes/{requirements,llm_requests}.py`; `frontend/src/{App,Requirements,ApiDesign}.tsx`.
- Reusable frontend picker/types/API: `frontend/src/RepositoryPicker.tsx` and `frontend/src/github.ts` (new).
- `backend/tests/test_github_connector.py` and `backend/tests/test_github_storage.py` (new): connector/API contracts and actual PostgreSQL lifecycle coverage.
- `backend/tests/test_{requirements_workflow,api_design_workflow,conversation_fork}.py`: extend existing session lifecycle coverage for metadata preservation and legacy compatibility.

## Reuse
- `frontend/src/api.ts`: `readBody`, `errorMessage`, `failure`.
- `backend/app/config.py`: existing environment-driven configuration.
- `frontend/src/main.tsx`: `modes`, `modeFromUrl`, `Shell.switchMode`.
- `frontend/src/Workflows.tsx`: cancellable fetches, response validation, paging, and retry/loading patterns.
- `frontend/src/styles.css`: existing CSS tokens, page shell, form/button/error styles, and responsive breakpoints.
- `backend/app/api/main.py`: lifespan schema initialization and `include_router` registration.
- `backend/app/api/routes/api_design.py`: Pydantic request validation and thin async route pattern.
- `backend/app/db.py`: async `connect()` with dictionary rows; parameterized SQL and transaction contexts from `backend/app/documents/generated.py`.
- `backend/app/documents/schema.py`: idempotent schema initialization at API startup; connector data needs its own persistent table, not expiring documents.
- `frontend/vite.config.ts`: `/api` proxy strips that prefix; backend connector routes must use `/connectors/github`.
- `backend/tests/test_workflow_listing.py`: isolated FastAPI router tests with `TestClient`; `backend/tests/test_api_design_workflow.py` also uses `httpx.ASGITransport`/`AsyncClient`. `backend/pytest.ini` supplies async pytest conventions.
- `backend/tests/test_requirements_workflow.py` and `backend/tests/test_api_design_workflow.py`: existing time-skipping Temporal environments, fake activities, fork/retry/continue-as-new coverage; extend these rather than creating duplicate harnesses.
- No frontend test runner is currently configured; use build/type checking and focused manual UI verification rather than introducing a framework for this milestone.
- `backend/app/requirements/service.py`: `start_session`, `fork_session`, and `get_session`.
- `backend/app/api_design/service.py`: `start_session` derives from requirements; `_start`, `fork_session`, and `retry_generation` retain source state.
- `backend/app/conversations/service.py`: `start_llm_request`, `_fork_session`, `retry_llm_request`, and workflow memos already model chat ancestry/session identity.
- `backend/app/temporal/sessions.py`: reuse existing fork/update helpers; do not rewrite ancestry logic.

## Steps
- [x] Resolve authentication, storage/access assumptions, session ownership, inheritance, and milestone boundaries.
- [x] Inspect navigation, backend routes, persistence, and session/test conventions.
- [x] Implement dependency-light selection types, encrypted singleton storage/schema initialization, environment readiness, and safe error handling.
- [x] Implement bounded GitHub HTTP operations and connector routes, including credential validation, pagination, and canonical selection resolution.
- [x] Add optional session repository metadata and backward-compatible creation inputs; propagate it through every continuation/fork/retry/Requirements-to-API-design path.
- [x] Build GitHub settings and the shared new-session picker; add read-only repository summaries and retain metadata through frontend polling/optimistic states.
- [x] Extend focused tests at observable boundaries; document key setup, PAT permissions, local-only security assumptions, session behavior, and explicit non-goals.
- [x] Run focused and existing tests, frontend build, and a bounded desktop/mobile manual verification pass.

## Verification
**Automated contracts** (no live GitHub credentials or real model calls):
- Connector routes against controlled HTTP transport responses: successful connect/check/list/disconnect, repo/branch pagination, missing connection, invalid PAT, insufficient permission/SSO, inaccessible branch/repository, rate limiting, timeout, and malformed GitHub data. Verify failed replacement preserves the old connection and hostile path/URL inputs cannot redirect authenticated requests.
- Secret sentinel checks for both successful and invalid submissions: absent from responses, captured logs, stored session payloads, and validation errors. Check cross-origin/non-JSON connector mutations are rejected while legitimate Vite-origin requests work.
- Storage integration uses an explicit disposable `TEST_DATABASE_URL`, never the developer's default app database: idempotent schema creation, encrypted-at-rest bytes, reopening with the same key after restart, atomic replacement, deletion, and wrong-key/missing-key recovery behavior. Keep real persistence assertions here, not against an in-memory fake.
- Extend existing Temporal test fixtures at the owning service/API boundary for standalone sessions with/without a repository, old state/memos, replies, Chat user/assistant forks, nested Requirements/API-design forks, Requirements-to-API-design inheritance, retry/generation restart, and continue-as-new serialization. Assert returned metadata and rejected overrides rather than private call shapes; do not duplicate the same inheritance case at every layer or add test-only production flags/exports.

**Commands during implementation:**
- From `backend/`: `env/bin/python -m pytest tests/test_github_connector.py tests/test_github_storage.py tests/test_requirements_workflow.py tests/test_api_design_workflow.py tests/test_conversation_fork.py` (storage tests require disposable `TEST_DATABASE_URL`).
- Run remaining backend suite with existing environment prerequisites; report any infrastructure-dependent tests not runnable rather than claiming success.
- From `frontend/`: `npm run build` for TypeScript and production build.
- `git diff --check` after implementation.

**Manual end-to-end:**
- Connect a least-privilege PAT against disposable public/private repositories, check it, replace with an invalid token, restart the API, and verify the saved connection survives without exposing credentials.
- Create separate sessions pointing to different repositories/branches; reload/retrieve each, fork both Chat paths and Requirements, create API design from Requirements, and verify immutable independent inherited selections.
- Cover branch names containing `/`, repository changes during loading, multi-page lists, empty repositories, no-repository sessions, revoked credentials, and Disconnect. Historic sessions still render/fork; fresh repository choices fail safely when disconnected.
- Inspect desktop and mobile together for keyboard access, labels/focus, loading/error feedback, overflow, and stale choices. One batched fix pass and at most one confirmation pass. Check browser Network/storage and backend logs for token leakage (the token appears only in its intentional HTTPS/local connection-submission body, never in returned data or persistent browser storage).

## Execution results
- Backend suite: `LLM_HARNESS= env/bin/python -m pytest -q --tb=short` → **226 passed, 7 skipped**. The initial full-suite invocation inherited the developer's Pi harness and failed in unrelated mocked-model tests; overriding the harness for that command resolved all failures without changing local configuration. Three skips are the new real PostgreSQL tests (no disposable `TEST_DATABASE_URL`); four are existing opt-in infrastructure tests.
- Frontend: `npm run build` passed; `git diff --check` passed.
- Bounded headless browser pass against controlled API responses at 1440px/390px: settings, token clearing/browser-storage absence, invalid replacement recovery, repository/branch controls, cancellation of stale branch results, creation/read-only summaries, independent Chat choice, new-session reset, Disconnect, and missing-key guidance passed. No runtime exceptions or horizontal overflow. Screenshots reviewed at `/tmp/github-ui-review/{settings,picker,session}-{desktop,mobile}.png`.
- No live GitHub credentials were used. Docker is not running; PostgreSQL encryption/persistence/restart tests and a real GitHub end-to-end connection remain unverified. Run the documented disposable-database tests before deployment.
- Mechanical UI detector ran once. Its two warnings are pre-existing thick borders at legacy stylesheet lines 601/691, outside this extension; they were not repaired or canonized.
- Setup/API/security documentation: `docs/github-connector.md`, linked from `frontend/README.md`; stable encryption-key and exact-origin settings added to `backend/.env.example`.

### UI finish review — inline fallback
`disposition: ship` (scoped GitHub extension; live integration limits above still apply).

**persistence:** Approved scope/setup are recorded in this plan and connector documentation. No PRODUCT.md/DESIGN.md existed; this ordinary extension preserves the incumbent CSS rather than inventing a new identity.

**fidelity:** TYPE matches the existing system stack and hierarchy; MATERIAL stays flat/native with no decorative assets; GROUND uses existing neutral/green tokens. Settings, picker, immutable summary, and mobile reflow match the approved operational flow.

**ceiling:** Reached for the scoped operational surface; repository configuration and state remain clear without visual replacement.

**material_fixes:** None in the reviewed extension. Legacy detector warnings remain outside scope.

**keep:** Preserve the existing neutral/green interface, credential-free session summaries, and default no-repository path.

### Design documentation check
No design-system changes: checked `frontend/src/styles.css`, the new settings/picker components, and session integrations against the incumbent interface.
- Palette: existing ink, neutral paper, green actions, and red errors.
- Type: existing system stack, heading hierarchy, and compact form labels.
- Layout: existing page shell and responsive session workspace; navigation now wraps.
- Forms: native controls, existing small-radius/border language, and visible select focus.
- States: existing button/loading patterns plus scoped connection and repository feedback.

Pre-existing thick accent borders were neither canonized nor repaired because they are unrelated to this feature.
