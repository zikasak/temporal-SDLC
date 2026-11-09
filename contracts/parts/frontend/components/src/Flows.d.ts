import type { JSX } from "react";
import type { RepositorySelection } from "./github";

/** A workflow execution as GET /api/workflows lists it. `status` is the execution's
 * status in lower case: "running", "completed", "canceled", "failed", "terminated",
 * "timed_out" or "continued_as_new". */
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
  onboarding_id: string | null;
};

/** Filters of the Flows page list; empty strings for none. */
type Filters = {
  type: "LLMRequestWorkflow" | "RequirementsWorkflow" | "ArchitectureWorkflow" | "ApiDesignWorkflow" | "OnboardingWorkflow" | "";
  repository: string;
  branch: string;
  onboarding_id: string;
};

/**
 * One top-level flow of the Flows page, as a list item (`<li>`), with its type, ID,
 * status and, once its toggle is opened, its child flows and steps (fetched from
 * GET /api/workflows?parent=<workflow ID>).
 *
 * It reads the flow's session from GET /api/requirements/sessions/<ID>,
 * /api/architecture/sessions/<ID>, /api/api-design/sessions/<ID> or
 * /api/onboarding/sessions/<ID> (by `flow.workflow_type`); a session's delivery
 * progress is its `delivery` object, or for an API design the session body itself.
 *
 * A flow that has not completed or been cancelled (`status` "running") is highlighted
 * (an "Open questions" button) for each thing it waits for from the user: unanswered
 * questions, a coding agent's questions, open review findings (`open_findings` true:
 * "Review findings wait for your decision") or a contract decision; the button calls
 * `onOpen` with the link of the view that asks.
 *
 * A flow that has completed or been cancelled (`status` "completed" or "canceled") whose
 * delivery still has open review findings (`open_findings` true) is shown with an
 * "unresolved findings" marker only: it is not highlighted as waiting for the user and
 * has no "Open questions" button for them. Without open findings it shows no marker.
 */
export declare function TopLevelFlow({ flow, tick, onOpen, onFilter }: {
  flow: Flow;
  tick: number;
  onOpen: (link: string) => void;
  onFilter: (next: Partial<Filters>) => void;
}): JSX.Element;
