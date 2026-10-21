# GitHub connector

## Scope

One shared GitHub App installation belongs to the app instance: the backend holds the app's ID and private key and acts on the repositories the app is installed on. Repository and branch selections belong to individual sessions, not global settings:

- A Requirements session **must** select a repository/branch. Standalone Chat may select one or start without; with one, the model answers from a read-only checkout of it (`list_files`, `search_files`, `read_file`).
- Chat replies, retries, and both fork paths inherit the source selection.
- Requirements forks and API-design sessions created from Requirements inherit it; API-design forks and generation retries retain it too. An API-design session cannot start from a Requirements session without a repository.
- A selection cannot change after creation. Start a new standalone session to choose a different repository.
- Sessions created before repositories were mandatory keep running as they were, without a checkout or push.

Personal access tokens, OAuth (acting as a user), Enterprise hosts, several installations at once, per-user credentials and pull requests are not supported.

## Checkout and push

Every Requirements and API-design session works in its own checkout of the selected branch:

1. **Checkout before the model.** A new session (forks included) is `preparing` until the main worker has cloned the branch into `<GENERATED_CODE_DIR>/<session-id>/repository`. No message is accepted and no model turn runs before that. The session records the checked-out commit; a failed checkout fails the session.
2. **File access stays in the checkout.** The interviewing and designing models get read-only `list_files`, `search_files` and `read_file` tools rooted in the checkout. The implementation agent's project is a copy of the checkout with the generated interfaces added; the test agent's project is the repository's `contract-tests/` directory. See [service implementation](service-implementation.md#working-in-the-repository).
3. **Push after verification.** Once the contract tests pass, the verified service, its tests and the contract they were built from (`contracts/`) are committed on top of the checked-out commit and pushed to the new branch `generated/<first 12 characters of the session ID>`. The selected branch itself is never written to and nothing is force-pushed: an existing remote branch of that name with other history is an error. The repository's `.gitignore` is respected, so build output stays out of the branch; `publication.ignored` lists the paths left out, so a rule that happens to match a real source file (a package named `build`, for instance) does not go unnoticed. Nothing is pushed when verification does not pass. A failed push can be repeated with **Retry push** (`{"stage": "repository"}` on the generation retry route) without generating or verifying again.

The clone is shallow (`--depth 1`) and does not fetch submodules. Each clone and push mints its own installation token, which GitHub expires after an hour; none is stored. The token is given to Git through its environment only: it is not in the command line, the checkout's `.git/config`, logs or error messages. Host Git configuration, credential helpers and hooks are disabled, and links in the repository are checked out as plain files.

## Local setup

1. Ensure PostgreSQL and the API's existing dependencies are available. Install the updated `backend/requirements.txt` in the backend virtual environment if necessary (`httpx` and `cryptography` are explicit dependencies).
2. Generate an encryption key **once**, from `backend/`:

   ```sh
   env/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

3. Put the result in `backend/.env` as `GITHUB_TOKEN_ENCRYPTION_KEY=...` (it encrypts the app's private key). Never commit this file. Keep the same key across API restarts and back it up separately from the database. Restart the API after environment changes.
4. Open the frontend's **GitHub** tab and choose **Create GitHub App**, optionally naming the organization that should own it. GitHub shows the app this instance asks for (private, **Contents: read and write**, **Pull requests: read and write**, and an active webhook pointing at `/api/connectors/github/events` subscribed to **Issue comment** and **Pull request review comment** events) for confirmation and sends the browser back to `/github-app` with a one-time code, which the backend exchanges for the app's ID, private key and webhook secret (stored encrypted). For an app the connection records as created from the manifest but without a webhook secret, **Check connection** turns its webhook on with a newly generated secret, generated, set at GitHub and stored under the connection's lock so that concurrent checks always store the secret GitHub uses. The webhook URL it sets is `GITHUB_WEBHOOK_URL` when configured, else the backend's address as the check reached it under the prefix the proxy reports in `X-Forwarded-Prefix` (the frontend's nginx and Vite proxies send `/api`). A connection saved before the backend recorded how its app was created (before mentions were supported) has its origin resolved by its first check: an app whose webhook has no address, or already points at a `/connectors/github/events` endpoint, and whose homepage is on this instance's host (as the manifest set it) counts as created from the manifest, gets its webhook turned on the same way and is recorded as such; any other app counts as manually connected and keeps its webhook settings. Then follow **Install the app on GitHub** and pick the repositories; GitHub returns to the same page and the connection is complete. If it does not (an organization owner has to approve the installation first, or the tab was closed), use **Check connection** afterwards. The app's addresses are those of the page it was created from, so create it from the address the instance is normally used at, in a signed-in tab.

To use an app that already exists instead, expand **Use an existing GitHub App**:

1. Create a [GitHub App](https://github.com/settings/apps/new) (for an organization: its settings, **Developer settings → GitHub Apps**). Give it the repository permissions **Contents: read and write** (sessions push their result to a new branch; a read-only app fails at that last step) and **Pull requests: read and write** (each pushed delivery opens or updates a pull request to the default branch; without it the push still succeeds and the delivery progress reports the missing permission); Metadata read access is added automatically. To have the app act on mentions in its pull requests, keep **Webhook → Active**, set the webhook URL to this instance's `/api/connectors/github/events`, choose a webhook secret and subscribe to **Issue comment** and **Pull request review comment** events; enter the same secret when connecting. Without a secret every webhook delivery is rejected and mentions are not handled. No callback URL or user authorization is used. On the app's page note the **App ID** and **Generate a private key**, which downloads a `.pem` file.
2. **Install** the app on the account or organization that owns the repositories, limited to the repositories it needs. An organization may require an owner's approval. Repositories added to or removed from the installation later are picked up without reconnecting.
3. Submit the App ID and the `.pem` file. If the app is installed on several accounts, also name the one to use; the connection works with a single installation. Choose a repository/branch in **Requirements** (required) or **Chat** (optional) before starting a new session. Branch lists are paginated; Refresh and Load more are available. Empty repositories cannot be selected until they have a branch.

The API creates `app.github_app_connection` at startup, storing only the encrypted private key and app/installation/validation metadata, and drops `app.github_connection`, the table of the personal access token this connector used before: reconnect with a GitHub App after upgrading. No connector worker or additional Temporal task queue is needed: the main worker (`python -m app.temporal.worker`) clones and pushes. It therefore needs `git` on its `PATH`, the same `GITHUB_TOKEN_ENCRYPTION_KEY` and database as the API, and the `GENERATED_CODE_DIR` shared with the coding workers. The UI clears the chosen key file on submission and never receives the saved key back.

## Access and credential safety

The API requires sign-in (see the frontend README), but the connection is shared: any signed-in user can use or replace it. Remote deployment requires HTTPS. Encryption at rest does not protect against a signed-in caller or a host compromise.

Connector mutation routes require JSON and check browser `Origin` against `GITHUB_ALLOWED_ORIGINS`. Defaults are:

```text
http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000
```

Configure the exact frontend origins if Vite uses another port or the app is served elsewhere. No wildcard CORS is added. Untrusted and `null` origins are denied; Origin-less CLI calls remain allowed. This is a browser cross-origin safeguard, **not authentication**. Origin is not compared against the proxy-rewritten Host.

GitHub REST calls stay on the backend, use only `https://api.github.com` (Git itself talks to `https://github.com`), and do not forward credentials through redirects or arbitrary pagination URLs. The private key signs a short-lived JWT that is sent only to mint installation tokens. Keys, tokens and ciphertext must not be put in query parameters, logs, tracing, Temporal payloads, or browser storage. Avoid enabling request-body logging in reverse proxies or debugging middleware: the connection submission necessarily contains the private key.

Commits are authored by the app's bot user (`<app-slug>[bot]`). A failed replacement leaves the previous connection intact. **Check connection** verifies that the key is still accepted and the app still installed; the timestamp in the UI is not a continuous availability check. Disconnect deletes saved credentials but preserves historical session metadata; the app itself stays on GitHub until it is deleted there, as does one replaced by creating another. Existing session conversations keep reading the checkout they already have without GitHub access; new repository choices need it, and so does a Chat fork, which clones the branch again for its own session. A Chat message whose repository cannot be checked out is still answered, with the model told that it cannot read the files.

If the encryption key is missing or invalid, saving or using the private key fails safely, while other sessions still work. If the encryption key changes, restore the original or disconnect/reconnect with the app's private key (generate a new one on GitHub if the file is gone). Disconnect does not require decryption. Back up the database and key separately; database-only backups cannot recover credentials without the key. Future access to historical selections must be revalidated before reading or modifying repository code.

## API

Backend prefix is `/connectors/github`; the frontend uses `/api/connectors/github` through Vite's existing proxy.

| Method/path | Purpose |
| --- | --- |
| `GET /connection` | Saved status, `app` (`id`, `slug`), `installation` (`id`, `account`), encryption readiness, and validation time. |
| `PUT /connection` | Validate and atomically save/replace `{"app_id":123,"private_key":"-----BEGIN RSA PRIVATE KEY-----…","account":"owner"}`; `account` is needed only when the app is installed on several. The key is write-only. |
| `POST /connection/manifest` | Save the app GitHub created from the manifest: `{"code":"..."}`, the one-time code of its redirect. The connection has no `installation` until the app is installed and checked. |
| `POST /connection/check` | Revalidate saved credentials and pick up a new installation; send `{}`. |
| `DELETE /connection` | Delete saved credentials; send `{}`; returns 204. |
| `GET /repositories?page=1` | Repositories the installation can access, 50 per page, with `next_page`. |
| `GET /repositories/{owner}/{repo}/branches?page=1` | Branch names and `next_page`. |

Errors distinguish a missing connection, a removed installation or an ambiguous choice of installation (409), rejected app credentials (401), denied access (403), an unavailable repository/branch or account (404), invalid input or key (422), rate limiting (429 with `Retry-After`), bad upstream responses (502), and unavailable storage/key/network (503). No raw GitHub response bodies are returned.

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

GitHub HTTP is mocked; no real GitHub App or model calls are made. Checkout and push tests run the real `git` against a repository on disk. Real storage tests require an explicit `TEST_DATABASE_URL` pointing to a **disposable** PostgreSQL database whose name starts with `github_test_`. They initialize the connector schema and clear the connector table between tests. They never default to `APP_DATABASE_URL`. Without the variable, storage tests skip; do not mistake that for persistence verification.

Run `npm run build` from `frontend/`. Manually inspect settings/picker/summary flows on desktop/mobile, including a wrong App ID or key, a missing encryption key, an uninstalled app, repositories removed from the installation, multi-page lists, branches containing `/`, empty repositories, stale requests during repository changes, and Disconnect. Verify independent sessions retain their own immutable selections after retrieval, branching, derivation, and restart.

## Mentions in pull requests

GitHub delivers pull request comments to `POST /connectors/github/events`, which needs no
sign-in: a delivery is accepted only when `X-Hub-Signature-256` is the HMAC-SHA256 of its
raw body with the connection's webhook secret. A newly created comment that mentions the
app (`@<slug>` or `@<slug>[bot]`) on an open pull request the app opened for a delivery, by
a user with write access, is handed to that delivery (202); every other signed delivery is
ignored (204) and only logged. A delivery ID or comment already handled answers 409.

The delivery's LLM classifies each mention as a fix request, a clarification or unclear.
A clarification is answered in the pull request from the delivery's session and the code as
last pushed; an unclear mention gets a reply asking which is meant. A fix request starts a
round of fixes with the comment as its instructions, then verification and code review, and
pushes to the same branch; requests arriving while a round runs are queued in order. The
app replies when the round starts and again with its outcome; a mention whose reply could
not be posted ends with status `error`, saying which reply failed, even when its fix was
pushed. The LLM reads the whole comment, up to GitHub's 65536 characters. Every mention,
its classification and what it led to are listed in the delivery progress as `mentions`.
They are kept, with their full text, in the `app.delivery_mentions` table: the delivery
workflow carries only the mentions it still works on, without their text, and review
rounds a comment started refer to it, so that its state stays bounded however many
comments its pull request receives.

Connecting or checking the connection sets `webhook_events_missing` when the app is not
subscribed to both comment events or the installation lacks `pull_requests: write`; GitHub's
API cannot change these, so the app's owner adds them in the app's settings.
