# Deterministic checks in place of LLM calls (review and routing)

## Context
Review and routing are about 40% of all model tokens (Langfuse, 13 Oct – 12 Nov 2026):
review 140M over 151 reviewer runs in 11 sessions, routing 74M over 14 runs (about 5.3M
tokens, 49 turns and 50–70K output tokens per run). Part of what these agents do is
mechanical. This plan moves five such pieces to host code or trusted tools.

What the code does today:
- Every review round rebuilds the parts from the whole delivery diff and runs one agent
  per part (`review_parts`, `prepare_review`, `ReviewWorkflow.run`), up to
  `MAX_REVIEW_ROUNDS = 5` and `MAX_STAGE_REVIEW_ROUNDS = 3`. A part no correction touched
  is reviewed again in full.
- `run_review` already returns `findings: []` without an agent when nothing changed, so
  the activity can decide to skip a run without any change to workflow history.
- The seam's signatures are checked by the conformance runner (`_add_seam`,
  `conformance_error`) before the seam review; the baseline run then proves the seam
  changed nothing the existing tests check (`judge_baseline(..., seam=True)`).
- Baseline failures already carry `kind` (`failure` or `error`) and reach the regression
  reviewer through `BASELINE_KEY`. Tests carry no requirement ID.
- No linter, type checker or secrets scanner runs on a delivery; the reviewer is asked to
  find those problems by reading.
- The routing agent hand-writes every stub and fixes it against `stubtest` in a shell loop
  (352 shell and 257 `write_file` calls in 14 runs). `ComponentContract.tools` already
  ships trusted files to the agent in `TOOLS_DIR`.

## Approach
One rule for all five: a deterministic check may **skip** an agent run or **emit a
finding** only when it is certain. Anything it cannot decide falls through to the agent
unchanged. Each check sits behind its own setting, so it can be compared and switched off.

Order: step 0, then 1, 5, 3, 2, 4. Items 1 and 5 carry most of the token saving.

### 0 — Baseline numbers
Record, per review scope (`review`, `seam-review`, `regression-review`) and for routing:
runs per delivery, rounds per run, parts per round, tokens and turns per run. The child
workflow IDs already name scope and round; add the part number and scope to the review
trace metadata so Langfuse can group by them. The share of seam and regression reviews is
not known today and decides how much items 2 and 3 are worth.

### 1 — Skip files settled in an earlier round
- After each completed review run, write `reviewed.json` in the scope's artifact directory:
  per reviewed file, the SHA-256 of its diff text, the findings reported on it, and a
  `judgement key` — a hash of scope, instructions, the contract index and brief, and a
  prompt version constant.
- In `prepare_review`, when `PREVIOUS_KEY` is present, a file is **settled** when its diff
  hash and the judgement key both match the record, and the run that covered it reported no
  finding without a `path`. Parts are built from unsettled files only; settled files are
  listed in `review_part.other_changes`, so the reviewer can still read them.
- No unsettled file left: return without running an agent.
- Findings recorded on settled files are carried into the round's result through the
  existing `_repeated` matching, so an uncorrected finding still counts as open and the
  round is never reported clean because its file was skipped.
- New instructions or a mention change the judgement key, so such a run reviews everything.
- Setting `REVIEW_SKIP_SETTLED` (default on once compared). Risk: a correction in one file
  breaks an unchanged one. The re-run verification covers what tests observe; the rest is
  the accepted trade, visible in the comparison of step 0.
- No workflow change and no Temporal patch: the decision is inside the activity.

### 5 — Generate stub signatures with a trusted tool
- New tool per language in `ComponentContract.tools`: `make_stub <module> <symbol>…`. It
  writes or extends the module's stub with the exact signatures of the named symbols, the
  types and collaborators they mention, decorators and `__init__`, bodies reduced to a
  docstring placeholder and `...`. It only appends to a published stub, never changes a
  statement, and its output must pass `read_stub` and the conformance check.
- Python first, from the source's AST (no import of the service needed). TypeScript second
  (`tsc --declaration --emitDeclarationOnly`, filtered to the named exports). Java last;
  until then Java keeps today's instructions.
- Component contract instructions change: generate signatures with the tool, write only
  the behaviour docstrings with `edit_file`, hand-write a stub only for symbols listed in
  `added` or `added_elsewhere`, which the code does not have yet.
- The final conformance check on the host stays as it is: the tool is a shortcut for the
  agent, not a new authority.
- Measure by routing output tokens, turns and shell calls per run.

### 3 — Regression review: coverage by tag, flakiness by rerun
- **Requirement tags.** The test authors' instructions require each added test to name the
  criteria it proves in a comment line `requirement: FR-…` inside the test. `test_cases`
  already returns each test's source, so one language-independent parser reads the tags.
- Host check before the regression review: every criterion routed to a suite has at least
  one added test tagged with it, and every tag names a requirement of the brief. A miss is
  a templated finding with target `tests`.
- When such findings exist, the round returns them **without running the reviewer**: the
  tests are about to change, so reviewing them now is spent twice. The reviewer's input
  gains a `criteria_tests` map, which replaces its own search for which test proves what.
- **Flakiness.** Run the baseline suite a second time in shuffled order and compare the
  tracked tests' outcomes; a test whose outcome differs is a templated finding. This costs
  one more verification run and saves few tokens; it is here for the quality of the result
  and is the first piece to drop if the extra run time is not wanted.
- Not planned: a finding from `kind == 'error'`. An erroring test is not certainly wrong,
  and the reviewer already sees the kind.

### 2 — Seam review: decide what a diff can decide
In `run_review` for scope `seam`, before the agent (Python first; other languages fall
through to the agent):
- **Certain violations, emitted as findings without an agent run:** a changed file that is
  not the source of a module listed in `seam`; a removed or modified line (for TypeScript,
  except an `export` added to an existing declaration). Needs a new
  `ComponentContract.source_paths(module, runtime)`, the inverse of `module`.
- **Skip the agent** when there is no violation and both hold: every added function body is
  minimal by AST (docstring, then `pass`, `...`, `raise NotImplementedError`, or a return of
  a constant, a name or an attribute), and no added symbol's name occurs outside the added
  lines. Anything else runs the agent as today.
- The minimal-body rule matters: a seam that already implements part of the fix makes the
  component tests pass in the baseline, which ends the delivery as `not_reproduced`
  instead of correcting it. Such a seam must reach the reviewer.

### 4 — Static tools before the delivery review
- New `Language.static_checks`: named commands with a parser, run in the language's agent
  image over the review workspace before the agent. Start with a secrets scanner
  (gitleaks, language-independent) and, for Python, ruff limited to error, bug and security
  rules (`F`, `B`, `S`), never style. Versions pinned in the image.
- Results are cut to the lines the delivery changed (hunk ranges of `changes.diff`) and
  given to the reviewer as `static_findings` in `input.json`, with a prompt paragraph: judge
  each one, report those that are real, and do not search for these categories yourself.
- The reviewer stays the judge: scanners report fixtures and test data too. The token
  effect is modest and uncertain; the main gain is that these classes are no longer left
  to chance.

## Files to modify
- 0: `backend/app/review/activities.py` (trace metadata).
- 1: `backend/app/review/activities.py` (`prepare_review`, `run_review`, the record);
  `backend/app/config.py`, `backend/.env.example`, `docker-compose.coolify.yml` (setting).
- 5: `backend/app/languages/python/component_contract/` (new tool) and
  `languages/python/components.py` (instructions, `tools`); later the TypeScript
  equivalents. `backend/app/routing/agent.py` only if the shared prompt needs a sentence.
- 3: `backend/app/test_generation/agent.py` and the suites' `instructions` (tags); new
  `backend/app/review/checks.py` (tag parser, coverage, templated findings);
  `backend/app/review/activities.py` (`_regression_changes`, early return);
  `backend/app/review/agent.py` (`criteria_tests`); `backend/app/delivery/flow/baseline.py`
  with a Temporal patch for the second run.
- 2: `backend/app/review/checks.py`; `backend/app/languages/__init__.py` and
  `languages/python/components.py` (`source_paths`); `backend/app/review/activities.py`.
- 4: `backend/app/languages/__init__.py` (`static_checks`), the Python language package and
  its agent Dockerfile, `backend/app/review/activities.py`, `backend/app/review/agent.py`.
- `docs/service-implementation.md` for each behaviour that changes.

## Steps
- [x] 0: add scope and part to review traces. [ ] Record the baseline for two weeks or ten
  deliveries, with every check below off (their default).
- [x] 1: settled-file record and skipping (`REVIEW_SKIP_SETTLED`). Deviation: a file with
  a finding is never settled, so no finding is carried; carrying one would keep it open
  after a correction in another file settled it. [ ] Compare against step 0.
- [x] 5: Python `make_stub`, instructions, host rejection of its placeholder.
  [ ] TypeScript; Java.
- [x] 3: requirement tags and the coverage check (`REVIEW_REQUIREMENT_TAGS`). Deviation:
  tags are read from the added lines of the suites' diffs, as Python's and TypeScript's
  `test_cases` drop comments. [ ] Decide on the flakiness rerun (not implemented).
- [x] 2: seam violations and the skip rule for Python (`REVIEW_SEAM_CHECKS`).
- [x] 4: gitleaks and ruff for Python reviews, gitleaks for Java (`REVIEW_STATIC_CHECKS`).

## Verification
- Existing owners of the behaviour: `tests/test_review_parts.py`,
  `tests/test_review_findings.py`, `tests/test_stage_review_corrections.py`,
  `tests/test_routing.py`, `tests/test_python_component_check.py`,
  `tests/test_component_contract_docker.py`. Workflow tests that start from the repository
  use the dev-server fixtures, not time skipping.
- Per item, the cases that must hold:
  1. an unchanged file is not reviewed again; an uncorrected finding on it stays open; a
     changed file, new instructions or a pathless finding review everything concerned;
  5. a generated stub passes `read_stub` and the conformance check for functions, methods,
     properties, `__init__` and a published stub that is extended;
  3. a criterion without a tagged test and a tag naming no requirement each yield a
     finding and no agent run; complete tags run the reviewer with `criteria_tests`;
  2. a change outside the seam's modules and a removed line each yield a finding; a
     non-minimal body or a reference to an added symbol runs the agent;
  4. a finding outside the changed lines is not passed on.
- Quality gate for every item, against step 0 on the same kinds of delivery: findings per
  delivery and their acceptance by the user, deliveries ending `findings_open`,
  `not_reproduced` or in a routing error, and repair rounds. Fewer tokens with more
  failed deliveries is not accepted.
