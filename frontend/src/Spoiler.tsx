import { useId, useState, type ReactNode } from "react";

/**
 * A section of an interview's results hidden under a spoiler: its heading stays visible
 * and is the toggle, the content below it is hidden until the heading is clicked or
 * tapped, and a further click hides it again. It always starts closed; give it a `key`
 * of the session so that opening another interview closes it again.
 */
export function SpoilerSection({ className, label, heading, children }: {
  className: string;
  // The section's accessible name.
  label: string;
  // The text of the heading, the toggle.
  heading: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  return (
    <section className={`${className} spoiler${open ? " spoiler-open" : ""}`} aria-label={label}>
      <h3 className="spoiler-heading">
        <button type="button" className="spoiler-toggle" aria-expanded={open}
          aria-controls={contentId} onClick={() => setOpen((current) => !current)}>
          {heading}
        </button>
      </h3>
      <div id={contentId} className="spoiler-content" hidden={!open}>
        {children}
      </div>
    </section>
  );
}
