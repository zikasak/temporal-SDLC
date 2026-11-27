import { useState, type FormEvent, type ReactNode } from "react";

/** What starts a session besides the repository: what needs to be done. The kind of task
 * is proposed by the analyst and confirmed in the interview. */
export type Intake = { task: string };

/** The start of every session: the repository and its branch, and what needs to be done,
 * numbered. The analyst then proposes the kind of task and asks its follow-up questions. */
export default function IntakeForm({ repository, repositoryReady, disabled, submitting, onSubmit }: {
  repository: ReactNode;
  repositoryReady: boolean;
  disabled: boolean;
  submitting: boolean;
  onSubmit: (intake: Intake) => void;
}) {
  const [task, setTask] = useState("");
  const complete = repositoryReady && Boolean(task.trim());

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (complete && !disabled) onSubmit({ task: task.trim() });
  }

  return (
    <form className="intake-form" onSubmit={submit}>
      <p className="field-hint">
        Choose the repository and say what needs to be done. The analyst then proposes the
        kind of task and asks follow-up questions until the requirements are complete.
      </p>
      <ol className="intake-steps">
        <li>{repository}</li>
        <li>
          <label className="intake-step-title" htmlFor="intake-task">What needs to be done?</label>
          <textarea id="intake-task" value={task} maxLength={10000} rows={5} disabled={disabled}
            placeholder="Describe the task in your own words…"
            onChange={(event) => setTask(event.target.value)} />
        </li>
      </ol>
      <button className="run-button" type="submit" disabled={disabled || !complete}>
        <span>{submitting ? "Starting" : "Start a session"}</span>
        {submitting && <span className="spinner" aria-hidden="true" />}
      </button>
    </form>
  );
}
