import { useEffect, useState } from "react";
import { errorMessage, failure, isObject, readBody } from "./api";
import { repositorySelection, type RepositorySelection } from "./github";
import { onboardingLink } from "./Onboarding";

type FlowType =
  | "LLMRequestWorkflow"
  | "TaskWorkflow"
  | "ArchitectureWorkflow"
  | "ApiDesignWorkflow"
  | "OnboardingWorkflow";

type Flow = {
  workflow_id: string;
  run_id: string;
  workflow_type: string;
  // The Temporal status, or pending or skipped for a stage of a task flow.
  status: string;
  parent_workflow_id: string | null;
  start_time: string;
  close_time: string | null;
  // The repository and source branch it works in, and the onboarding it was started for;
  // null when they do not apply.
  repository: RepositorySelection | null;
  branch: string | null;
  onboarding_id: string | null;
  // A task flow's short name; null for any other flow.
  name: string | null;
  // Listed under a forked or fix-round task flow as an earlier stage of the original task.
  linked: boolean;
  // As the backend computes them; null for flows started before it did, which are only
  // listed, never highlighted or marked.
  waiting_for_user: boolean | null;
  unresolved_findings: boolean | null;
};

type Page = { workflows: Flow[]; next_page_token: string | null };

// Filters on the type, repository (owner/name), source branch and onboarding; empty for none.
type Filters = { type: FlowType | ""; repository: string; branch: string; onboarding_id: string };
const filterNames = ["repository", "branch", "onboarding_id"] as const;
const filterLabels: Record<(typeof filterNames)[number], string> = {
  repository: "Repository",
  branch: "Branch",
  onboarding_id: "Onboarding",
};

/** A flow that waits for the user, and the view that asks for it. */
type Attention = {
  workflow_id: string;
  label: string;
  link: string;
};

// The flows the type filter offers: chats, the task flows that requirements interviews
// start (their requirements sessions are reached by opening them), the architecture
// sessions and API designs of tasks, and onboardings.
const flowTypes: [FlowType, string][] = [
  ["LLMRequestWorkflow", "Chat"],
  ["TaskWorkflow", "Requirements"],
  ["ArchitectureWorkflow", "Architecture"],
  ["ApiDesignWorkflow", "API design"],
  ["OnboardingWorkflow", "Onboarding"],
];

const typeLabels: Record<string, string> = {
  LLMRequestWorkflow: "Chat",
  TaskWorkflow: "Task",
  RequirementsWorkflow: "Requirements",
  ArchitectureWorkflow: "Architecture",
  ApiDesignWorkflow: "API design",
  OnboardingWorkflow: "Onboarding",
  DeliveryWorkflow: "Delivery",
  CodegenWorkflow: "Code generation",
  ImplementationWorkflow: "Implementation",
  TestGenerationWorkflow: "Test generation",
  DocumentationWorkflow: "Documentation",
  VerificationWorkflow: "Verification",
  DiagnosisWorkflow: "Diagnosis",
  ReviewWorkflow: "Code review",
};

// Flows that start flows of their own; the others are single steps. A requirements session
// and an API design started before task flows existed hold their later stages themselves.
const parentTypes = ["TaskWorkflow", "RequirementsWorkflow", "ApiDesignWorkflow", "OnboardingWorkflow",
  "DeliveryWorkflow"];

// Flows that are always stages of a task: filtering by them lists every one, not only the
// top-level flows.
const childTypes: string[] = ["ArchitectureWorkflow", "ApiDesignWorkflow"];

const statusLabels: Record<string, string> = {
  running: "Running",
  pending: "Pending",
  skipped: "Skipped",
  completed: "Completed",
  failed: "Failed",
  canceled: "Canceled",
  terminated: "Terminated",
  timed_out: "Timed out",
  continued_as_new: "Continued as new",
};

// What each kind of flow waits for when the backend reports it waiting for the user.
const waitLabels: Record<string, string> = {
  TaskWorkflow: "A stage of this task waits for you",
  RequirementsWorkflow: "The requirements interview waits for your answers",
  ArchitectureWorkflow: "The architecture session waits for your answers or a decision on skipping it",
  ApiDesignWorkflow: "The API design waits for your answers",
  DeliveryWorkflow: "The delivery waits for answers to its agents' questions or a decision on review findings",
  OnboardingWorkflow: "The onboarding waits for your answers, a contract decision or its delivery",
};

const POLL_MS = 10000;

function text(value: unknown) {
  return typeof value === "string" && value ? value : null;
}

function typeLabel(type: string) {
  return typeLabels[type] ?? type.replace(/Workflow$/, "");
}

export function filtersFromSearch(search: string): Filters {
  const params = new URLSearchParams(search);
  // Requirements are listed by their task flows, also from links made before task flows.
  const type = params.get("type") === "RequirementsWorkflow" ? "TaskWorkflow" : params.get("type");
  return {
    type: flowTypes.find(([item]) => item === type)?.[0] ?? "",
    repository: params.get("repository") ?? "",
    branch: params.get("branch") ?? "",
    onboarding_id: params.get("onboarding_id") ?? "",
  };
}

function filterSearch(filters: Filters) {
  const params = new URLSearchParams();
  if (filters.type) params.set("type", filters.type);
  for (const name of filterNames) if (filters[name]) params.set(name, filters[name]);
  const query = params.toString();
  return query ? `?${query}` : "?";
}

function pageRecord(value: unknown): Page {
  if (
    !isObject(value) ||
    !Array.isArray(value.workflows) ||
    !value.workflows.every(
      (item) =>
        isObject(item) &&
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
        name: text(item.name),
        linked: item.linked === true,
        waiting_for_user: typeof item.waiting_for_user === "boolean" ? item.waiting_for_user : null,
        unresolved_findings: typeof item.unresolved_findings === "boolean"
          ? item.unresolved_findings : null,
      } as Flow;
    }),
    next_page_token: typeof value.next_page_token === "string" ? value.next_page_token : null,
  };
}

/** The newest run of each workflow; a list holds every run, newest first. */
function latestRuns(flows: Flow[]) {
  const seen = new Set<string>();
  return flows.filter((item) => !seen.has(item.workflow_id) && seen.add(item.workflow_id));
}

async function fetchPage(params: URLSearchParams, signal?: AbortSignal) {
  const response = await fetch(`/api/workflows?${params}`, { signal });
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return pageRecord(body);
}

function listParams(filters: Filters, pageToken: string | null) {
  const params = new URLSearchParams(childTypes.includes(filters.type) ? {} : { top_level: "true" });
  if (filters.type) params.set("type", filters.type);
  for (const name of filterNames) if (filters[name]) params.set(name, filters[name]);
  if (pageToken) params.set("page_token", pageToken);
  return params;
}

/** Every child of `parent`, oldest first, up to a few pages. */
async function fetchChildren(parent: string, signal?: AbortSignal, type?: string) {
  const flows: Flow[] = [];
  let token: string | null = null;
  for (let page = 0; page < 5; page++) {
    const params = new URLSearchParams({ parent });
    if (type) params.set("type", type);
    if (token) params.set("page_token", token);
    const result = await fetchPage(params, signal);
    flows.push(...result.workflows);
    token = result.next_page_token;
    if (!token) break;
  }
  return latestRuns(flows).reverse();
}

// The session ID prefix of a flow that carries on a closed delivery on a new branch.
const followUpPrefix = "fix-session-";

/** A delivery is named after the session whose task it builds, and shown in its view. */
function deliveryLink(workflowId: string): string | null {
  const sessionId = workflowId.replace(/^delivery-/, "");
  if (sessionId.startsWith("onboarding-session-")) return onboardingLink(sessionId);
  // A flow that carries on a closed delivery on a new branch is shown, with its delivery's
  // progress, questions, findings and new pull request, as a session of its own.
  if (sessionId.startsWith(followUpPrefix)) {
    return `?mode=requirements&session_id=${encodeURIComponent(sessionId)}`;
  }
  const mode = sessionId.startsWith("requirements-session-") ? "requirements" : "api-design";
  return `?mode=${mode}&session_id=${encodeURIComponent(sessionId)}`;
}

/** The view that shows this flow, if it has one of its own: none for a task flow, whose
 * stages each have theirs, nor for a stage that has not started. */
function flowLink(item: Flow) {
  if (item.status === "pending") return null;
  const id = encodeURIComponent(item.workflow_id);
  if (item.onboarding_id && !item.onboarding_id.startsWith(followUpPrefix)) {
    return onboardingLink(item.onboarding_id);
  }
  switch (item.workflow_type) {
    case "LLMRequestWorkflow":
      return `?mode=chat&request_id=${id}`;
    case "RequirementsWorkflow":
      return `?mode=requirements&session_id=${id}`;
    case "ArchitectureWorkflow":
      return `?mode=architecture&session_id=${id}`;
    case "ApiDesignWorkflow":
      return `?mode=api-design&session_id=${id}`;
    case "OnboardingWorkflow":
      return onboardingLink(item.workflow_id);
    case "DeliveryWorkflow":
      return deliveryLink(item.workflow_id);
    default:
      return null;
  }
}

// Statuses of a flow that has closed with its pull request: merged, or closed unmerged.
const closedStatuses = ["completed", "canceled"];

/** Whether the backend reports `flow` waiting for the user; a closed flow never is. */
function waits(flow: Flow) {
  return flow.waiting_for_user === true && (flow.status === "running" || flow.status === "pending");
}

/** Whether `flow` closed with open review findings nobody accepted: marked, not highlighted. */
function unresolvedFindings(flow: Flow) {
  return flow.unresolved_findings === true && closedStatuses.includes(flow.status);
}

function attentionOf(flow: Flow): Attention | null {
  const link = flowLink(flow);
  if (!link) return null;
  return { workflow_id: flow.workflow_id, link,
    label: waitLabels[flow.workflow_type] ?? "Waits for your input" };
}

/** What a waiting top-level flow waits for: a task flow's own children that wait, each
 * with the view that asks, or the flow itself. */
async function fetchAttention(flow: Flow, signal: AbortSignal): Promise<Attention[]> {
  if (!waits(flow)) return [];
  if (flow.workflow_type !== "TaskWorkflow") {
    const own = attentionOf(flow);
    return own ? [own] : [];
  }
  const children = await fetchChildren(flow.workflow_id, signal);
  return children.filter((child) => !child.linked && waits(child))
    .map(attentionOf).filter((item): item is Attention => item !== null);
}

/** What a top-level flow waits for, checked again on every `tick`, as GET /workflows
 * reports it; for one that has completed or been cancelled, whether it closed with review
 * findings unresolved. Flows started before the backend reported either get neither. */
function useAttention(flow: Flow, tick: number) {
  const [attention, setAttention] = useState<Attention[]>([]);
  const waiting = waits(flow);
  useEffect(() => {
    if (!waiting) {
      setAttention([]);
      return;
    }
    const controller = new AbortController();
    fetchAttention(flow, controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) setAttention(items);
      })
      // Keep the last answer; the next tick tries again.
      .catch(() => undefined);
    return () => controller.abort();
  }, [flow.workflow_id, flow.run_id, flow.status, waiting, tick]);
  return { attention, unresolved: unresolvedFindings(flow) };
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function duration(start: string, end: string | null) {
  if (!end) return null;
  const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

function Status({ status }: { status: string }) {
  return (
    <span className={`status status-${status}`}>
      <span className="status-dot" aria-hidden="true" />
      {statusLabels[status] ?? status}
    </span>
  );
}

function AttentionList({ items, onOpen }: { items: Attention[]; onOpen: (link: string) => void }) {
  if (items.length === 0) return null;
  return (
    <ul className="flow-attention-list" aria-label="Waiting for your input">
      {items.map((item) => (
        <li key={`${item.workflow_id}/${item.label}`}>
          <span>{item.label}</span>
          <button type="button" className="flow-questions-button" onClick={() => onOpen(item.link)}>
            Open questions
          </button>
        </li>
      ))}
    </ul>
  );
}

/** The child flows and steps of `parent`, each one expandable when it has its own. */
function FlowChildren({ parent, link, tick, onOpen }: {
  parent: Flow;
  link: string | null;
  tick: number;
  onOpen: (link: string) => void;
}) {
  const [children, setChildren] = useState<Flow[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    fetchChildren(parent.workflow_id, controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return;
        setChildren(items);
        setError("");
      })
      .catch((caught) => {
        if (!controller.signal.aborted) setError(failure(caught, "Could not list the steps."));
      });
    return () => controller.abort();
    // A finished flow's steps do not change; a running one's are listed again on every tick.
  }, [parent.workflow_id, parent.run_id, parent.status === "running" ? tick : 0]);

  if (error) return <p className="flow-children-note connector-error" role="alert">{error}</p>;
  if (children === null) return <p className="flow-children-note" role="status">Loading steps…</p>;
  if (children.length === 0) return <p className="flow-children-note">No steps have started yet.</p>;
  return (
    <ol className="flow-children">
      {children.map((child) => (
        <FlowRow key={child.workflow_id} flow={child} inheritedLink={link} attention={[]}
          tick={tick} onOpen={onOpen} unresolved={unresolvedFindings(child)} />
      ))}
    </ol>
  );
}

/** A flow and, once opened, its steps; `attention` is what a top-level flow's tree waits
 * for, while a step is highlighted for what it waits for itself. */
function FlowRow({ flow, inheritedLink, attention, tick, onOpen, onFilter, unresolved = false,
  topLevel = false }: {
  flow: Flow;
  inheritedLink: string | null;
  attention: Attention[];
  tick: number;
  onOpen: (link: string) => void;
  // Narrows the list to the flow's repository or branch; top-level flows only.
  onFilter?: (next: Partial<Filters>) => void;
  // A closed flow whose review findings were neither accepted nor acted on.
  unresolved?: boolean;
  topLevel?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const own = flowLink(flow);
  const link = own ?? inheritedLink;
  // A top-level flow is highlighted for anything in its tree; a step for what it asks itself.
  const self = waits(flow) ? attentionOf(flow) : null;
  const asking = topLevel ? attention : self ? [self] : [];
  const expandable = topLevel || parentTypes.includes(flow.workflow_type);
  const took = duration(flow.start_time, flow.close_time);
  const childrenId = `flow-children-${flow.workflow_id}`;

  return (
    <li className={`flow-item${asking.length ? " flow-needs-input" : ""}${topLevel ? " flow-top" : ""}`}>
      <div className="flow-row">
        {expandable ? (
          <button type="button" className="flow-toggle" aria-expanded={expanded}
            aria-controls={childrenId} onClick={() => setExpanded(!expanded)}>
            <span className="flow-chevron" aria-hidden="true" />
            <span className="flow-type">{typeLabel(flow.workflow_type)}</span>
          </button>
        ) : (
          <span className="flow-toggle flow-leaf">
            <span className="flow-type">{typeLabel(flow.workflow_type)}</span>
          </span>
        )}
        {flow.workflow_type === "TaskWorkflow" ? (
          <span className="flow-name" title={flow.workflow_id}>{flow.name ?? "Untitled task"}</span>
        ) : (
          <code className="flow-id" title={flow.workflow_id}>{flow.workflow_id}</code>
        )}
        <span className="flow-meta">
          {flow.repository && (onFilter ? (
            <span>
              <button type="button" className="workflow-filter-link"
                title={`Only flows in ${flow.repository.full_name}`}
                onClick={() => onFilter({ repository: flow.repository!.full_name })}>
                {flow.repository.full_name}
              </button>
              {flow.branch && <>{" · "}
                <button type="button" className="workflow-filter-link" title={`Only flows on ${flow.branch}`}
                  onClick={() => onFilter({ repository: flow.repository!.full_name, branch: flow.branch! })}>
                  {flow.branch}
                </button>
              </>}
            </span>
          ) : <span>{flow.repository.full_name}{flow.branch ? ` · ${flow.branch}` : ""}</span>)}
          <time dateTime={flow.start_time}>{dateFormat.format(new Date(flow.start_time))}</time>
          {took && <span>{took}</span>}
        </span>
        <Status status={flow.status} />
        {flow.linked && (
          <span className="flow-linked" title="An earlier stage of the original task, which this task flow links to">
            linked from another task
          </span>
        )}
        {unresolved && (
          <span className="flow-unresolved" title="Closed with open review findings">
            unresolved findings
          </span>
        )}
        <span className="flow-actions">
          {asking.length > 0 && (
            <button type="button" className="flow-questions-button" onClick={() => onOpen(asking[0].link)}>
              Open questions
            </button>
          )}
          {own && (
            <button type="button" className="flow-open-button" onClick={() => onOpen(own)}>Open</button>
          )}
        </span>
      </div>
      {expanded && (
        <div id={childrenId} className="flow-body">
          {topLevel && <AttentionList items={attention} onOpen={onOpen} />}
          <FlowChildren parent={flow} link={link} tick={tick} onOpen={onOpen} />
        </div>
      )}
    </li>
  );
}

export function TopLevelFlow({ flow, tick, onOpen, onFilter }: {
  flow: Flow;
  tick: number;
  onOpen: (link: string) => void;
  onFilter: (next: Partial<Filters>) => void;
}) {
  const { attention, unresolved } = useAttention(flow, tick);
  return <FlowRow flow={flow} inheritedLink={null} attention={attention} tick={tick} onOpen={onOpen}
    onFilter={onFilter} unresolved={unresolved} topLevel />;
}

/** Every top-level flow, newest first; each opens into its steps and its view. */
export default function Flows({ search, refreshKey, onNavigate }: {
  // The list's own query: its type, repository, branch and onboarding filters.
  search: string;
  refreshKey: number;
  onNavigate: (search: string) => void;
}) {
  const filters = filtersFromSearch(search);
  const filterKey = filterSearch(filters);
  const [reload, setReload] = useState(0);
  const [tick, setTick] = useState(0);
  const [flows, setFlows] = useState<Flow[]>([]);
  const [nextPageToken, setNextPageToken] = useState<string | null>(null);
  const [pages, setPages] = useState(1);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") setTick((value) => value + 1);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setPages(1);
    fetchPage(listParams(filters, null), controller.signal)
      .then((page) => {
        setFlows(latestRuns(page.workflows));
        setNextPageToken(page.next_page_token);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setFlows([]);
        setNextPageToken(null);
        setError(failure(caught, "Could not list the flows."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [filterKey, reload, refreshKey]);

  // The first page is listed again in the background, unless more pages were loaded.
  useEffect(() => {
    if (tick === 0 || pages > 1 || loading) return;
    const controller = new AbortController();
    fetchPage(listParams(filters, null), controller.signal)
      .then((page) => {
        setFlows(latestRuns(page.workflows));
        setNextPageToken(page.next_page_token);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [tick]);

  function filter(next: Partial<Filters>) {
    onNavigate(filterSearch({ ...filters, ...next }));
  }

  async function loadMore() {
    if (!nextPageToken) return;
    setLoadingMore(true);
    try {
      const page = await fetchPage(listParams(filters, nextPageToken));
      setFlows((current) => latestRuns([...current, ...page.workflows]));
      setNextPageToken(page.next_page_token);
      setPages((value) => value + 1);
    } catch (caught) {
      setError(failure(caught, "Could not load more flows."));
    } finally {
      setLoadingMore(false);
    }
  }

  const filtered = filterNames.some((name) => filters[name]);

  return (
    <main className="page-shell">
      <header className="page-heading">
        <h1>Flows</h1>
        <p className="page-intro">
          Every flow you started, newest first. Open one to follow its steps; a step waiting
          for you is highlighted with its questions a click away.
        </p>
      </header>

      <section className="workflows-panel" aria-labelledby="flows-title">
        <div className="workflows-toolbar">
          <div className="section-heading">
            <h2 id="flows-title">{filters.type ? typeLabel(filters.type) : "All flows"}</h2>
          </div>
          <div className="flow-start" role="group" aria-label="Start a flow">
            <span>Start</span>
            <button type="button" onClick={() => onNavigate("?mode=chat")}>Chat</button>
            <button type="button" onClick={() => onNavigate("?mode=requirements")}>Requirements</button>
            <button type="button" onClick={() => onNavigate("?mode=onboarding")}>Onboarding</button>
            <button className="workflows-refresh" type="button" disabled={loading}
              onClick={() => setReload((value) => value + 1)}>
              Refresh
            </button>
          </div>
        </div>

        <div className="type-filter" role="group" aria-label="Filter by kind of flow">
          <button type="button" aria-pressed={filters.type === ""} onClick={() => filter({ type: "" })}>
            All
          </button>
          {flowTypes.map(([item, label]) => (
            <button key={item} type="button" aria-pressed={filters.type === item}
              onClick={() => filter({ type: item })}>
              {label}
            </button>
          ))}
        </div>

        {filtered && (
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
            <p>The flows could not be listed.</p>
            <span>{error}</span>
            <button type="button" onClick={() => setReload((value) => value + 1)}>Try again</button>
          </div>
        )}

        {loading ? (
          <div className="empty-state workflows-empty" role="status">
            <span className="loading-line" aria-hidden="true" />
            <p>Loading flows…</p>
          </div>
        ) : flows.length === 0 && !error ? (
          <div className="empty-state workflows-empty">
            <p>No flows yet.</p>
            <span>
              {filtered
                ? "No flows match these filters."
                : filters.type
                  ? `No ${typeLabel(filters.type)} flows have been started.`
                  : "Start a chat, a requirements interview or an onboarding to see it here."}
            </span>
          </div>
        ) : (
          flows.length > 0 && (
            <ol className="flow-list">
              {flows.map((flow) => (
                <TopLevelFlow key={flow.workflow_id} flow={flow} tick={tick} onOpen={onNavigate}
                  onFilter={filter} />
              ))}
            </ol>
          )
        )}

        {!loading && nextPageToken && (
          <button className="workflows-more" type="button" onClick={() => void loadMore()} disabled={loadingMore}>
            {loadingMore ? "Loading…" : "Load more"}
          </button>
        )}
      </section>
    </main>
  );
}
