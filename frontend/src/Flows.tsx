import { useEffect, useState } from "react";
import { errorMessage, failure, readBody } from "./api";
import { repositorySelection, type RepositorySelection } from "./github";
import { onboardingLink } from "./Onboarding";
import { questionList } from "./Questions";

type FlowType =
  | "LLMRequestWorkflow"
  | "RequirementsWorkflow"
  | "ApiDesignWorkflow"
  | "OnboardingWorkflow";

type Flow = {
  workflow_id: string;
  run_id: string;
  workflow_type: string;
  status: string;
  parent_workflow_id: string | null;
  start_time: string;
  close_time: string | null;
  // The repository and source branch it works in, and the onboarding it was started for;
  // null when they do not apply.
  repository: RepositorySelection | null;
  branch: string | null;
  onboarding_id: string | null;
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

/** Something a flow waits for from the user, and the session view that asks for it. */
type Attention = {
  // The workflow that asks, and the row it is shown on: the session for its own questions,
  // its delivery for the delivery's agents and findings.
  workflow_id: string;
  scope: string;
  label: string;
  link: string;
};

// The flows the type filter offers: those a user starts, and the API designs that
// finished requirements start.
const flowTypes: [FlowType, string][] = [
  ["LLMRequestWorkflow", "Chat"],
  ["RequirementsWorkflow", "Requirements"],
  ["ApiDesignWorkflow", "API design"],
  ["OnboardingWorkflow", "Onboarding"],
];

const typeLabels: Record<string, string> = {
  ...Object.fromEntries(flowTypes),
  DeliveryWorkflow: "Delivery",
  CodegenWorkflow: "Code generation",
  ImplementationWorkflow: "Implementation",
  TestGenerationWorkflow: "Test generation",
  DocumentationWorkflow: "Documentation",
  VerificationWorkflow: "Verification",
  DiagnosisWorkflow: "Diagnosis",
  ReviewWorkflow: "Code review",
};

// Flows that start flows of their own; the others are single steps.
const parentTypes = ["RequirementsWorkflow", "ApiDesignWorkflow", "OnboardingWorkflow", "DeliveryWorkflow"];

const sessionPaths: Record<string, string> = {
  RequirementsWorkflow: "/api/requirements/sessions/",
  ApiDesignWorkflow: "/api/api-design/sessions/",
  OnboardingWorkflow: "/api/onboarding/sessions/",
};

const statusLabels: Record<string, string> = {
  running: "Running",
  completed: "Completed",
  failed: "Failed",
  canceled: "Canceled",
  terminated: "Terminated",
  timed_out: "Timed out",
  continued_as_new: "Continued as new",
};

const POLL_MS = 10000;

function text(value: unknown) {
  return typeof value === "string" && value ? value : null;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function typeLabel(type: string) {
  return typeLabels[type] ?? type.replace(/Workflow$/, "");
}

export function filtersFromSearch(search: string): Filters {
  const params = new URLSearchParams(search);
  const type = params.get("type");
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
  const params = new URLSearchParams({ top_level: "true" });
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

/** A delivery is named after the session whose task it builds, and shown in its view. */
function deliveryLink(workflowId: string) {
  const sessionId = workflowId.replace(/^delivery-/, "");
  if (sessionId.startsWith("onboarding-session-")) return onboardingLink(sessionId);
  const mode = sessionId.startsWith("requirements-session-") ? "requirements" : "api-design";
  return `?mode=${mode}&session_id=${encodeURIComponent(sessionId)}`;
}

/** The view that shows this flow, if it has one of its own. */
function flowLink(item: Flow) {
  const id = encodeURIComponent(item.workflow_id);
  if (item.onboarding_id) return onboardingLink(item.onboarding_id);
  switch (item.workflow_type) {
    case "LLMRequestWorkflow":
      return `?mode=chat&request_id=${id}`;
    case "RequirementsWorkflow":
      return `?mode=requirements&session_id=${id}`;
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

function plural(count: number, word: string) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

/** What the session `body` of `flow` waits for from the user. */
function sessionAttention(flow: Flow, body: unknown): Attention[] {
  if (!isObject(body)) return [];
  const sessionId = flow.workflow_id;
  const link = flowLink(flow) ?? "";
  const items: Attention[] = [];
  const messages = Array.isArray(body.messages) ? body.messages : [];
  const last = messages.at(-1);
  if (body.status === "awaiting_confirmation") {
    items.push({ workflow_id: sessionId, scope: sessionId, link,
      label: "Decide whether to replace the contract the branch already has" });
  } else if (body.status === "interviewing" && isObject(last) && last.role === "assistant") {
    const asked = questionList(last.questions).length;
    items.push({ workflow_id: sessionId, scope: sessionId, link,
      label: asked ? `${plural(asked, "question")} to answer` : "Waiting for your reply" });
  }
  // An API design reports its delivery's progress with its own fields.
  const delivery = isObject(body.delivery) ? body.delivery
    : flow.workflow_type === "ApiDesignWorkflow" ? body : null;
  if (delivery) {
    const scope = `delivery-${sessionId}`;
    for (const item of Array.isArray(delivery.agent_questions) ? delivery.agent_questions : []) {
      if (!isObject(item) || typeof item.workflow_id !== "string") continue;
      const asked = questionList(item.questions).length;
      if (!asked) continue;
      items.push({ workflow_id: item.workflow_id, scope, link,
        label: `The ${typeof item.stage === "string" ? item.stage : "coding"} agent asks ${plural(asked, "question")}` });
    }
    if (delivery.open_findings === true) {
      items.push({ workflow_id: scope, scope, link,
        label: "Review findings wait for your decision" });
    }
  }
  return items;
}

async function fetchAttention(flow: Flow, signal: AbortSignal): Promise<Attention[]> {
  const path = sessionPaths[flow.workflow_type];
  if (!path || flow.status !== "running") return [];
  const response = await fetch(`${path}${encodeURIComponent(flow.workflow_id)}`, { signal });
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  const items = sessionAttention(flow, body);
  // A requirements session's API designs are flows of their own, asking their own questions.
  if (flow.workflow_type === "RequirementsWorkflow") {
    const designs = await fetchChildren(flow.workflow_id, signal, "ApiDesignWorkflow");
    const nested = await Promise.all(designs.map((design) => fetchAttention(design, signal)));
    items.push(...nested.flat());
  }
  return items;
}

/** What a running top-level flow waits for, checked again on every `tick`. */
function useAttention(flow: Flow, tick: number) {
  const [attention, setAttention] = useState<Attention[]>([]);
  useEffect(() => {
    if (!sessionPaths[flow.workflow_type] || flow.status !== "running") {
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
  }, [flow.workflow_id, flow.run_id, flow.status, flow.workflow_type, tick]);
  return attention;
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
function FlowChildren({ parent, link, attention, tick, onOpen }: {
  parent: Flow;
  link: string | null;
  attention: Attention[];
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
        <FlowRow key={child.workflow_id} flow={child} inheritedLink={link} attention={attention}
          tick={tick} onOpen={onOpen} />
      ))}
    </ol>
  );
}

/** A flow and, once opened, its steps; `attention` is what the whole tree waits for. */
function FlowRow({ flow, inheritedLink, attention, tick, onOpen, onFilter, topLevel = false }: {
  flow: Flow;
  inheritedLink: string | null;
  attention: Attention[];
  tick: number;
  onOpen: (link: string) => void;
  // Narrows the list to the flow's repository or branch; top-level flows only.
  onFilter?: (next: Partial<Filters>) => void;
  topLevel?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const link = flowLink(flow) ?? inheritedLink;
  // A top-level flow is highlighted for anything in its tree; a step for what it asks itself.
  const asking = topLevel ? attention : attention.filter(
    (item) => item.scope === flow.workflow_id || item.workflow_id === flow.workflow_id);
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
        <code className="flow-id" title={flow.workflow_id}>{flow.workflow_id}</code>
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
        <span className="flow-actions">
          {asking.length > 0 && (
            <button type="button" className="flow-questions-button" onClick={() => onOpen(asking[0].link)}>
              Open questions
            </button>
          )}
          {topLevel && link && (
            <button type="button" className="flow-open-button" onClick={() => onOpen(link)}>Open</button>
          )}
        </span>
      </div>
      {expanded && (
        <div id={childrenId} className="flow-body">
          {topLevel && <AttentionList items={attention} onOpen={onOpen} />}
          <FlowChildren parent={flow} link={link} attention={attention} tick={tick} onOpen={onOpen} />
        </div>
      )}
    </li>
  );
}

function TopLevelFlow({ flow, tick, onOpen, onFilter }: {
  flow: Flow;
  tick: number;
  onOpen: (link: string) => void;
  onFilter: (next: Partial<Filters>) => void;
}) {
  const attention = useAttention(flow, tick);
  return <FlowRow flow={flow} inheritedLink={null} attention={attention} tick={tick} onOpen={onOpen}
    onFilter={onFilter} topLevel />;
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
