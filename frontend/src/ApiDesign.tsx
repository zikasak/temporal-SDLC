import { useEffect, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import { errorMessage, failure, readBody } from "./api";
import ForkButton from "./ForkButton";
import { markdownComponents, remarkPlugins } from "./markdown";

type SessionStatus = "interviewing" | "turn_running" | "finalizing" | "completed" | "failed";

type Requirement = {
  id: string;
  category: string;
  title: string;
};

type Property = {
  name: string;
  type: string;
  required: boolean;
  array: boolean;
  format?: string;
  enum?: string[];
  description?: string;
};

type Schema = { name: string; description: string; properties: Property[] };

type Parameter = { name: string; location: string; type: string; required: boolean };

type Endpoint = {
  method: string;
  path: string;
  summary: string;
  parameters: Parameter[];
  request_schema: string | null;
  response_status: number;
  response_schema: string | null;
  response_array: boolean;
  error_statuses: number[];
  requirement_ids: string[];
};

type ApiModel = {
  info: { title: string; version: string; description: string; auth: string };
  schemas: Schema[];
  endpoints: Endpoint[];
};

type OpenApiDocument = {
  document_id: string;
  filename: string;
  size_bytes: number;
  revision: number;
};

type GeneratedCode = {
  server: { path: string; files: number };
};

type ImageArtifact = { image_id: string };
type VerificationStatus = "not_applicable" | "waiting" | "running" | "completed" | "skipped" | "error";
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

const verificationStatuses: VerificationStatus[] = [
  "not_applicable", "waiting", "running", "completed", "skipped", "error",
];

function verificationRecord(value: unknown): VerificationResult | null {
  if (!isObject(value)) return null;
  if (
    !["passed", "failed", "error"].includes(String(value.status)) ||
    !["tests", "passed", "failures", "errors", "skipped", "duration_seconds"].every(
      (key) => typeof value[key] === "number" && Number.isFinite(value[key]) && value[key] >= 0,
    ) ||
    !Array.isArray(value.test_failures) ||
    !value.test_failures.every((item) => isObject(item) &&
      ["class_name", "name", "kind", "message"].every((key) => typeof item[key] === "string")) ||
    typeof value.details_truncated !== "boolean" ||
    typeof value.service_log !== "string" || typeof value.runner_log !== "string" ||
    !(value.error === null || typeof value.error === "string")
  ) throw new Error("The backend returned invalid verification results.");
  return value as VerificationResult;
}

function imageArtifact(value: unknown): ImageArtifact | null {
  return isObject(value) && typeof value.image_id === "string" ? { image_id: value.image_id } : null;
}

type GenerationStage = "server" | "tests";

function VerificationResults({ session, retrying, disabled, onRetry }: {
  session: Session;
  retrying: GenerationStage | null;
  disabled: boolean;
  onRetry: (stage: GenerationStage) => void;
}) {
  const result = session.verification;
  const stage = session.verification_status;
  const pending = stage === "waiting" || stage === "running";
  return (
    <section className="verification-results" aria-labelledby="verification-title">
      <h3 id="verification-title">Service verification</h3>
      {pending && (
        <div role="status">
          <p>{stage === "running" ? "Running contract tests against the service…" :
            !session.code ? "Waiting for server code…" : "Waiting for service and test builds…"}</p>
          {session.code && <p className="verification-builds">
            Service: {session.implementation ? "built" : session.implementation_error ? "build failed" : "building"}
            {" · "}Tests: {session.tests ? "compiled" : session.tests_error ? "generation or build failed" : "generating and compiling"}
          </p>}
        </div>
      )}
      {stage === "not_applicable" && <p>Verification is not available for this session.</p>}
      {stage === "skipped" && <p>Verification skipped because a prerequisite did not complete.</p>}
      {session.implementation_error && <div className="generation-failure">
        <p role="alert">{session.implementation_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("server")}>
          {retrying === "server" ? "Restarting server generation…" : "Retry server generation"}
        </button>
      </div>}
      {session.tests_error && <div className="generation-failure">
        <p role="alert">{session.tests_error}</p>
        <button type="button" className="generate-button" disabled={disabled}
          onClick={() => onRetry("tests")}>
          {retrying === "tests" ? "Restarting test generation…" : "Retry test generation"}
        </button>
      </div>}
      {session.verification_error && <p role="alert">{session.verification_error}</p>}
      {result && <>
        <p className={`verification-outcome verification-${result.status}`} role="status">
          {result.status === "passed" ? "Tests passed" : result.status === "failed" ? "Tests failed" : "Verification could not complete"}
        </p>
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
        {result.details_truncated && <p>Showing the first {result.test_failures.length} failures. Full reports are saved with the verification artifacts.</p>}
        {(result.service_log || result.runner_log) && <details className="verification-logs">
          <summary>Container logs (last 200 lines, up to 8,000 characters each)</summary>
          {result.service_log && <><h4>Service</h4><pre>{result.service_log}</pre></>}
          {result.runner_log && <><h4>Test runner</h4><pre>{result.runner_log}</pre></>}
        </details>}
      </>}
    </section>
  );
}

type Session = {
  session_id: string;
  source_session_id: string;
  status: SessionStatus;
  messages: { role: "user" | "assistant"; content: string }[];
  requirements: Requirement[];
  api: ApiModel;
  problems: string[];
  document: OpenApiDocument | null;
  error: string | null;
  code: GeneratedCode | null;
  code_error: string | null;
  implementation: ImageArtifact | null;
  implementation_error: string | null;
  tests: ImageArtifact | null;
  tests_error: string | null;
  verification_status: VerificationStatus;
  verification: VerificationResult | null;
  verification_error: string | null;
  forked_from: { session_id: string; message_index: number } | null;
};

const sessionStatuses: SessionStatus[] = [
  "interviewing",
  "turn_running",
  "finalizing",
  "completed",
  "failed",
];
const pendingStatuses: SessionStatus[] = ["turn_running", "finalizing"];
const statusLabels: Record<SessionStatus, string> = {
  interviewing: "Discussing",
  turn_running: "Thinking",
  finalizing: "Writing OpenAPI",
  completed: "Complete",
  failed: "Failed",
};
const authLabels: Record<string, string> = {
  none: "No authentication",
  bearer: "Bearer token",
  api_key: "API key",
  basic: "Basic auth",
};

function urlParam(name: string) {
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

function rememberSessionId(sessionId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "api-design");
  url.searchParams.delete("requirements_session_id");
  if (sessionId) url.searchParams.set("session_id", sessionId);
  else url.searchParams.delete("session_id");
  window.history.replaceState(null, "", url);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function sessionRecord(value: unknown): Session {
  if (
    !isObject(value) ||
    typeof value.session_id !== "string" ||
    !(sessionStatuses as unknown[]).includes(value.status) ||
    !Array.isArray(value.messages) ||
    !value.messages.every(
      (item) =>
        isObject(item) &&
        (item.role === "user" || item.role === "assistant") &&
        typeof item.content === "string",
    ) ||
    !Array.isArray(value.requirements) ||
    !isObject(value.api) ||
    !isObject(value.api.info) ||
    !Array.isArray(value.api.schemas) ||
    !Array.isArray(value.api.endpoints) ||
    !Array.isArray(value.problems)
  ) {
    throw new Error("The backend returned an invalid session.");
  }
  const document =
    isObject(value.document) && typeof value.document.document_id === "string"
      ? (value.document as OpenApiDocument)
      : null;
  return {
    session_id: value.session_id,
    source_session_id:
      typeof value.source_session_id === "string" ? value.source_session_id : "",
    status: value.status as SessionStatus,
    messages: value.messages as Session["messages"],
    requirements: value.requirements as Requirement[],
    api: value.api as ApiModel,
    problems: value.problems.filter((item): item is string => typeof item === "string"),
    document,
    error: typeof value.error === "string" ? value.error : null,
    code:
      isObject(value.code) &&
      isObject(value.code.server) &&
      typeof value.code.server.path === "string"
        ? (value.code as GeneratedCode)
        : null,
    code_error: typeof value.code_error === "string" ? value.code_error : null,
    implementation: imageArtifact(value.implementation),
    implementation_error: typeof value.implementation_error === "string" ? value.implementation_error : null,
    tests: imageArtifact(value.tests),
    tests_error: typeof value.tests_error === "string" ? value.tests_error : null,
    verification_status: verificationStatuses.includes(value.verification_status as VerificationStatus)
      ? value.verification_status as VerificationStatus : "not_applicable",
    verification: verificationRecord(value.verification),
    verification_error: typeof value.verification_error === "string" ? value.verification_error : null,
    forked_from:
      isObject(value.forked_from) &&
      typeof value.forked_from.session_id === "string" &&
      typeof value.forked_from.message_index === "number"
        ? {
            session_id: value.forked_from.session_id,
            message_index: value.forked_from.message_index,
          }
        : null,
  };
}

// The server code is generated after the OpenAPI document is written.
function codePending(session: Session) {
  return session.status === "completed" && !session.code && !session.code_error;
}

function pipelinePending(session: Session) {
  return codePending(session) || session.verification_status === "waiting" ||
    session.verification_status === "running";
}

function typeLabel(type: string | null, array: boolean) {
  if (!type) return "—";
  return array ? `${type}[]` : type;
}

export default function ApiDesign() {
  const [sessionId, setSessionId] = useState(() => urlParam("session_id"));
  const [lookupId, setLookupId] = useState(() => urlParam("session_id"));
  const [requirementsId, setRequirementsId] = useState(() =>
    urlParam("requirements_session_id"),
  );
  const [refreshKey, setRefreshKey] = useState(0);
  const [session, setSession] = useState<Session | null>(null);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retryingGeneration, setRetryingGeneration] = useState<GenerationStage | null>(null);
  const [error, setError] = useState("");
  const conversation = useRef<HTMLDivElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!sessionId) return;

    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      try {
        const response = await fetch(
          `/api/api-design/sessions/${encodeURIComponent(sessionId)}`,
          { signal: controller.signal },
        );
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        if (controller.signal.aborted) return;
        const record = sessionRecord(body);
        setSession(record);
        setError("");
        if (pendingStatuses.includes(record.status) || pipelinePending(record)) {
          timer = window.setTimeout(poll, 2000);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        setError(failure(caught, "Could not retrieve this session."));
      }
    }

    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [sessionId, refreshKey]);

  function openSession(id: string) {
    setSessionId(id);
    setLookupId(id);
    setRefreshKey((value) => value + 1);
    rememberSessionId(id);
  }

  async function post(path: string, body?: unknown) {
    const response = await fetch(`/api/api-design/sessions${path}`, {
      method: "POST",
      ...(body === undefined
        ? {}
        : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    const parsed = await readBody(response);
    if (!response.ok) throw new Error(errorMessage(parsed, response.status));
    return parsed;
  }

  async function startSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const source = requirementsId.trim();
    if (!source) return;
    setSubmitting(true);
    setError("");
    setSession(null);
    try {
      const body = await post("", { requirements_session_id: source });
      if (!isObject(body) || typeof body.session_id !== "string") {
        throw new Error("The backend did not return a session ID.");
      }
      openSession(body.session_id);
    } catch (caught) {
      setError(failure(caught, "Could not start a session."));
    } finally {
      setSubmitting(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = message.trim();
    if (!session || !content) return;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/messages`, { message: content });
      setMessage("");
      setSession({
        ...session,
        status: "turn_running",
        error: null,
        messages: [...session.messages, { role: "user", content }],
      });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not send the message."));
    } finally {
      setSubmitting(false);
    }
  }

  async function retryMessage() {
    if (!session || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/retry`);
      setSession({ ...session, status: "turn_running", error: null });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not retry the message."));
    } finally {
      setSubmitting(false);
    }
  }

  async function retryGeneration(stage: GenerationStage) {
    if (!session || submitting || pipelinePending(session)) return;
    setSubmitting(true);
    setRetryingGeneration(stage);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/generation/retry`, { stage });
      setSession({
        ...session,
        ...(stage === "server"
          ? { code_error: null, implementation: null, implementation_error: null }
          : { tests: null, tests_error: null }),
        verification: null,
        verification_error: null,
        verification_status: "waiting",
      });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, `Could not restart ${stage === "server" ? "server" : "test"} generation.`));
    } finally {
      setSubmitting(false);
      setRetryingGeneration(null);
    }
  }

  async function generateDocument() {
    if (!session) return;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/finalize`);
      setSession({ ...session, status: "finalizing", error: null });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not generate the OpenAPI document."));
    } finally {
      setSubmitting(false);
    }
  }

  async function forkSession(messageIndex: number) {
    if (!session) return;
    setSubmitting(true);
    setError("");
    try {
      const body = await post(`/${encodeURIComponent(session.session_id)}/fork`, {
        message_index: messageIndex,
      });
      if (!isObject(body) || typeof body.session_id !== "string") {
        throw new Error("The backend did not return a session ID.");
      }
      setSession(null);
      setMessage("");
      openSession(body.session_id);
    } catch (caught) {
      setError(failure(caught, "Could not fork the session."));
    } finally {
      setSubmitting(false);
    }
  }

  function retrieveSession(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = lookupId.trim();
    if (!id) return;
    setSession(null);
    setError("");
    openSession(id);
  }

  // Scroll to the latest turn, not past it to the API model below.
  useEffect(() => {
    const el = conversation.current;
    const end = chatEnd.current;
    if (el && end) el.scrollTop = Math.max(0, end.offsetTop - el.clientHeight);
  }, [session?.messages.length, session?.status]);

  const unanswered = session?.messages.at(-1)?.role === "user" && session.status === "interviewing";
  const canCompose = session?.status === "interviewing" && !unanswered && !submitting;
  const forkedFrom = session?.forked_from ?? null;
  const canFinalize = canCompose && session.problems.length === 0;
  const status = error ? "error" : session ? session.status : sessionId ? "loading" : "idle";
  const documentUrl = session
    ? `/api/api-design/sessions/${encodeURIComponent(session.session_id)}/document`
    : "";
  const linked = new Set(session?.api.endpoints.flatMap((item) => item.requirement_ids));
  const uncovered =
    session?.requirements.filter(
      (item) => item.category === "functional" && !linked.has(item.id),
    ) ?? [];

  return (
    <main className="page-shell">
      <header className="page-heading">
        <h1>API design</h1>
        <p className="page-intro">
          Start from a finished requirements session. The LLM drafts the API, discusses
          resources, endpoints and data models with you, and writes them up as an OpenAPI
          schema.
        </p>
      </header>

      <div className="workspace">
        <section className="request-panel" aria-labelledby="design-title">
          <div className="section-heading">
            <h2 id="design-title">{session ? "Your answer" : "New session"}</h2>
          </div>

          {session ? (
            <form className="message-form" onSubmit={sendMessage}>
              <label htmlFor="design-message">Message</label>
              <textarea
                id="design-message"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                maxLength={10000}
                rows={6}
                placeholder="Answer the questions or change the design…"
                disabled={!canCompose}
              />
              <button
                className="run-button"
                type="submit"
                disabled={!canCompose || !message.trim()}
              >
                <span>{submitting ? "Sending" : "Send answer"}</span>
                {submitting ? (
                  <span className="spinner" aria-hidden="true" />
                ) : (
                  <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                    <path d="M4.5 10h10m-4-4 4 4-4 4" />
                  </svg>
                )}
              </button>
              <button
                className="generate-button"
                type="button"
                onClick={generateDocument}
                disabled={!canFinalize}
                title={session.problems.length ? "Resolve the open problems first" : undefined}
              >
                Generate OpenAPI
              </button>
            </form>
          ) : (
            <form className="message-form" onSubmit={startSession}>
              <label htmlFor="requirements-session-id">Requirements session ID</label>
              <input
                id="requirements-session-id"
                className="text-input"
                value={requirementsId}
                onChange={(event) => setRequirementsId(event.target.value)}
                placeholder="requirements-session-..."
                disabled={submitting}
              />
              <button
                className="run-button"
                type="submit"
                disabled={submitting || !requirementsId.trim()}
              >
                <span>{submitting ? "Starting" : "Start API design"}</span>
                {submitting && <span className="spinner" aria-hidden="true" />}
              </button>
            </form>
          )}

          {session && (
            <button
              className="new-conversation"
              type="button"
              onClick={() => {
                setSession(null);
                setSessionId("");
                setLookupId("");
                setRequirementsId(session.source_session_id);
                setMessage("");
                setError("");
                rememberSessionId("");
              }}
            >
              Start a new session
            </button>
          )}

          <form className="lookup-form" onSubmit={retrieveSession}>
            <label htmlFor="design-session-id">Retrieve by session ID</label>
            <div className="lookup-controls">
              <input
                id="design-session-id"
                value={lookupId}
                onChange={(event) => setLookupId(event.target.value)}
                placeholder="api-design-session-..."
                disabled={submitting}
              />
              <button type="submit" disabled={submitting || !lookupId.trim()}>
                Find
              </button>
            </div>
          </form>
        </section>

        <section className="response-panel" aria-labelledby="discussion-title">
          <div className="response-header">
            <div className="section-heading">
              <h2 id="discussion-title">Discussion</h2>
            </div>
            <span className={`status status-${status}`}>
              <span className="status-dot" aria-hidden="true" />
              {status === "idle" && "Waiting"}
              {status === "loading" && "Loading"}
              {status === "error" && "Error"}
              {session && !error && statusLabels[session.status]}
            </span>
          </div>

          {sessionId && (
            <p className="request-id">
              Session ID <code>{sessionId}</code>
            </p>
          )}

          {forkedFrom && (
            <p className="fork-note">
              Forked after message {forkedFrom.message_index + 1} of another session.
              <button
                type="button"
                onClick={() => {
                  setSession(null);
                  openSession(forkedFrom.session_id);
                }}
              >
                Open the original
              </button>
            </p>
          )}

          <div
            ref={conversation}
            className="response-content"
            aria-live="polite"
            aria-busy={session ? pendingStatuses.includes(session.status) : Boolean(sessionId)}
          >
            {session && (
              <ol className="conversation-list">
                {session.messages.map((item, index) => (
                  <li className={`conversation-turn turn-${item.role}`} key={index}>
                    <span className="turn-label">{item.role === "user" ? "You" : "Designer"}</span>
                    {item.role === "assistant" ? (
                      <div className="turn-content turn-markdown">
                        <Markdown remarkPlugins={remarkPlugins} components={markdownComponents}>
                          {item.content}
                        </Markdown>
                      </div>
                    ) : (
                      <p className="turn-content">{item.content}</p>
                    )}
                    <ForkButton
                      role={item.role}
                      disabled={submitting}
                      onFork={() => void forkSession(index)}
                    />
                  </li>
                ))}
              </ol>
            )}

            {session && pendingStatuses.includes(session.status) && (
              <div className="pending-answer" role="status">
                <span className="loading-line" aria-hidden="true" />
                <p>
                  {session.status === "finalizing"
                    ? "Writing the OpenAPI document…"
                    : session.messages.length === 0
                      ? "Reading the requirements and drafting the API…"
                      : "Waiting for the designer…"}
                </p>
              </div>
            )}

            <div ref={chatEnd} aria-hidden="true" />

            {session?.document && (
              <section className="generated-documents" aria-label="OpenAPI document">
                <h3>OpenAPI document</h3>
                <ul>
                  <li>
                    <a href={documentUrl} download={session.document.filename}>
                      {session.document.filename}
                    </a>
                    <span>revision {session.document.revision}</span>
                  </li>
                </ul>
                <h3>Server code</h3>
                {codePending(session) && <p>Generating Spring server interfaces…</p>}
                {session.code && (
                  <ul>
                    <li>
                      <code>{session.code.server.path}</code>
                      <span>{session.code.server.files} files</span>
                    </li>
                  </ul>
                )}
                {session.code_error && <div className="generation-failure">
                  <p role="alert">{session.code_error}</p>
                  <button type="button" className="generate-button"
                    disabled={submitting || pipelinePending(session)}
                    onClick={() => void retryGeneration("server")}>
                    {retryingGeneration === "server" ? "Restarting server generation…" : "Retry server generation"}
                  </button>
                </div>}
                <VerificationResults session={session} retrying={retryingGeneration}
                  disabled={submitting || pipelinePending(session)}
                  onRetry={(stage) => void retryGeneration(stage)} />
              </section>
            )}

            {session && (
              <section className="requirements-list api-model" aria-label="API model">
                <h3>
                  {session.api.info.title} <small>v{session.api.info.version}</small>
                </h3>
                <p className="api-auth">
                  {authLabels[session.api.info.auth] ?? session.api.info.auth}
                </p>

                {session.problems.length > 0 && (
                  <div className="requirement-group api-problems">
                    <h4>Open problems</h4>
                    <ul>
                      {session.problems.map((item) => (
                        <li key={item}>
                          <p>{item}</p>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="requirement-group">
                  <h4>Endpoints ({session.api.endpoints.length})</h4>
                  {session.api.endpoints.length === 0 ? (
                    <p className="requirement-empty">None yet.</p>
                  ) : (
                    <ul>
                      {session.api.endpoints.map((item) => (
                        <li key={`${item.method} ${item.path}`}>
                          <div className="requirement-heading">
                            <span className={`method method-${item.method.toLowerCase()}`}>
                              {item.method}
                            </span>
                            <code className="endpoint-path">{item.path}</code>
                            <strong>{item.summary}</strong>
                          </div>
                          <p className="endpoint-io">
                            {item.request_schema && <>body {item.request_schema} → </>}
                            {item.response_status}{" "}
                            {typeLabel(item.response_schema, item.response_array)}
                            {item.error_statuses.length > 0 &&
                              ` · errors ${item.error_statuses.join(", ")}`}
                            {item.parameters
                              .filter((parameter) => parameter.location !== "path")
                              .map((parameter) => ` · ${parameter.location} ${parameter.name}`)
                              .join("")}
                          </p>
                          {item.requirement_ids.length > 0 && (
                            <small>{item.requirement_ids.join(", ")}</small>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                <div className="requirement-group">
                  <h4>Schemas ({session.api.schemas.length})</h4>
                  {session.api.schemas.length === 0 ? (
                    <p className="requirement-empty">None yet.</p>
                  ) : (
                    <ul>
                      {session.api.schemas.map((schema) => (
                        <li key={schema.name}>
                          <div className="requirement-heading">
                            <strong>{schema.name}</strong>
                          </div>
                          {schema.description && <small>{schema.description}</small>}
                          <table className="schema-properties">
                            <tbody>
                              {schema.properties.map((item) => (
                                <tr key={item.name}>
                                  <td>
                                    <code>{item.name}</code>
                                    {item.required && <span title="Required">*</span>}
                                  </td>
                                  <td>
                                    {typeLabel(item.type, item.array)}
                                    {item.format && ` (${item.format})`}
                                    {item.enum && ` ${item.enum.join(" | ")}`}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                {uncovered.length > 0 && (
                  <div className="requirement-group">
                    <h4>Functional requirements without endpoints</h4>
                    <ul>
                      {uncovered.map((item) => (
                        <li key={item.id}>
                          <div className="requirement-heading">
                            <code>{item.id}</code>
                            <span>{item.title}</span>
                          </div>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </section>
            )}

            {!session && !error && (
              <div className="empty-state">
                <p>{sessionId ? "Loading session…" : "Your API design starts here."}</p>
                <span>
                  {sessionId
                    ? "Querying the workflow in Temporal."
                    : "Enter a completed requirements session, or retrieve an existing design."}
                </span>
              </div>
            )}

            {(error || session?.error) && (
              <div className="error-state" role="alert">
                <p>{error ? "Something went wrong." : "The last step did not complete."}</p>
                <span>{error || session?.error}</span>
                {unanswered && !submitting && (
                  <button type="button" onClick={retryMessage}>Retry this message</button>
                )}
                {error && sessionId && (
                  <button type="button" onClick={() => setRefreshKey((value) => value + 1)}>
                    Reload session
                  </button>
                )}
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
