import { useEffect, useRef, useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";
import {
  AgentQuestionsPanel, DeliveryResults, answeredProgress, decidedProgress, deliveryPending, deliveryProgress, retriedProgress,
  type DeliveryProgress, type GenerationStage,
} from "./Delivery";
import ForkButton from "./ForkButton";
import { onboardingLink, startOnboarding } from "./Onboarding";
import IntakeForm, { type Intake } from "./Intake";
import { AskedQuestions, QuestionForm, questionList, type Question } from "./Questions";
import RepositoryPicker, { RepositorySummary } from "./RepositoryPicker";
import { repositoryCheckout, repositorySelection, selectionInput, type RepositoryCheckout, type RepositorySelection } from "./github";
import {
  ConversationMarkdown, RequirementLookupProvider, lookupRequirementList, type LookupRequirement,
} from "./RequirementLookup";
import {
  ForceDialog, MessageBox, ReviewPanel, messageWarning, requirementsReview, type RequirementsReview,
} from "./RequirementsReview";
import { flowStepsLink, rememberSessionId, urlParam } from "./url";

type SessionStatus = "preparing" | "interviewing" | "turn_running" | "finalizing" | "completed" | "failed";
type Category = "functional" | "non_functional" | "technical";
type TaskKind = "new_service" | "api_change" | "bugfix" | "docs";

type Requirement = {
  id: string;
  category: Category;
  title: string;
  description: string;
  priority: "must" | "should" | "could";
  rationale?: string;
};

type RequirementsDocument = {
  document_id: string;
  filename: string;
  size_bytes: number;
  revision: number;
};

type Session = {
  session_id: string;
  status: SessionStatus;
  // An assistant message's questions are answered by the next user message.
  messages: { role: "user" | "assistant"; content: string; questions: Question[] }[];
  requirements: Requirement[];
  // Requirements of the existing service that this change ends.
  retired: Requirement[];
  // Every requirement a mentioned requirement ID can refer to, shown on hover or tap.
  lookup_requirements: LookupRequirement[];
  kind: TaskKind | null;
  // The requirements review of a new service, API change or bugfix: the document is
  // written once it finds no contradictions, or when the user continues despite them.
  review: RequirementsReview;
  document: RequirementsDocument | null;
  error: string | null;
  // The top-level task flow the session is a stage of; null for sessions from before.
  task_flow_id: string | null;
  forked_from: { session_id: string; message_index: number } | null;
  repository: RepositorySelection | null;
  checkout: RepositoryCheckout | null;
  // The delivery of a task this session hands over itself, once it has finished.
  delivery: DeliveryProgress | null;
  // The architecture session the session started itself once the document of a new
  // service or an API change was generated; it later starts the API design.
  architecture_session_id: string | null;
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
const categories: [Category, string][] = [
  ["functional", "Functional"],
  ["non_functional", "Non-functional"],
  ["technical", "Technical"],
];
// Kinds that leave the API as it is: the session delivers them without an API design.
const directKinds: TaskKind[] = ["bugfix", "docs"];
const kindLabels: Record<TaskKind, string> = {
  new_service: "New service",
  api_change: "API change",
  bugfix: "Bugfix or other non-API change",
  docs: "Documentation only",
};
const statusLabels: Record<SessionStatus, string> = {
  preparing: "Checking out repository",
  interviewing: "Interviewing",
  turn_running: "Thinking",
  finalizing: "Writing document",
  completed: "Complete",
  failed: "Failed",
};

function isRequirement(item: unknown): boolean {
  if (typeof item !== "object" || item === null) return false;
  const record = item as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    categories.some(([category]) => category === record.category) &&
    typeof record.title === "string" &&
    typeof record.description === "string" &&
    typeof record.priority === "string"
  );
}

function sessionRecord(value: unknown): Session {
  if (
    typeof value !== "object" ||
    value === null ||
    !("session_id" in value) ||
    typeof value.session_id !== "string" ||
    !("status" in value) ||
    !(sessionStatuses as unknown[]).includes(value.status) ||
    !("messages" in value) ||
    !Array.isArray(value.messages) ||
    !value.messages.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        (item.role === "user" || item.role === "assistant") &&
        typeof item.content === "string",
    ) ||
    !("requirements" in value) ||
    !Array.isArray(value.requirements) ||
    !value.requirements.every(isRequirement) ||
    !("retired" in value) ||
    !Array.isArray(value.retired) ||
    !value.retired.every(isRequirement)
  ) {
    throw new Error("The backend returned an invalid session.");
  }
  const document =
    "document" in value &&
    typeof value.document === "object" &&
    value.document !== null &&
    "document_id" in value.document &&
    typeof value.document.document_id === "string"
      ? (value.document as RequirementsDocument)
      : null;
  const repository = repositorySelection("repository" in value ? value.repository : null);
  const delivery = "delivery" in value ? value.delivery : null;
  return {
    delivery: typeof delivery === "object" && delivery !== null
      ? deliveryProgress(delivery as Record<string, unknown>, repository) : null,
    session_id: value.session_id,
    status: value.status as SessionStatus,
    messages: value.messages.map((item) => ({
      role: item.role, content: item.content, questions: questionList(item.questions),
    })),
    requirements: value.requirements as Requirement[],
    retired: value.retired as Requirement[],
    lookup_requirements: lookupRequirementList(
      "lookup_requirements" in value ? value.lookup_requirements : null),
    kind: "kind" in value && typeof value.kind === "string" && value.kind in kindLabels
      ? (value.kind as TaskKind)
      : null,
    review: requirementsReview("review" in value ? value.review : null),
    document,
    error: "error" in value && typeof value.error === "string" ? value.error : null,
    task_flow_id: "task_flow_id" in value && typeof value.task_flow_id === "string"
      ? value.task_flow_id : null,
    forked_from: forkOrigin(value),
    repository,
    checkout: repositoryCheckout("checkout" in value ? value.checkout : null),
    architecture_session_id: "architecture_session_id" in value &&
      typeof value.architecture_session_id === "string" ? value.architecture_session_id : null,
  };
}

function forkOrigin(value: object): Session["forked_from"] {
  if (!("forked_from" in value)) return null;
  const origin = value.forked_from;
  return typeof origin === "object" &&
    origin !== null &&
    "session_id" in origin &&
    typeof origin.session_id === "string" &&
    "message_index" in origin &&
    typeof origin.message_index === "number"
    ? { session_id: origin.session_id, message_index: origin.message_index }
    : null;
}

export default function Requirements() {
  const [sessionId, setSessionId] = useState(() => urlParam("session_id"));
  const [lookupId, setLookupId] = useState(() => urlParam("session_id"));
  const [refreshKey, setRefreshKey] = useState(0);
  const [session, setSession] = useState<Session | null>(null);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retryingGeneration, setRetryingGeneration] = useState<GenerationStage | null>(null);
  const [error, setError] = useState("");
  const [repositoryChoice, setRepositoryChoice] = useState<RepositorySelection | null>(null);
  const [onboarding, setOnboarding] = useState(false);
  // Whether the warning before continuing despite the requirements review is shown.
  const [confirmingForce, setConfirmingForce] = useState(false);
  const conversation = useRef<HTMLDivElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!sessionId) return;

    const controller = new AbortController();
    let timer: number | undefined;
    let designPolls = 15;

    async function poll() {
      try {
        const response = await fetch(
          `/api/requirements/sessions/${encodeURIComponent(sessionId)}`,
          { signal: controller.signal },
        );
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        if (controller.signal.aborted) return;
        const record = sessionRecord(body);
        setSession(record);
        setError("");
        // A finished task that designs its API starts its architecture right after the
        // document; sessions finalized before that existed never do, so the wait is bounded.
        const designStarting = record.status === "completed" && record.kind !== null &&
          !directKinds.includes(record.kind) && !record.architecture_session_id && designPolls-- > 0;
        if (pendingStatuses.includes(record.status) || (record.delivery && deliveryPending(record.delivery)) ||
            designStarting) {
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
    rememberSessionId("requirements", id);
  }

  async function post(path: string, body?: unknown) {
    const response = await fetch(`/api/requirements/sessions${path}`, {
      method: "POST",
      ...(body === undefined
        ? {}
        : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    const parsed = await readBody(response);
    if (!response.ok) throw new Error(errorMessage(parsed, response.status));
    return parsed;
  }

  async function startSession(intake: Intake) {
    if (!repositoryChoice?.branch) return;
    setSubmitting(true);
    setError("");
    setSession(null);
    try {
      const body = await post("", { repository: selectionInput(repositoryChoice), ...intake });
      if (
        typeof body !== "object" ||
        body === null ||
        !("session_id" in body) ||
        typeof body.session_id !== "string"
      ) {
        throw new Error("The backend did not return a session ID.");
      }
      openSession(body.session_id);
    } catch (caught) {
      setError(failure(caught, "Could not start a session."));
    } finally {
      setSubmitting(false);
    }
  }

  /** Starts onboarding the chosen branch instead and opens it in the Onboarding tab. */
  async function onboard(repository: RepositorySelection) {
    if (!repository.branch) return;
    setOnboarding(true);
    setError("");
    try {
      window.location.assign(onboardingLink(await startOnboarding(repository)));
    } catch (caught) {
      setError(failure(caught, "Could not start the onboarding."));
      setOnboarding(false);
    }
  }

  function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void sendText();
  }

  async function sendText() {
    const content = message.trim();
    if (!session || !content) return;
    const warning = messageWarning(session.review);
    if (warning && !window.confirm(warning)) return;
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

  async function answerQuestions(answers: string[]) {
    if (!session) return false;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/answers`, { answers });
      const content = asked.map((item, index) => `${item.question}\n→ ${answers[index]}`).join("\n\n");
      setSession({
        ...session,
        status: "turn_running",
        error: null,
        messages: [...session.messages, { role: "user", content, questions: [] }],
      });
      setRefreshKey((value) => value + 1);
      return true;
    } catch (caught) {
      setError(failure(caught, "Could not send the answers."));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function decideFindings(instructions: string | null) {
    if (!session?.delivery) return false;
    setSubmitting(true);
    setError("");
    try {
      const path = `/${encodeURIComponent(session.session_id)}/delivery/findings`;
      await (instructions === null ? post(`${path}/accept`) : post(`${path}/instructions`, { instructions }));
      setSession({ ...session, delivery: decidedProgress(session.delivery, instructions) });
      setRefreshKey((value) => value + 1);
      return true;
    } catch (caught) {
      setError(failure(caught, instructions === null ? "Could not accept the findings." : "Could not send the instructions."));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function answerAgent(workflowId: string, answers: string[]) {
    if (!session?.delivery) return false;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/delivery/answers`,
        { workflow_id: workflowId, answers });
      setSession({ ...session, delivery: answeredProgress(session.delivery, workflowId) });
      setRefreshKey((value) => value + 1);
      return true;
    } catch (caught) {
      setError(failure(caught, "Could not send the answers."));
      return false;
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

  async function updateCheckout() {
    if (!session || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/checkout`);
      setSession({ ...session, status: "preparing", error: null });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not update the checkout."));
    } finally {
      setSubmitting(false);
    }
  }

  async function generateDocument(force = false) {
    if (!session) return;
    // While the review's questions are open, the document is only written despite them.
    if (!force && session.review.questions_open) {
      setConfirmingForce(true);
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/finalize${force ? "?force=true" : ""}`);
      setConfirmingForce(false);
      setSession({ ...session, status: "finalizing", error: null });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not generate the document."));
    } finally {
      setSubmitting(false);
    }
  }

  async function retryReview() {
    if (!session || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/review/retry`);
      setSession({
        ...session, status: "finalizing", error: null,
        review: { ...session.review, status: "running", error: null },
      });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not retry the review round."));
    } finally {
      setSubmitting(false);
    }
  }

  async function retryGeneration(stage: GenerationStage) {
    if (!session?.delivery || submitting || deliveryPending(session.delivery)) return;
    setSubmitting(true);
    setRetryingGeneration(stage);
    setError("");
    try {
      await post(`/${encodeURIComponent(session.session_id)}/generation/retry`, { stage });
      setSession({
        ...session,
        delivery: retriedProgress(session.delivery, stage, Boolean(session.checkout)),
      });
      setRefreshKey((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not retry this step."));
    } finally {
      setSubmitting(false);
      setRetryingGeneration(null);
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
      if (
        typeof body !== "object" ||
        body === null ||
        !("session_id" in body) ||
        typeof body.session_id !== "string"
      ) {
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

  // Scroll to the latest turn, not past it to the requirements list below.
  useEffect(() => {
    const el = conversation.current;
    const end = chatEnd.current;
    if (el && end) el.scrollTop = Math.max(0, end.offsetTop - el.clientHeight);
  }, [session?.messages.length, session?.status]);

  const unanswered = session?.messages.at(-1)?.role === "user" && session.status === "interviewing";
  const canCompose = session?.status === "interviewing" && !unanswered && !submitting;
  // Questions of the last message are answered with the form, not with a message.
  const last = session?.messages.at(-1);
  const asked = last?.role === "assistant" ? last.questions : [];
  const forkedFrom = session?.forked_from ?? null;
  const canFinalize = canCompose && session.kind !== null && session.requirements.length > 0;
  const reviewing = session?.review.status === "running";
  // Continuing despite the review is possible while a round runs, too.
  const canForce = Boolean(session) && !submitting && session!.kind !== null &&
    session!.requirements.length > 0 && !unanswered &&
    (session!.status === "interviewing" || (session!.status === "finalizing" && reviewing));
  const canUpdateCheckout = canCompose && Boolean(session.checkout);
  const status = error ? "error" : session ? session.status : sessionId ? "loading" : "idle";
  const documentUrl = session
    ? `/api/requirements/sessions/${encodeURIComponent(session.session_id)}/document`
    : "";

  return (
    <RequirementLookupProvider requirements={session?.lookup_requirements ?? null}>
    <main className="page-shell">
      <header className="page-heading">
        <h1>Requirements interview</h1>
        <p className="page-intro">
          Choose the kind of task, answer a few questions about it, and describe it. The
          LLM then asks follow-up questions, records requirements as you go, and writes
          them up as a document. A bugfix is then delivered from here: regression tests,
          the fix, and a new branch.
        </p>
      </header>

      <div className="workspace">
        <section className="request-panel" aria-labelledby="interview-title">
          <div className="section-heading">
            <h2 id="interview-title">{session ? "Your answer" : "New session"}</h2>
          </div>

          <AgentQuestionsPanel progress={session?.delivery} answering={submitting} onAnswer={answerAgent} />

          {session && asked.length > 0 ? (
            <div className="message-form">
              <QuestionForm key={session.messages.length} questions={asked}
                disabled={!canCompose} submitting={submitting}
                onSubmit={answerQuestions} />
              {session.review.questions_open && (
                <p className="field-hint">
                  Or write a message below the questions instead: it drops them, and the
                  next review round starts when you generate the document again.
                </p>
              )}
              {session.review.questions_open && (
                <MessageBox message={message} onChange={setMessage} disabled={!canCompose}
                  submitting={submitting} onSend={() => void sendText()} />
              )}
              <button
                className="generate-button"
                type="button"
                onClick={() => void generateDocument()}
                disabled={session.review.questions_open ? !canForce : !canFinalize}
              >
                {session.review.questions_open ? "Generate document anyway…" : "Generate document"}
              </button>
            </div>
          ) : session ? (
            <form className="message-form" onSubmit={sendMessage}>
              <label htmlFor="requirements-message">Message</label>
              <textarea
                id="requirements-message"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                maxLength={10000}
                rows={6}
                placeholder="Answer the questions or add details…"
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
                onClick={() => void generateDocument()}
                disabled={!canFinalize}
              >
                {session.review.status === "contradictions_found" || session.review.status === "failed"
                  ? "Generate document (review again)" : "Generate document"}
              </button>
            </form>
          ) : (
            !sessionId && (
              <IntakeForm
                repository={<RepositoryPicker value={repositoryChoice} onChange={setRepositoryChoice}
                  disabled={submitting || onboarding} required
                  onOnboard={(repository) => void onboard(repository)} onboarding={onboarding} />}
                repositoryReady={Boolean(repositoryChoice?.branch)}
                disabled={submitting}
                submitting={submitting}
                onSubmit={(intake) => void startSession(intake)}
              />
            )
          )}

          {session && (
            <button
              className="new-conversation"
              type="button"
              onClick={() => {
                setSession(null);
                setRepositoryChoice(null);
                setSessionId("");
                setLookupId("");
                setMessage("");
                setError("");
                rememberSessionId("requirements", "");
              }}
            >
              Start a new session
            </button>
          )}

          <form className="lookup-form" onSubmit={retrieveSession}>
            <label htmlFor="session-id">Retrieve by session ID</label>
            <div className="lookup-controls">
              <input
                id="session-id"
                value={lookupId}
                onChange={(event) => setLookupId(event.target.value)}
                placeholder="requirements-session-..."
                disabled={submitting}
              />
              <button type="submit" disabled={submitting || !lookupId.trim()}>
                Find
              </button>
            </div>
          </form>
        </section>

        <section className="response-panel" aria-labelledby="session-title">
          <div className="response-header">
            <div className="section-heading">
              <h2 id="session-title">Interview</h2>
            </div>
            <span className={`status status-${status}`}>
              <span className="status-dot" aria-hidden="true" />
              {status === "idle" && "Waiting"}
              {status === "loading" && "Loading"}
              {status === "error" && "Error"}
              {session && !error && (reviewing ? "Reviewing requirements" : statusLabels[session.status])}
            </span>
          </div>

          <RepositorySummary repository={session?.repository ?? (submitting && !session ? repositoryChoice : null)}
            checkout={session?.checkout} onUpdate={canUpdateCheckout ? updateCheckout : undefined} />

          {sessionId && (
            <p className="request-id">
              Session ID <code>{sessionId}</code>
            </p>
          )}

          {session && (
            <p className="request-id">
              Task kind <strong>{session.kind ? kindLabels[session.kind] : "Not chosen yet"}</strong>
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
                    <span className="turn-label">{item.role === "user" ? "You" : "Analyst"}</span>
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

            {session && (
              <ReviewPanel review={session.review} disabled={submitting}
                onRetry={() => void retryReview()}
                onForce={canForce ? () => setConfirmingForce(true) : undefined} />
            )}

            {session && pendingStatuses.includes(session.status) && (
              <div className="pending-answer" role="status">
                <span className="loading-line" aria-hidden="true" />
                <p>
                  {session.status === "finalizing" && reviewing
                    ? "Reviewing the requirements for contradictions…"
                    : session.status === "finalizing"
                    ? "Writing the requirements document…"
                    : session.status === "preparing"
                      ? "Checking out the repository…"
                      : "Waiting for the analyst…"}
                </p>
              </div>
            )}

            <div ref={chatEnd} aria-hidden="true" />

            {session && (session.document || session.delivery) && (
              <section className="generated-documents" aria-label="Requirements document">
                {session.document ? (
                  <>
                    <h3>Requirements document</h3>
                    <ul>
                      <li>
                        <a href={documentUrl} download={session.document.filename}>
                          {session.document.filename}
                        </a>
                        <span>revision {session.document.revision}</span>
                      </li>
                    </ul>
                  </>
                ) : (
                  // A fix flow of a closed delivery has no document: only its delivery,
                  // which pushes to a new branch and opens a new pull request.
                  <h3>Fix round on a new branch</h3>
                )}
                {session.architecture_session_id && (
                  <a className="next-step"
                    href={`?mode=architecture&session_id=${encodeURIComponent(session.architecture_session_id)}`}>
                    Continue with the architecture →
                  </a>
                )}
                {session.delivery && (
                  <DeliveryResults progress={session.delivery} kind={session.kind} retrying={retryingGeneration}
                    documentation={session.kind === "docs"}
                    disabled={submitting || deliveryPending(session.delivery)}
                    onRetry={(stage) => void retryGeneration(stage)}
                    onFindings={decideFindings} />
                )}
              </section>
            )}

            {session && (
              <section className="requirements-list" aria-label="Requirements gathered">
                <h3>Requirements gathered ({session.requirements.length})</h3>
                {categories.map(([category, label]) => {
                  const items = session.requirements.filter(
                    (item) => item.category === category,
                  );
                  return (
                    <div className="requirement-group" key={category}>
                      <h4>{label}</h4>
                      {items.length === 0 ? (
                        <p className="requirement-empty">None yet.</p>
                      ) : (
                        <ul>
                          {items.map((item) => (
                            <li key={item.id}>
                              <div className="requirement-heading">
                                <code>{item.id}</code>
                                <strong>{item.title}</strong>
                                <span className={`priority priority-${item.priority}`}>
                                  {item.priority}
                                </span>
                              </div>
                              <p>{item.description}</p>
                              {item.rationale && <small>{item.rationale}</small>}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  );
                })}
                {session.retired.length > 0 && (
                  <div className="requirement-group">
                    <h4>Retired from the existing service</h4>
                    <ul>
                      {session.retired.map((item) => (
                        <li key={item.id}>
                          <div className="requirement-heading">
                            <code>{item.id}</code>
                            <strong>{item.title}</strong>
                          </div>
                          <p>{item.description}</p>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </section>
            )}

            {!session && !error && (
              <div className="empty-state">
                <p>{sessionId ? "Loading session…" : "Your interview starts here."}</p>
                <span>
                  {sessionId
                    ? "Querying the workflow in Temporal."
                    : "Start a session or retrieve an existing one."}
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
      {session && (
        <ForceDialog open={confirmingForce} review={session.review} busy={submitting}
          what="write the requirements document and start the next stage"
          onConfirm={() => void generateDocument(true)} onCancel={() => setConfirmingForce(false)} />
      )}
    </main>
    </RequirementLookupProvider>
  );
}
