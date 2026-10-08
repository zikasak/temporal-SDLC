import { useEffect, useRef, useState, type FormEvent } from "react";
import Markdown from "react-markdown";
import { errorMessage, readBody } from "./api";
import ForkButton from "./ForkButton";
import RepositoryPicker, { RepositorySummary } from "./RepositoryPicker";
import { repositorySelection, selectionInput, type RepositorySelection } from "./github";
import { markdownComponents, remarkPlugins } from "./markdown";

type RequestStatus =
  | "running"
  | "completed"
  | "failed"
  | "canceled"
  | "terminated"
  | "timed_out";

type ConversationMessage = {
  role: "user" | "assistant";
  content: string;
  attachment?: AttachmentMetadata;
};

type AttachmentMetadata = {
  document_id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
};

type GeneratedDocument = AttachmentMetadata & {
  revision: number;
};

type RequestRecord = {
  request_id: string;
  status: RequestStatus;
  result: string | null;
  error: string | null;
  messages: ConversationMessage[];
  parent_request_id: string | null;
  generated_documents: GeneratedDocument[];
  repository: RepositorySelection | null;
};

type Submission = {
  content: string;
  parentRequestId: string | null;
  priorMessages: ConversationMessage[];
  fallbackRecord: RequestRecord | null;
  selectedFile: File | null;
  forkAfterIndex: number | null;
  repository: RepositorySelection | null;
};

type ViewState =
  | { kind: "idle" }
  | { kind: "loading"; requestId: string }
  | { kind: "submitting"; messages: ConversationMessage[]; repository: RepositorySelection | null }
  | { kind: "request"; record: RequestRecord }
  | {
      kind: "error";
      phase: "submit" | "retrieve";
      message: string;
      record: RequestRecord | null;
      submission?: Submission;
    };

const terminalStatuses: RequestStatus[] = [
  "completed",
  "failed",
  "canceled",
  "terminated",
  "timed_out",
];
const maxFileBytes = 20 * 1024 * 1024;

function requestIdFromUrl() {
  return new URLSearchParams(window.location.search).get("request_id") ?? "";
}

function rememberRequestId(requestId: string) {
  const url = new URL(window.location.href);
  if (requestId) url.searchParams.set("request_id", requestId);
  else url.searchParams.delete("request_id");
  window.history.replaceState(null, "", url);
}

function fileSize(bytes: number) {
  if (bytes === 0) return "0 B";
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function requestRecord(value: unknown): RequestRecord {
  if (
    typeof value !== "object" ||
    value === null ||
    !("request_id" in value) ||
    typeof value.request_id !== "string" ||
    !("status" in value) ||
    typeof value.status !== "string" ||
    !(["running", ...terminalStatuses] as string[]).includes(value.status)
  ) {
    throw new Error("The backend returned an invalid request status.");
  }

  const messages = "messages" in value ? value.messages : [];
  const generatedDocuments =
    "generated_documents" in value ? value.generated_documents : [];
  if (
    !Array.isArray(messages) ||
    !messages.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        "role" in item &&
        (item.role === "user" || item.role === "assistant") &&
        "content" in item &&
        typeof item.content === "string" &&
        (!("attachment" in item) ||
          (typeof item.attachment === "object" &&
            item.attachment !== null &&
            "document_id" in item.attachment &&
            typeof item.attachment.document_id === "string" &&
            "filename" in item.attachment &&
            typeof item.attachment.filename === "string" &&
            "content_type" in item.attachment &&
            typeof item.attachment.content_type === "string" &&
            "size_bytes" in item.attachment &&
            typeof item.attachment.size_bytes === "number")),
    )
  ) {
    throw new Error("The backend returned an invalid conversation.");
  }
  if (
    !Array.isArray(generatedDocuments) ||
    !generatedDocuments.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        typeof item.document_id === "string" &&
        typeof item.filename === "string" &&
        typeof item.content_type === "string" &&
        typeof item.size_bytes === "number" &&
        typeof item.revision === "number",
    )
  ) {
    throw new Error("The backend returned an invalid document list.");
  }

  return {
    request_id: value.request_id,
    status: value.status as RequestStatus,
    result:
      "result" in value && typeof value.result === "string" ? value.result : null,
    error:
      "error" in value && typeof value.error === "string" ? value.error : null,
    messages: messages as ConversationMessage[],
    generated_documents: generatedDocuments as GeneratedDocument[],
    repository: repositorySelection("repository" in value ? value.repository : null),
    parent_request_id:
      "parent_request_id" in value && typeof value.parent_request_id === "string"
        ? value.parent_request_id
        : null,
  };
}

function visibleRecord(view: ViewState): RequestRecord | null {
  if (view.kind === "request") return view.record;
  if (view.kind === "error") return view.record;
  return null;
}

export default function App() {
  const [message, setMessage] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState("");
  const [repositoryChoice, setRepositoryChoice] = useState<RepositorySelection | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const conversation = useRef<HTMLDivElement>(null);
  const [requestId, setRequestId] = useState(requestIdFromUrl);
  const [lookupId, setLookupId] = useState(requestIdFromUrl);
  const [refreshKey, setRefreshKey] = useState(0);
  // The answer a new message will branch from, instead of the latest one.
  const [forkAfter, setForkAfter] = useState<number | null>(null);
  const [view, setView] = useState<ViewState>(() => {
    const id = requestIdFromUrl();
    return id ? { kind: "loading", requestId: id } : { kind: "idle" };
  });

  useEffect(() => {
    if (!requestId) return;

    const controller = new AbortController();
    let timer: number | undefined;

    async function poll() {
      try {
        const response = await fetch(
          `/api/llm/requests/${encodeURIComponent(requestId)}`,
          { signal: controller.signal },
        );
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        if (controller.signal.aborted) return;

        const record = requestRecord(body);
        setView({ kind: "request", record });
        if (!terminalStatuses.includes(record.status)) {
          timer = window.setTimeout(poll, 2000);
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setView((current) => ({
          kind: "error",
          phase: "retrieve",
          message:
            error instanceof TypeError
              ? "Could not reach the backend. Check that it is running, then try again."
              : error instanceof Error
                ? error.message
                : "Could not retrieve this request.",
          record:
            current.kind === "request" && current.record.request_id === requestId
              ? current.record
              : null,
        }));
      }
    }

    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [requestId, refreshKey]);

  async function sendMessage(
    content: string,
    parentRequestId: string | null,
    priorMessages: ConversationMessage[],
    fallbackRecord: RequestRecord | null,
    selectedFile: File | null,
    forkAfterIndex: number | null,
    repository: RepositorySelection | null = parentRequestId ? fallbackRecord?.repository ?? null : repositoryChoice,
  ) {
    const attachment: AttachmentMetadata | undefined = selectedFile
      ? {
          document_id: "",
          filename: selectedFile.name,
          content_type: selectedFile.type,
          size_bytes: selectedFile.size,
        }
      : undefined;
    const pendingMessages: ConversationMessage[] = [
      ...priorMessages,
      { role: "user", content, ...(attachment ? { attachment } : {}) },
    ];
    setView({ kind: "submitting", messages: pendingMessages, repository });

    try {
      const payload = selectedFile ? new FormData() : null;
      if (payload && selectedFile) {
        payload.append("message", content);
        if (parentRequestId) payload.append("parent_request_id", parentRequestId);
        if (forkAfterIndex !== null) payload.append("fork_after", String(forkAfterIndex));
        payload.append("file", selectedFile);
        if (!parentRequestId && repository) payload.append("repository", JSON.stringify(selectionInput(repository)));
      }
      const response = await fetch("/api/llm/requests", {
        method: "POST",
        ...(payload ? {} : { headers: { "Content-Type": "application/json" } }),
        body:
          payload ??
          JSON.stringify({
            message: content,
            ...(parentRequestId ? { parent_request_id: parentRequestId } : {}),
            ...(forkAfterIndex !== null ? { fork_after: forkAfterIndex } : {}),
            ...(!parentRequestId && repository ? { repository: selectionInput(repository) } : {}),
          }),
      });
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      if (
        typeof body !== "object" ||
        body === null ||
        !("request_id" in body) ||
        typeof body.request_id !== "string"
      ) {
        throw new Error("The backend did not return a request ID.");
      }

      const id = body.request_id;
      setMessage("");
      setForkAfter(null);
      setFile(null);
      setFileError("");
      if (fileInput.current) fileInput.current.value = "";
      setLookupId(id);
      setRequestId(id);
      rememberRequestId(id);
      setView({
        kind: "request",
        record: {
          request_id: id,
          status: "running",
          result: null,
          error: null,
          messages: pendingMessages,
          repository,
          parent_request_id: parentRequestId,
          generated_documents:
            forkAfterIndex === null ? fallbackRecord?.generated_documents ?? [] : [],
        },
      });
    } catch (error) {
      setView({
        kind: "error",
        phase: "submit",
        message:
          error instanceof TypeError
            ? "Could not reach the backend. Check that it is running, then try again."
            : error instanceof Error
              ? error.message
              : "Could not submit the message.",
        record: fallbackRecord,
        submission: { content, parentRequestId, priorMessages, fallbackRecord, selectedFile, forkAfterIndex, repository },
      });
    }
  }

  function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = message.trim();
    if (!trimmed || fileError || (!visibleRecord(view) && repositoryChoice && !repositoryChoice.branch)) return;
    const record = visibleRecord(view);
    if (record && forkAfter !== null) {
      void sendMessage(
        trimmed, record.request_id, record.messages.slice(0, forkAfter + 1), record, file,
        forkAfter,
      );
      return;
    }
    if (record && (record.status !== "completed" || !record.messages.length)) return;
    void sendMessage(trimmed, record?.request_id ?? null, record?.messages ?? [], record, file, null);
  }

  async function forkMessage(index: number) {
    const record = visibleRecord(view);
    if (!record) return;
    if (record.messages[index]?.role === "assistant") {
      setForkAfter(index);
      return;
    }
    // Forking after your own message answers it again in a new branch.
    const kept = record.messages.slice(0, index + 1);
    setView({ kind: "submitting", messages: kept, repository: record.repository });
    try {
      const response = await fetch(
        `/api/llm/requests/${encodeURIComponent(record.request_id)}/fork`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ message_index: index }),
        },
      );
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      if (
        typeof body !== "object" ||
        body === null ||
        !("request_id" in body) ||
        typeof body.request_id !== "string"
      ) {
        throw new Error("The backend did not return a request ID.");
      }
      const id = body.request_id;
      setForkAfter(null);
      setLookupId(id);
      setRequestId(id);
      rememberRequestId(id);
      setView({
        kind: "request",
        record: {
          request_id: id,
          status: "running",
          result: null,
          error: null,
          messages: kept,
          repository: record.repository,
          parent_request_id: record.request_id,
          generated_documents: [],
        },
      });
    } catch (error) {
      setView({
        kind: "error",
        phase: "submit",
        message: error instanceof Error ? error.message : "Could not fork the conversation.",
        record,
      });
    }
  }

  async function retryMessage() {
    const record = visibleRecord(view);
    if (!record || record.status === "completed" || !record.messages.length) return;
    const pending = record.messages.at(-1);
    if (pending?.role !== "user") return;
    setView({ kind: "submitting", messages: record.messages, repository: record.repository });
    try {
      const response = await fetch(
        `/api/llm/requests/${encodeURIComponent(record.request_id)}/retry`,
        { method: "POST" },
      );
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      if (
        typeof body !== "object" ||
        body === null ||
        !("request_id" in body) ||
        typeof body.request_id !== "string"
      ) {
        throw new Error("The backend did not return a request ID.");
      }
      const id = body.request_id;
      setLookupId(id);
      setRequestId(id);
      rememberRequestId(id);
      setView({
        kind: "request",
        record: { ...record, request_id: id, status: "running", error: null, result: null },
      });
    } catch (error) {
      setView({
        kind: "error",
        phase: "submit",
        message: error instanceof Error ? error.message : "Could not retry this message.",
        record,
      });
    }
  }

  function retrieveRequest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const id = lookupId.trim();
    if (!id) return;
    setForkAfter(null);
    setFile(null);
    setFileError("");
    if (fileInput.current) fileInput.current.value = "";
    setRequestId(id);
    setRefreshKey((value) => value + 1);
    rememberRequestId(id);
    setView({ kind: "loading", requestId: id });
  }

  function startNewConversation() {
    setRepositoryChoice(null);
    setMessage("");
    setForkAfter(null);
    setFile(null);
    setFileError("");
    if (fileInput.current) fileInput.current.value = "";
    setRequestId("");
    setLookupId("");
    rememberRequestId("");
    setView({ kind: "idle" });
  }

  const record = visibleRecord(view);
  const isForking = record !== null && forkAfter !== null;
  const messages =
    view.kind === "submitting"
      ? view.messages
      : view.kind === "error" && view.submission
        ? [...view.submission.priorMessages, {
            role: "user" as const,
            content: view.submission.content,
            ...(view.submission.selectedFile ? { attachment: {
              document_id: "",
              filename: view.submission.selectedFile.name,
              content_type: view.submission.selectedFile.type,
              size_bytes: view.submission.selectedFile.size,
            } } : {}),
          }]
      : isForking
        ? record.messages.slice(0, forkAfter + 1)
        : record?.messages ?? [];
  const isBusy = view.kind === "loading" || view.kind === "submitting";
  const isRunning = view.kind === "request" && record?.status === "running";
  const canCompose =
    !isBusy &&
    !(view.kind === "error" && view.submission) &&
    !isRunning &&
    (!record || isForking || (record.status === "completed" && record.messages.length > 0));
  const canFork = record !== null && !isBusy && !isRunning;
  const isLegacy = record?.status === "completed" && !record.messages.length;
  const canRetry =
    record !== null &&
    record.status !== "completed" &&
    record.status !== "running" &&
    record.messages.at(-1)?.role === "user";
  const status = view.kind === "error" ? "error" : record?.status ?? view.kind;
  const displayedRequestId =
    record?.request_id ?? (view.kind === "loading" ? view.requestId : "");

  useEffect(() => {
    const el = conversation.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, view.kind]);

  return (
    <main className="page-shell">
      <header className="page-heading">
        <h1>LLM conversations</h1>
        <p className="page-intro">
          Ask a question, reply to the answer, and return to the conversation with its latest request ID.
        </p>
      </header>

      <div className="workspace">
        <section className="request-panel" aria-labelledby="request-title">
          <div className="section-heading">
            <h2 id="request-title">
              {isForking ? "Branch conversation" : record ? "Continue conversation" : "New conversation"}
            </h2>
          </div>

          {isForking && (
            <p className="fork-note">
              Your message starts a new branch after message {forkAfter + 1}. The original
              conversation stays as it is.
              <button type="button" onClick={() => setForkAfter(null)}>
                Cancel
              </button>
            </p>
          )}

          <div className="endpoint">
            <span className="method">POST</span>
            <code>/llm/requests</code>
          </div>

          <form className="message-form" onSubmit={submitMessage}>
            {!record && !requestId && <RepositoryPicker value={repositoryChoice} onChange={setRepositoryChoice} disabled={!canCompose} />}
            <label htmlFor="message">{record ? "Reply" : "Message"}</label>
            <textarea
              id="message"
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              maxLength={10000}
              rows={6}
              placeholder={record ? "Write a follow-up…" : "What would you like to ask?"}
              disabled={!canCompose}
            />
            <div className="attachment-control">
              <input
                ref={fileInput}
                id="document-file"
                className="visually-hidden"
                type="file"
                accept=".pdf,.docx,.txt"
                disabled={!canCompose}
                onChange={(event) => {
                  const selected = event.target.files?.[0] ?? null;
                  if (selected && selected.size > maxFileBytes) {
                    setFile(null);
                    setFileError("Choose a file smaller than 20 MiB.");
                    event.target.value = "";
                  } else {
                    setFile(selected);
                    setFileError("");
                  }
                }}
              />
              <label className={`attach-button${!canCompose ? " attach-button-disabled" : ""}`} htmlFor="document-file">
                <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                  <path d="m6.7 10.2 4.7-4.7a2.7 2.7 0 0 1 3.8 3.8l-6 6a4.2 4.2 0 0 1-5.9-5.9l6-6" />
                </svg>
                Attach document
              </label>
              <span className="attachment-hint">PDF, DOCX or TXT · 20 MiB max</span>
            </div>
            {file && (
              <div className="selected-file">
                <span className="selected-file-name">{file.name}</span>
                <span className="selected-file-size">{fileSize(file.size)}</span>
                <button
                  type="button"
                  onClick={() => {
                    setFile(null);
                    if (fileInput.current) fileInput.current.value = "";
                  }}
                  disabled={!canCompose}
                  aria-label={`Remove ${file.name}`}
                >
                  Remove
                </button>
              </div>
            )}
            {fileError && (
              <div className="file-error" role="alert">
                <span>{fileError}</span>
                <button type="button" onClick={() => setFileError("")}>Clear</button>
              </div>
            )}
            <button
              className="run-button"
              type="submit"
              disabled={!canCompose || !message.trim() || Boolean(fileError) || (!record && Boolean(repositoryChoice && !repositoryChoice.branch))}
            >
              <span>
                {view.kind === "submitting"
                  ? "Submitting"
                  : isForking
                    ? "Start branch"
                    : record
                      ? "Send reply"
                      : "Send message"}
              </span>
              {view.kind === "submitting" ? (
                <span className="spinner" aria-hidden="true" />
              ) : (
                <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                  <path d="M4.5 10h10m-4-4 4 4-4 4" />
                </svg>
              )}
            </button>
          </form>

          {record && (
            <button className="new-conversation" type="button" onClick={startNewConversation}>
              Start a new conversation
            </button>
          )}

          <form className="lookup-form" onSubmit={retrieveRequest}>
            <label htmlFor="request-id">Retrieve by request ID</label>
            <div className="lookup-controls">
              <input
                id="request-id"
                value={lookupId}
                onChange={(event) => setLookupId(event.target.value)}
                placeholder="llm-request-..."
                disabled={view.kind === "submitting"}
              />
              <button type="submit" disabled={view.kind === "submitting" || !lookupId.trim()}>
                Find
              </button>
            </div>
          </form>
        </section>

        <section className="response-panel" aria-labelledby="response-title">
          <div className="response-header">
            <div className="section-heading">
              <h2 id="response-title">Conversation</h2>
            </div>
            <span className={`status status-${status}`}>
              <span className="status-dot" aria-hidden="true" />
              {status === "idle" && "Waiting"}
              {status === "loading" && "Loading"}
              {status === "submitting" && "Submitting"}
              {status === "running" && "In progress"}
              {status === "completed" && "Complete"}
              {status === "error" && "Error"}
              {status === "failed" && "Failed"}
              {status === "canceled" && "Canceled"}
              {status === "terminated" && "Terminated"}
              {status === "timed_out" && "Timed out"}
            </span>
          </div>

          <RepositorySummary repository={view.kind === "submitting" ? view.repository :
            record?.repository ?? (view.kind === "error" ? view.submission?.repository ?? null : null)} />

          {displayedRequestId && (
            <p className="request-id">
              Latest request ID <code>{displayedRequestId}</code>
            </p>
          )}

          <div
            ref={conversation}
            className="response-content"
            aria-live="polite" aria-busy={isBusy || isRunning}>
            {messages.length > 0 && (
              <ol className="conversation-list">
                {messages.map((item, index) => (
                  <li className={`conversation-turn turn-${item.role}`} key={index}>
                    <span className="turn-label">{item.role === "user" ? "You" : "LLM"}</span>
                    {item.role === "assistant" ? (
                      <div className="turn-content turn-markdown">
                        <Markdown remarkPlugins={remarkPlugins} components={markdownComponents}>
                          {item.content}
                        </Markdown>
                      </div>
                    ) : (
                      <p className="turn-content">{item.content}</p>
                    )}
                    {item.attachment && (
                      <div className="turn-attachment">
                        <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
                          <path d="m6.7 10.2 4.7-4.7a2.7 2.7 0 0 1 3.8 3.8l-6 6a4.2 4.2 0 0 1-5.9-5.9l6-6" />
                        </svg>
                        {record?.request_id && item.attachment.document_id ? (
                          <a
                            href={`/api/llm/requests/${encodeURIComponent(record.request_id)}/documents/${encodeURIComponent(item.attachment.document_id)}`}
                            download={item.attachment.filename}
                          >
                            {item.attachment.filename}
                          </a>
                        ) : (
                          <span>{item.attachment.filename}</span>
                        )}
                        <small>{fileSize(item.attachment.size_bytes)}</small>
                      </div>
                    )}
                    {canFork && (
                      <ForkButton
                        role={item.role}
                        disabled={false}
                        onFork={() => void forkMessage(index)}
                      />
                    )}
                  </li>
                ))}
              </ol>
            )}

            {record && record.generated_documents.length > 0 && (
              <section className="generated-documents" aria-label="Files created by the LLM">
                <h3>Files created by the LLM</h3>
                <ul>
                  {record.generated_documents.map((item) => (
                    <li key={item.document_id}>
                      <a
                        href={`/api/llm/requests/${encodeURIComponent(record.request_id)}/documents/${encodeURIComponent(item.document_id)}`}
                        download={item.filename}
                      >
                        {item.filename}
                      </a>
                      <span>{fileSize(item.size_bytes)} · revision {item.revision}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {isLegacy && record && (
              <div className="legacy-answer">
                <p>This older request has an answer but no saved conversation history.</p>
                <div className="turn-content turn-markdown">
                  <Markdown remarkPlugins={remarkPlugins} components={markdownComponents}>
                    {record.result}
                  </Markdown>
                </div>
              </div>
            )}

            {messages.length === 0 && !isLegacy && view.kind !== "error" && (
              <div className="empty-state">
                <p>{view.kind === "loading" ? "Loading conversation…" : "Your conversation starts here."}</p>
                <span>
                  {view.kind === "loading"
                    ? "Retrieving this request from Temporal."
                    : "Send a message or retrieve an existing request."}
                </span>
              </div>
            )}

            {(view.kind === "submitting" || isRunning) && (
              <div className="pending-answer" role="status">
                <span className="loading-line" aria-hidden="true" />
                <p>
                  {view.kind === "submitting"
                    ? "Starting the workflow…"
                    : "Waiting for the LLM answer…"}
                </p>
              </div>
            )}

            {view.kind === "error" && (
              <div className="error-state" role="alert">
                <p>
                  {view.phase === "submit"
                    ? "The message could not be submitted."
                    : "The request could not be retrieved."}
                </p>
                <span>{view.message}</span>
                {view.submission ? (
                  <button type="button" onClick={() => {
                    const attempt = view.submission!;
                    void sendMessage(
                      attempt.content, attempt.parentRequestId, attempt.priorMessages,
                      attempt.fallbackRecord, attempt.selectedFile, attempt.forkAfterIndex, attempt.repository,
                    );
                  }}>Retry this message</button>
                ) : canRetry && (
                  <button type="button" onClick={retryMessage}>Retry this message</button>
                )}
                {view.submission && !view.submission.parentRequestId && <button type="button"
                  onClick={() => setView({ kind: "idle" })}>Change message or repository</button>}
              </div>
            )}

            {view.kind === "request" && record && canRetry && !isForking && (
              <div className="error-state" role="alert">
                <p>The request did not complete.</p>
                <span>{record.error ?? "The request ended without an answer."}</span>
                <button type="button" onClick={retryMessage}>Retry this message</button>
              </div>
            )}

            {view.kind === "request" && record && record.status !== "completed" && !isRunning &&
              !canRetry && !isForking && (
              <div className="error-state" role="alert">
                <p>The request did not complete.</p>
                <span>{record.error ?? "The request ended without an answer."}</span>
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
