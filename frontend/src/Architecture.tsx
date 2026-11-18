import { useEffect, useRef, useState, type FormEvent } from "react";
import { errorMessage, failure, isObject, readBody } from "./api";
import ForkButton from "./ForkButton";
import { AskedQuestions, QuestionForm, questionList, type Question } from "./Questions";
import { RepositorySummary } from "./RepositoryPicker";
import {
  repositoryCheckout, repositorySelection, type RepositoryCheckout, type RepositorySelection,
} from "./github";
import {
  ConversationMarkdown, RequirementLookupProvider, lookupRequirementList, type LookupRequirement,
} from "./RequirementLookup";
import { flowStepsLink, rememberSessionId, urlParam } from "./url";

type SessionStatus =
  | "preparing" | "drafting" | "interviewing" | "turn_running" | "finalizing" | "completed"
  | "skipped" | "failed";

type Requirement = { id: string; category: string; title: string };

export type ArchitectureStructure = {
  components: { name: string; responsibilities: string[] }[];
  data_flows: { source: string; target: string; description: string }[];
  technology_choices: { topic: string; choice: string; rationale?: string }[];
};

type Session = {
  session_id: string;
  source_session_id: string;
  status: SessionStatus;
  kind: "new_service" | "api_change";
  messages: { role: "user" | "assistant"; content: string; questions: Question[] }[];
  requirements: Requirement[];
  // Every requirement a mentioned requirement ID can refer to, shown on hover or tap.
  lookup_requirements: LookupRequirement[];
  architecture: string | null;
  structure: ArchitectureStructure | null;
  skip_proposal: { reason: string; message_index: number } | null;
  skip_declined: boolean;
  document: { document_id: string; filename: string; revision: number } | null;
  error: string | null;
  // The top-level task flow the session is a stage of; null for sessions from before.
  task_flow_id: string | null;
  forked_from: { session_id: string; message_index: number } | null;
  repository: RepositorySelection | null;
  checkout: RepositoryCheckout | null;
  problems: string[];
  api_design_session_id: string | null;
};

const sessionStatuses: SessionStatus[] = [
  "preparing", "drafting", "interviewing", "turn_running", "finalizing", "completed", "skipped",
  "failed",
];
const pendingStatuses: SessionStatus[] = ["preparing", "drafting", "turn_running", "finalizing"];
const statusLabels: Record<SessionStatus, string> = {
  preparing: "Checking out repository",
  drafting: "Drafting",
  interviewing: "Discussing",
  turn_running: "Thinking",
  finalizing: "Writing the architecture",
  completed: "Complete",
  skipped: "Skipped",
  failed: "Failed",
};

/** The architecture structure in `value`, or null when it is not one. */
export function architectureStructure(value: unknown): ArchitectureStructure | null {
  if (
    !isObject(value) ||
    !Array.isArray(value.components) ||
    !Array.isArray(value.data_flows) ||
    !Array.isArray(value.technology_choices)
  ) return null;
  const text = (item: unknown) => (typeof item === "string" ? item : "");
  return {
    components: value.components.filter(isObject).map((item) => ({
      name: text(item.name),
      responsibilities: Array.isArray(item.responsibilities) ? item.responsibilities.map(text) : [],
    })),
    data_flows: value.data_flows.filter(isObject).map((item) => ({
      source: text(item.source), target: text(item.target), description: text(item.description),
    })),
    technology_choices: value.technology_choices.filter(isObject).map((item) => ({
      topic: text(item.topic), choice: text(item.choice),
      ...(typeof item.rationale === "string" ? { rationale: item.rationale } : {}),
    })),
  };
}

function sessionRecord(value: unknown): Session {
  if (
    !isObject(value) ||
    typeof value.session_id !== "string" ||
    !(sessionStatuses as unknown[]).includes(value.status) ||
    !Array.isArray(value.messages) ||
    !value.messages.every(
      (item) => isObject(item) && (item.role === "user" || item.role === "assistant") &&
        typeof item.content === "string",
    ) ||
    !Array.isArray(value.requirements) ||
    !Array.isArray(value.problems)
  ) {
    throw new Error("The backend returned an invalid architecture session.");
  }
  const proposal = isObject(value.skip_proposal) && typeof value.skip_proposal.reason === "string" &&
    typeof value.skip_proposal.message_index === "number"
    ? { reason: value.skip_proposal.reason, message_index: value.skip_proposal.message_index }
    : null;
  return {
    session_id: value.session_id,
    source_session_id: typeof value.source_session_id === "string" ? value.source_session_id : "",
    status: value.status as SessionStatus,
    kind: value.kind === "api_change" ? "api_change" : "new_service",
    messages: value.messages.map((item) => ({
      role: item.role, content: item.content, questions: questionList(item.questions),
    })),
    requirements: value.requirements as Requirement[],
    lookup_requirements: lookupRequirementList(
      "lookup_requirements" in value ? value.lookup_requirements : null),
    architecture: typeof value.architecture === "string" ? value.architecture : null,
    structure: architectureStructure(value.structure),
    skip_proposal: proposal,
    skip_declined: value.skip_declined === true,
    document: isObject(value.document) && typeof value.document.document_id === "string"
      ? (value.document as Session["document"])
      : null,
    error: typeof value.error === "string" ? value.error : null,
    task_flow_id: "task_flow_id" in value && typeof value.task_flow_id === "string"
      ? value.task_flow_id : null,
    forked_from: isObject(value.forked_from) && typeof value.forked_from.session_id === "string" &&
      typeof value.forked_from.message_index === "number"
      ? { session_id: value.forked_from.session_id, message_index: value.forked_from.message_index }
      : null,
    repository: repositorySelection(value.repository),
    checkout: repositoryCheckout(value.checkout),
    problems: value.problems.filter((item): item is string => typeof item === "string"),
    api_design_session_id: typeof value.api_design_session_id === "string"
      ? value.api_design_session_id : null,
  };
}

/** The components, data flow and technology choices of an architecture. */
export function ArchitectureSection({ structure, problems }: {
  structure: ArchitectureStructure | null;
  problems: string[];
}) {
  return (
    <section className="requirements-list api-model architecture-model" aria-label="Architecture">
      <h3>Architecture</h3>
      {problems.length > 0 && (
        <div className="requirement-group api-problems">
          <h4>Open problems</h4>
          <ul>{problems.map((item) => <li key={item}><p>{item}</p></li>)}</ul>
        </div>
      )}
      {!structure ? <p className="requirement-empty">Not drafted yet.</p> : (
        <>
          <div className="requirement-group">
            <h4>Components ({structure.components.length})</h4>
            {structure.components.length === 0 ? <p className="requirement-empty">None yet.</p> : (
              <ul>
                {structure.components.map((item) => (
                  <li key={item.name}>
                    <div className="requirement-heading"><strong>{item.name}</strong></div>
                    <ul className="architecture-responsibilities">
                      {item.responsibilities.map((entry, index) => <li key={index}>{entry}</li>)}
                    </ul>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="requirement-group">
            <h4>Data flow ({structure.data_flows.length})</h4>
            {structure.data_flows.length === 0 ? <p className="requirement-empty">None yet.</p> : (
              <ul>
                {structure.data_flows.map((item) => (
                  <li key={`${item.source}→${item.target}`}>
                    <div className="requirement-heading"><strong>{item.source} → {item.target}</strong></div>
                    <p>{item.description}</p>
                  </li>
                ))}
              </ul>
            )}
          </div>
          <div className="requirement-group">
            <h4>Technology choices ({structure.technology_choices.length})</h4>
            {structure.technology_choices.length === 0 ? <p className="requirement-empty">None yet.</p> : (
              <ul>
                {structure.technology_choices.map((item) => (
                  <li key={item.topic}>
                    <div className="requirement-heading"><strong>{item.topic}</strong>: {item.choice}</div>
                    {item.rationale && <p>{item.rationale}</p>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </>
      )}
    </section>
  );
}

export default function Architecture() {
  const [sessionId, setSessionId] = useState(() => urlParam("session_id"));
  const [lookupId, setLookupId] = useState(() => urlParam("session_id"));
  const [refreshKey, setRefreshKey] = useState(0);
  const [session, setSession] = useState<Session | null>(null);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const conversation = useRef<HTMLDivElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    let timer: number | undefined;
    // A finalized or skipped session starts its API design right after it ends.
    let designPolls = 15;

    async function poll() {
      try {
        const response = await fetch(`/api/architecture/sessions/${encodeURIComponent(sessionId)}`,
          { signal: controller.signal });
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        if (controller.signal.aborted) return;
        const record = sessionRecord(body);
        setSession(record);
        setError("");
        const designStarting = (record.status === "completed" || record.status === "skipped") &&
          !record.api_design_session_id && designPolls-- > 0;
        if (pendingStatuses.includes(record.status) || designStarting) {
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
    rememberSessionId("architecture", id);
  }

  async function post(path: string, body?: unknown) {
    const response = await fetch(`/api/architecture/sessions${path}`, {
      method: "POST",
      ...(body === undefined
        ? {}
        : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    const parsed = await readBody(response);
    if (!response.ok) throw new Error(errorMessage(parsed, response.status));
    return parsed;
  }

  /** Runs `action` on the open session, then shows `pending` until the next poll. */
  async function act(action: (id: string) => Promise<unknown>, pending: (current: Session) => Session,
    fallback: string) {
    if (!session) return false;
    setSubmitting(true);
    setError("");
    try {
      await action(encodeURIComponent(session.session_id));
      setSession(pending(session));
      setRefreshKey((value) => value + 1);
      return true;
    } catch (caught) {
      setError(failure(caught, fallback));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = message.trim();
    if (!content) return;
    const declining = Boolean(session?.skip_proposal);
    if (await act((id) => post(`/${id}/messages`, { message: content }), (current) => ({
      ...current, status: current.structure ? "turn_running" : "drafting", error: null,
      skip_proposal: null, skip_declined: current.skip_declined || declining,
      messages: [...current.messages, { role: "user", content, questions: [] }],
    }), "Could not send the message.")) setMessage("");
  }

  function answerQuestions(answers: string[]) {
    const content = asked.map((item, index) => `${item.question}\n→ ${answers[index]}`).join("\n\n");
    return act((id) => post(`/${id}/answers`, { answers }), (current) => ({
      ...current, status: current.structure ? "turn_running" : "drafting", error: null,
      skip_proposal: null, skip_declined: current.skip_declined || Boolean(current.skip_proposal),
      messages: [...current.messages, { role: "user", content, questions: [] }],
    }), "Could not send the answers.");
  }

  function retryMessage() {
    return act((id) => post(`/${id}/retry`), (current) => ({
      ...current, status: current.structure ? "turn_running" : "drafting", error: null,
    }), "Could not retry.");
  }

  function finalize() {
    return act((id) => post(`/${id}/finalize`), (current) => ({
      ...current, status: "finalizing", error: null,
    }), "Could not finalize the architecture.");
  }

  function skip() {
    return act((id) => post(`/${id}/skip`), (current) => ({
      ...current, status: "skipped", skip_proposal: null, error: null,
    }), "Could not skip the architecture stage.");
  }

  async function forkSession(messageIndex: number) {
    if (!session) return;
    setSubmitting(true);
    setError("");
    try {
      const body = await post(`/${encodeURIComponent(session.session_id)}/fork`,
        { message_index: messageIndex });
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

  useEffect(() => {
    const el = conversation.current;
    const end = chatEnd.current;
    if (el && end) el.scrollTop = Math.max(0, end.offsetTop - el.clientHeight);
  }, [session?.messages.length, session?.status]);

  const unanswered = session?.status === "interviewing" && session.messages.at(-1)?.role === "user";
  const neverDrafted = session?.status === "interviewing" && !session.structure &&
    !session.skip_proposal && !session.messages.some((item) => item.role === "assistant");
  const canCompose = session?.status === "interviewing" && !unanswered && !submitting;
  const last = session?.messages.at(-1);
  const asked = last?.role === "assistant" && !session?.skip_proposal ? last.questions : [];
  const canFinalize = canCompose && session.problems.length === 0 && !session.skip_proposal;
  const canSkip = session?.kind === "api_change" && session.status === "interviewing" && !submitting;
  // Adding the API design once it started would fork the task twice.
  const canFork = !session?.api_design_session_id;
  const status = error ? "error" : session ? session.status : sessionId ? "loading" : "idle";
  const documentUrl = session
    ? `/api/architecture/sessions/${encodeURIComponent(session.session_id)}/document`
    : "";

  return (
    <RequirementLookupProvider requirements={session?.lookup_requirements ?? null}>
    <main className="page-shell">
      <header className="page-heading">
        <h1>Architecture</h1>
        <p className="page-intro">
          Each finished requirements session of a new service or an API change starts its own.
          The LLM drafts the components, the data flow between them and the key technology
          choices; review and correct them, then finalize to start the API design.
        </p>
      </header>

      <div className="workspace">
        <section className="request-panel" aria-labelledby="architecture-title">
          <div className="section-heading">
            <h2 id="architecture-title">{session ? "Your answer" : "Open a session"}</h2>
          </div>

          {session?.skip_proposal && (
            <div className="skip-proposal" role="status">
              <p><strong>The architect proposes skipping this stage.</strong></p>
              <p>{session.skip_proposal.reason}</p>
              <p>
                Skipping keeps the repository's existing architecture. To decline, say below what
                the architecture should cover.
              </p>
              <button type="button" className="generate-button" disabled={!canSkip}
                onClick={() => void skip()}>
                Confirm skipping
              </button>
            </div>
          )}

          {session && asked.length > 0 ? (
            <div className="message-form">
              <QuestionForm key={session.messages.length} questions={asked}
                disabled={!canCompose} submitting={submitting} onSubmit={answerQuestions} />
              <button className="generate-button" type="button" onClick={() => void finalize()}
                disabled={!canFinalize}
                title={session.problems.length ? "Resolve the open problems first" : undefined}>
                Finalize architecture
              </button>
            </div>
          ) : session ? (
            <form className="message-form" onSubmit={sendMessage}>
              <label htmlFor="architecture-message">Message</label>
              <textarea id="architecture-message" value={message}
                onChange={(event) => setMessage(event.target.value)} maxLength={10000} rows={6}
                placeholder={session.skip_proposal
                  ? "Decline the proposal: say what the architecture should cover…"
                  : "Answer the questions or change the architecture…"}
                disabled={!canCompose} />
              <button className="run-button" type="submit" disabled={!canCompose || !message.trim()}>
                <span>{submitting ? "Sending" : session.skip_proposal ? "Decline and send" : "Send answer"}</span>
                {submitting ? <span className="spinner" aria-hidden="true" /> : (
                  <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                    <path d="M4.5 10h10m-4-4 4 4-4 4" />
                  </svg>
                )}
              </button>
              <button className="generate-button" type="button" onClick={() => void finalize()}
                disabled={!canFinalize}
                title={session.problems.length ? "Resolve the open problems first" : undefined}>
                Finalize architecture
              </button>
              {session.kind === "api_change" && !session.skip_proposal && (
                <button className="generate-button" type="button" onClick={() => void skip()}
                  disabled={!canSkip}
                  title="Keep the repository's existing architecture and go on with the API design">
                  Skip this stage
                </button>
              )}
            </form>
          ) : (
            <p className="request-note">
              Architecture sessions start by themselves when the requirements of a new service or an
              API change are generated. Open one from the Flows page or by its session ID.
            </p>
          )}

          <form className="lookup-form" onSubmit={retrieveSession}>
            <label htmlFor="architecture-session-id">Retrieve by session ID</label>
            <div className="lookup-controls">
              <input id="architecture-session-id" value={lookupId}
                onChange={(event) => setLookupId(event.target.value)}
                placeholder="architecture-session-..." disabled={submitting} />
              <button type="submit" disabled={submitting || !lookupId.trim()}>Find</button>
            </div>
          </form>
        </section>

        <section className="response-panel" aria-labelledby="architecture-discussion-title">
          <div className="response-header">
            <div className="section-heading">
              <h2 id="architecture-discussion-title">Discussion</h2>
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

          {sessionId && <p className="request-id">Session ID <code>{sessionId}</code></p>}

          {session?.source_session_id && (
            <p className="fork-note">
              Started by the requirements session.
              <a href={`?mode=requirements&session_id=${encodeURIComponent(session.source_session_id)}`}>
                Open the requirements
              </a>
            </p>
          )}

          {session?.task_flow_id && (
            <p className="request-id">
              Part of a task flow:{" "}
              <a href={flowStepsLink(session.task_flow_id)}>show all its steps</a>
            </p>
          )}

          {session?.forked_from && (
            <p className="fork-note">
              Forked after message {session.forked_from.message_index + 1} of another session.
              <button type="button" onClick={() => {
                const origin = session.forked_from!.session_id;
                setSession(null);
                openSession(origin);
              }}>
                Open the original
              </button>
            </p>
          )}

          <div ref={conversation} className="response-content" aria-live="polite"
            aria-busy={session ? pendingStatuses.includes(session.status) : Boolean(sessionId)}>
            {session && (
              <ol className="conversation-list">
                {session.messages.map((item, index) => (
                  <li className={`conversation-turn turn-${item.role}`} key={index}>
                    <span className="turn-label">{item.role === "user" ? "You" : "Architect"}</span>
                    {item.role === "assistant" ? (
                      <div className="turn-content turn-markdown">
                        <ConversationMarkdown>{item.content}</ConversationMarkdown>
                        <AskedQuestions questions={item.questions} />
                      </div>
                    ) : (
                      <p className="turn-content">{item.content}</p>
                    )}
                    {canFork && (
                      <ForkButton role={item.role} disabled={submitting}
                        onFork={() => void forkSession(index)} />
                    )}
                  </li>
                ))}
              </ol>
            )}

            {session && pendingStatuses.includes(session.status) && (
              <div className="pending-answer" role="status">
                <span className="loading-line" aria-hidden="true" />
                <p>
                  {session.status === "finalizing" ? "Writing the architecture document…"
                    : session.status === "preparing" ? "Checking out the repository…"
                    : session.status === "drafting" ? "Reading the requirements and drafting the architecture…"
                    : "Waiting for the architect…"}
                </p>
              </div>
            )}

            <div ref={chatEnd} aria-hidden="true" />

            {session && (session.status === "completed" || session.status === "skipped") && (
              <section className="generated-documents" aria-label="Architecture document">
                {session.document ? (
                  <>
                    <h3>Architecture document</h3>
                    <ul>
                      <li><a href={documentUrl} download="architecture.md">architecture.md</a></li>
                      <li><a href={`${documentUrl}?format=json`} download="architecture.json">architecture.json</a></li>
                    </ul>
                  </>
                ) : (
                  <p>The stage was skipped: the repository's existing architecture is kept.</p>
                )}
                {session.api_design_session_id ? (
                  <a className="next-step"
                    href={`?mode=api-design&session_id=${encodeURIComponent(session.api_design_session_id)}`}>
                    Continue with the API design →
                  </a>
                ) : <p>Starting the API design…</p>}
              </section>
            )}

            {session && session.status !== "skipped" && (
              <ArchitectureSection structure={session.structure}
                problems={session.status === "completed" ? [] : session.problems} />
            )}

            {!session && !error && (
              <div className="empty-state">
                <p>{sessionId ? "Loading session…" : "Your architecture starts here."}</p>
                <span>{sessionId ? "Querying the workflow in Temporal." : "Retrieve a session by its ID."}</span>
              </div>
            )}

            {(error || session?.error) && (
              <div className="error-state" role="alert">
                <p>{error ? "Something went wrong." : "The last step did not complete."}</p>
                <span>{error || session?.error}</span>
                {(unanswered || neverDrafted) && !submitting && (
                  <button type="button" onClick={() => void retryMessage()}>
                    {neverDrafted ? "Retry drafting" : "Retry this message"}
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
