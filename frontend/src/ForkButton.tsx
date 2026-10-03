/** Starts a new branch of the conversation that ends with this message. */
export default function ForkButton({
  role,
  disabled,
  onFork,
}: {
  role: "user" | "assistant";
  disabled: boolean;
  onFork: () => void;
}) {
  const title =
    role === "user"
      ? "Start a new branch from here and get a fresh answer to this message"
      : "Start a new branch from here and continue it with a different message";
  return (
    <button className="fork-button" type="button" onClick={onFork} disabled={disabled} title={title}>
      <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
        <circle cx="6" cy="4" r="1.6" />
        <circle cx="6" cy="16" r="1.6" />
        <circle cx="14" cy="5" r="1.6" />
        <path d="M6 5.6v8.8M14 6.6c0 4-8 3-8 6.5" />
      </svg>
      {role === "user" ? "Fork and answer again" : "Fork from here"}
    </button>
  );
}
