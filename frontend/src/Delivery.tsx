import type { ReactNode } from "react";
import { repositoryPublication, type RepositoryPublication, type RepositorySelection } from "./github";

type GeneratedCode = {
  server: { path: string; files: number };
};

type ImageArtifact = { image_id: string };
// The test suite's image and, for a change, how its tests differ from the repository's.
type TestSuite = ImageArtifact & { added_tests: string[]; changed_tests: string[]; removed_tests: string[] };
type TestRef = { class_name: string; name: string };
type VerificationStatus = "not_applicable" | "waiting" | "running" | "diagnosing" | "repairing" |
  "completed" | "skipped" | "error";
// How the tests a change added fared against the service as the repository holds it.
type BaselineStatus = "not_applicable" | "waiting" | "running" | "reproduced" | "not_reproduced" |
  "skipped" | "error";
// `tests` are the failing tests the diagnosis found broken: the only ones a repair may change.
type RepairRound = { attempt: number; target: "implementation" | "tests"; tests: TestRef[]; summary: string };
type VerificationResult = {
  status: "passed" | "failed" | "error";
  tests: number;
  passed: number;
  failures: number;
  errors: number;
  skipped: number;
  duration_seconds: number;
  test_failures: { class_name: string; name: string; kind: string; message: string }[];
  details_truncated: boolean;
  service_log: string;
  runner_log: string;
  error: string | null;
};

export type GenerationStage = "server" | "tests" | "repository";
type PublicationStatus = "not_applicable" | "waiting" | "running" | "completed" | "skipped" | "error";

/** What a session's delivery has produced so far, as the session API reports it. */
export type DeliveryProgress = {
  code: GeneratedCode | null;
  code_error: string | null;
  implementation: ImageArtifact | null;
  implementation_error: string | null;
  tests: TestSuite | null;
  tests_error: string | null;
  baseline_status: BaselineStatus;
  baseline: VerificationResult | null;
  baseline_error: string | null;
  verification_status: VerificationStatus;
  verification: VerificationResult | null;
  verification_error: string | null;
  repairs: RepairRound[];
  publication: RepositoryPublication | null;
  publication_error: string | null;
  publication_status: PublicationStatus;
};

const verificationStatuses: VerificationStatus[] = [
  "not_applicable", "waiting", "running", "diagnosing", "repairing", "completed", "skipped", "error",
];
const baselineStatuses: BaselineStatus[] = [
  "not_applicable", "waiting", "running", "reproduced", "not_reproduced", "skipped", "error",
];
const publicationStatuses: PublicationStatus[] = [
  "not_applicable", "waiting", "running", "completed", "skipped", "error",
];

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

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
  }) as RepairRound);
}

function verificationRecord(value: unknown): VerificationResult | null {
  if (!isObject(value)) return null;
  if (
    !["passed", "failed", "error"].includes(String(value.status)) ||
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
  return value as VerificationResult;
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
    baseline_status: baselineStatuses.includes(value.baseline_status as BaselineStatus)
      ? value.baseline_status as BaselineStatus : "not_applicable",
    baseline: verificationRecord(value.baseline),
    baseline_error: text(value.baseline_error),
    verification_status: verificationStatuses.includes(value.verification_status as VerificationStatus)
      ? value.verification_status as VerificationStatus : "not_applicable",
    verification: verificationRecord(value.verification),
    verification_error: text(value.verification_error),
    repairs: repairRounds(value.repairs),
    publication: repositoryPublication(value.publication, repository),
    publication_error: text(value.publication_error),
    publication_status: publicationStatuses.includes(value.publication_status as PublicationStatus)
      ? value.publication_status as PublicationStatus : "not_applicable",
  };
}

/** Whether the delivery is still working, so its session should be polled again. */
export function deliveryPending(progress: DeliveryProgress) {
  return ["waiting", "running", "diagnosing", "repairing"].includes(progress.verification_status) ||
    ["waiting", "running"].includes(progress.publication_status);
}

/** What the page shows at once after asking to retry a stage, until the next poll. */
export function retriedProgress(progress: DeliveryProgress, stage: GenerationStage, checkedOut: boolean): DeliveryProgress {
  const change = progress.baseline_status !== "not_applicable";
  return {
    ...progress,
    ...(stage === "server"
      ? { code_error: null, implementation: null, implementation_error: null }
      : stage === "tests" ? { tests: null, tests_error: null } : {}),
    ...(stage === "tests" && change
      ? { baseline: null, baseline_error: null, baseline_status: "waiting" as const } : {}),
    // A repeated push keeps the verification it publishes.
    ...(stage === "repository" ? {} : {
      verification: null,
      verification_error: null,
      verification_status: "waiting" as const,
      repairs: [],
    }),
    ...(checkedOut ? { publication_error: null, publication_status: "waiting" as const } : {}),
  };
}

function TestRun({ result, children }: { result: VerificationResult; children?: ReactNode }) {
  return <>
    <p className="verification-counts">
      {result.tests} tests · {result.passed} passed · {result.failures} failed · {result.errors} errors · {result.skipped} skipped
      {" · "}{result.duration_seconds.toFixed(1)}s
    </p>
    {result.error && <p role="alert">{result.error}</p>}
    {result.test_failures.length > 0 && <ul className="verification-failures">
      {result.test_failures.map((item, index) => <li key={index}>
        <strong>{item.class_name}{item.class_name && "."}{item.name}</strong>
        <span>{item.kind === "error" ? "Test error" : "Assertion failed"}</span>
        <pre>{item.message || "No failure message was reported."}</pre>
      </li>)}
    </ul>}
    {children}
    {result.details_truncated && <p>Showing the first {result.test_failures.length} failures. Full reports are saved with the verification artifacts.</p>}
    {(result.service_log || result.runner_log) && <details className="verification-logs">
      <summary>Container logs (last 200 lines, up to 8,000 characters each)</summary>
      {result.service_log && <><h4>Service</h4><pre>{result.service_log}</pre></>}
      {result.runner_log && <><h4>Test runner</h4><pre>{result.runner_log}</pre></>}
    </details>}
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
  return (
    <section className="verification-results" aria-labelledby="baseline-title">
      <h3 id="baseline-title">Baseline</h3>
      {stage === "waiting" && <p role="status">
        {progress.tests_error ? "Waiting for regression tests." : "Writing regression tests for the brief…"}
      </p>}
      {stage === "running" && <p role="status">
        Running the regression tests against the service as the repository holds it…
      </p>}
      {stage === "skipped" && <p>The baseline was skipped because the regression tests were not written.</p>}
      {stage === "reproduced" && <p className="verification-outcome verification-passed" role="status">
        Reproduced: the added tests fail against the unchanged service and the existing tests pass
      </p>}
      {suite && suite.added_tests.length > 0 && <p>Added tests: {suite.added_tests.join(", ")}</p>}
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
      {progress.baseline && stage !== "reproduced" && <TestRun result={progress.baseline} />}
    </section>
  );
}

export function DeliveryResults({ progress, retrying, disabled, onRetry }: {
  progress: DeliveryProgress;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
}) {
  const result = progress.verification;
  const stage = progress.verification_status;
  const pending = stage === "waiting" || stage === "running";
  const repairing = progress.repairs[progress.repairs.length - 1];
  // A change to an existing service generates no code and has a baseline instead.
  const change = progress.baseline_status !== "not_applicable";
  // A change whose verification proved nothing goes on by retrying its tests, unless
  // a failed stage above already offers that.
  const unverified = change && !progress.tests_error && !progress.baseline_error &&
    (Boolean(progress.verification_error) || result?.status === "error");
  const service = progress.implementation ? "built" : progress.implementation_error ? "build failed" : "building";
  return <>
    {change && <Baseline progress={progress} retrying={retrying} disabled={disabled} onRetry={onRetry} />}
    <section className="verification-results" aria-labelledby="verification-title">
      <h3 id="verification-title">Service verification</h3>
      {pending && (
        <div role="status">
          <p>{stage === "running" ? "Running contract tests against the service…" :
            change ? (progress.baseline_status === "reproduced" ? `Changing the service: ${service}…` : "Waiting for the baseline…") :
            !progress.code ? "Waiting for server code…" : "Waiting for service and test builds…"}</p>
          {progress.code && <p className="verification-builds">
            Service: {service}
            {" · "}Tests: {progress.tests ? "compiled" : progress.tests_error ? "generation or build failed" : "generating and compiling"}
          </p>}
        </div>
      )}
      {stage === "diagnosing" && <p role="status">
        Tests failed. Deciding whether the implementation or the tests are wrong…
      </p>}
      {stage === "repairing" && repairing && <p role="status">
        Repair round {repairing.attempt}: correcting the {repairing.target}…
      </p>}
      {stage === "not_applicable" && <p>Verification is not available for this session.</p>}
      {stage === "skipped" && <p>Verification skipped because a prerequisite did not complete.</p>}
      {progress.implementation_error && <div className="generation-failure">
        <p role="alert">{progress.implementation_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("server")}>
          {retrying === "server" ? "Restarting server generation…" : "Retry server generation"}
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
          {retrying === "tests" ? "Writing the regression tests again…" : "Write the regression tests again"}
        </button>
      </div>}
      {result && <>
        <p className={`verification-outcome verification-${result.status}`} role="status">
          {result.status === "passed" ? "Tests passed" : result.status === "failed" ? "Tests failed" : "Verification could not complete"}
        </p>
        <TestRun result={result}>
          {progress.repairs.length > 0 && <ol className="verification-failures">
            {progress.repairs.map((round) => <li key={round.attempt}>
              <strong>Repair round {round.attempt}: {round.target} corrected</strong>
              {round.tests.length > 0 && <span>
                Tests found broken: {round.tests.map((test) => `${test.class_name}.${test.name}`).join(", ")}
              </span>}
              {round.summary && <span>{round.summary}</span>}
            </li>)}
          </ol>}
        </TestRun>
      </>}
      {progress.publication_status !== "not_applicable" && <>
        <h3>Repository branch</h3>
        {progress.publication_status === "waiting" && <p>Verified sources are pushed to a new branch once the tests pass.</p>}
        {progress.publication_status === "running" && <p role="status">Pushing the verified sources to a new branch…</p>}
        {progress.publication_status === "skipped" && <p>Nothing was pushed because verification did not pass.</p>}
        {progress.publication && <p className="verification-outcome verification-passed" role="status">
          Pushed to <a href={progress.publication.url} target="_blank" rel="noreferrer">{progress.publication.branch}</a>
          {" "}at <code>{progress.publication.commit.slice(0, 7)}</code>
        </p>}
        {progress.publication && progress.publication.ignored_count > 0 && <p>
          Not committed because the repository's .gitignore matches them ({progress.publication.ignored_count}):
          {" "}{progress.publication.ignored.join(", ")}
          {progress.publication.ignored_count > progress.publication.ignored.length && ", …"}
        </p>}
        {progress.publication_error && <div className="generation-failure">
          <p role="alert">{progress.publication_error}</p>
          <button type="button" className="generate-button" disabled={disabled}
            onClick={() => onRetry("repository")}>
            {retrying === "repository" ? "Pushing again…" : "Retry push"}
          </button>
        </div>}
      </>}
    </section>
  </>;
}
