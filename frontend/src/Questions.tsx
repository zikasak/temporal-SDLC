import { useState, type FormEvent } from "react";
import { RequirementText } from "./RequirementLookup";

/** A question the model asked with ask_user: the user picks an option or writes their own. */
export type Question = { question: string; options: string[] };

const OTHER = "\u0000other";

function isQuestion(value: unknown): value is Question {
  return typeof value === "object" && value !== null &&
    typeof (value as Question).question === "string" &&
    Array.isArray((value as Question).options) &&
    (value as Question).options.every((option) => typeof option === "string");
}

/** The questions in `value`; none when it holds anything else. */
export function questionList(value: unknown): Question[] {
  return Array.isArray(value) && value.every(isQuestion) ? value : [];
}

/** The questions a message asked, listed under it in the conversation. */
export function AskedQuestions({ questions }: { questions: Question[] }) {
  if (questions.length === 0) return null;
  return (
    <ol className="asked-questions">
      {questions.map((item, index) => <li key={index}>
        <strong><RequirementText text={item.question} /></strong>
        <span>{item.options.map((option, position) => <span key={position}>
          {position > 0 && " · "}<RequirementText text={option} />
        </span>)}</span>
      </li>)}
    </ol>
  );
}

/** One choice per question, each an option or the user's own answer under "Other". */
export function QuestionForm({ questions, disabled, submitting, label = "Send answers", onSubmit }: {
  questions: Question[];
  disabled: boolean;
  submitting: boolean;
  label?: string;
  onSubmit: (answers: string[]) => Promise<boolean>;
}) {
  const [choices, setChoices] = useState<Choice[]>(() => questions.map(() => noChoice));
  const answers = choices.map(choiceAnswer);
  const complete = answers.length === questions.length && answers.every(Boolean);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!complete || disabled) return;
    if (await onSubmit(answers)) setChoices(questions.map(() => noChoice));
  }

  return (
    <form className="question-form" onSubmit={submit}>
      {questions.map((item, index) => (
        <QuestionField key={index} name={`question-${index}`} question={item} disabled={disabled}
          value={choices[index]}
          onChange={(choice) => setChoices(choices.map((old, position) => (position === index ? choice : old)))} />
      ))}
      <button className="run-button" type="submit" disabled={disabled || !complete}>
        <span>{submitting ? "Sending" : label}</span>
        {submitting && <span className="spinner" aria-hidden="true" />}
      </button>
    </form>
  );
}

/** What the user picked for one question: an option, or "Other" with their own text. */
export type Choice = { option: string; other: string };

export const noChoice: Choice = { option: "", other: "" };

/** The answer a choice gives; empty while it gives none. */
export function choiceAnswer(choice: Choice) {
  return (choice.option === OTHER ? choice.other : choice.option).trim();
}

/** One question with its options and "Other"; the parent holds the choice. */
export function QuestionField({ name, question, value, disabled, onChange }: {
  name: string;
  question: Question;
  value: Choice;
  disabled: boolean;
  onChange: (choice: Choice) => void;
}) {
  return (
    <fieldset disabled={disabled}>
      <legend><RequirementText text={question.question} /></legend>
      {question.options.map((option) => (
        <label key={option} className="question-option">
          <input type="radio" name={name} value={option} checked={value.option === option}
            onChange={() => onChange({ ...value, option })} />
          <span><RequirementText text={option} /></span>
        </label>
      ))}
      <label className="question-option">
        <input type="radio" name={name} value={OTHER} checked={value.option === OTHER}
          onChange={() => onChange({ ...value, option: OTHER })} />
        <span>Other</span>
      </label>
      {value.option === OTHER && (
        <textarea className="question-other" rows={2} maxLength={2000} autoFocus
          aria-label={`Your answer to: ${question.question}`} value={value.other}
          placeholder="Write your own answer…"
          onChange={(event) => onChange({ ...value, other: event.target.value })} />
      )}
    </fieldset>
  );
}
