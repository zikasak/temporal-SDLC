import { useEffect, useState } from "react";
import { errorMessage, failure, readBody } from "./api";
import { repositorySelection, type RepositorySelection } from "./github";
import { onboardingLink } from "./Onboarding";

type WorkflowType =
  | "LLMRequestWorkflow"
  | "RequirementsWorkflow"
  | "ApiDesignWorkflow"
  | "OnboardingWorkflow"
  | "DeliveryWorkflow"
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
  // The repository and source branch it works in, and the onboarding it was started for;
  // null when they do not apply.
  repository: RepositorySelection | null;
  branch: string | null;
  onboarding_id: string | null;
};

// Filters on the repository (owner/name), source branch and onboarding; empty for none.
type Filters = { repository: string; branch: string; onboarding_id: string };
const filterNames: (keyof Filters)[] = ["repository", "branch", "onboarding_id"];
const filterLabels: Record<keyof Filters, string> = {
  repository: "Repository",
  branch: "Branch",
  onboarding_id: "Onboarding",
};

type Page = {
  workflows: WorkflowExecution[];
  next_page_token: string | null;
};

const workflowTypes: [WorkflowType, string][] = [
  ["LLMRequestWorkflow", "Chat"],
  ["RequirementsWorkflow", "Requirements"],
  ["ApiDesignWorkflow", "API design"],
  ["OnboardingWorkflow", "Onboarding"],
  ["DeliveryWorkflow", "Delivery"],
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

function filtersFromUrl(): Filters {
  const params = new URLSearchParams(window.location.search);
  return {
    repository: params.get("repository") ?? "",
    branch: params.get("branch") ?? "",
    onboarding_id: params.get("onboarding_id") ?? "",
  };
}

function rememberQuery(type: WorkflowType | "", filters: Filters) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", "workflows");
  if (type) url.searchParams.set("type", type);
  else url.searchParams.delete("type");
  for (const name of filterNames) {
    if (filters[name]) url.searchParams.set(name, filters[name]);
    else url.searchParams.delete(name);
  }
  window.history.replaceState(null, "", url);
}

function text(value: unknown) {
  return typeof value === "string" && value ? value : null;
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
    workflows: value.workflows.map((item) => {
      let repository: RepositorySelection | null = null;
      try {
        repository = repositorySelection(item.repository ?? null);
      } catch {
        // Listed without a repository rather than failing the whole page.
      }
      return {
        ...item,
        parent_workflow_id: text(item.parent_workflow_id),
        close_time: text(item.close_time),
        repository,
        branch: text(item.branch) ?? repository?.branch ?? null,
        onboarding_id: text(item.onboarding_id),
      } as WorkflowExecution;
    }),
    next_page_token:
      "next_page_token" in value && typeof value.next_page_token === "string"
        ? value.next_page_token
        : null,
  };
}

/** A delivery is named after the session whose task it builds, and shown on its page. */
function deliveryLink(workflowId: string) {
  const sessionId = workflowId.replace(/^delivery-/, "");
  if (sessionId.startsWith("onboarding-session-")) return onboardingLink(sessionId);
  const mode = sessionId.startsWith("requirements-session-") ? "requirements" : "api-design";
  return `?mode=${mode}&session_id=${encodeURIComponent(sessionId)}`;
}

/** The screen that shows this workflow, if there is one. */
function workflowLink(item: WorkflowExecution) {
  const id = encodeURIComponent(item.workflow_id);
  // An onboarding and the delivery and code generation it started open its page.
  if (item.onboarding_id) return onboardingLink(item.onboarding_id);
  switch (item.workflow_type) {
    case "LLMRequestWorkflow":
      return `?request_id=${id}`;
    case "RequirementsWorkflow":
      return `?mode=requirements&session_id=${id}`;
    case "ApiDesignWorkflow":
      return `?mode=api-design&session_id=${id}`;
    case "OnboardingWorkflow":
      return onboardingLink(item.workflow_id);
    case "DeliveryWorkflow":
      return deliveryLink(item.workflow_id);
    case "CodegenWorkflow":
      return item.parent_workflow_id ? deliveryLink(item.parent_workflow_id) : null;
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

async function fetchPage(type: WorkflowType | "", filters: Filters, pageToken: string | null,
                         signal?: AbortSignal) {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  for (const name of filterNames) if (filters[name]) params.set(name, filters[name]);
  if (pageToken) params.set("page_token", pageToken);
  const query = params.toString();
  const response = await fetch(`/api/workflows${query ? `?${query}` : ""}`, { signal });
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return pageRecord(body);
}

export default function Workflows() {
  const [type, setType] = useState<WorkflowType | "">(typeFromUrl);
  const [filters, setFilters] = useState<Filters>(filtersFromUrl);
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
    fetchPage(type, filters, null, controller.signal)
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
  }, [type, filters, refreshKey]);

  function selectType(next: WorkflowType | "") {
    rememberQuery(next, filters);
    setType(next);
  }

  function filter(next: Partial<Filters>) {
    const combined = { ...filters, ...next };
    rememberQuery(type, combined);
    setFilters(combined);
  }

  async function loadMore() {
    if (!nextPageToken) return;
    setLoadingMore(true);
    try {
      const page = await fetchPage(type, filters, nextPageToken);
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

        {filterNames.some((name) => filters[name]) && (
          <div className="type-filter" role="group" aria-label="Active filters">
            {filterNames.filter((name) => filters[name]).map((name) => (
              <button key={name} type="button" aria-pressed="true" title="Remove this filter"
                onClick={() => filter({ [name]: "" })}>
                {filterLabels[name]}: {filters[name]} ×
              </button>
            ))}
            <button type="button" onClick={() => filter({ repository: "", branch: "", onboarding_id: "" })}>
              Clear filters
            </button>
          </div>
        )}

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
              {filterNames.some((name) => filters[name])
                ? "No workflows match these filters."
                : type
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
                    <th scope="col">Repository</th>
                    <th scope="col">Branch</th>
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
                          {item.repository ? (
                            <button type="button" className="workflow-filter-link"
                              title={`Only workflows in ${item.repository.full_name}`}
                              onClick={() => filter({ repository: item.repository!.full_name })}>
                              {item.repository.full_name}
                            </button>
                          ) : "—"}
                        </td>
                        <td>
                          {item.branch ? (
                            <button type="button" className="workflow-filter-link"
                              title={`Only workflows on ${item.branch}`}
                              onClick={() => filter({
                                ...(item.repository ? { repository: item.repository.full_name } : {}),
                                branch: item.branch!,
                              })}>
                              {item.branch}
                            </button>
                          ) : "—"}
                        </td>
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
