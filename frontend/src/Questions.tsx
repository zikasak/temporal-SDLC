import { useState, type FormEvent } from "react";

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
        <strong>{item.question}</strong>
        <span>{item.options.join(" · ")}</span>
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
  const [choices, setChoices] = useState<string[]>(() => questions.map(() => ""));
  const [others, setOthers] = useState<string[]>(() => questions.map(() => ""));
  const answers = choices.map((choice, index) => (choice === OTHER ? others[index] : choice).trim());
  const complete = answers.length === questions.length && answers.every(Boolean);

  function update(list: string[], index: number, value: string) {
    return list.map((item, position) => (position === index ? value : item));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!complete || disabled) return;
    if (await onSubmit(answers)) {
      setChoices(questions.map(() => ""));
      setOthers(questions.map(() => ""));
    }
  }

  return (
    <form className="question-form" onSubmit={submit}>
      {questions.map((item, index) => (
        <fieldset key={index} disabled={disabled}>
          <legend>{item.question}</legend>
          {item.options.map((option) => (
            <label key={option} className="question-option">
              <input type="radio" name={`question-${index}`} value={option}
                checked={choices[index] === option}
                onChange={() => setChoices(update(choices, index, option))} />
              <span>{option}</span>
            </label>
          ))}
          <label className="question-option">
            <input type="radio" name={`question-${index}`} value={OTHER}
              checked={choices[index] === OTHER}
              onChange={() => setChoices(update(choices, index, OTHER))} />
            <span>Other</span>
          </label>
          {choices[index] === OTHER && (
            <textarea className="question-other" rows={2} maxLength={2000} autoFocus
              aria-label={`Your answer to: ${item.question}`} value={others[index]}
              placeholder="Write your own answer…"
              onChange={(event) => setOthers(update(others, index, event.target.value))} />
          )}
        </fieldset>
      ))}
      <button className="run-button" type="submit" disabled={disabled || !complete}>
        <span>{submitting ? "Sending" : label}</span>
        {submitting && <span className="spinner" aria-hidden="true" />}
      </button>
    </form>
  );
}
