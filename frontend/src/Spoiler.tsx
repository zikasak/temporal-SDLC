import { useId, useState, type ReactNode } from "react";

/**
 * A result section of an interview whose heading stays visible and toggles its content.
 *
 * The content is hidden (not rendered) until the heading is clicked or tapped, and a
 * further click hides it again. It always starts closed: nothing remembers the last state.
 * `testid` names the toggle `<testid>-spoiler-toggle` and the content `<testid>-spoiler-content`.
 */
export function SpoilerSection({ label, className, heading, testid, children }: {
  label: string;
  className: string;
  heading: ReactNode;
  testid: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  return (
    <section className={`${className} spoiler`} aria-label={label}>
      <h3 className="spoiler-heading">
        <button type="button" className="spoiler-toggle" aria-expanded={open}
          aria-controls={open ? contentId : undefined}
          data-testid={`${testid}-spoiler-toggle`}
          onClick={() => setOpen((value) => !value)}>
          <span className="spoiler-marker" aria-hidden="true">{open ? "▾" : "▸"}</span>
          <span className="spoiler-label">{heading}</span>
        </button>
      </h3>
      {open && (
        <div id={contentId} className="spoiler-content" data-testid={`${testid}-spoiler-content`}>
          {children}
        </div>
      )}
    </section>
  );
}
