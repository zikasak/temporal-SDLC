# Reduce context usage in implementation, test generation, and verification

## Context
The user requested an assessment of context-reduction opportunities in `backend/app/implementation/`, `backend/app/test_generation/`, and `backend/app/verification/`. Planning only: no source changes.

Initial findings:
- Implementation and test generation use the same `backend/app/agent_workflows/runtime.py` agent loop; every assistant/tool message remains in context for up to 80 turns.
- Shared tools return up to 30,000 characters per file read and 16,000 characters per shell invocation. File contents supplied in `write_file` calls also remain in history.
- Test generation injects both structured `api_design` (inside input JSON) and its rendered OpenAPI contract. This duplicates contract information.
- Verification executes Docker and summarizes Surefire XML deterministically; it makes no LLM calls. Downstream workflow inspection confirms its results are not fed into authoring prompts.

## Approach
Assessment only (confirmed by the user): rank opportunities without choosing an implementation scope. Prioritize removing duplicated inputs and reducing avoidable tool output; preserve contract completeness and independent HTTP testing. Investigate safe history management only after mapping existing utilities and observability.

### Confirmed data flow
- Both authoring branches receive `_coding_context()` from `backend/app/temporal/workflows.py`: structured requirements, a requirements document, and structured API design. No interview conversation is passed.
- Shared host code serializes this context once into `input.json`, but the runtime resends its contents and all subsequent messages on every model invocation.
- Verification results are stored in workflow state and artifacts; the current retry path reruns authoring with the original contract, not verification logs. Therefore verification currently contributes no model input tokens.
- No existing token-counting, message-trimming, or context-compaction helper was found in the backend search. The factory supports OpenAI-compatible and Gemini providers; any budget/counting scheme must work across both.
- Requirements tables are deterministically rendered from structured requirements (`backend/app/requirements/document.py`), but documents can also contain an LLM-written narrative (`backend/app/temporal/activities.py`). Removing the whole document could lose unique information.
- Existing Langfuse callbacks trace each `decide-next-action` generation. Content capture is disabled by default; provider usage can still supply token measurements without exporting source/contracts. Reuse this rather than adding another tracing system.
- Existing tests exercise real shared tools with a scripted provider and enforce host isolation/publication boundaries. They are suitable owners for future behavioral regression coverage.
- Agent containers receive an explicit environment allowlist (`backend/app/docker_agents.py`); stage max-turn environment variables are read by the runtime but are not forwarded by the standard host launch path.

### Ranked opportunities
Ranked by practicality and risk, not claimed percentage savings. Impact depends on contract size and tool usage.

| Rank | Opportunity | Applicable stages | Expected impact | Risk |
| --- | --- | --- | --- | --- |
| 1 | Supply the API contract once, not both structured JSON and rendered OpenAPI | Test generation | Removes one complete API representation from every invocation | Low, with contract equivalence checks |
| 2 | Return targeted file ranges/search results and concise build diagnostics | Both authoring stages | High on read/build-heavy runs; prevents repeated large outputs | Low–medium; errors must remain retrievable |
| 3 | Deduplicate requirements tables while preserving unique narrative | Both authoring stages | Contract-size-dependent saving on every invocation | Medium; do not discard narrative or requirement metadata |
| 4 | Add precise file editing instead of repeatedly replacing whole files | Both authoring stages | Reduces generated tokens and retained tool-call arguments | Low–medium; edits must fail clearly on ambiguous matches |
| 5 | Compact stale history under a model-aware context budget | Both authoring stages | Largest potential saving on long runs | High; state loss, extra summary calls, protocol mistakes |
| 6 | Load large contracts on demand rather than embedding them in full | Both authoring stages | Potentially useful for very large contracts, not guaranteed | High; omitted requirements and additional reads/turns |

**1 — API deduplication.** Keep `api_design` available on the host to render `openapi.yaml`, but exclude it from the test agent's model-visible JSON. Keep the complete OpenAPI contract as the API authority, including security, schema descriptions, errors, and `x-requirements` mappings. Implementation currently receives only the structured API as initial context; do not remove its sole explicit API representation.

**2 — Output shaping.** Extend `read_file` with offset/limit and explicit truncation/continuation information. Provide bounded search/listing so agents can discover relevant files without dumping trees or generated models. For compilation, show exit status plus useful error locations/messages; retain verbose output in workspace files for explicit retrieval. Preserve arbitrary `shell` for necessary operations, but guide normal reads/builds through bounded tools. Simply lowering today's caps can hide the information needed to finish and cause more calls.

**3 — Requirements deduplication.** Generated Markdown tables repeat the requirement list, while the narrative may carry extra assumptions or business context. Use one complete requirements representation plus unique narrative, with a fallback when document provenance/equivalence is uncertain. Preserve IDs, categories, priorities, rationale, and business/non-functional requirements. Do not summarize away requirements just to reduce tokens.

**4 — Incremental edits.** The existing `write_file` tool returns only `Written`, but its full source payload stays inside the assistant tool call for all future turns. An exact-match edit tool can substantially reduce this duplication during fixes; reuse workspace path validation, keep full writes for new files, and return concise outcomes.

**5 — History compaction.** Preserve system instructions and authoritative inputs, recent complete assistant/tool-call batches, and a checkpoint containing changed files, decisions, remaining work, and unresolved errors. Use the filesystem as the source of truth for current code; re-read relevant regions when needed. Drop only complete older batches so tool calls/results remain correlated. Account for model/provider differences, tool schemas, and output-token headroom. Summary calls have their own token/latency cost. No safe compaction utility currently exists to reuse; this needs quality evaluation before adoption.

**6 — On-demand contracts.** Only consider after measuring large-contract runs. Provide safe read/search access to authoritative contracts and an index covering all requirements/endpoints. The test agent's `read_file` is currently rooted at `/workspace/tests`, while OpenAPI lives at `/workspace/openapi.yaml`; retrieval needs an explicit read-only contract path or prepared contract subdirectory, not broader access to implementation files. Reads remain in history, so loading everything eventually can eliminate the saving.

### Verification and lower-value levers
- Verification already bounds logs to 8,000 characters each and failure details to 50 entries with 2,000-character messages. Further reductions target Temporal/API payload and artifact size, **not LLM context**, and may weaken diagnostics. No change recommended for the context goal.
- The system prompts are comparatively short and protect important behavior; shortening them is not the main opportunity.
- Fewer turns or prompt caching can lower cost, but are not substitutes for context management. Caching does not reduce logical context length; lower turn limits can merely abort unfinished work. Standard container launch does not currently forward the stage turn-limit settings.

## Files to modify
**None for this assessment.** Potential locations if an opportunity is later selected:
- #1: `backend/app/test_generation/activities.py` (`prepare_inputs`); `backend/app/test_generation/agent.py` if input layout changes.
- #2, #4, #5: `backend/app/agent_workflows/runtime.py`; tool guidance in `backend/app/implementation/agent.py` and `backend/app/test_generation/agent.py` only as needed.
- #3: stage input builders in `backend/app/implementation/activities.py` and `backend/app/test_generation/activities.py`; keep shared workflow state/contracts intact.
- #6: stage input builders, agent entry points, and shared runtime contract access.
- Any new container configuration: explicitly forward only required settings in `backend/app/docker_agents.py`.
- Existing behavioral test owners: `backend/tests/test_agent_workflows.py`, `backend/tests/test_implementation.py`, `backend/tests/test_test_generation.py`. No test changes proposed now.

## Reuse
- `AgentProfile`, `workspace_tools`, `_run_agent`: `backend/app/agent_workflows/runtime.py`.
- `StageInputs` / `run_stage`: `backend/app/agent_workflows/host.py`.
- `render_openapi`: `backend/app/api_design/openapi.py`.
- `render_requirements_markdown`: `backend/app/requirements/document.py` (existing table rendering, not a narrative extractor).
- `langchain_config`, `trace_operation`, and privacy masking: `backend/app/tracing.py`; operational guidance in `docs/langfuse-tracing.md`.
- `summarize_reports`: `backend/app/verification/activities.py` (already deterministic; keep verification non-LLM).

## Steps
- [x] Trace stage inputs, shared runtime, and downstream verification consumers.
- [x] Identify existing context-management/token-usage instrumentation.
- [x] Rank concrete reductions by expected impact and behavioral risk.
- [x] Record candidate files, reusable utilities, and quality safeguards.
- [x] Leave implementation scope unselected, as requested.

## Verification
This is a code-based assessment: no tests, model calls, or live trace queries were run, and no numerical savings are claimed. Only this Markdown report was created/updated.

If an opportunity is selected later:
1. Baseline small, large, and compilation-failure/rework contracts using the same provider/model settings; compare repeated runs because generation is nondeterministic.
2. Reuse existing Langfuse `decide-next-action` usage by authoring role. Measure total input tokens (including cache buckets without double counting), peak input per invocation, output tokens, turns, cost/latency, and completion rate. Keep content capture disabled unless data is approved. If provider usage is missing, report that limitation rather than treating character counts as exact tokens.
3. For input deduplication, independently check endpoint methods/paths, security, schemas, success/error responses, requirement mappings, and unique requirements narrative survive. Extend existing host-boundary fixtures, not source-string tests.
4. For tools/history changes, extend shared runtime behavioral coverage: retrieve content beyond current limits, recover complete failure diagnostics, edit safely, preserve workspace isolation, and maintain valid multi-tool-call/result correlation after compaction. Avoid production seams used only by tests.
5. Compare generated service compilation/package/Docker build success, independently compiled HTTP tests, requirement/endpoint coverage, and final verification status. Do not accept fewer generated tests or weaker assertions as token savings.
6. Run from `backend` after implementation approval: `python -m pytest tests/test_agent_workflows.py tests/test_implementation.py tests/test_test_generation.py tests/test_verification.py tests/test_tracing.py -q`; use the existing opt-in real Docker proof with `RUN_DOCKER_VERIFICATION_TESTS=1 python -m pytest tests/test_verification_docker.py -q`. The Docker test proves runner infrastructure, not authoring quality; also run the actual three-stage pipeline on representative contracts.

Reference consulted: [Langfuse token and cost tracking](https://langfuse.com/docs/observability/features/token-and-cost-tracking). Existing tracing is sufficient for the proposed measurements; no SDK upgrade or new observability system is part of this assessment.
