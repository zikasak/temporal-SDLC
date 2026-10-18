# Langfuse tracing

The backend and isolated coding agents use **Langfuse Python SDK 4.16.0** and its
native LangChain/LangGraph callback integration. No frontend receives Langfuse keys.

## Configure

From `backend`, install `requirements.txt`. Copy `.env.example` to `.env` if one does
not already exist, and set project-scoped `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, and `LANGFUSE_BASE_URL`. Local settings load before provider
or tracing initialization; deployment environment variables take precedence.
`.env` is ignored by Git and excluded from Docker builds.

Restart workers after changing settings. Missing credentials/base URL or
`LANGFUSE_TRACING_ENABLED=false` disables tracing without disabling application
features. The SDK batches exports in the background; network/export failures are
logged and do not retry application operations. Workers and short-lived agents
flush on graceful shutdown. Forced termination can lose buffered spans.

Useful settings:

- `LANGFUSE_TRACING_ENVIRONMENT=development` (default); use `staging` or `production`
  in deployments so smoke/test data stays out of production dashboards.
- `LANGFUSE_RELEASE=<commit-or-release>` for deployment comparisons.
- `LANGFUSE_SAMPLE_RATE=1.0` (default); reduce this to sample production traces.
- `APP_TRACE_CONTENT_ENABLED=false` (default). Opt into content capture only for
  data approved for storage in your Langfuse project.

Rotate credentials formerly hard-coded in `app/config.py`, including the provider
key. They were preserved locally in `.env`, not in tracked application code.

## Trace structure

Each turn is a separate trace. The conversation/workflow ID is its session ID,
which groups multi-turn interactions without one indefinitely open trace.

Stable root observations include:

- `respond-to-message`: chat agent, including document tools;
- `gather-requirements`: interview agent and requirement tools;
- `design-api`: API-design agent and schema/endpoint tools;
- `write-requirements-document`, `render-openapi`, `parse-document`;
- `generate-server-code` and `verify-service`;
- `build-implementation` / `build-test-generation`: authoring/build stages.

Generations capture the provider's model name, token usage, tool requests,
responses, and reasoning when preserved by the provider adapter and content capture
is on. Tool-call payloads are normalized to OpenAI message format for readable cards.
Internal LangGraph chain/routing callbacks are skipped: each generation and tool
is a direct child of the application agent rather than a forest of node wrappers.
The root turn shows the latest user message and final reply, not all activity
arguments. Generations retain the full model context when content capture is on.
The SDK automatically calculates cost only for recognized model pricing; configure
custom model pricing in Langfuse for the local Qwen model if cost accounting is
needed. No fabricated user IDs or token/cost estimates are added.

With the Claude adapter, the pinned **Langfuse Claude Observability Plugin v1.2.1**
exports native conversation turns, generations (including thinking), MCP calls/results,
and token usage. It replaces LangChain generation/tool callbacks for Claude, avoiding
repeated observations and cost accounting. Other providers retain their LangChain tracing.
The upstream plugin source and license are vendored under
`app/llm/plugins/langfuse-observability`; `UPSTREAM.md` records the exact commit.

The SDK loads the plugin explicitly, without a marketplace installation or runtime package
download. Its hooks invoke `app.llm.claude_trace_hook` with the application's Python and
Langfuse 4.16.0, so content opt-in, masking, error sanitization, environment and release
also apply to plugin observations. Image upload is disabled. Missing credentials or
`LANGFUSE_TRACING_ENABLED=false` prevents plugin loading. Unsampled application roots
also skip the plugin. Hook export remains best-effort and flushes on Stop/SessionEnd.

The plugin receives the current application trace/parent IDs explicitly and attaches its
`Conversational Turn` beneath that observation. Generations and MCP tools are children
of the native turn, linked by the tool-use ID. The application's session ID stays
on all observations; Claude's native session ID remains separate. Cache usage follows the
plugin's native names (`cache_read_input_tokens` and `cache_creation_input_tokens`, with
separate cache-write durations when supplied), alongside uncached input and output.

Claude owns its history and automatic compaction. The SDK's built-in tool loop calls the
flow MCP server directly; the application never hands back placeholder tool results.
Host activities start a native session from application conversation context and keep it
open through tool calls and any corrective follow-ups. Coding pauses retain `.claude-native`
beside the project and resume the same native session in the next container. Normal
completion, errors, timeouts and limits close the SDK client.

Host sessions disable transcript persistence when native tracing is off. The tracing
plugin reads Claude's JSONL files, so traced host sessions use a temporary Claude config
directory that is deleted when the SDK session closes. They never store transcripts in
the worker's default Claude directory. These temporary files contain unmasked session
content even when export content capture is disabled. Coding transcripts are retained
only with a paused workspace and never enter published service sources. Corrective coding
queries resume that transcript with the remaining stage turn budget. Plugin cursor/log
state is scoped per native session workspace. Forced termination can lose observations
that hooks have not yet exported and leave temporary files for the host to clean up.

Feature tags, environment, release and session attributes propagate to children.
Host roots include Temporal workflow/run/activity IDs and attempt numbers. API
design roots also identify their source requirements session.

Docker agents resume the host stage's trace ID and parent observation ID, receive
the same session ID, and emit one distinctly named `agent` observation:

```text
build-implementation (host span)
└── author-implementation (isolated agent)
    ├── decide-next-action (generation)
    ├── read_file / write_file / edit_file / … / shell (tools)
    └── decide-next-action (generation)
```

For Claude, the agent instead contains the plugin's native turn with generation and
MCP-tool children.

Only coding-agent containers receive an explicit telemetry environment allowlist
at creation. Credentials never enter build arguments, images, generated service
containers or verification containers. Loopback Langfuse URLs are translated to
`host.docker.internal`, like the provider URL. Custom authoring images must include
`app/tracing.py`, `langchain`, and the Langfuse dependency to use this instrumentation.

Tracing lives in **activities**, not Temporal workflow code. Workflow replays do
not emit observations or perform telemetry I/O. Actual activity retry attempts
are separate executions/traces, with attempt metadata. The application intentionally
does not create a single cross-workflow trace for a long-lived conversation;
sessions group the separate units of work, including parallel coding stages.

## Privacy

By default, prompt, document, generated source and tool input/output are replaced
with `[CONTENT CAPTURE DISABLED]` before export. Structural data, model/usage,
latency, session/feature metadata and error classification remain available.

When content capture is enabled, export-time masking covers both custom spans
and framework callbacks: known environment secrets, common API-key patterns,
email addresses and sensitive JSON fields (including nested serialized tool
results). This is **not a complete PII or confidential-data classifier**. Add
application-specific masking before enabling content for sensitive sessions.

The SDK masking hook cannot edit OTEL status descriptions or exception events.
A privacy-preserving OTLP exporter removes those fields; useful sanitized failure
information remains in observation attributes. With content capture off, raw
provider error bodies are excluded as well. SDK debug logs and existing application
DEBUG logs are separate from export masking; do not enable them for sensitive
production traffic without a logging policy.

Coding agents can execute arbitrary shell commands and access their supplied
provider/telemetry credentials. Use least-privilege, non-production project keys
and a dedicated Docker host for untrusted inputs. Export masking does not secure
the authoring sandbox or prevent an agent from directly exfiltrating a key.

## Validate and audit

From `backend`, with Temporal and the configured model provider running:

```sh
APP_TRACE_CONTENT_ENABLED=true python -m tools.trace_smoke
```

This sends only a synthetic catalog requirement and fictitious email through a
real Temporal worker/workflow, model and requirements tool. It terminates the
smoke workflow afterward without finalizing documents or launching coding stages.
It prints a session ID. The model call is real and may incur provider charges.

Use the installed Langfuse skill/CLI to fetch the observations for that session:

```sh
# Load the Langfuse variables from your local shell/.env; never paste keys in chat.
export LANGFUSE_HOST="$LANGFUSE_BASE_URL"
npx --yes langfuse-cli api observations list \
  --session-id '<printed-session-id>' \
  --fields core,basic,io,metadata,model,usage,trace_context --limit 100 --json
```

Check [current Langfuse best practices](https://langfuse.com/docs/observability/best-practices):
root input/reply, per-call generations and tool observations, hierarchy, model,
usage, session/tags/environment, and masked `trace-test@example.com`. In the UI,
use Sessions for conversation flows and feature tags to filter Traces/Dashboards.

Local transport tests run the actual SDK and model/framework adapters against
HTTP test servers, not mocked callbacks. They verify concurrent session isolation,
generation/token capture, native Claude MCP execution and plugin parenting/privacy,
tool hierarchy, cross-process agent parenting, exit
flushing, content exclusion, redaction on provider failure, and disabled tracing.
Normal tests forcibly disable external tracing, even when a developer has `.env`.

```sh
python -m pytest tests/test_tracing.py -q
python -m pytest -q
```

### Live validation

The native Claude/plugin migration is validated against a local Anthropic-compatible
endpoint and OTLP collector using the actual bundled CLI, including a non-root Docker
agent. Coverage includes native thinking, MCP side effects/results, cache usage,
parent/session propagation, disabled/sampled-out tracing, masking, and pause/resume
without repeated exports. The hosted traces below predate this migration; re-audit
hosted ingestion after deploying the new worker and agent images.

After the self-hosted ingestion issue was fixed, the real Temporal → LangGraph →
local-model run completed and its observations were fetched via the Langfuse CLI.
The audit removed unnecessary chain/routing spans and normalized tool-call cards.
The final trace has one agent root, two generations and one requirements tool,
with correct sibling parenting, model/token/cache usage, session/feature/environment
attributes, meaningful root input/reply, and a masked fictitious email. The local
provider returned no separate reasoning field; no reasoning or cost is fabricated.

- [Audited requirements trace](https://langfuse.zikasak.uk/project/cmur49qts0006nt07o02xll9w/traces/c4bf0b8e4fed78caab63d9e0d6c3b7d8)
- [Audited Docker-agent trace](https://langfuse.zikasak.uk/project/cmur49qts0006nt07o02xll9w/traces/374e9854bc2d8d5173e7484afb6c98f2): a freshly built, constrained non-root container
  created a synthetic artifact. Its agent, two generations and file tool were
  fetched and verified under the host span with the same trace/session. The
  disposable container and image were removed; no real service was generated.

Do not treat successful model execution or authentication alone as evidence that
telemetry was ingested; query the observations after every integration change.
