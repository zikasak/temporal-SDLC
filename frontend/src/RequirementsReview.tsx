import { useEffect, useRef } from "react";
import { isObject } from "./api";
import { questionList, type Question } from "./Questions";
import { RequirementText } from "./RequirementLookup";

/** The requirements review of a task's or an onboarding's requirements, as the session
 * reports it: an LLM checks them for contradictions before the document is written. */
export type ReviewStatus =
  | "not_started"
  | "running"
  | "passed"
  | "contradictions_found"
  | "failed"
  | "continued_anyway"
  | "skipped";

export type Contradiction = {
  id: string;
  summary: string;
  requirement_ids: string[];
  question: Question;
};

export type RequirementsReview = {
  status: ReviewStatus;
  round: number;
  contradictions: Contradiction[];
  // Whether the contradiction questions of the last message can still be answered.
  questions_open: boolean;
  error: string | null;
};

const reviewStatuses: ReviewStatus[] = [
  "not_started", "running", "passed", "contradictions_found", "failed", "continued_anyway", "skipped",
];

export const reviewStatusLabels: Record<ReviewStatus, string> = {
  not_started: "Not started",
  running: "Reviewing",
  passed: "No contradictions",
  contradictions_found: "Contradictions found",
  failed: "Review failed",
  continued_anyway: "Continued despite contradictions",
  skipped: "Skipped",
};

export const notStarted: RequirementsReview = {
  status: "not_started", round: 0, contradictions: [], questions_open: false, error: null,
};

function contradictionList(value: unknown): Contradiction[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isObject(item) || typeof item.id !== "string" || typeof item.summary !== "string") return [];
    const [question] = questionList([item.question]);
    return [{
      id: item.id,
      summary: item.summary,
      requirement_ids: Array.isArray(item.requirement_ids)
        ? item.requirement_ids.filter((id): id is string => typeof id === "string") : [],
      question: question ?? { question: "", options: [] },
    }];
  });
}

/** The review in a session response; not started when the session reports none. */
export function requirementsReview(value: unknown): RequirementsReview {
  if (!isObject(value) || !(reviewStatuses as unknown[]).includes(value.status)) return notStarted;
  return {
    status: value.status as ReviewStatus,
    round: typeof value.round === "number" ? value.round : 0,
    contradictions: contradictionList(value.contradictions),
    questions_open: value.questions_open === true,
    error: typeof value.error === "string" && value.error ? value.error : null,
  };
}

/** Whether finalizing needs the user to confirm continuing despite the review. */
export function reviewBlocks(review: RequirementsReview) {
  return review.status === "running" || review.questions_open ||
    review.status === "failed" || review.status === "contradictions_found";
}

/** Whether a free message now drops the open contradiction questions, or is sent after a
 * failed round, so the user is warned first. */
export function messageWarning(review: RequirementsReview): string | null {
  if (review.questions_open) {
    return "Sending a message instead of answering drops the contradiction questions of the " +
      "requirements review: they can no longer be answered, and generating the document again " +
      "starts the next review round. Send the message anyway?";
  }
  if (review.status === "failed") {
    return "The last requirements review round failed. A message may change the requirements; " +
      "generating the document again then starts a new round on them. Send the message anyway?";
  }
  return null;
}

function Contradictions({ items }: { items: Contradiction[] }) {
  return (
    <ul className="review-contradictions">
      {items.map((item) => (
        <li key={item.id}>
          <div className="requirement-heading">
            <code>{item.id}</code>
            <span>{item.requirement_ids.join(", ")}</span>
          </div>
          <p><RequirementText text={item.summary} /></p>
          {item.question.question && <small><RequirementText text={item.question.question} /></small>}
        </li>
      ))}
    </ul>
  );
}

/** The review's status, round, contradictions and any reviewer error, with a retry of a
 * failed round and the way to continue despite the review. */
export function ReviewPanel({ review, disabled, onRetry, onForce }: {
  review: RequirementsReview;
  disabled: boolean;
  onRetry?: () => void;
  onForce?: () => void;
}) {
  if (review.status === "not_started" && review.round === 0) return null;
  return (
    <section className={`requirements-review review-${review.status}`} aria-label="Requirements review">
      <div className="review-heading">
        <h3>Requirements review</h3>
        <span className={`status status-review-${review.status}`}>
          <span className="status-dot" aria-hidden="true" />
          {reviewStatusLabels[review.status]}
        </span>
        {review.round > 0 && <span className="review-round">Round {review.round}</span>}
      </div>
      {review.status === "running" && (
        <p role="status">The reviewer is checking the requirements for contradictions…</p>
      )}
      {review.status === "contradictions_found" && (
        <p>
          {review.questions_open
            ? "Answer the questions below on how to resolve each contradiction; the review then runs again."
            : "The questions were dropped by a message. Generate the document again to start the next review round."}
        </p>
      )}
      {review.status === "continued_anyway" && review.contradictions.length > 0 && (
        <p>These contradictions remained and are listed in the requirements document:</p>
      )}
      {review.contradictions.length > 0 && <Contradictions items={review.contradictions} />}
      {review.status === "failed" && (
        <div className="error-state" role="alert">
          <p>The review round did not complete.</p>
          <span>{review.error ?? "The reviewer could not be reached."}</span>
          {onRetry && <button type="button" disabled={disabled} onClick={onRetry}>Retry the review round</button>}
        </div>
      )}
      {onForce && reviewBlocks(review) && (
        <button type="button" className="review-force" disabled={disabled} onClick={onForce}>
          Continue anyway…
        </button>
      )}
    </section>
  );
}

/** The warning shown before finalizing despite the requirements review. */
export function ForceDialog({ open, review, busy, what, onConfirm, onCancel }: {
  open: boolean;
  review: RequirementsReview;
  busy: boolean;
  // What continuing does, such as "write the requirements document".
  what: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    if (open && !element.open) {
      if (typeof element.showModal === "function") element.showModal();
      else element.setAttribute("open", "");
    } else if (!open && element.open) {
      element.close();
    }
  }, [open]);

  return (
    <dialog ref={dialog} className="force-dialog" aria-labelledby="force-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        onCancel();
      }}>
      <h2 id="force-dialog-title">Continue despite the requirements review?</h2>
      <p>
        {review.status === "running"
          ? "A review round is still running; it will be cancelled."
          : review.status === "failed"
            ? "The last review round failed, so the requirements were not confirmed free of contradictions."
            : review.contradictions.length > 0
              ? `The review still reports ${review.contradictions.length === 1
                ? "a contradiction" : `${review.contradictions.length} contradictions`} between the requirements.`
              : "The review has not confirmed the requirements yet."}
      </p>
      {review.contradictions.length > 0 && <Contradictions items={review.contradictions} />}
      <p>
        Continuing will {what} at once. The contradictions that remain are listed in the
        requirements document, and the next stages work from requirements that contradict
        each other.
      </p>
      <div className="force-dialog-actions">
        <button type="button" onClick={onCancel} disabled={busy}>Go back</button>
        <button type="button" className="danger-button" onClick={onConfirm} disabled={busy}>
          {busy ? "Continuing…" : "Continue anyway"}
        </button>
      </div>
    </dialog>
  );
}

/** A free message sent instead of answering the review's contradiction questions. */
export function MessageBox({ message, onChange, disabled, submitting, onSend }: {
  message: string;
  onChange: (value: string) => void;
  disabled: boolean;
  submitting: boolean;
  onSend: () => void;
}) {
  return (
    <div className="message-instead">
      <label htmlFor="message-instead">Message instead</label>
      <textarea id="message-instead" value={message} rows={3} maxLength={10000}
        onChange={(event) => onChange(event.target.value)} disabled={disabled}
        placeholder="Change the requirements instead of answering…" />
      <button type="button" className="run-button" disabled={disabled || !message.trim()} onClick={onSend}>
        <span>{submitting ? "Sending" : "Send message"}</span>
      </button>
    </div>
  );
}
