# Requirements specification

This repository is the pipeline itself: a web application that turns a task described in conversation into requirements, an API design and verified, published code in a GitHub repository. This contract describes its HTTP API, served by the backend in `backend/`; the contract tests in `contract-tests/` exercise it over HTTP.

Endpoints that need Temporal, an LLM provider or GitHub answer 503 or a GitHub error when those are not reachable, as they are not in the self-contained service image the contract tests run against.

Errors carry a `detail` string, except FastAPI's own request validation, which answers 422 with a list of the problems found in `detail`.

## Functional requirements

| ID | Requirement | Description | Priority | Rationale |
| --- | --- | --- | --- | --- |
| FR-1 | Register the first account | While no account exists, anyone can register one with a login and a password and is signed in as its administrator. Once any account exists, registration is closed and answers 403. GET /auth/registration tells whether it is still open. | Must | The first user sets up the instance without an operator. |
| FR-2 | Sign in | A user signs in with login and password and receives a bearer token, which is also set as an HttpOnly, SameSite=Strict session cookie. An unknown login or a wrong password answers 401 without saying which. Logins are compared case-insensitively and surrounding whitespace is ignored. | Must |  |
| FR-3 | Require a signed-in user | Every endpoint other than sign-in, registration, its status and sign-out requires a valid, unexpired token, sent as a bearer token or the session cookie, and answers 401 with WWW-Authenticate: Bearer otherwise. | Must |  |
| FR-4 | Current user | GET /auth/me returns the signed-in user's login, when the session expires and whether the user is the administrator. | Must |  |
| FR-5 | Sign out | POST /auth/logout clears the session cookie and answers 204, signed in or not. | Must |  |
| FR-6 | Administer accounts | Only the first user, the administrator, lists accounts (login and creation time) and creates new ones; any other user receives 403. Creating a login that already exists answers 409. | Must |  |
| FR-7 | Validate account details | A login is an email address or a username of letters, digits and . _ % + -, 3 to 254 characters. A password has 8 to 1024 characters. Anything else answers 422 when an account is created. | Must |  |
| FR-8 | Connect GitHub | All users share one GitHub App connection. It is created from an app ID, its PEM private key and optionally the account it is installed on, or from the code GitHub returns after creating an app from this instance's manifest. It can be checked against GitHub and removed. Its status shows whether it is configured, whether the key can be encrypted, the app, the installation and when it was last validated. | Must |  |
| FR-9 | Browse repositories | Signed-in users list the repositories the GitHub App installation can access and the branches of one of them, a page at a time with the number of the next page. | Must |  |
| FR-10 | Specify a task in a requirements session | A requirements session starts in a chosen repository and branch from the intake: the kind of task (new service, API change, bugfix or documentation), the answers to that kind's questions and what needs to be done. An LLM interviewer then records requirements while the user sends messages or answers its questions. A failed turn can be retried, and a session can be forked after any message. | Must |  |
| FR-11 | Requirements intake | GET /requirements/intake returns the kind question, the kinds of task, the questions asked for each kind and the task question, for the page to show before a session starts. | Must |  |
| FR-12 | Finalize requirements | Finalizing a session writes its requirements document, available for download once ready. A task on an existing service (API change, bugfix or documentation) is specified against the contract the repository holds in contracts/; a repository without a readable contract cannot be finalized for one. | Must |  |
| FR-13 | Design the API | A new service or an API change continues in an API design session started from its completed requirements session. Its conversation records the endpoints, schemas and implementation language, and finalizing it renders the OpenAPI document. | Must |  |
| FR-14 | Deliver the task | A finalized task is delivered: code is generated or changed, the service and its contract tests are written by separate coding agents, and verification runs the tests against the built service, repairing the failing side a bounded number of times. A bugfix's new tests must first fail against the service as the repository holds it. The session shows the delivery's progress, its agents' questions can be answered, and a failed stage can be retried. | Must |  |
| FR-15 | Publish verified work | Only a delivery that passed verification is pushed, with the repository's updated contract and contract tests, to a new branch of the session's repository. A documentation change, which has no check yet, is pushed and marked as unverified. | Must |  |
| FR-16 | Chat with the LLM | A user sends a free-form request, optionally with one attached file and a repository to work in, follows it up, retries a failed answer or forks the conversation after one of their messages. Attachments and generated documents can be downloaded. | Must |  |
| FR-17 | List workflow executions | Signed-in users list the workflow executions of the namespace, newest first, 50 per page, optionally of one workflow type, continuing with the returned page token. | Should |  |
| FR-18 | Start the sample workflow | POST /start runs the sample greeting workflow and returns its result. | Could |  |

## Non-functional requirements

| ID | Requirement | Description | Priority | Rationale |
| --- | --- | --- | --- | --- |
| NFR-1 | Limit sign-in attempts | Sign-in and registration attempts are limited per client address per minute (AUTH_LOGIN_ATTEMPTS_PER_MINUTE, default 10); further attempts answer 429 with Retry-After. | Must |  |
| NFR-2 | Never echo secrets | Passwords, private keys and tokens are never part of a response or a validation error. Passwords are stored as salted scrypt hashes and the GitHub App key is stored encrypted. | Must |  |
| NFR-3 | Expiring sessions | Session tokens are HS256 JWTs signed with AUTH_JWT_SECRET (at least 32 characters) that expire after AUTH_TOKEN_TTL_HOURS, 12 by default. Without a usable secret, sign-in and authenticated endpoints answer 503. | Must |  |
| NFR-4 | Report unavailable dependencies | When Temporal answers a request with an error or the request storage cannot be reached, an endpoint answers 503 with a detail saying which. | Must |  |
| NFR-5 | Bound request sizes | Messages and tasks have at most 10000 characters, an attachment at most 20 MiB and GitHub configuration input at most 16 KiB; larger input answers 413 or 422. | Must |  |
| NFR-6 | Validation errors | Malformed input answers 422 with a detail; an unknown session or request ID answers 404; an action the session's state does not allow answers 409. | Must |  |

## Technical requirements

| ID | Requirement | Description | Priority | Rationale |
| --- | --- | --- | --- | --- |
| TR-1 | Stack | The backend is a Python 3.12 FastAPI application with Temporal workflows and PostgreSQL storage, in backend/. The web client is a React application in frontend/. | Must |  |
| TR-2 | Isolated coding agents | Coding agents run in Docker containers built by the workers, without access to Temporal, the database or Docker. | Must |  |
| TR-3 | Verifiable service image | The Dockerfile at the repository root builds the API as a self-contained service with its own PostgreSQL database, served on port 8080 by a non-root user, so that contract tests can run against it alone. | Must |  |
