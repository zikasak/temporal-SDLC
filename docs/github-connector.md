# GitHub connector

## Scope

One shared GitHub personal access token belongs to the app instance. Repository and branch selections belong to individual sessions, not global settings:

- A Requirements session **must** select a repository/branch. Standalone Chat may select one or start without; Chat only records the selection.
- Chat replies, retries, and both fork paths inherit the source selection.
- Requirements forks and API-design sessions created from Requirements inherit it; API-design forks and generation retries retain it too. An API-design session cannot start from a Requirements session without a repository.
- A selection cannot change after creation. Start a new standalone session to choose a different repository.
- Sessions created before repositories were mandatory keep running as they were, without a checkout or push.

GitHub Apps, OAuth, Enterprise hosts, per-user credentials and pull requests are not supported yet.

## Checkout and push

Every Requirements and API-design session works in its own checkout of the selected branch:

1. **Checkout before the model.** A new session (forks included) is `preparing` until the main worker has cloned the branch into `<GENERATED_CODE_DIR>/<session-id>/repository`. No message is accepted and no model turn runs before that. The session records the checked-out commit; a failed checkout fails the session.
2. **File access stays in the checkout.** The interviewing and designing models get read-only `list_files`, `search_files` and `read_file` tools rooted in the checkout. The implementation agent's project is a copy of the checkout with the generated interfaces added; the test agent's project is the repository's `contract-tests/` directory. See [service implementation](service-implementation.md#working-in-the-repository).
3. **Push after verification.** Once the contract tests pass, the verified service and its tests are committed on top of the checked-out commit and pushed to the new branch `generated/<first 12 characters of the session ID>`. The selected branch itself is never written to and nothing is force-pushed: an existing remote branch of that name with other history is an error. The repository's `.gitignore` is respected, so build output stays out of the branch; `publication.ignored` lists the paths left out, so a rule that happens to match a real source file (a package named `build`, for instance) does not go unnoticed. Nothing is pushed when verification does not pass. A failed push can be repeated with **Retry push** (`{"stage": "repository"}` on the generation retry route) without generating or verifying again.

The clone is shallow (`--depth 1`) and does not fetch submodules. The token is given to Git through its environment only: it is not in the command line, the checkout's `.git/config`, logs or error messages. Host Git configuration, credential helpers and hooks are disabled, and links in the repository are checked out as plain files.

## Local setup

1. Ensure PostgreSQL and the API's existing dependencies are available. Install the updated `backend/requirements.txt` in the backend virtual environment if necessary (`httpx` and `cryptography` are explicit dependencies).
2. Generate an encryption key **once**, from `backend/`:

   ```sh
   env/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

3. Put the result in `backend/.env` as `GITHUB_TOKEN_ENCRYPTION_KEY=...`. Never commit this file. Keep the same key across API restarts and back it up separately from the database. Restart the API after environment changes.
4. Create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new), limited to the required repositories with **Contents: read and write** permission (sessions push their result to a new branch; a read-only token fails at that last step). Metadata access is implicit. Organization policy may require approval or SSO authorization; a valid account token does not guarantee access to every repository.
5. Open the frontend's **GitHub** tab and submit the token. Choose a repository/branch in **Requirements** (required) or **Chat** (optional) before starting a new session. Branch lists are paginated; Refresh and Load more are available. Empty repositories cannot be selected until they have a branch.

The API creates `app.github_connection` at startup, storing only the encrypted token and account/validation metadata. No connector worker or additional Temporal task queue is needed: the main worker (`python -m app.temporal.worker`) clones and pushes. It therefore needs `git` on its `PATH`, the same `GITHUB_TOKEN_ENCRYPTION_KEY` and database as the API, and the `GENERATED_CODE_DIR` shared with the coding workers. The UI clears the token input on submission and never receives the saved token back.

## Access and credential safety

The API requires sign-in (see the frontend README), but the connection is shared: any signed-in user can use or replace it. Remote deployment requires HTTPS. Encryption at rest does not protect against a signed-in caller or a host compromise.

Connector mutation routes require JSON and check browser `Origin` against `GITHUB_ALLOWED_ORIGINS`. Defaults are:

```text
http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000
```

Configure the exact frontend origins if Vite uses another port or the app is served elsewhere. No wildcard CORS is added. Untrusted and `null` origins are denied; Origin-less CLI calls remain allowed. This is a browser cross-origin safeguard, **not authentication**. Origin is not compared against the proxy-rewritten Host.

GitHub REST calls stay on the backend, use only `https://api.github.com` (Git itself talks to `https://github.com`), and do not forward credentials through redirects or arbitrary pagination URLs. Tokens/ciphertext must not be put in query parameters, logs, tracing, Temporal payloads, or browser storage. Avoid enabling request-body logging in reverse proxies or debugging middleware: the connection submission necessarily contains the new token.

A failed replacement leaves the previous token intact. **Check connection** revalidates the saved token; the timestamp in the UI is not a continuous availability check. Disconnect deletes saved credentials but preserves historical session metadata. Existing session conversations/forks do not require GitHub access; new repository choices do.

If the encryption key is missing or invalid, token save/use operations fail safely, while other sessions still work. If the key changes, restore the original key or disconnect/reconnect with a new token. Disconnect does not require decryption. Back up the database and key separately; database-only backups cannot recover credentials without the key. Future access to historical selections must be revalidated before reading or modifying repository code.

## API

Backend prefix is `/connectors/github`; the frontend uses `/api/connectors/github` through Vite's existing proxy.

| Method/path | Purpose |
| --- | --- |
| `GET /connection` | Saved status, account identity, encryption readiness, and validation time. |
| `PUT /connection` | Validate and atomically save/replace `{"token":"..."}`. The token is write-only. |
| `POST /connection/check` | Revalidate saved credentials; send `{}`. |
| `DELETE /connection` | Delete saved credentials; send `{}`; returns 204. |
| `GET /repositories?page=1` | Accessible public/private/org repositories, 50 per page, with `next_page`. |
| `GET /repositories/{owner}/{repo}/branches?page=1` | Branch names and `next_page`. |

Errors distinguish missing connection (409), invalid token (401), denied access/SSO (403), unavailable repository/branch (404), invalid input (422), rate limiting (429 with `Retry-After`), bad upstream responses (502), and unavailable storage/key/network (503). No raw GitHub response bodies are returned.

Standalone Chat JSON may include:

```json
{"message":"Describe the system", "repository":{"full_name":"owner/repository", "branch":"feature/example"}}
```

Multipart Chat uses a `repository` field containing the same selection object as a JSON string, alongside the existing message/file fields. Repository access is validated before storing an uploaded original. Parented Chat submissions must omit `repository` (even `null` is rejected).

Requirements creation requires a JSON body naming the repository; a missing one is rejected with 422:

```json
{"repository":{"full_name":"owner/repository", "branch":"main"}}
```

Starting API design from a Requirements session without a repository is rejected with 409. Requirements and API-design retrieval also returns `checkout` (`{"commit": ...}` once checked out); API design adds `publication_status`, `publication` (`branch`, `commit`, `url`, `ignored`, `ignored_count`) and `publication_error`. Session retrieval returns `repository: null` or a canonical, non-secret snapshot containing `provider`, `repository_id`, `full_name`, `html_url`, `private`, and `branch`. Credentials stay in PostgreSQL; Requirements/API-design carry snapshots in Temporal state, and Chat carries them in request memos.

## Verification

Run focused connector and existing session tests from `backend/`:

```sh
env/bin/python -m pytest tests/test_github_connector.py tests/test_github_storage.py tests/test_repository_checkout.py tests/test_requirements_workflow.py tests/test_api_design_workflow.py tests/test_conversation_fork.py
```

GitHub HTTP is mocked; no real PAT or model calls are made. Checkout and push tests run the real `git` against a repository on disk. Real storage tests require an explicit `TEST_DATABASE_URL` pointing to a **disposable** PostgreSQL database whose name starts with `github_test_`. They initialize the connector schema and clear the connector table between tests. They never default to `APP_DATABASE_URL`. Without the variable, storage tests skip; do not mistake that for persistence verification.

Run `npm run build` from `frontend/`. Manually inspect settings/picker/summary flows on desktop/mobile, including invalid tokens, missing keys, revoked access, multi-page lists, branches containing `/`, empty repositories, stale requests during repository changes, and Disconnect. Verify independent sessions retain their own immutable selections after retrieval, branching, derivation, and restart.
