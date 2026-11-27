import { useState, type MouseEvent, type ReactNode } from "react";

/**
 * A section whose list content stays hidden behind its heading, so the interview's
 * results (gathered requirements, the designed API model, the architecture) are not
 * visible before the user has gone through the conversation. The heading stays visible
 * as the toggle: clicking or tapping it reveals the content and a further click closes
 * it again. The spoiler always starts closed.
 */
export default function Spoiler({
  heading,
  ariaLabel,
  className,
  children,
}: {
  // The heading text, which stays visible as the toggle.
  heading: ReactNode;
  ariaLabel: string;
  className?: string;
  // The list content hidden until the heading is clicked.
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const toggle = () => setOpen((value) => !value);
  return (
    <section className={className} aria-label={ariaLabel}>
      <h3 className="spoiler-heading" onClick={toggle}>
        <button
          type="button"
          className="spoiler-toggle"
          aria-expanded={open}
          onClick={(event: MouseEvent<HTMLButtonElement>) => {
            event.stopPropagation();
            toggle();
          }}
        >
          {heading}
          <span className="spoiler-caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
        </button>
      </h3>
      {open && <div className="spoiler-content">{children}</div>}
    </section>
  );
}
