import { useEffect, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import { errorMessage, failure, readBody } from "./api";
import { ApiModelSection, isApiModel, type ApiModel } from "./ApiModel";
import {
  DeliveryResults, answeredProgress, decidedProgress, deliveryPending, deliveryProgress, retriedProgress,
  type DeliveryProgress, type GenerationStage,
} from "./Delivery";
import ForkButton from "./ForkButton";
import { AskedQuestions, QuestionForm, questionList, type Question } from "./Questions";
import RepositoryPicker, { RepositorySummary } from "./RepositoryPicker";
import {
  repositoryCheckout, repositorySelection, selectionInput, type RepositoryCheckout, type RepositorySelection,
} from "./github";
import { markdownComponents, remarkPlugins } from "./markdown";

type OnboardingStatus =
  | "preparing"
  | "awaiting_confirmation"
  | "declined"
  | "drafting"
  | "interviewing"
  | "turn_running"
  | "finalizing"
  | "completed"
  | "failed"
  | "superseded";
type Artifact = "contract" | "contract_tests" | "dockerfile";
type Category = "functional" | "non_functional" | "technical";

type Requirement = {
  id: string;
  category: Category;
  title: string;
  description: string;
  priority: "must" | "should" | "could";
  rationale?: string;
};

type Session = {
  session_id: string;
  status: OnboardingStatus;
  repository: RepositorySelection | null;
  checkout: RepositoryCheckout | null;
  forked_from: { session_id: string; message_index: number } | null;
  existing_contract: boolean;
  existing_artifacts: Artifact[];
  replaced_contract: boolean;
  // An assistant message's questions are answered by the next user message.
  messages: { role: "user" | "assistant"; content: string; questions: Question[] }[];
  requirements: Requirement[];
  api: ApiModel;
  language: string | null;
  problems: string[];
  error: string | null;
  // The delivery of the contract, contract tests and Dockerfile, once finalized.
  delivery: DeliveryProgress | null;
};

const statuses: OnboardingStatus[] = [
  "preparing", "awaiting_confirmation", "declined", "drafting", "interviewing", "turn_running",
  "finalizing", "completed", "failed", "superseded",
];
const pendingStatuses: OnboardingStatus[] = ["preparing", "drafting", "turn_running", "finalizing"];
// An onboarding in one of these is over: it takes no messages and cannot be forked.
const finishedStatuses: OnboardingStatus[] = ["completed", "failed", "declined", "superseded"];
const statusLabels: Record<OnboardingStatus, string> = {
  preparing: "Checking out repository",
  awaiting_confirmation: "Waiting for your decision",
  declined: "Declined",
  drafting: "Drafting the contract",
  interviewing: "Reviewing drafts",
  turn_running: "Thinking",
  finalizing: "Finalizing",
  completed: "Delivering",
  failed: "Failed",
  superseded: "Superseded by a fork",
};
const artifactLabels: Record<Artifact, string> = {
  contract: "a contract in contracts/",
  contract_tests: "contract tests in contract-tests/",
  dockerfile: "a root Dockerfile",
};
const categories: [Category, string][] = [
  ["functional", "Functional"],
  ["non_functional", "Non-functional"],
  ["technical", "Technical"],
];
const NOT_DRAFTED = "The contract has not been drafted yet.";

/** The link that opens an onboarding's view over the Flows page. */
export function onboardingLink(sessionId: string) {
  return `?mode=onboarding&session_id=${encodeURIComponent(sessionId)}`;
}

function sessionIdResult(body: unknown): string {
  if (typeof body !== "object" || body === null || !("session_id" in body) ||
      typeof body.session_id !== "string") {
    throw new Error("The backend did not return a session ID.");
  }
  return body.session_id;
}

/** Starts onboarding the repository's branch; returns the onboarding's session ID. */
export async function startOnboarding(repository: RepositorySelection): Promise<string> {
  const response = await fetch("/api/onboarding/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repository: selectionInput(repository) }),
  });
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return sessionIdResult(body);
}

function sessionIdFromUrl() {
  return new URLSearchParams(window.location.search).get("session_id") ?? "";
}

function rememberSessionId(sessionId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "onboarding");
  if (sessionId) url.searchParams.set("session_id", sessionId);
  else url.searchParams.delete("session_id");
  window.history.replaceState(null, "", url);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isRequirement(item: unknown): item is Requirement {
  return isObject(item) && typeof item.id === "string" &&
    categories.some(([category]) => category === item.category) &&
    typeof item.title === "string" && typeof item.description === "string" &&
    typeof item.priority === "string";
}

function sessionRecord(value: unknown): Session {
  if (
    !isObject(value) ||
    typeof value.session_id !== "string" ||
    !(statuses as unknown[]).includes(value.status) ||
    !Array.isArray(value.messages) ||
    !value.messages.every((item) => isObject(item) &&
      (item.role === "user" || item.role === "assistant") && typeof item.content === "string") ||
    !Array.isArray(value.requirements) ||
    !value.requirements.every(isRequirement) ||
    !isApiModel(value.api) ||
    !Array.isArray(value.problems) ||
    !Array.isArray(value.existing_artifacts)
  ) {
    throw new Error("The backend returned an invalid onboarding.");
  }
  const repository = repositorySelection(value.repository);
  return {
    session_id: value.session_id,
    status: value.status as OnboardingStatus,
    repository,
    checkout: repositoryCheckout(value.checkout),
    forked_from:
      isObject(value.forked_from) && typeof value.forked_from.session_id === "string" &&
      typeof value.forked_from.message_index === "number"
        ? { session_id: value.forked_from.session_id, message_index: value.forked_from.message_index }
        : null,
    existing_contract: value.existing_contract === true,
    existing_artifacts: value.existing_artifacts.filter(
      (item): item is Artifact => typeof item === "string" && item in artifactLabels),
    replaced_contract: value.replaced_contract === true,
    messages: value.messages.map((item) => ({
      role: item.role, content: item.content, questions: questionList(item.questions),
    })),
    requirements: value.requirements as Requirement[],
    api: value.api,
    language: typeof value.language === "string" ? value.language : null,
    problems: value.problems.filter((item): item is string => typeof item === "string"),
    error: typeof value.error === "string" ? value.error : null,
    delivery: isObject(value.delivery) ? deliveryProgress(value.delivery, repository) : null,
  };
}

function artifactList(artifacts: Artifact[]) {
  const labels = artifacts.map((item) => artifactLabels[item]);
  return labels.length < 2 ? labels.join("") : `${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
}

export default function Onboarding() {
  const [sessionId, setSessionId] = useState(sessionIdFromUrl);
  const [lookupId, setLookupId] = useState(sessionIdFromUrl);
  const [refreshKey, setRefreshKey] = useState(0);
  const [session, setSession] = useState<Session | null>(null);
  const [message, setMessage] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [retryingGeneration, setRetryingGeneration] = useState<GenerationStage | null>(null);
  const [error, setError] = useState("");
  const [repositoryChoice, setRepositoryChoice] = useState<RepositorySelection | null>(null);
  const conversation = useRef<HTMLDivElement>(null);
  const chatEnd = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!sessionId) return;
    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      try {
        const response = await fetch(
          `/api/onboarding/sessions/${encodeURIComponent(sessionId)}`,
          { signal: controller.signal },
        );
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        if (controller.signal.aborted) return;
        const record = sessionRecord(body);
        setSession(record);
        setError("");
        if (pendingStatuses.includes(record.status) ||
            (record.status === "completed" && (!record.delivery || deliveryPending(record.delivery)))) {
          timer = window.setTimeout(poll, 2000);
        }
      } catch (caught) {
        if (controller.signal.aborted) return;
        setError(failure(caught, "Could not retrieve this onboarding."));
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
    const response = await fetch(`/api/onboarding/sessions${path}`, {
      method: "POST",
      ...(body === undefined
        ? {}
        : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    const parsed = await readBody(response);
    if (!response.ok) throw new Error(errorMessage(parsed, response.status));
    return parsed;
  }

  /** Runs an action of the open onboarding, shows its effect at once and polls again. */
  async function act(action: (id: string) => Promise<unknown>, next: (current: Session) => Session,
                     fallback: string) {
    if (!session) return false;
    setSubmitting(true);
    setError("");
    try {
      await action(encodeURIComponent(session.session_id));
      setSession(next(session));
      setRefreshKey((value) => value + 1);
      return true;
    } catch (caught) {
      setError(failure(caught, fallback));
      return false;
    } finally {
      setSubmitting(false);
    }
  }

  async function start(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!repositoryChoice?.branch) return;
    setSubmitting(true);
    setError("");
    setSession(null);
    try {
      openSession(await startOnboarding(repositoryChoice));
    } catch (caught) {
      setError(failure(caught, "Could not start the onboarding."));
    } finally {
      setSubmitting(false);
    }
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = message.trim();
    if (!content) return;
    const sent = await act((id) => post(`/${id}/messages`, { message: content }), (current) => ({
      ...current, status: "turn_running", error: null,
      messages: [...current.messages, { role: "user", content, questions: [] }],
    }), "Could not send the message.");
    if (sent) setMessage("");
  }

  function answerQuestions(answers: string[]) {
    const content = asked.map((item, index) => `${item.question}\n→ ${answers[index]}`).join("\n\n");
    return act((id) => post(`/${id}/answers`, { answers }), (current) => ({
      ...current, status: "turn_running", error: null,
      messages: [...current.messages, { role: "user", content, questions: [] }],
    }), "Could not send the answers.");
  }

  function decideContract(replace: boolean) {
    return act((id) => post(`/${id}/contract-decision`, { replace_contract: replace }), (current) => ({
      ...current, error: null, replaced_contract: replace,
      status: !replace ? "declined" : drafted(current) ? "interviewing" : "drafting",
    }), "Could not record the decision.");
  }

  function retryTurn() {
    return act((id) => post(`/${id}/retry`), (current) => ({
      ...current, error: null, status: drafted(current) ? "turn_running" : "drafting",
    }), "Could not retry.");
  }

  function updateCheckout() {
    return act((id) => post(`/${id}/checkout`), (current) => ({
      ...current, error: null, status: "preparing",
    }), "Could not update the checkout.");
  }

  function finalize() {
    return act((id) => post(`/${id}/finalize`), (current) => ({
      ...current, error: null, status: "finalizing",
    }), "Could not finalize the onboarding.");
  }

  function answerAgent(workflowId: string, answers: string[]) {
    return act((id) => post(`/${id}/delivery/answers`, { workflow_id: workflowId, answers }),
      (current) => ({ ...current, delivery: current.delivery && answeredProgress(current.delivery, workflowId) }),
      "Could not send the answers.");
  }

  function decideFindings(instructions: string | null) {
    return act((id) => instructions === null
      ? post(`/${id}/delivery/findings/accept`)
      : post(`/${id}/delivery/findings/instructions`, { instructions }),
    (current) => ({ ...current, delivery: current.delivery && decidedProgress(current.delivery, instructions) }),
    instructions === null ? "Could not accept the findings." : "Could not send the instructions.");
  }

  async function retryGeneration(stage: GenerationStage) {
    if (!session?.delivery || deliveryPending(session.delivery)) return;
    setRetryingGeneration(stage);
    await act((id) => post(`/${id}/generation/retry`, { stage }), (current) => ({
      ...current,
      delivery: current.delivery && retriedProgress(current.delivery, stage, Boolean(current.checkout)),
    }), "Could not retry this step.");
    setRetryingGeneration(null);
  }

  async function forkSession(messageIndex: number) {
    if (!session) return;
    setSubmitting(true);
    setError("");
    try {
      const forkId = sessionIdResult(
        await post(`/${encodeURIComponent(session.session_id)}/fork`, { message_index: messageIndex }));
      setSession(null);
      setMessage("");
      openSession(forkId);
    } catch (caught) {
      setError(failure(caught, "Could not fork the onboarding."));
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

  function startNew() {
    setSession(null);
    setRepositoryChoice(null);
    setSessionId("");
    setLookupId("");
    setMessage("");
    setError("");
    rememberSessionId("");
  }

  // Scroll to the latest turn, not past it to the drafts below.
  useEffect(() => {
    const el = conversation.current;
    const end = chatEnd.current;
    if (el && end) el.scrollTop = Math.max(0, end.offsetTop - el.clientHeight);
  }, [session?.messages.length, session?.status]);

  const last = session?.messages.at(-1);
  const unanswered = last?.role === "user" && session?.status === "interviewing";
  const isDrafted = session ? drafted(session) : false;
  const canCompose = session?.status === "interviewing" && isDrafted && !unanswered && !submitting;
  // Questions of the last message are answered with the form, not with a message.
  const asked = last?.role === "assistant" ? last.questions : [];
  const canRetry = session?.status === "interviewing" && (!isDrafted || unanswered) && !submitting;
  const canFinalize = canCompose && session.problems.length === 0;
  const canUpdateCheckout = Boolean(session?.checkout) && !submitting && !unanswered &&
    (session?.status === "interviewing" || session?.status === "awaiting_confirmation");
  const canFork = Boolean(session) && !submitting &&
    (session?.status === "interviewing" || session?.status === "awaiting_confirmation");
  const status = error ? "error" : session ? session.status : sessionId ? "loading" : "idle";

  const finalizeButton = session && !finishedStatuses.includes(session.status) && (
    <button className="generate-button" type="button" onClick={() => void finalize()}
      disabled={!canFinalize}
      title={session.problems.length ? "Resolve the open problems first" : undefined}>
      Finalize and deliver
    </button>
  );

  return (
    <main className="page-shell">
      <header className="page-heading">
        <h1>Onboarding</h1>
        <p className="page-intro">
          Bring an existing repository under contract. The LLM drafts the requirements, the API
          and the language from the code; you review and correct the drafts. Finalizing writes the
          contract, contract tests and a root Dockerfile, verifies the tests against the existing
          code, and opens a pull request from a new branch.
        </p>
      </header>

      <div className="workspace">
        <section className="request-panel" aria-labelledby="onboarding-title">
          <div className="section-heading">
            <h2 id="onboarding-title">{session ? "Your review" : "New onboarding"}</h2>
          </div>

          {session?.status === "awaiting_confirmation" ? (
            <div className="message-form contract-decision" role="group" aria-label="Existing contract">
              <p className="connector-error" role="alert">
                The branch already has {artifactList(session.existing_artifacts)}. Onboarding replaces
                {session.existing_artifacts.length > 1 ? " them" : " it"}. Declining ends the
                onboarding and leaves the existing files unchanged.
              </p>
              <button className="run-button" type="button" disabled={submitting}
                onClick={() => void decideContract(true)}>
                <span>Replace and continue</span>
              </button>
              <button className="generate-button" type="button" disabled={submitting}
                onClick={() => void decideContract(false)}>
                Keep the existing files (decline)
              </button>
            </div>
          ) : session && asked.length > 0 ? (
            <div className="message-form">
              <QuestionForm key={session.messages.length} questions={asked}
                disabled={!canCompose} submitting={submitting}
                onSubmit={answerQuestions} />
              {finalizeButton}
            </div>
          ) : session ? (
            <form className="message-form" onSubmit={sendMessage}>
              <label htmlFor="onboarding-message">Message</label>
              <textarea
                id="onboarding-message"
                value={message}
                onChange={(event) => setMessage(event.target.value)}
                maxLength={10000}
                rows={6}
                placeholder="Correct the drafts or answer the questions…"
                disabled={!canCompose}
              />
              <button className="run-button" type="submit" disabled={!canCompose || !message.trim()}>
                <span>{submitting ? "Sending" : "Send"}</span>
                {submitting ? (
                  <span className="spinner" aria-hidden="true" />
                ) : (
                  <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                    <path d="M4.5 10h10m-4-4 4 4-4 4" />
                  </svg>
                )}
              </button>
              {finalizeButton}
            </form>
          ) : (
            !sessionId && (
              <form className="message-form" onSubmit={(event) => void start(event)}>
                <RepositoryPicker value={repositoryChoice} onChange={setRepositoryChoice}
                  disabled={submitting} required />
                <button className="run-button" type="submit"
                  disabled={submitting || !repositoryChoice?.branch}>
                  <span>{submitting ? "Starting" : "Start onboarding"}</span>
                  {submitting && <span className="spinner" aria-hidden="true" />}
                </button>
              </form>
            )
          )}

          {session && (
            <button className="new-conversation" type="button" onClick={startNew}>
              Start a new onboarding
            </button>
          )}

          <form className="lookup-form" onSubmit={retrieveSession}>
            <label htmlFor="onboarding-session-id">Retrieve by session ID</label>
            <div className="lookup-controls">
              <input
                id="onboarding-session-id"
                value={lookupId}
                onChange={(event) => setLookupId(event.target.value)}
                placeholder="onboarding-session-..."
                disabled={submitting}
              />
              <button type="submit" disabled={submitting || !lookupId.trim()}>Find</button>
            </div>
          </form>
          <p className="field-hint">
            <a href="?type=OnboardingWorkflow">All onboardings</a>
          </p>
        </section>

        <section className="response-panel" aria-labelledby="onboarding-review-title">
          <div className="response-header">
            <div className="section-heading">
              <h2 id="onboarding-review-title">Review</h2>
            </div>
            <span className={`status status-${status}`}>
              <span className="status-dot" aria-hidden="true" />
              {status === "idle" && "Waiting"}
              {status === "loading" && "Loading"}
              {status === "error" && "Error"}
              {session && !error && statusLabels[session.status]}
            </span>
          </div>

          <RepositorySummary repository={session?.repository ?? (submitting && !session ? repositoryChoice : null)}
            checkout={session?.checkout} onUpdate={canUpdateCheckout ? () => void updateCheckout() : undefined} />

          {sessionId && (
            <p className="request-id">
              Session ID <code>{sessionId}</code>
            </p>
          )}

          {session && session.existing_contract && session.status !== "awaiting_confirmation" && (
            <p className="request-id">
              The branch had {artifactList(session.existing_artifacts)}
              {session.replaced_contract ? "; the onboarding replaces it." : "."}
            </p>
          )}

          {session?.forked_from && (
            <p className="fork-note">
              Forked after message {session.forked_from.message_index + 1} of another onboarding.
              <button type="button" onClick={() => {
                const origin = session.forked_from!.session_id;
                setSession(null);
                openSession(origin);
              }}>
                Open the original
              </button>
            </p>
          )}

          {session?.status === "superseded" && (
            <p className="fork-note">This onboarding was forked and ends here; continue in the fork.</p>
          )}
          {session?.status === "declined" && (
            <p className="fork-note">You kept the existing files. Nothing was changed.</p>
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
                        <Markdown remarkPlugins={remarkPlugins} components={markdownComponents}>
                          {item.content}
                        </Markdown>
                        <AskedQuestions questions={item.questions} />
                      </div>
                    ) : (
                      <p className="turn-content">{item.content}</p>
                    )}
                    <ForkButton role={item.role} disabled={!canFork}
                      onFork={() => void forkSession(index)} />
                  </li>
                ))}
              </ol>
            )}

            {session && pendingStatuses.includes(session.status) && (
              <div className="pending-answer" role="status">
                <span className="loading-line" aria-hidden="true" />
                <p>
                  {session.status === "preparing"
                    ? "Checking out the repository…"
                    : session.status === "drafting"
                      ? "Reading the code and drafting the requirements, the API and the language…"
                      : session.status === "finalizing"
                        ? "Fixing the drafts as the contract…"
                        : "Waiting for the analyst…"}
                </p>
              </div>
            )}

            <div ref={chatEnd} aria-hidden="true" />

            {session?.status === "completed" && (
              <section className="generated-documents" aria-label="Onboarding delivery">
                <h3>Contract, contract tests and Dockerfile</h3>
                {session.delivery ? (
                  <DeliveryResults progress={session.delivery} onboarding retrying={retryingGeneration}
                    disabled={submitting || deliveryPending(session.delivery)}
                    onRetry={(stage) => void retryGeneration(stage)}
                    answering={submitting} onAnswer={answerAgent}
                    onFindings={decideFindings} />
                ) : <p role="status">Starting the delivery…</p>}
              </section>
            )}

            {session && isDrafted && (
              <section className="requirements-list" aria-label="Drafted requirements">
                <h3>Drafted requirements ({session.requirements.length})</h3>
                {categories.map(([category, label]) => {
                  const items = session.requirements.filter((item) => item.category === category);
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
                                <span className={`priority priority-${item.priority}`}>{item.priority}</span>
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
              </section>
            )}

            {session && isDrafted && (
              <ApiModelSection api={session.api} problems={session.problems}
                requirements={session.requirements}>
                <p className="api-auth">Language: {session.language ?? "not detected yet"}</p>
              </ApiModelSection>
            )}

            {!session && !error && (
              <div className="empty-state">
                <p>{sessionId ? "Loading onboarding…" : "Your onboarding starts here."}</p>
                <span>
                  {sessionId
                    ? "Querying the workflow in Temporal."
                    : "Choose a repository and branch, or retrieve an existing onboarding."}
                </span>
              </div>
            )}

            {(error || session?.error) && (
              <div className="error-state" role="alert">
                <p>{error ? "Something went wrong." : "The last step did not complete."}</p>
                <span>{error || session?.error}</span>
                {canRetry && (
                  <button type="button" onClick={() => void retryTurn()}>
                    {isDrafted ? "Retry this message" : "Retry drafting"}
                  </button>
                )}
                {error && sessionId && (
                  <button type="button" onClick={() => setRefreshKey((value) => value + 1)}>
                    Reload onboarding
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

/** Whether the drafts were written, so that the interview can run. */
function drafted(session: Session) {
  return !session.problems.includes(NOT_DRAFTED);
}
