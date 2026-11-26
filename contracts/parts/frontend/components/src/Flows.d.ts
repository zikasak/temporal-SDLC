import type { JSX } from "react";
import type { RepositorySelection } from "./github";

/** The types of flow the Flows page filters by. */
type FlowType =
  | "LLMRequestWorkflow"
  | "TaskWorkflow"
  | "ArchitectureWorkflow"
  | "ApiDesignWorkflow";

/** What started a task flow: a requirements interview or an onboarding. */
type Origin = "requirements" | "onboarding";

/** A workflow execution as GET /api/workflows lists it. `status` is the execution's
 * status in lower case, such as "running", "completed", "canceled", "failed", "pending"
 * or "skipped". `name` and `origin` are a task flow's short name and what started it,
 * null for any other flow; `linked` marks an earlier stage of another task, listed under
 * a forked or fix-round task flow. `waiting_for_user` and `unresolved_findings` are as
 * the backend reports them: null for flows from before it did, and for flows that
 * cannot wait or have no delivery. */
type Flow = {
  workflow_id: string;
  run_id: string;
  workflow_type: string;
  status: string;
  parent_workflow_id: string | null;
  start_time: string;
  close_time: string | null;
  repository: RepositorySelection | null;
  branch: string | null;
  name: string | null;
  origin: Origin | null;
  linked: boolean;
  waiting_for_user: boolean | null;
  unresolved_findings: boolean | null;
};

/** Filters of the Flows page list; empty strings for none. `parent` lists the children
 * of one flow. */
type Filters = {
  type: FlowType | "";
  origin: Origin | "";
  repository: string;
  branch: string;
  parent: string;
};

/**
 * One top-level flow of the Flows page, as a list item (`<li>`), with its type, its name
 * (or its ID when it has none), its repository and branch, its start time, how long it
 * took once closed, and its status. The repository and the branch are buttons that call
 * `onFilter` with `{ repository }` and `{ repository, branch }`. Its toggle, unless the
 * flow is `linked`, opens its child flows and steps (fetched from
 * GET /api/workflows?parent=<workflow ID>), each shown the same way.
 *
 * A flow that waits for the user (`waiting_for_user` true) and has not closed (`status`
 * not "completed", "canceled" or "cancelled") nor is "pending" is highlighted with an
 * "Open questions" button: it calls `onOpen` with the link of the view of the first flow
 * of its tree that waits for the user.
 *
 * A closed flow with `unresolved_findings` true is shown with an "unresolved findings"
 * marker only: a closed flow is never highlighted as waiting and has no "Open questions"
 * button. Without unresolved findings it shows no marker.
 *
 * When either flag is null (a flow from before the backend reported them), both are read
 * from the flow's session: GET /api/requirements/sessions/<ID>,
 * /api/architecture/sessions/<ID>, /api/api-design/sessions/<ID> or
 * /api/onboarding/sessions/<ID> (by `flow.workflow_type`). The session waits for the user
 * when the interviewer's last message asks questions while it is "interviewing", when
 * its delivery has an agent's questions or open review findings (`open_findings` true),
 * a skip proposal, or when it is "awaiting_confirmation"; its findings are unresolved
 * when its delivery has open review findings. A session's delivery progress is its
 * `delivery` object, or for an API design the session body itself. A top-level
 * requirements session adds the flags of its architecture and API design stages. A flag
 * the backend reports is never replaced. The session is read again on every `tick` while
 * the flow runs or its findings are unresolved.
 *
 * A flow with a view of its own that is neither "pending" nor "skipped" has an "Open"
 * button that calls `onOpen` with the link of that view.
 */
export declare function TopLevelFlow({ flow, tick, onOpen, onFilter }: {
  flow: Flow;
  tick: number;
  onOpen: (link: string) => void;
  onFilter: (next: Partial<Filters>) => void;
}): JSX.Element;
