import { useState, type ReactNode } from "react";
import { repositoryPublication, type RepositoryPublication, type RepositorySelection } from "./github";
import { QuestionForm, questionList, type Question } from "./Questions";
import { isObject } from "./api";
import { flowStepsLink } from "./url";

type GeneratedCode = {
  server: { path: string; files: number };
};

type ImageArtifact = { image_id: string };
// What the author of a documentation task wrote, against the repository's files.
type Documentation = { changed_files: string[] };
// The test suite's image and, for a change, how its tests differ from the repository's.
type TestSuite = ImageArtifact & { added_tests: string[]; changed_tests: string[]; removed_tests: string[] };
type TestRef = { class_name: string; name: string };
// `findings_open`: review findings remained after the last review round.
// `checking`: the repository's checks script runs over the verified delivery.
type VerificationStatus = "not_applicable" | "waiting" | "running" | "checking" | "diagnosing" |
  "repairing" | "reviewing" | "completed" | "findings_open" | "skipped" | "error";
// The last run of the repository's checks script, and the LLM's analysis of a failure.
type ChecksRun = {
  status: "passed" | "failed" | "missing" | "not_applicable" | "error";
  exit_code: number | null;
  excerpt: string;
  error: string | null;
  analysis: { summary: string; files: string[] } | null;
};
// An agent waiting, until `until`, for its model provider's limits.
type ProviderWait = { workflow_id: string; stage: string; until: string; message: string | null };
// How the tests a change added fared against the service as the repository holds it.
type BaselineStatus = "not_applicable" | "waiting" | "running" | "reproduced" | "not_reproduced" |
  "skipped" | "error";
// Which suite can prove each acceptance criterion of a change: `routed` when every one has
// one, `unobservable` when one has none and the delivery stopped before its tests ran,
// `unsupported` when a delivery started before browser tests existed routed one to them.
type RoutingStatus = "not_applicable" | "waiting" | "running" | "routed" | "unobservable" |
  "unsupported" | "error";
// The module and symbols of the code at which a `component` route observes its criterion;
// `added` are those the code does not have yet, which the change adds first. `part` names
// the part of the repository the seam is in, such as `frontend`; absent for the service.
type Seam = { part?: string; module: string; symbols: string[]; added: string[] };
// An element of a page as a browser test finds it: an ARIA role with its accessible name,
// or a data-testid.
type PageElement = { role?: string; name?: string; testid?: string };
// The page of a part's web client at which a `browser` route observes its criterion in a
// real browser: its entry, the viewports it holds in and the elements its test finds;
// `added` are those the page lacks yet, which the change adds first.
type Page = { part: string; entry: string; viewports: string[]; roles: PageElement[]; added: PageElement[] };
// `prepared`: the code has the seam the change adds first, matching its component contract.
type SeamStatus = "not_applicable" | "waiting" | "running" | "prepared" | "error";
// `source`: who decided the route, the routing stage or the test author. For an unobservable
// criterion, `suggestion` rephrases it as behaviour a suite observes, and `covered_by` names
// the other criteria that already prove all of it any suite could.
type RequirementRoute = {
  route: "contract" | "component" | "browser" | "unobservable"; reason: string; source: "routing" | "tests";
  seam?: Seam; page?: Page; suggestion?: string; covered_by?: string[];
};
// `tests` are the failing tests the diagnosis found broken: the only ones a repair may change.
// `suite` names the tests a repair of a change with component tests corrected.
type RepairRound = {
  attempt: number; target: "implementation" | "tests"; tests: TestRef[]; summary: string;
  suite?: "contract" | "component";
};
type PartCounts = { tests: number; passed: number; failures: number; errors: number; skipped: number };
type VerificationResult = {
  // `not_implemented`: this kind of task has no check yet, so nothing was run.
  status: "passed" | "failed" | "error" | "not_implemented";
  tests: number;
  passed: number;
  failures: number;
  errors: number;
  skipped: number;
  duration_seconds: number;
  test_failures: { class_name: string; name: string; kind: string; message: string }[];
  details_truncated: boolean;
  // A component run of the tests of more than the service: the outcomes by part, the
  // service's as `service`.
  parts?: Record<string, PartCounts>;
  service_log: string;
  runner_log: string;
  error: string | null;
};

// `review` runs a failed code review again, or goes on from a failed correction of its findings.
export type GenerationStage = "server" | "tests" | "documentation" | "repository" | "review";
// The kind of task a session delivers, as the requirements session recorded it.
export type TaskKind = "new_service" | "api_change" | "bugfix" | "docs";
const taskKinds: TaskKind[] = ["new_service", "api_change", "bugfix", "docs"];

/** The task kind a session record carries, or null when it has none. */
export function taskKind(value: unknown): TaskKind | null {
  return taskKinds.includes(value as TaskKind) ? value as TaskKind : null;
}
// `repeats`: the finding was reported again after a correction did not settle it.
// `unresolved`: why no author of the delivery settled a finding left open.
type ReviewFinding = {
  summary: string; path?: string; target: "implementation" | "tests" | "both"; reason: string; repeats?: number;
  unresolved?: string;
};
type ReviewRound = { round: number; findings: ReviewFinding[]; instructions?: string };
// Questions a coding agent asked; its run is paused until they are answered, and only
// that agent sees the answers.
type AgentQuestions = { workflow_id: string; stage: string; questions: Question[] };
type PublicationStatus = "not_applicable" | "waiting" | "running" | "completed" | "skipped" | "error";
type PullRequest = { number: number; title: string; url: string; draft: boolean; base: string };

/** What a session's delivery has produced so far, as the session API reports it. */
export type DeliveryProgress = {
  code: GeneratedCode | null;
  code_error: string | null;
  implementation: ImageArtifact | null;
  implementation_error: string | null;
  tests: TestSuite | null;
  tests_error: string | null;
  // The component tests of a change's criteria routed to a seam of the code.
  component_tests: TestSuite | null;
  component_tests_error: string | null;
  documentation: Documentation | null;
  documentation_error: string | null;
  routing_status: RoutingStatus;
  // By requirement ID.
  routing: Record<string, RequirementRoute> | null;
  routing_error: string | null;
  // The seam the change adds, with no behaviour change, before its tests are written.
  seam_status: SeamStatus;
  seam_error: string | null;
  // The seam's review rounds before any test is written, each corrected by its author.
  seam_reviews: ReviewRound[];
  baseline_status: BaselineStatus;
  baseline: VerificationResult | null;
  // The component tests' run against the code as the repository holds it.
  component_baseline: VerificationResult | null;
  baseline_error: string | null;
  // The regression tests' review rounds once they failed without the fix, before it.
  test_reviews: ReviewRound[];
  verification_status: VerificationStatus;
  verification: VerificationResult | null;
  // The component tests' run with the change, once it conforms to its component contract.
  component_verification: VerificationResult | null;
  verification_error: string | null;
  repairs: RepairRound[];
  publication: RepositoryPublication | null;
  publication_error: string | null;
  publication_status: PublicationStatus;
  agent_questions: AgentQuestions[];
  provider_waits: ProviderWait[];
  checks: ChecksRun | null;
  reviews: ReviewRound[];
  review_error: string | null;
  // The correction of a review round's findings did not pass verification.
  correction_failed: boolean;
  // Pushed as unverified with findings left; the user accepts them or gives instructions.
  open_findings: boolean;
  // The pull request step after the push, and the pull request once it is open.
  pull_request: PullRequest | null;
  pull_request_error: string | null;
  pull_request_status: PublicationStatus;
  // Set when the generic title and description were used because the LLM could not write them.
  pull_request_text_error: string | null;
  // Authorised mentions of the app on the pull request and what they led to.
  mentions: PullRequestMention[];
  // Whether an onboarding changed the existing code after the user confirmed a bug in it.
  existing_code_changed: boolean;
  // The repository's CI on the last pushed commit.
  ci: CiChecks | null;
  // The new top-level flows that fix rounds started after the flow closed, oldest first.
  fix_flow_ids: string[];
};

const ciStatuses = ["pending", "passed", "failed", "inconclusive", "none", "timed_out", "unavailable"] as const;
type CiStatus = typeof ciStatuses[number];
type CiChecks = {
  commit: string;
  status: CiStatus;
  failed: { name: string; url: string | null }[];
  error: string | null;
  fix_round: number;
};

function ciChecks(value: unknown): CiChecks | null {
  if (!value || typeof value !== "object") return null;
  const item = value as Record<string, unknown>;
  if (typeof item.commit !== "string" || !ciStatuses.includes(item.status as CiStatus)) return null;
  const checks = Array.isArray(item.checks) ? item.checks : [];
  const failed = checks.flatMap((check) => {
    if (!check || typeof check !== "object") return [];
    const entry = check as Record<string, unknown>;
    if (typeof entry.name !== "string"
      || !["failure", "timed_out", "startup_failure", "action_required"].includes(entry.conclusion as string)) return [];
    const url = typeof entry.url === "string" && entry.url.startsWith("https://github.com/") ? entry.url : null;
    return [{ name: entry.name, url }];
  });
  return {
    commit: item.commit, status: item.status as CiStatus, failed, error: text(item.error),
    fix_round: typeof item.fix_round === "number" ? item.fix_round : 0,
  };
}

const ciText: Record<CiStatus, string> = {
  pending: "CI is running on the pushed commit…",
  passed: "CI passed on the pushed commit.",
  failed: "CI failed on the pushed commit.",
  inconclusive: "CI finished without a verdict on the pushed commit, such as a cancelled check.",
  none: "The repository ran no CI checks on the pushed commit.",
  timed_out: "CI did not finish on the pushed commit within three hours.",
  unavailable: "The CI of the pushed commit could not be read.",
};

export type PullRequestMention = {
  comment_id: number;
  comment_type: "conversation" | "review";
  url: string;
  author: string;
  body: string;
  received_at: string;
  classification: "pending" | "fix" | "clarification" | "unclear";
  status: "classifying" | "queued" | "running" | "replied" | "completed" | "error";
  round: number | null;
  reply_url: string | null;
  error: string | null;
};

function githubLink(value: unknown): string | null {
  return typeof value === "string" && value.startsWith("https://github.com/") ? value : null;
}

function pullRequestMentions(value: unknown): PullRequestMention[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (typeof item !== "object" || item === null) return [];
    const record = item as Record<string, unknown>;
    const url = githubLink(record.url);
    if (!Number.isSafeInteger(record.comment_id) || !url || typeof record.author !== "string"
        || typeof record.body !== "string") return [];
    return [{
      comment_id: record.comment_id as number,
      comment_type: record.comment_type === "review" ? "review" : "conversation",
      url, author: record.author, body: record.body,
      received_at: typeof record.received_at === "string" ? record.received_at : "",
      classification: ["fix", "clarification", "unclear"].includes(record.classification as string)
        ? record.classification as PullRequestMention["classification"] : "pending",
      status: ["queued", "running", "replied", "completed", "error"].includes(record.status as string)
        ? record.status as PullRequestMention["status"] : "classifying",
      round: Number.isSafeInteger(record.round) ? record.round as number : null,
      reply_url: githubLink(record.reply_url),
      error: typeof record.error === "string" ? record.error : null,
    } satisfies PullRequestMention];
  });
}

const verificationStatuses: VerificationStatus[] = [
  "not_applicable", "waiting", "running", "checking", "diagnosing", "repairing", "reviewing",
  "completed", "findings_open", "skipped", "error",
];
const baselineStatuses: BaselineStatus[] = [
  "not_applicable", "waiting", "running", "reproduced", "not_reproduced", "skipped", "error",
];
const routingStatuses: RoutingStatus[] = [
  "not_applicable", "waiting", "running", "routed", "unobservable", "unsupported", "error",
];
const seamStatuses: SeamStatus[] = ["not_applicable", "waiting", "running", "prepared", "error"];
const publicationStatuses: PublicationStatus[] = [
  "not_applicable", "waiting", "running", "completed", "skipped", "error",
];

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function repairRounds(value: unknown): RepairRound[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => isObject(item) &&
    typeof item.attempt === "number" && typeof item.summary === "string" &&
    (item.target === "implementation" || item.target === "tests")).map((item) => ({
    ...item,
    tests: Array.isArray(item.tests) ? item.tests.filter((test: unknown): test is TestRef =>
      isObject(test) && typeof test.class_name === "string" && typeof test.name === "string") : [],
    suite: item.suite === "contract" || item.suite === "component" ? item.suite : undefined,
  }) as RepairRound);
}

function seam(value: unknown): Seam | undefined {
  return isObject(value) && typeof value.module === "string"
    ? {
      ...(typeof value.part === "string" && value.part ? { part: value.part } : {}),
      module: value.module, symbols: strings(value.symbols),
      // Symbols added to other modules of the part are named after their module.
      added: [...strings(value.added), ...(isObject(value.added_elsewhere)
        ? Object.entries(value.added_elsewhere).flatMap(([module, symbols]) =>
          strings(symbols).map((symbol) => `${module}.${symbol}`)) : [])],
    } : undefined;
}

function pageElements(value: unknown): PageElement[] {
  return Array.isArray(value) ? value.filter((item): item is PageElement => isObject(item) &&
    (typeof item.testid === "string" || typeof item.role === "string")) : [];
}

function page(value: unknown): Page | undefined {
  return isObject(value) && typeof value.part === "string" && typeof value.entry === "string"
    ? { part: value.part, entry: value.entry, viewports: strings(value.viewports),
      roles: pageElements(value.roles), added: pageElements(value.added) } : undefined;
}

/** How a page's element is named, as the page contract's checks name it. */
export function elementLabel(element: PageElement): string {
  if (element.testid !== undefined) return `testid=${element.testid}`;
  return element.name !== undefined ? `role=${element.role}[name=${JSON.stringify(element.name)}]`
    : `role=${element.role}`;
}

function routes(value: unknown): Record<string, RequirementRoute> | null {
  if (!isObject(value)) return null;
  return Object.fromEntries(Object.entries(value).filter(([, item]) => isObject(item) &&
    (item.route === "contract" || item.route === "unobservable" ||
      item.route === "component" && seam(item.seam) !== undefined ||
      item.route === "browser" && page(item.page) !== undefined) && typeof item.reason === "string" &&
    (item.source === "routing" || item.source === "tests"))
    .map(([requirement, item]) => [requirement, {
      ...item as RequirementRoute, seam: seam((item as RequirementRoute).seam),
      page: page((item as RequirementRoute).page),
      suggestion: typeof (item as { suggestion?: unknown }).suggestion === "string"
        ? (item as RequirementRoute).suggestion || undefined : undefined,
      covered_by: strings((item as RequirementRoute).covered_by).length > 0
        ? strings((item as RequirementRoute).covered_by) : undefined,
    }]));
}

/** The message that rephrases the unobservable criteria as the routing proposed, for a
 * fork of the interview; null when it proposed nothing. */
function rephrasing(routing: Record<string, RequirementRoute> | null): string | null {
  const lines = Object.entries(routing ?? {}).filter(([, item]) => item.route === "unobservable")
    .map(([requirement, item]) => item.covered_by?.length
      ? `Remove ${requirement}: ${item.covered_by.join(", ")} already ${item.covered_by.length === 1 ? "proves" : "prove"} what can be observed of it.`
      : item.suggestion ? `Replace ${requirement} with: ${item.suggestion}` : null)
    .filter((line): line is string => line !== null);
  return lines.length > 0
    ? `No test can observe some acceptance criteria as they are written.\n${lines.join("\n")}`
    : null;
}

function reviewRounds(value: unknown): ReviewRound[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => isObject(item) && typeof item.round === "number").map((item) => ({
    round: item.round as number,
    findings: Array.isArray(item.findings) ? item.findings.filter((finding: unknown): finding is ReviewFinding =>
      isObject(finding) && typeof finding.summary === "string" && typeof finding.reason === "string" &&
      ["implementation", "tests", "both"].includes(String(finding.target)) &&
      (finding.path === undefined || typeof finding.path === "string")) : [],
    ...(typeof item.instructions === "string" ? { instructions: item.instructions } : {}),
  }));
}

function verificationRecord(value: unknown): VerificationResult | null {
  if (!isObject(value)) return null;
  if (
    !["passed", "failed", "error", "not_implemented"].includes(String(value.status)) ||
    !["tests", "passed", "failures", "errors", "skipped", "duration_seconds"].every((key) => {
      const count = value[key];
      return typeof count === "number" && Number.isFinite(count) && count >= 0;
    }) ||
    !Array.isArray(value.test_failures) ||
    !value.test_failures.every((item) => isObject(item) &&
      ["class_name", "name", "kind", "message"].every((key) => typeof item[key] === "string")) ||
    typeof value.details_truncated !== "boolean" ||
    typeof value.service_log !== "string" || typeof value.runner_log !== "string" ||
    !(value.error === null || typeof value.error === "string")
  ) throw new Error("The backend returned invalid verification results.");
  const parts = isObject(value.parts) ? Object.fromEntries(Object.entries(value.parts).filter(
    ([, counts]) => isObject(counts) && ["tests", "passed", "failures", "errors", "skipped"].every(
      (key) => typeof counts[key] === "number" && Number.isFinite(counts[key]) && counts[key] >= 0)))
    : {};
  const { parts: _, ...record } = value;
  return { ...record, ...(Object.keys(parts).length > 0 ? { parts } : {}) } as VerificationResult;
}

function testSuite(value: unknown): TestSuite | null {
  return isObject(value) && typeof value.image_id === "string" ? {
    image_id: value.image_id, added_tests: strings(value.added_tests),
    changed_tests: strings(value.changed_tests), removed_tests: strings(value.removed_tests),
  } : null;
}

function imageArtifact(value: unknown): ImageArtifact | null {
  return isObject(value) && typeof value.image_id === "string" ? { image_id: value.image_id } : null;
}

function providerWaits(value: unknown): ProviderWait[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => isObject(item) && typeof item.workflow_id === "string" &&
    typeof item.stage === "string" && typeof item.until === "string").map((item) => ({
    workflow_id: item.workflow_id, stage: item.stage, until: item.until,
    message: typeof item.message === "string" ? item.message : null,
  }));
}

const checksStatuses: ChecksRun["status"][] = ["passed", "failed", "missing", "not_applicable", "error"];

function checksRun(value: unknown): ChecksRun | null {
  if (!isObject(value) || !checksStatuses.includes(value.status as ChecksRun["status"])) return null;
  const analysis = isObject(value.analysis) && typeof value.analysis.summary === "string"
    ? {
      summary: value.analysis.summary,
      files: Array.isArray(value.analysis.files)
        ? value.analysis.files.filter((item): item is string => typeof item === "string") : [],
    } : null;
  return {
    status: value.status as ChecksRun["status"],
    exit_code: typeof value.exit_code === "number" ? value.exit_code : null,
    excerpt: typeof value.excerpt === "string" ? value.excerpt : "",
    error: typeof value.error === "string" ? value.error : null,
    analysis,
  };
}

function agentQuestions(value: unknown): AgentQuestions[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => isObject(item) && typeof item.workflow_id === "string" &&
    typeof item.stage === "string").map((item) => ({
    workflow_id: item.workflow_id, stage: item.stage, questions: questionList(item.questions),
  })).filter((item) => item.questions.length > 0);
}

/** A pull request of `repository` only; any other link is dropped. */
function pullRequest(value: unknown, repository: RepositorySelection | null): PullRequest | null {
  if (!isObject(value) || !repository || typeof value.number !== "number" ||
      typeof value.title !== "string" || typeof value.url !== "string" ||
      typeof value.draft !== "boolean" || typeof value.base !== "string" ||
      value.url !== `https://github.com/${repository.full_name}/pull/${value.number}`) return null;
  return { number: value.number, title: value.title, url: value.url, draft: value.draft, base: value.base };
}

function text(value: unknown) {
  return typeof value === "string" ? value : null;
}

/** Reads the delivery fields of `value`; publications only ever link to `repository`. */
export function deliveryProgress(value: Record<string, unknown>, repository: RepositorySelection | null): DeliveryProgress {
  return {
    code:
      isObject(value.code) && isObject(value.code.server) && typeof value.code.server.path === "string"
        ? (value.code as GeneratedCode)
        : null,
    code_error: text(value.code_error),
    implementation: imageArtifact(value.implementation),
    implementation_error: text(value.implementation_error),
    tests: testSuite(value.tests),
    tests_error: text(value.tests_error),
    component_tests: testSuite(value.component_tests),
    component_tests_error: text(value.component_tests_error),
    documentation: isObject(value.documentation)
      ? { changed_files: strings(value.documentation.changed_files) } : null,
    documentation_error: text(value.documentation_error),
    routing_status: routingStatuses.includes(value.routing_status as RoutingStatus)
      ? value.routing_status as RoutingStatus : "not_applicable",
    routing: routes(value.routing),
    routing_error: text(value.routing_error),
    seam_status: seamStatuses.includes(value.seam_status as SeamStatus)
      ? value.seam_status as SeamStatus : "not_applicable",
    seam_error: text(value.seam_error),
    seam_reviews: reviewRounds(value.seam_reviews),
    baseline_status: baselineStatuses.includes(value.baseline_status as BaselineStatus)
      ? value.baseline_status as BaselineStatus : "not_applicable",
    baseline: verificationRecord(value.baseline),
    component_baseline: verificationRecord(value.component_baseline),
    baseline_error: text(value.baseline_error),
    test_reviews: reviewRounds(value.test_reviews),
    verification_status: verificationStatuses.includes(value.verification_status as VerificationStatus)
      ? value.verification_status as VerificationStatus : "not_applicable",
    verification: verificationRecord(value.verification),
    component_verification: verificationRecord(value.component_verification),
    verification_error: text(value.verification_error),
    repairs: repairRounds(value.repairs),
    publication: repositoryPublication(value.publication, repository),
    publication_error: text(value.publication_error),
    publication_status: publicationStatuses.includes(value.publication_status as PublicationStatus)
      ? value.publication_status as PublicationStatus : "not_applicable",
    agent_questions: agentQuestions(value.agent_questions),
    provider_waits: providerWaits(value.provider_waits),
    checks: checksRun(value.checks),
    reviews: reviewRounds(value.reviews),
    review_error: text(value.review_error),
    correction_failed: value.correction_failed === true,
    open_findings: value.open_findings === true,
    pull_request: pullRequest(value.pull_request, repository),
    pull_request_error: text(value.pull_request_error),
    pull_request_text_error: text(value.pull_request_text_error),
    pull_request_status: publicationStatuses.includes(value.pull_request_status as PublicationStatus)
      ? value.pull_request_status as PublicationStatus : "not_applicable",
    mentions: pullRequestMentions(value.mentions),
    existing_code_changed: value.existing_code_changed === true,
    ci: ciChecks(value.ci),
    fix_flow_ids: Array.isArray(value.fix_flow_ids)
      ? value.fix_flow_ids.filter((item): item is string => typeof item === "string" && item !== "")
      : [],
  };
}

/** What the page shows at once after answering an agent, until the next poll. */
export function answeredProgress(progress: DeliveryProgress, workflowId: string): DeliveryProgress {
  return { ...progress, agent_questions: progress.agent_questions.filter((item) => item.workflow_id !== workflowId) };
}

const agentLabels: Record<string, string> = {
  implementation: "The implementation agent",
  tests: "The test agent",
  documentation: "The documentation agent",
  diagnosis: "The reviewer of the failed tests",
  routing: "The router of the acceptance criteria",
};

/** Open questions of the coding agents; each waits, paused, for its own answers. */
function AgentQuestionsPanel({ progress, answering, onAnswer }: {
  progress: DeliveryProgress;
  answering: boolean;
  onAnswer: (workflowId: string, answers: string[]) => Promise<boolean>;
}) {
  if (progress.agent_questions.length === 0) return null;
  return (
    <section className="verification-results agent-questions" aria-labelledby="agent-questions-title">
      <h3 id="agent-questions-title">Questions for you</h3>
      {progress.agent_questions.map((item) => <div key={item.workflow_id}>
        <p role="status">
          {agentLabels[item.stage] ?? "An agent"} is paused until you answer. Only it sees your answers.
        </p>
        <QuestionForm questions={item.questions} disabled={answering} submitting={answering}
          onSubmit={(answers) => onAnswer(item.workflow_id, answers)} />
      </div>)}
    </section>
  );
}

/** Agents stopped by their model provider's limits; each goes on by itself. */
function ProviderWaitsPanel({ progress }: { progress: DeliveryProgress }) {
  if (progress.provider_waits.length === 0) return null;
  return (
    <section className="verification-results" aria-label="Waiting for the model provider">
      {progress.provider_waits.map((item) => <p role="status" key={item.workflow_id}>
        {agentLabels[item.stage] ?? "An agent"} waits for the model provider's limits and goes on
        by itself at {new Date(item.until).toLocaleString()}.
        {item.message && <span className="verification-builds"> The provider said: {item.message}</span>}
      </p>)}
    </section>
  );
}

/** The last run of the repository's checks script, when it says something. */
function ChecksResult({ checks }: { checks: DeliveryProgress["checks"] }) {
  if (!checks || checks.status === "not_applicable") return null;
  if (checks.status === "passed") return <p>The repository's checks script passes.</p>;
  if (checks.status === "missing") {
    return <p>The repository has no checks script (.sdlc/checks.sh), so only the contract tests verified it.</p>;
  }
  if (checks.status === "error") {
    return <p role="alert">{checks.error ?? "The repository's checks script could not be run."}</p>;
  }
  return (
    <div>
      <p role="alert">
        The repository's checks script failed (exit status {checks.exit_code ?? "unknown"})
        {checks.analysis?.summary ? `: ${checks.analysis.summary}` : "."}
      </p>
      {checks.analysis && checks.analysis.files.length > 0 &&
        <p>Files to look at: {checks.analysis.files.join(", ")}</p>}
      {checks.excerpt && <details>
        <summary>What the checks reported</summary>
        <pre>{checks.excerpt}</pre>
      </details>}
    </div>
  );
}

/** Whether the delivery is still working, so its session should be polled again. */
export function deliveryPending(progress: DeliveryProgress) {
  return ["waiting", "running", "checking", "diagnosing", "repairing", "reviewing"].includes(progress.verification_status) ||
    ["waiting", "running"].includes(progress.publication_status) ||
    ["waiting", "running"].includes(progress.pull_request_status);
}

/** What the page shows at once after deciding about open review findings, until the next
 * poll: accepted, or a fix round from the user's instructions. */
export function decidedProgress(progress: DeliveryProgress, instructions: string | null): DeliveryProgress {
  return instructions === null ? { ...progress, open_findings: false } : {
    ...progress, review_error: null, correction_failed: false, verification_status: "repairing",
  };
}

/** What the page shows at once after asking to retry a stage, until the next poll. */
export function retriedProgress(progress: DeliveryProgress, stage: GenerationStage, checkedOut: boolean): DeliveryProgress {
  const change = progress.baseline_status !== "not_applicable";
  if (stage === "review") {
    // The verified delivery is reviewed again, or the failed correction goes on.
    return {
      ...progress,
      review_error: null,
      correction_failed: false,
      verification_status: progress.correction_failed ? "repairing" : "reviewing",
      ...(checkedOut && !progress.open_findings
        ? { publication_error: null, publication_status: "waiting" as const,
            pull_request_error: null, pull_request_status: "waiting" as const } : {}),
    };
  }
  return {
    ...progress,
    correction_failed: false,
    ...(stage === "server"
      ? { code_error: null, implementation: null, implementation_error: null }
      : stage === "tests" ? { tests: null, tests_error: null, component_tests: null, component_tests_error: null }
      : stage === "documentation" ? { documentation: null, documentation_error: null } : {}),
    // A seam that was not added, or whose baseline proved nothing, is added again.
    ...(stage === "tests" && change && (progress.seam_status === "error" ||
      progress.routing_status !== "routed" || ["not_reproduced", "error"].includes(progress.baseline_status))
      ? { seam_status: "not_applicable" as const, seam_error: null, seam_reviews: [] } : {}),
    ...(stage === "tests" && change
      ? { baseline: null, component_baseline: null, baseline_error: null, baseline_status: "waiting" as const,
          routing_error: null, test_reviews: [] } : {}),
    // The criteria are routed again unless every one had a suite that observes it.
    ...(stage === "tests" && change && progress.routing_status !== "routed" &&
      progress.routing_status !== "not_applicable"
      ? { routing: null, routing_status: "waiting" as const } : {}),
    // A repeated push keeps the verification it publishes.
    ...(stage === "repository" ? {} : {
      review_error: null,
      verification: null,
      component_verification: null,
      verification_error: null,
      verification_status: "waiting" as const,
      repairs: [],
    }),
    ...(checkedOut ? {
      publication_error: null, publication_status: "waiting" as const,
      pull_request_error: null, pull_request_status: "waiting" as const,
    } : {}),
  };
}

type TestFailure = VerificationResult["test_failures"][number];

function Failures({ failures }: { failures: TestFailure[] }) {
  return failures.length > 0 ? <ul className="verification-failures">
    {failures.map((item, index) => <li key={index}>
      <strong>{item.class_name}{item.class_name && "."}{item.name}</strong>
      <span>{item.kind === "error" ? "Test error" : "Assertion failed"}</span>
      <pre>{item.message || "No failure message was reported."}</pre>
    </li>)}
  </ul> : null;
}

/** The part of the repository a component test failure belongs to: a test of part P is
 * reported with class names starting `P:`, the service's without a prefix. */
function failurePart(item: TestFailure, parts: string[]): string {
  return parts.filter((part) => part !== "service" && item.class_name.startsWith(`${part}:`))
    .sort((a, b) => b.length - a.length)[0] ?? "service";
}

/** How a member of a component run is named: the service, a part, or a part's browser tests
 * (`frontend~browser`). */
function partTitle(part: string): ReactNode {
  if (part === "service") return "Service";
  return part.endsWith("~browser")
    ? <>Part <code>{part.slice(0, -"~browser".length)}</code> (browser)</>
    : <>Part <code>{part}</code></>;
}

/** A component run's tests of each part of the repository, with that part's failures. */
function PartRuns({ result }: { result: VerificationResult }) {
  const parts = Object.keys(result.parts ?? {})
    .sort((a, b) => Number(a !== "service") - Number(b !== "service") || a.localeCompare(b));
  return <>{parts.map((part) => {
    const counts = result.parts![part];
    const failures = result.test_failures.filter((item) => failurePart(item, parts) === part)
      .map((item) => part === "service" ? item
        : { ...item, class_name: item.class_name.slice(part.length + 1) });
    return <div key={part} className="part-run">
      <h5>{partTitle(part)}</h5>
      <p className="verification-counts">
        {counts.tests} tests · {counts.passed} passed · {counts.failures} failed · {counts.errors} errors · {counts.skipped} skipped
      </p>
      <Failures failures={failures} />
    </div>;
  })}</>;
}

function TestRun({ result, children }: { result: VerificationResult; children?: ReactNode }) {
  return <>
    <p className="verification-counts">
      {result.tests} tests · {result.passed} passed · {result.failures} failed · {result.errors} errors · {result.skipped} skipped
      {" · "}{result.duration_seconds.toFixed(1)}s
    </p>
    {result.error && <p role="alert">{result.error}</p>}
    {result.parts ? <PartRuns result={result} /> : <Failures failures={result.test_failures} />}
    {children}
    {result.details_truncated && <p>Showing the first {result.test_failures.length} failures. Full reports are saved with the verification artifacts.</p>}
    {(result.service_log || result.runner_log) && <details className="verification-logs">
      <summary>Container logs (last 200 lines, up to 8,000 characters each)</summary>
      {result.service_log && <><h4>Service</h4><pre>{result.service_log}</pre></>}
      {result.runner_log && <><h4>Test runner</h4><pre>{result.runner_log}</pre></>}
    </details>}
  </>;
}

/** The runs of a change's suites: its contract tests, its component tests, or both. Each
 * is named by its suite when the change has component tests. */
function SuiteRuns({ contract, component, outcome, children }: {
  contract: VerificationResult | null;
  component: VerificationResult | null;
  // The line above a run's counts, by its suite's name; none above a baseline's.
  outcome?: (result: VerificationResult, suite: string) => ReactNode;
  children?: ReactNode;
}) {
  const runs = [
    ...(contract ? [{ suite: "Contract tests", result: contract }] : []),
    ...(component ? [{ suite: "Component tests", result: component }] : []),
  ];
  const named = component !== null;
  return <>
    {runs.map(({ suite, result }, index) => <div key={suite}>
      {named && !outcome && <h4>{suite}</h4>}
      {outcome?.(result, named ? suite : "Tests")}
      <TestRun result={result}>{index === runs.length - 1 && children}</TestRun>
    </div>)}
    {runs.length === 0 && children}
  </>;
}

/** Which suite can prove each acceptance criterion of a change, decided before its tests. */
function Routing({ progress, retrying, disabled, onRetry, onRephrase }: {
  progress: DeliveryProgress;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRephrase?: (message: string) => void;
  onRetry: (stage: GenerationStage) => void;
}) {
  const stage = progress.routing_status;
  const entries = Object.entries(progress.routing ?? {});
  const proposal = rephrasing(progress.routing);
  return (
    <section className="verification-results" aria-labelledby="routing-title">
      <h3 id="routing-title">Routing</h3>
      {stage === "waiting" && <p role="status">Waiting to route the acceptance criteria.</p>}
      {stage === "running" && <p role="status">
        Deciding which tests can prove each acceptance criterion…
      </p>}
      {stage === "routed" && <p className="verification-outcome verification-passed" role="status">
        {entries.some(([, item]) => item.route === "browser")
          ? "Every acceptance criterion can be proved by the contract, component or browser tests"
          : entries.some(([, item]) => item.route === "component")
            ? "Every acceptance criterion can be proved by the contract or component tests"
            : "Every acceptance criterion can be proved by the contract tests"}
      </p>}
      {stage === "unobservable" && <p className="verification-outcome verification-failed" role="status">
        Some acceptance criteria cannot be proved by any test
      </p>}
      {stage === "unsupported" && <p className="verification-outcome verification-failed" role="status">
        Some acceptance criteria were routed to browser tests, which this run cannot run
      </p>}
      {stage === "error" && <p className="verification-outcome verification-error" role="status">
        The routing did not complete
      </p>}
      {entries.length > 0 && <ul className="verification-failures">
        {entries.map(([requirement, item]) => <li key={requirement}>
          <strong>{requirement}: {item.route === "contract" ? "contract tests"
            : item.route === "component" ? (item.seam?.part ? `component tests of part ${item.seam.part}` : "component tests")
            : item.route === "browser" ? `browser tests of part ${item.page?.part ?? ""}`
            : "cannot be observed"}</strong>
          <span>{item.reason}{item.source === "tests" ? " (found by the test author)" : ""}</span>
          {item.covered_by && <span>
            Already proved by {item.covered_by.join(", ")} as far as any test can: it can be removed</span>}
          {!item.covered_by && item.suggestion && <span>Proposed instead: {item.suggestion}</span>}
          {item.seam && <span>Seam: <code>{item.seam.part ? `${item.seam.part}: ` : ""}{item.seam.module}</code>
            {item.seam.symbols.length > 0 && <> ({item.seam.symbols.map((symbol, index) =>
              <span key={symbol}>{index > 0 && ", "}<code>{symbol}</code></span>)})</>}</span>}
          {item.seam && item.seam.added.length > 0 && <span>
            Added by the change first: {item.seam.added.map((symbol, index) =>
              <span key={symbol}>{index > 0 && ", "}<code>{symbol}</code></span>)}</span>}
          {item.page && <span>Page: <code>{item.page.part}: {item.page.entry}</code>
            {item.page.viewports.length > 0 && <> at {item.page.viewports.map((name, index) =>
              <span key={name}>{index > 0 && ", "}<code>{name}</code></span>)}</>}</span>}
          {item.page && item.page.added.length > 0 && <span>
            Added by the change first: {item.page.added.map((element, index) =>
              <span key={elementLabel(element)}>{index > 0 && ", "}<code>{elementLabel(element)}</code></span>)}</span>}
        </li>)}
      </ul>}
      {progress.seam_status === "running" && <p role="status">
        Adding the seam the acceptance criteria are observed at, without changing behaviour…
      </p>}
      {progress.seam_status === "prepared" && <p className="verification-outcome verification-passed" role="status">
        The seam was added and matches its component contract; the baseline runs against the code with it
      </p>}
      <StageReview name="Seam review" rounds={progress.seam_reviews} />
      {progress.seam_error && <div className="generation-failure">
        <p role="alert">{progress.seam_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {retrying === "tests" ? "Adding the seam again…" : "Add the seam again"}
        </button>
      </div>}
      {progress.routing_error && <div className="generation-failure">
        <p role="alert">{progress.routing_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {retrying === "tests" ? "Routing the criteria again…" : "Route the criteria again"}
        </button>
        {stage === "unobservable" && onRephrase && proposal && <button type="button"
          className="generate-button" disabled={disabled} onClick={() => onRephrase(proposal)}>
          Take the proposals to a fork of the interview
        </button>}
      </div>}
    </section>
  );
}

/** The review rounds of a stage before the fix: the seam a change adds, or its regression
 * tests. Each round's new findings go back to their author, unless it is the last; those
 * of the last round go on with the delivery, whose code review sees them again. */
function StageReview({ name, rounds }: { name: string; rounds: ReviewRound[] }) {
  const last = rounds[rounds.length - 1];
  if (!last) return null;
  const corrected = rounds.slice(0, -1).reduce((count, item) => count + item.findings.length, 0);
  return <>
    <p role="status">
      {name}, round {last.round}: {last.findings.length === 0 ? "no findings" :
        `${last.findings.length} finding${last.findings.length === 1 ? "" : "s"}`}
      {corrected > 0 && ` (${corrected} sent for correction in earlier rounds)`}
    </p>
    {last.findings.length > 0 && <ul className="verification-failures">
      {last.findings.map((item, index) => <li key={index}>
        <strong>{item.path ?? "General"}{item.repeats !== undefined && ", reported again after a correction"}</strong>
        <span>{item.summary}</span>
      </li>)}
    </ul>}
  </>;
}

/** The baseline run of a change: its added tests against the unchanged service. */
function Baseline({ progress, retrying, disabled, onRetry }: {
  progress: DeliveryProgress;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
}) {
  const stage = progress.baseline_status;
  const suite = progress.tests;
  const components = progress.component_tests;
  return (
    <section className="verification-results" aria-labelledby="baseline-title">
      <h3 id="baseline-title">Baseline</h3>
      {stage === "waiting" && <p role="status">
        {["waiting", "running"].includes(progress.routing_status) ? "Waiting for the routing of the acceptance criteria."
          : progress.tests_error ? "Waiting for regression tests." : "Writing regression tests for the brief…"}
      </p>}
      {stage === "running" && <p role="status">
        {progress.seam_status === "prepared"
          ? "Running the regression tests against the code with only the seam added…"
          : "Running the regression tests against the service as the repository holds it…"}
      </p>}
      {stage === "skipped" && <p>
        {progress.routing_error
          ? "The baseline was skipped because the acceptance criteria could not all be routed to tests."
          : progress.seam_error
          ? "The baseline was skipped because the seam the criteria are observed at could not be added."
          : progress.baseline_error
          ? "The baseline was skipped."
          : "The baseline was skipped because the regression tests were not written."}
      </p>}
      {stage === "reproduced" && <p className="verification-outcome verification-passed" role="status">
        {components
          ? "Reproduced: the added tests of both suites fail against the unchanged code and the existing tests pass"
          : "Reproduced: the added tests fail against the unchanged service and the existing tests pass"}
      </p>}
      {suite && suite.added_tests.length > 0 && <p>Added tests: {suite.added_tests.join(", ")}</p>}
      {components && components.added_tests.length > 0 && <p>
        Added component tests: {components.added_tests.join(", ")}
      </p>}
      {suite && suite.changed_tests.length > 0 && <p>
        Existing tests corrected by a repair: {suite.changed_tests.join(", ")}
      </p>}
      {suite && suite.removed_tests.length > 0 && <p>
        Existing tests removed by a repair: {suite.removed_tests.join(", ")}
      </p>}
      {stage === "not_reproduced" && <p className="verification-outcome verification-failed" role="status">
        Not reproduced
      </p>}
      {stage === "error" && <p className="verification-outcome verification-error" role="status">
        The baseline did not hold
      </p>}
      {progress.baseline_error && <div className="generation-failure">
        <p role="alert">{progress.baseline_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {retrying === "tests" ? "Writing the regression tests again…" : "Write the regression tests again"}
        </button>
      </div>}
      <StageReview name="Regression test review" rounds={progress.test_reviews} />
      {stage !== "reproduced" &&
        <SuiteRuns contract={progress.baseline} component={progress.component_baseline} />}
    </section>
  );
}

/** The branch a delivery pushed, or why it pushed none. */
function Publication({ progress, documentation, retrying, disabled, onRetry }: {
  progress: DeliveryProgress;
  documentation: boolean;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
}) {
  const publication = progress.publication;
  // A change with component tests verified before such changes were code-reviewed was
  // never reviewed, so it is not pushed as it is; the error says so. One that was reviewed
  // is pushed by retrying the push.
  const unpublishable = progress.component_tests !== null && progress.publication_status === "skipped" &&
    progress.reviews.length === 0 && Boolean(progress.publication_error);
  if (progress.publication_status === "not_applicable") return null;
  return <>
    <h3>Repository branch</h3>
    {progress.publication_status === "waiting" && <p>{documentation
      ? "The documentation is pushed to a new branch once it is written."
      : "Verified sources are pushed to a new branch once the tests pass."}</p>}
    {progress.publication_status === "running" && <p role="status">{documentation
      ? "Pushing the documentation to a new branch…" : "Pushing the verified sources to a new branch…"}</p>}
    {progress.publication_status === "skipped" && !progress.publication_error && <p>{documentation
      ? "Nothing was pushed because the documentation was not completed."
      : "Nothing was pushed because verification did not pass."}</p>}
    {publication && <p className="verification-outcome verification-passed" role="status">
      Pushed to <a href={publication.url} target="_blank" rel="noreferrer">{publication.branch}</a>
      {" "}at <code>{publication.commit.slice(0, 7)}</code>
    </p>}
    {publication && !publication.verified && <p>
      This branch is not verified: nothing checked the change. Review it before merging.
    </p>}
    {publication && publication.ignored_count > 0 && <p>
      Not committed because the repository's .gitignore matches them ({publication.ignored_count}):
      {" "}{publication.ignored.join(", ")}
      {publication.ignored_count > publication.ignored.length && ", …"}
    </p>}
    {progress.publication_error && <div className="generation-failure">
      <p role="alert">{progress.publication_error}</p>
      {!unpublishable && <button type="button" className="generate-button" disabled={disabled}
        onClick={() => onRetry("repository")}>
        {retrying === "repository" ? "Pushing again…" : "Retry push"}
      </button>}
    </div>}
    {progress.pull_request_status === "running" && <p role="status">Opening or updating the pull request…</p>}
    {progress.pull_request && <p className="verification-outcome verification-passed" role="status">
      {progress.pull_request.draft ? "Draft pull request" : "Pull request"}{" "}
      <a href={progress.pull_request.url} target="_blank" rel="noreferrer">
        #{progress.pull_request.number} {progress.pull_request.title}
      </a>{" "}into <code>{progress.pull_request.base}</code>
    </p>}
    {progress.pull_request && progress.ci && <p
      className={`verification-outcome ${progress.ci.status === "passed" ? "verification-passed" : progress.ci.status === "failed" ? "verification-failed" : ""}`}
      role="status">
      {ciText[progress.ci.status]} <code>{progress.ci.commit.slice(0, 12)}</code>
      {progress.ci.fix_round > 0 && <> (pushed by automatic fix round {progress.ci.fix_round})</>}
      {progress.ci.failed.length > 0 && <> Failed:{" "}
        {progress.ci.failed.map((check, index) => <span key={index}>
          {index > 0 && ", "}
          {check.url ? <a href={check.url} target="_blank" rel="noreferrer">{check.name}</a> : check.name}
        </span>)}
      </>}
      {progress.ci.error && <> {progress.ci.error}</>}
    </p>}
    {progress.pull_request_error && <div className="generation-failure">
      <p role="alert">{progress.pull_request_error} The push itself succeeded.</p>
      {!progress.publication_error && <button type="button" className="generate-button" disabled={disabled}
        onClick={() => onRetry("repository")}>
        {retrying === "repository" ? "Pushing again…" : "Push again and retry the pull request"}
      </button>}
    </div>}
    {progress.pull_request_text_error && !progress.pull_request_error && <div className="generation-failure">
      <p role="alert">{progress.pull_request_text_error}</p>
      {!progress.publication_error && <button type="button" className="generate-button" disabled={disabled}
        onClick={() => onRetry("repository")}>
        {retrying === "repository" ? "Pushing again…" : "Push again and rewrite the pull request text"}
      </button>}
    </div>}
    {progress.mentions.length > 0 && <section aria-label="Mentions in the pull request">
      <h3>Mentions in the pull request</h3>
      <ul>
        {progress.mentions.map((mention) => <li key={`${mention.comment_type}-${mention.comment_id}`}>
          <a href={mention.url} target="_blank" rel="noreferrer">{mention.author}</a>{" "}
          ({mention.comment_type === "review" ? "inline review comment" : "comment"}):{" "}
          {mention.classification === "pending" ? "being classified" : mention.classification}, {mention.status}
          {mention.round !== null && <> — review round {mention.round}</>}
          {mention.reply_url && <> — <a href={mention.reply_url} target="_blank" rel="noreferrer">reply</a></>}
          {mention.error && <p role="alert">{mention.error}</p>}
        </li>)}
      </ul>
    </section>}
    {progress.fix_flow_ids.length > 0 && <section aria-label="Fix rounds after the flow closed">
      <h3>Fix rounds after the flow closed</h3>
      <p>Each runs as a new flow of its own, pushed to a new branch with a new pull request.</p>
      <ul>
        {progress.fix_flow_ids.map((id) => <li key={id}>
          <a href={flowStepsLink(id)}><code>{id}</code></a>
        </li>)}
      </ul>
    </section>}
  </>;
}

const findingTargets: Record<ReviewFinding["target"], string> = {
  implementation: "implementation", tests: "tests", both: "implementation and tests",
};

/** The code review rounds of a verified delivery, and what the user may do when they end
 * without passing: run a failed review or correction again, or decide about open findings. */
function CodeReview({ progress, retrying, disabled, onRetry, onFindings }: {
  progress: DeliveryProgress;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
  onFindings: (instructions: string | null) => Promise<boolean>;
}) {
  const [instructions, setInstructions] = useState("");
  const last = progress.reviews[progress.reviews.length - 1];
  const stage = progress.verification_status;
  if (!last && !progress.review_error && stage !== "reviewing") return null;
  const open = progress.open_findings || progress.correction_failed;
  return <>
    <h3>Code review</h3>
    {stage === "reviewing" && <p role="status">Reviewing the changes…</p>}
    {last && <p>
      Round {last.round}: {last.findings.length === 0 ? "no findings" :
        `${last.findings.length} finding${last.findings.length === 1 ? "" : "s"}`}
    </p>}
    {last && last.findings.length > 0 && (open || stage === "repairing") && <ul className="verification-failures">
      {last.findings.map((item, index) => <li key={index}>
        <strong>{item.path ?? "General"} ({findingTargets[item.target]}){item.repeats !== undefined && ", reported again after a correction"}</strong>
        <span>{item.summary}</span>
        {typeof item.unresolved === "string" && <span>Left open: {item.unresolved}</span>}
      </li>)}
    </ul>}
    {progress.review_error && <div className="generation-failure">
      <p role="alert">{progress.review_error}</p>
      <button type="button" className="generate-button" disabled={disabled} onClick={() => onRetry("review")}>
        {retrying === "review" ? "Reviewing again…" : "Run the code review again"}
      </button>
    </div>}
    {progress.correction_failed && <div className="generation-failure">
      <p role="alert">The corrections of these findings did not pass verification.</p>
      <button type="button" className="generate-button" disabled={disabled} onClick={() => onRetry("review")}>
        {retrying === "review" ? "Going on…" : "Retry the correction and verify again"}
      </button>
    </div>}
    {progress.open_findings && <form className="generation-failure" onSubmit={(event) => {
      event.preventDefault();
      void onFindings(instructions.trim()).then((sent) => { if (sent) setInstructions(""); });
    }}>
      <p role="alert">These findings remained after the last review round, so the branch was pushed as unverified.</p>
      <label htmlFor="findings-instructions">How should they be fixed?</label>
      <textarea id="findings-instructions" rows={3} maxLength={10000} value={instructions}
        disabled={disabled} onChange={(event) => setInstructions(event.target.value)} />
      <button type="submit" className="generate-button" disabled={disabled || !instructions.trim()}>
        Fix them with these instructions
      </button>
      <button type="button" className="generate-button" disabled={disabled}
        onClick={() => void onFindings(null)}>
        Accept the branch as it is
      </button>
    </form>}
  </>;
}

/** A documentation task: one author limited to documentation files, and no check yet. */
function DocumentationResults({ progress, retrying, disabled, onRetry }: {
  progress: DeliveryProgress;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
}) {
  const result = progress.verification;
  const stage = progress.verification_status;
  const written = progress.documentation;
  // A check that could not complete is retried by writing the documentation again.
  const failed = progress.documentation_error ?? progress.verification_error ??
    (result?.status === "error" ? result.error ?? "Documentation verification could not complete." : null);
  return (
    <section className="verification-results" aria-labelledby="documentation-title">
      <h3 id="documentation-title">Documentation</h3>
      {stage === "waiting" && !failed && <p role="status">Writing the documentation…</p>}
      {stage === "running" && <p role="status">Checking the documentation…</p>}
      {written && <p>Changed files: {written.changed_files.join(", ")}</p>}
      {failed && <div className="generation-failure">
        <p role="alert">{failed}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("documentation")}>
          {retrying === "documentation" ? "Writing the documentation again…" : "Write the documentation again"}
        </button>
      </div>}
      {result?.status === "not_implemented" && <p className="verification-outcome verification-error" role="status">
        Not verified: documentation changes are not checked yet
      </p>}
      {result?.status === "passed" && <p className="verification-outcome verification-passed" role="status">
        Documentation verified
      </p>}
      {result?.status === "failed" && <p className="verification-outcome verification-failed" role="status">
        Documentation did not pass verification
      </p>}
      <Publication progress={progress} documentation retrying={retrying} disabled={disabled} onRetry={onRetry} />
    </section>
  );
}

export function DeliveryResults({ progress, kind = null, documentation = false, onboarding = false, retrying, disabled, onRetry, answering, onAnswer, onFindings, onRephrase }: {
  progress: DeliveryProgress;
  // The kind of task the session delivers, when it records one.
  kind?: TaskKind | null;
  // Whether the session's task changes documentation only.
  documentation?: boolean;
  // Whether this is an onboarding: the existing code is built with a new Dockerfile, no
  // server code is generated.
  onboarding?: boolean;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
  // Whether answers to an agent are being sent.
  answering: boolean;
  onAnswer: (workflowId: string, answers: string[]) => Promise<boolean>;
  // The user's decision about open review findings: instructions, or null to accept them.
  onFindings: (instructions: string | null) => Promise<boolean>;
  // Opens a fork of the interview with the message, when the session has an interview.
  onRephrase?: (message: string) => void;
}) {
  const questions = <>
    <AgentQuestionsPanel progress={progress} answering={answering} onAnswer={onAnswer} />
    <ProviderWaitsPanel progress={progress} />
  </>;
  if (documentation) {
    return <>
      {questions}
      <DocumentationResults progress={progress} retrying={retrying} disabled={disabled} onRetry={onRetry} />
    </>;
  }
  const result = progress.verification;
  const stage = progress.verification_status;
  const pending = stage === "waiting" || stage === "running";
  const repairing = progress.repairs[progress.repairs.length - 1];
  // A bugfix generates no code and has a baseline instead.
  const change = kind === "bugfix" || progress.baseline_status !== "not_applicable";
  // A change, a bugfix or an API change, is published only with a verification that
  // proved all of it. When it proved nothing, the change goes on by retrying its tests,
  // unless a failed stage above already offers that; an API change needs its code first.
  const proved = change || kind === "api_change";
  const unverified = proved && !progress.tests_error && !progress.baseline_error &&
    (change || Boolean(progress.code)) &&
    (Boolean(progress.verification_error) || result?.status === "error");
  const service = progress.implementation ? "built" : progress.implementation_error ? "build failed" : "building";
  const repairs = progress.repairs.length > 0 && <ol className="verification-failures">
    {progress.repairs.map((round) => <li key={round.attempt}>
      <strong>Repair round {round.attempt}: {round.target === "tests" && round.suite
        ? `${round.suite} tests` : round.target} corrected</strong>
      {round.tests.length > 0 && <span>
        Tests found broken: {round.tests.map((test) => `${test.class_name}.${test.name}`).join(", ")}
      </span>}
      {round.summary && <span>{round.summary}</span>}
    </li>)}
  </ol>;
  return <>
    {questions}
    {change && progress.routing_status !== "not_applicable" &&
      <Routing progress={progress} retrying={retrying} disabled={disabled} onRetry={onRetry}
        onRephrase={onRephrase} />}
    {change && <Baseline progress={progress} retrying={retrying} disabled={disabled} onRetry={onRetry} />}
    <section className="verification-results" aria-labelledby="verification-title">
      <h3 id="verification-title">Service verification</h3>
      {pending && (
        <div role="status">
          <p>{stage === "running" ? (progress.component_tests
              ? "Running the contract and component tests against the change…"
              : "Running contract tests against the service…") :
            onboarding ? `Building the existing code with the new Dockerfile: ${service} · Tests: ${progress.tests ? "compiled" : progress.tests_error ? "generation or build failed" : "generating and compiling"}…` :
            change ? (progress.baseline_status === "reproduced" ? `Changing the service: ${service}…` : "Waiting for the baseline…") :
            !progress.code ? "Waiting for server code…" : "Waiting for service and test builds…"}</p>
          {progress.code && <p className="verification-builds">
            Service: {service}
            {" · "}Tests: {progress.tests ? "compiled" : progress.tests_error ? "generation or build failed" : "generating and compiling"}
          </p>}
        </div>
      )}
      {stage === "checking" && <p role="status">
        The tests pass. Running the repository's checks script over the delivered code…
      </p>}
      <ChecksResult checks={progress.checks} />
      {stage === "diagnosing" && <p role="status">
        Tests failed. Deciding whether the implementation or the tests are wrong…
      </p>}
      {stage === "repairing" && repairing && <p role="status">
        Repair round {repairing.attempt}: correcting the {repairing.target}…
      </p>}
      {stage === "not_applicable" && <p>Verification is not available for this session.</p>}
      {onboarding && progress.existing_code_changed && <p role="status">
        The existing code was changed after you confirmed a bug in it; the commit message and
        the pull request description say so.
      </p>}
      {stage === "skipped" && <p>Verification skipped because a prerequisite did not complete.</p>}
      {progress.implementation_error && <div className="generation-failure">
        <p role="alert">{progress.implementation_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("server")}>
          {retrying === "server" ? "Restarting server generation…" : "Retry server generation"}
        </button>
      </div>}
      {progress.component_tests_error && <div className="generation-failure">
        <p role="alert">{progress.component_tests_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {retrying === "tests" ? "Restarting test generation…" : "Retry test generation"}
        </button>
      </div>}
      {progress.tests_error && <div className="generation-failure">
        <p role="alert">{progress.tests_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {retrying === "tests" ? "Restarting test generation…" : "Retry test generation"}
        </button>
      </div>}
      {progress.verification_error && <p role="alert">{progress.verification_error}</p>}
      {unverified && <div className="generation-failure">
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {change
            ? retrying === "tests" ? "Writing the regression tests again…" : "Write the regression tests again"
            : retrying === "tests" ? "Writing the tests again…" : "Write the tests again"}
        </button>
      </div>}
      {/* A change proved by its component tests alone has no contract test run. */}
      <SuiteRuns contract={result} component={progress.component_verification}
        outcome={(run, suite) => <p className={`verification-outcome verification-${run.status}`} role="status">
          {run.status === "passed" ? `${suite} passed` : run.status === "failed" ? `${suite} failed`
            : progress.component_tests ? `${suite} could not complete` : "Verification could not complete"}
        </p>}>{repairs}</SuiteRuns>
      <CodeReview progress={progress} retrying={retrying} disabled={disabled} onRetry={onRetry}
        onFindings={onFindings} />
      <Publication progress={progress} documentation={false} retrying={retrying} disabled={disabled} onRetry={onRetry} />
    </section>
  </>;
}
