import { useEffect, useState } from "react";
import { errorMessage, failure, readBody } from "./api";

type WorkflowType =
  | "LLMRequestWorkflow"
  | "RequirementsWorkflow"
  | "ApiDesignWorkflow"
  | "CodegenWorkflow"
  | "SayHelloWorkflow";

type WorkflowExecution = {
  workflow_id: string;
  run_id: string;
  workflow_type: string;
  status: string;
  task_queue: string;
  parent_workflow_id: string | null;
  start_time: string;
  close_time: string | null;
};

type Page = {
  workflows: WorkflowExecution[];
  next_page_token: string | null;
};

const workflowTypes: [WorkflowType, string][] = [
  ["LLMRequestWorkflow", "Chat"],
  ["RequirementsWorkflow", "Requirements"],
  ["ApiDesignWorkflow", "API design"],
  ["CodegenWorkflow", "Code generation"],
  ["SayHelloWorkflow", "Hello"],
];

const statusLabels: Record<string, string> = {
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  canceled: "Canceled",
  terminated: "Terminated",
  timed_out: "Timed out",
  continued_as_new: "Continued as new",
};

function typeFromUrl(): WorkflowType | "" {
  const type = new URLSearchParams(window.location.search).get("type");
  return workflowTypes.find(([item]) => item === type)?.[0] ?? "";
}

function rememberType(type: WorkflowType | "") {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "workflows");
  if (type) url.searchParams.set("type", type);
  else url.searchParams.delete("type");
  window.history.replaceState(null, "", url);
}

function typeLabel(type: string) {
  return workflowTypes.find(([item]) => item === type)?.[1] ?? type;
}

function pageRecord(value: unknown): Page {
  if (
    typeof value !== "object" ||
    value === null ||
    !("workflows" in value) ||
    !Array.isArray(value.workflows) ||
    !value.workflows.every(
      (item) =>
        typeof item === "object" &&
        item !== null &&
        typeof item.workflow_id === "string" &&
        typeof item.run_id === "string" &&
        typeof item.workflow_type === "string" &&
        typeof item.status === "string" &&
        typeof item.start_time === "string",
    )
  ) {
    throw new Error("The backend returned an invalid workflow list.");
  }
  return {
    workflows: value.workflows as WorkflowExecution[],
    next_page_token:
      "next_page_token" in value && typeof value.next_page_token === "string"
        ? value.next_page_token
        : null,
  };
}

/** The screen that shows this workflow, if there is one. */
function workflowLink(item: WorkflowExecution) {
  const id = encodeURIComponent(item.workflow_id);
  switch (item.workflow_type) {
    case "LLMRequestWorkflow":
      return `?request_id=${id}`;
    case "RequirementsWorkflow":
      return `?mode=requirements&session_id=${id}`;
    case "ApiDesignWorkflow":
      return `?mode=api-design&session_id=${id}`;
    case "CodegenWorkflow":
      return item.parent_workflow_id
        ? `?mode=api-design&session_id=${encodeURIComponent(item.parent_workflow_id)}`
        : null;
    default:
      return null;
  }
}

const dateFormat = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});

function duration(start: string, end: string | null) {
  if (!end) return "—";
  const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

async function fetchPage(type: WorkflowType | "", pageToken: string | null, signal?: AbortSignal) {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  if (pageToken) params.set("page_token", pageToken);
  const query = params.toString();
  const response = await fetch(`/api/workflows${query ? `?${query}` : ""}`, { signal });
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return pageRecord(body);
}

export default function Workflows() {
  const [type, setType] = useState<WorkflowType | "">(typeFromUrl);
  const [refreshKey, setRefreshKey] = useState(0);
  const [workflows, setWorkflows] = useState<WorkflowExecution[]>([]);
  const [nextPageToken, setNextPageToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    fetchPage(type, null, controller.signal)
      .then((page) => {
        setWorkflows(page.workflows);
        setNextPageToken(page.next_page_token);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setWorkflows([]);
        setNextPageToken(null);
        setError(failure(caught, "Could not list workflows."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [type, refreshKey]);

  function selectType(next: WorkflowType | "") {
    rememberType(next);
    setType(next);
  }

  async function loadMore() {
    if (!nextPageToken) return;
    setLoadingMore(true);
    try {
      const page = await fetchPage(type, nextPageToken);
      setWorkflows((current) => [...current, ...page.workflows]);
      setNextPageToken(page.next_page_token);
    } catch (caught) {
      setError(failure(caught, "Could not load more workflows."));
    } finally {
      setLoadingMore(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="page-heading">
        <h1>Workflows</h1>
        <p className="page-intro">
          Every workflow the system has started in Temporal, newest first. Open one to see its
          conversation, session or generated code.
        </p>
      </header>

      <section className="workflows-panel" aria-labelledby="workflows-title">
        <div className="workflows-toolbar">
          <div className="section-heading">
            <h2 id="workflows-title">
              {type ? typeLabel(type) : "All workflows"}
            </h2>
          </div>
          <button
            className="workflows-refresh"
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
            disabled={loading}
          >
            Refresh
          </button>
        </div>

        <div className="type-filter" role="group" aria-label="Filter by workflow type">
          <button type="button" aria-pressed={type === ""} onClick={() => selectType("")}>
            All
          </button>
          {workflowTypes.map(([item, label]) => (
            <button
              key={item}
              type="button"
              aria-pressed={type === item}
              onClick={() => selectType(item)}
            >
              {label}
            </button>
          ))}
        </div>

        {error && (
          <div className="error-state workflows-error" role="alert">
            <p>The workflows could not be listed.</p>
            <span>{error}</span>
            <button type="button" onClick={() => setRefreshKey((value) => value + 1)}>
              Try again
            </button>
          </div>
        )}

        {loading ? (
          <div className="empty-state workflows-empty" role="status">
            <span className="loading-line" aria-hidden="true" />
            <p>Loading workflows…</p>
          </div>
        ) : workflows.length === 0 && !error ? (
          <div className="empty-state workflows-empty">
            <p>No workflows yet.</p>
            <span>
              {type
                ? `No ${typeLabel(type)} workflows have been started.`
                : "Start a conversation, interview or API design to see it here."}
            </span>
          </div>
        ) : (
          workflows.length > 0 && (
            <div className="workflows-table-wrap">
              <table className="workflows-table">
                <thead>
                  <tr>
                    <th scope="col">Workflow ID</th>
                    <th scope="col">Type</th>
                    <th scope="col">Status</th>
                    <th scope="col">Started</th>
                    <th scope="col">Duration</th>
                  </tr>
                </thead>
                <tbody>
                  {workflows.map((item) => {
                    const link = workflowLink(item);
                    return (
                      <tr key={`${item.workflow_id}/${item.run_id}`}>
                        <td className="workflow-id">
                          {link ? <a href={link}>{item.workflow_id}</a> : <code>{item.workflow_id}</code>}
                        </td>
                        <td>{typeLabel(item.workflow_type)}</td>
                        <td>
                          <span className={`status status-${item.status}`}>
                            <span className="status-dot" aria-hidden="true" />
                            {statusLabels[item.status] ?? item.status}
                          </span>
                        </td>
                        <td>
                          <time dateTime={item.start_time}>
                            {dateFormat.format(new Date(item.start_time))}
                          </time>
                        </td>
                        <td>{duration(item.start_time, item.close_time)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )
        )}

        {!loading && nextPageToken && (
          <button
            className="workflows-more"
            type="button"
            onClick={() => void loadMore()}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
    </main>
  );
}
