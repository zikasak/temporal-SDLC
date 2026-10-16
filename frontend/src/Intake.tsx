import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { errorMessage, failure, readBody } from "./api";
import { QuestionField, choiceAnswer, noChoice, questionList, type Choice, type Question } from "./Questions";

/** The questions every new requirements session starts with, as the backend defines them. */
type IntakeQuestions = {
  kind_question: string;
  kinds: { kind: string; label: string }[];
  questions: Record<string, Question[]>;
  task_question: string;
};

/** The user's answers to the intake, which start the session. */
export type Intake = { kind: string; answers: string[]; task: string };

function intakeRecord(value: unknown): IntakeQuestions {
  const record = value as Partial<IntakeQuestions> | null;
  if (
    typeof record !== "object" || record === null ||
    typeof record.kind_question !== "string" ||
    typeof record.task_question !== "string" ||
    !Array.isArray(record.kinds) ||
    !record.kinds.every((item) => typeof item?.kind === "string" && typeof item.label === "string") ||
    typeof record.questions !== "object" || record.questions === null
  ) {
    throw new Error("The backend returned invalid starting questions.");
  }
  const questions = record.questions as Record<string, unknown>;
  return {
    ...(record as IntakeQuestions),
    questions: Object.fromEntries(record.kinds.map(({ kind }) => [kind, questionList(questions[kind])])),
  };
}

/** The fixed start of every session: the repository, the kind of task, the questions of
 * that kind, and what needs to be done. All of it is shown up front, numbered. */
export default function IntakeForm({ repository, repositoryReady, disabled, submitting, onSubmit }: {
  repository: ReactNode;
  repositoryReady: boolean;
  disabled: boolean;
  submitting: boolean;
  onSubmit: (intake: Intake) => void;
}) {
  const [intake, setIntake] = useState<IntakeQuestions | null>(null);
  const [loadError, setLoadError] = useState("");
  const [kind, setKind] = useState("");
  const [choices, setChoices] = useState<Choice[]>([]);
  const [task, setTask] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    (async () => {
      try {
        const response = await fetch("/api/requirements/intake", { signal: controller.signal });
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        setIntake(intakeRecord(body));
      } catch (caught) {
        if (!controller.signal.aborted) setLoadError(failure(caught, "Could not load the starting questions."));
      }
    })();
    return () => controller.abort();
  }, []);

  const questions = (kind && intake?.questions[kind]) || [];
  const answers = choices.map(choiceAnswer);
  const complete = repositoryReady && Boolean(kind) && answers.length === questions.length &&
    answers.every(Boolean) && Boolean(task.trim());

  function chooseKind(value: string) {
    setKind(value);
    setChoices((intake?.questions[value] ?? []).map(() => noChoice));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (complete && !disabled) onSubmit({ kind, answers, task: task.trim() });
  }

  if (loadError) return <p className="connector-error" role="alert">{loadError}</p>;
  if (!intake) return <p className="field-hint">Loading the starting questions…</p>;

  return (
    <form className="intake-form" onSubmit={submit}>
      <p className="field-hint">
        Every session starts with these steps. The analyst then asks follow-up questions
        until the requirements are complete.
      </p>
      <ol className="intake-steps">
        <li>{repository}</li>
        <li className="question-form">
          <fieldset disabled={disabled}>
            <legend>{intake.kind_question}</legend>
            {intake.kinds.map((item) => (
              <label key={item.kind} className="question-option">
                <input type="radio" name="intake-kind" value={item.kind} checked={kind === item.kind}
                  onChange={() => chooseKind(item.kind)} />
                <span>{item.label}</span>
              </label>
            ))}
          </fieldset>
        </li>
        <li className="question-form">
          {kind ? (
            questions.map((item, index) => (
              <QuestionField key={`${kind}-${index}`} name={`intake-${index}`} question={item}
                disabled={disabled} value={choices[index] ?? noChoice}
                onChange={(choice) => setChoices(choices.map((old, position) => (position === index ? choice : old)))} />
            ))
          ) : (
            <>
              <span className="intake-step-title">Questions about the task</span>
              <p className="field-hint">Choose the kind of task to see its questions: for a change,
                the area it touches and its nature.</p>
            </>
          )}
        </li>
        <li>
          <label className="intake-step-title" htmlFor="intake-task">{intake.task_question}</label>
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
