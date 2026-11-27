import { useEffect, useRef, useState, type FormEvent } from "react";
import { errorMessage, failure, isObject, readBody } from "./api";
import { ApiModelSection, type ApiModel } from "./ApiModel";
import { ArchitectureSection, architectureStructure, type ArchitectureStructure } from "./Architecture";
import ForkButton from "./ForkButton";
import { AskedQuestions, questionList, type Question } from "./Questions";
import { RepositorySummary } from "./RepositoryPicker";
import {
  DeliveryResults, OpenQuestionsForm, answeredProgress, decidedProgress, deliveryPending, deliveryProgress, retriedProgress, taskKind,
  type DeliveryProgress, type GenerationStage, type OpenAnswers, type TaskKind,
} from "./Delivery";
import {
  repositoryCheckout, repositorySelection, type RepositoryCheckout, type RepositorySelection,
} from "./github";
import {
  ConversationMarkdown, RequirementLookupProvider, lookupRequirementList, type LookupRequirement,
} from "./RequirementLookup";
import { flowStepsLink, rememberSessionId, urlParam } from "./url";

type SessionStatus = "preparing" | "interviewing" | "turn_running" | "finalizing" | "completed" | "failed";

type Requirement = {
  id: string;
  category: string;
  title: string;
};

type OpenApiDocument = {
  document_id: string;
  filename: string;
  size_bytes: number;
  revision: number;
};

// The design and the progress of the delivery it started, reported as one session.
type Session = DeliveryProgress & {
  session_id: string;
  source_session_id: string;
  status: SessionStatus;
  // An assistant message's questions are answered by the next user message.
  messages: { role: "user" | "assistant"; content: string; questions: Question[] }[];
  requirements: Requirement[];
  // Every requirement a mentioned requirement ID can refer to, shown on hover or tap.
  lookup_requirements: LookupRequirement[];
  // The kind of task the requirements session recorded: a new service or an API change.
  kind: TaskKind | null;
  api: ApiModel;
  language: string | null;
  problems: string[];
  document: OpenApiDocument | null;
  error: string | null;
  // The top-level task flow the session is a stage of; null for sessions from before.
  task_flow_id: string | null;
  forked_from: { session_id: string; message_index: number } | null;
  repository: RepositorySelection | null;
  checkout: RepositoryCheckout | null;
  // The architecture session that started this design, and the architecture it hands on.
  architecture_session_id: string | null;
  architecture_structure: ArchitectureStructure | null;
};

const sessionStatuses: SessionStatus[] = [
  "preparing",
  "interviewing",
  "turn_running",
  "finalizing",
  "completed",
  "failed",
];
const pendingStatuses: SessionStatus[] = ["preparing", "turn_running", "finalizing"];
const statusLabels: Record<SessionStatus, string> = {
  preparing: "Checking out repository",
  interviewing: "Discussing",
  turn_running: "Thinking",
  finalizing: "Writing OpenAPI",
  completed: "Complete",
  failed: "Failed",
};
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
  const repository = repositorySelection(value.repository);
  return {
    ...deliveryProgress(value, repository),
    session_id: value.session_id,
    repository,
    checkout: repositoryCheckout(value.checkout),
    source_session_id:
      typeof value.source_session_id === "string" ? value.source_session_id : "",
    architecture_session_id:
      typeof value.architecture_session_id === "string" ? value.architecture_session_id : null,
    architecture_structure: architectureStructure(value.architecture_structure),
    status: value.status as SessionStatus,
    messages: value.messages.map((item) => ({
      role: item.role, content: item.content, questions: questionList(item.questions),
    })),
    requirements: value.requirements as Requirement[],
    lookup_requirements: lookupRequirementList(
      "lookup_requirements" in value ? value.lookup_requirements : null),
    kind: taskKind(value.kind),
    api: value.api as ApiModel,
    language: typeof value.language === "string" ? value.language : null,
    problems: value.problems.filter((item): item is string => typeof item === "string"),
    document,
    error: typeof value.error === "string" ? value.error : null,
    task_flow_id: "task_flow_id" in value && typeof value.task_flow_id === "string"
      ? value.task_flow_id : null,
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
  return codePending(session) || deliveryPending(session);
}

export default function ApiDesign() {
  const [sessionId, setSessionId] = useState(() => urlParam("session_id"));
  const [lookupId, setLookupId] = useState(() => urlParam("session_id"));
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
    rememberSessionId("api-design", id);
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
        messages: [...session.messages, { role: "user", content, questions: [] }],
      });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not send the message."));
    } finally {
      setSubmitting(false);
    }
  }

  async function decideFindings(instructions: string | null) {
    if (!session) return false;
    setSubmitting(true);
    setError("");
    try {
      const path = `/${encodeURIComponent(session.session_id)}/delivery/findings`;
      await (instructions === null ? post(`${path}/accept`) : post(`${path}/instructions`, { instructions }));
      setSession({ ...session, ...decidedProgress(session, instructions) });
      setRefreshKey((value) => value + 1);
      return true;
    } catch (caught) {
      setError(failure(caught, instructions === null ? "Could not accept the findings." : "Could not send the instructions."));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  /** Sends each group of the open questions on its own: the interviewer's answers to
   * .../answers and each agent's to .../delivery/answers, one call per agent. */
  async function sendOpenAnswers({ interviewer, agents }: OpenAnswers) {
    if (!session) return [];
    setSubmitting(true);
    setError("");
    const id = encodeURIComponent(session.session_id);
    const sent: string[] = [];
    const errors: string[] = [];
    if (interviewer) {
      try {
        await post(`/${id}/answers`, { answers: interviewer });
        sent.push("interviewer");
      } catch (caught) {
        errors.push(failure(caught, "Could not send the answers to the interviewer."));
      }
    }
    for (const agent of agents) {
      try {
        await post(`/${id}/delivery/answers`, agent);
        sent.push(agent.workflow_id);
      } catch (caught) {
        errors.push(failure(caught, "Could not send the answers to the agent."));
      }
    }
    let next = session;
    if (interviewer && sent.includes("interviewer")) {
      const content = asked.map((item, index) => `${item.question}\n→ ${interviewer[index]}`).join("\n\n");
      next = {
        ...next,
        status: "turn_running",
        error: null,
        messages: [...next.messages, { role: "user", content, questions: [] }],
      };
    }
    for (const agent of agents) {
      if (sent.includes(agent.workflow_id)) next = { ...next, ...answeredProgress(next, agent.workflow_id) };
    }
    if (sent.length > 0) {
      setSession(next);
      setRefreshKey((value) => value + 1);
    }
    if (errors.length > 0) setError(errors.join(" "));
    setSubmitting(false);
    return sent;
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
      setSession({ ...session, ...retriedProgress(session, stage, Boolean(session.checkout)) });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, stage === "repository" ? "Could not retry the push."
        : stage === "review" ? "Could not retry the code review."
        : `Could not restart ${stage === "server" ? "server" : "test"} generation.`));
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
  // A change session whose opening failed has nothing to answer until it is retried.
  const openingFailed = session?.status === "interviewing" && session.messages.length === 0 && Boolean(session.error);
  const canCompose = session?.status === "interviewing" && session.messages.length > 0 && !unanswered && !submitting;
  // Questions of the last message are answered with the form, not with a message.
  const last = session?.messages.at(-1);
  const asked = last?.role === "assistant" ? last.questions : [];
  const forkedFrom = session?.forked_from ?? null;
  const canFinalize = canCompose && session.problems.length === 0;
  const status = error ? "error" : session ? session.status : sessionId ? "loading" : "idle";
  const documentUrl = session
    ? `/api/api-design/sessions/${encodeURIComponent(session.session_id)}/document`
    : "";

  return (
    <RequirementLookupProvider requirements={session?.lookup_requirements ?? null}>
    <main className="page-shell">
      <header className="page-heading">
        <h1>API design</h1>
        <p className="page-intro">
          Each finished requirements session starts its own. The LLM drafts the API, discusses
          resources, endpoints and data models with you, and writes them up as an OpenAPI
          schema.
        </p>
      </header>

      <div className="workspace">
        <section className="request-panel" aria-labelledby="design-title">
          <div className="section-heading">
            <h2 id="design-title">{session ? "Your answer" : "Open a session"}</h2>
          </div>

          {!(session && asked.length > 0) && (session?.agent_questions.length ?? 0) > 0 && (
            <div className="message-form">
              <OpenQuestionsForm questions={[]} questionsKey={session?.messages.length ?? 0}
                questionsDisabled progress={session} submitting={submitting}
                onSubmit={sendOpenAnswers} />
            </div>
          )}

          {session && asked.length > 0 ? (
            <div className="message-form">
              <OpenQuestionsForm questions={asked} questionsKey={session.messages.length}
                questionsDisabled={!canCompose} progress={session} submitting={submitting}
                onSubmit={sendOpenAnswers} />
              <button
                className="generate-button"
                type="button"
                onClick={generateDocument}
                disabled={!canFinalize}
                title={session.problems.length ? "Resolve the open problems first" : undefined}
              >
                Generate OpenAPI
              </button>
            </div>
          ) : session ? (
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
            <p className="request-note">
              API designs start by themselves when the requirements of a new service or an API
              change are generated. Open one from the Flows page or by its session ID.
            </p>
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

          <RepositorySummary repository={session?.repository ?? null} checkout={session?.checkout} />

          {sessionId && (
            <p className="request-id">
              Session ID <code>{sessionId}</code>
            </p>
          )}

          {session?.task_flow_id && (
            <p className="request-id">
              Part of a task flow:{" "}
              <a href={flowStepsLink(session.task_flow_id)}>show all its steps</a>
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

          {session?.architecture_session_id && (
            <p className="fork-note">
              Designed against the architecture of its architecture session.
              <a href={`?mode=architecture&session_id=${encodeURIComponent(session.architecture_session_id)}`}>
                Open the architecture
              </a>
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
                        <ConversationMarkdown>{item.content}</ConversationMarkdown>
                        <AskedQuestions questions={item.questions} />
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
                    : session.status === "preparing"
                      ? "Checking out the repository…"
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
                {codePending(session) && <p>Generating {session.language ?? ""} server code…</p>}
                {session.code && (
                  <ul>
                    <li>
                      <code>{session.code.server.path}</code>
                      <span>{session.code.server.files} files</span>
                      {session.language && <span>{session.language}</span>}
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
                <DeliveryResults progress={session} kind={session.kind} retrying={retryingGeneration}
                  disabled={submitting || pipelinePending(session)}
                  onRetry={(stage) => void retryGeneration(stage)}
                    onFindings={decideFindings} />
              </section>
            )}

            {session && (
              <ApiModelSection key={sessionId} api={session.api} problems={session.problems} requirements={session.requirements} />
            )}

            {session?.architecture_structure && (
              <ArchitectureSection key={sessionId} structure={session.architecture_structure} problems={[]} />
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
                {(unanswered || openingFailed) && !submitting && (
                  <button type="button" onClick={retryMessage}>
                    {openingFailed ? "Retry" : "Retry this message"}
                  </button>
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
    </RequirementLookupProvider>
  );
}
