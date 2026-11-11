import {
  createContext, useContext, useEffect, useId, useMemo, useRef, useState,
  type KeyboardEvent, type MouseEvent, type ReactNode,
} from "react";
import Markdown, { type Components, type Options } from "react-markdown";
import { isObject } from "./api";
import { markdownComponents, remarkPlugins } from "./markdown";

/** A requirement a requirement ID mentioned in a conversation can refer to, as a session
 * response lists it in lookup_requirements. */
export type LookupRequirement = {
  id: string;
  category: "functional" | "non_functional" | "technical";
  title: string;
  description: string;
  priority: "must" | "should" | "could";
  retired: boolean;
};

const categoryLabels: Record<LookupRequirement["category"], string> = {
  functional: "Functional",
  non_functional: "Non-functional",
  technical: "Technical",
};
const priorities = ["must", "should", "could"];
// The prefixes of the requirement categories; a session's own IDs may add others.
const defaultPrefixes = ["FR", "NFR", "TR"];

function isLookupRequirement(value: unknown): value is LookupRequirement {
  return isObject(value) && typeof value.id === "string" &&
    typeof value.category === "string" && value.category in categoryLabels &&
    typeof value.title === "string" && typeof value.description === "string" &&
    typeof value.priority === "string" && priorities.includes(value.priority) &&
    typeof value.retired === "boolean";
}

/** The lookup requirements in `value`, skipping malformed entries; none for anything else. */
export function lookupRequirementList(value: unknown): LookupRequirement[] {
  return Array.isArray(value) ? value.filter(isLookupRequirement) : [];
}

/** The canonical form PREFIX-NUMBER of fr12, FR12, fr-12 or FR-12; null for other text. */
export function canonicalRequirementId(text: string): string | null {
  const match = /^([A-Za-z]+)-?(\d+)$/.exec(text.trim());
  return match ? `${match[1].toUpperCase()}-${match[2]}` : null;
}

type Lookup = { requirements: Map<string, LookupRequirement>; pattern: RegExp };

const LookupContext = createContext<Lookup | null>(null);

function escapeRegExp(text: string) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function buildLookup(requirements: LookupRequirement[]): Lookup {
  const entries = new Map<string, LookupRequirement>();
  const prefixes = new Set(defaultPrefixes);
  for (const item of requirements) {
    const id = canonicalRequirementId(item.id) ?? item.id.toUpperCase();
    entries.set(id, item);
    const prefix = /^([A-Za-z]+)-?\d+$/.exec(item.id)?.[1];
    if (prefix) prefixes.add(prefix.toUpperCase());
  }
  const alternatives = [...prefixes].sort((a, b) => b.length - a.length).map(escapeRegExp);
  return {
    requirements: entries,
    pattern: new RegExp(`\\b(?:${alternatives.join("|")})-?\\d+\\b`, "gi"),
  };
}

/** Makes requirement IDs in the conversation below explainable with `requirements`, the
 * session's lookup_requirements; null (no session yet) leaves text as it is. */
export function RequirementLookupProvider({ requirements, children }: {
  requirements: LookupRequirement[] | null;
  children: ReactNode;
}) {
  const lookup = useMemo(() => (requirements ? buildLookup(requirements) : null), [requirements]);
  return <LookupContext.Provider value={lookup}>{children}</LookupContext.Provider>;
}

type Segment = { text: string; id: boolean };

function segments(text: string, pattern: RegExp): Segment[] {
  const result: Segment[] = [];
  let last = 0;
  for (const match of text.matchAll(new RegExp(pattern.source, pattern.flags))) {
    const start = match.index ?? 0;
    if (start > last) result.push({ text: text.slice(last, start), id: false });
    result.push({ text: match[0], id: true });
    last = start + match[0].length;
  }
  if (last < text.length) result.push({ text: text.slice(last), id: false });
  return result;
}

/** Plain text such as a question or an option, with its requirement IDs marked. */
export function RequirementText({ text }: { text: string }) {
  const lookup = useContext(LookupContext);
  if (!lookup) return <>{text}</>;
  return <>{segments(text, lookup.pattern).map((item, index) => (
    item.id ? <RequirementRef key={index} text={item.text} /> : <span key={index}>{item.text}</span>
  ))}</>;
}

function RequirementPopup({ id, requirement, popupId }: {
  id: string; requirement: LookupRequirement | undefined; popupId: string;
}) {
  return (
    <span className="requirement-popup" role="tooltip" id={popupId}>
      {requirement ? (
        <>
          <span className="requirement-popup-heading">
            <strong>{requirement.id}</strong>
            {requirement.retired && <span className="requirement-retired">retired</span>}
          </span>
          <span className="requirement-popup-title">{requirement.title}</span>
          {requirement.description && (
            <span className="requirement-popup-description">{requirement.description}</span>
          )}
          <span className="requirement-popup-meta">
            <span className={`priority priority-${requirement.priority}`}>{requirement.priority}</span>
            <span>{categoryLabels[requirement.category]}</span>
          </span>
        </>
      ) : (
        <>
          <span className="requirement-popup-heading"><strong>{id}</strong></span>
          <span className="requirement-popup-title">Unknown requirement</span>
        </>
      )}
    </span>
  );
}

/** A mentioned requirement ID: hovering over it or tapping it shows the requirement. */
export function RequirementRef({ text }: { text: string }) {
  const lookup = useContext(LookupContext);
  const [hovered, setHovered] = useState(false);
  const [pinned, setPinned] = useState(false);
  const wrapper = useRef<HTMLSpanElement>(null);
  const popupId = useId();
  const visible = hovered || pinned;

  useEffect(() => {
    if (!visible) return;
    function close(event: PointerEvent) {
      if (wrapper.current && !wrapper.current.contains(event.target as Node)) {
        setPinned(false);
        setHovered(false);
      }
    }
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [visible]);

  const id = canonicalRequirementId(text);
  if (!lookup || !id) return <>{text}</>;
  const requirement = lookup.requirements.get(id);

  function toggle(event: MouseEvent<HTMLSpanElement>) {
    // Inside an option's label, a tap shows the requirement without choosing the option.
    event.preventDefault();
    event.stopPropagation();
    setPinned((value) => !value);
  }

  function onKey(event: KeyboardEvent<HTMLSpanElement>) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      event.stopPropagation();
      setPinned((value) => !value);
    } else if (event.key === "Escape") {
      setPinned(false);
      setHovered(false);
    }
  }

  return (
    <span className="requirement-ref-wrapper" ref={wrapper}
      onMouseEnter={() => setHovered(true)} onMouseLeave={() => setHovered(false)}>
      <span role="button" tabIndex={0}
        className={`requirement-ref${requirement ? "" : " requirement-ref-unknown"}${requirement?.retired ? " requirement-ref-retired" : ""}`}
        aria-expanded={visible} aria-describedby={visible ? popupId : undefined}
        onClick={toggle} onKeyDown={onKey}
        onFocus={() => setHovered(true)} onBlur={() => { setHovered(false); setPinned(false); }}>
        {text}
      </span>
      {visible && <RequirementPopup id={id} requirement={requirement} popupId={popupId} />}
    </span>
  );
}

// The Markdown tree of an LLM message, as far as marking requirement IDs needs it.
type MarkdownNode = {
  type: string;
  value?: string;
  children?: MarkdownNode[];
  data?: Record<string, unknown>;
};
const REFERENCE_CLASS = "requirement-id";

function markRequirementIds(pattern: RegExp) {
  function mark(node: MarkdownNode) {
    // A link's text stays a link.
    if (!node.children || node.type === "link" || node.type === "linkReference") return;
    node.children = node.children.flatMap((child): MarkdownNode[] => {
      if (child.type !== "text" || !child.value) {
        mark(child);
        return [child];
      }
      return segments(child.value, pattern).map((item) => (item.id
        ? {
          type: "requirementId",
          children: [{ type: "text", value: item.text }],
          data: { hName: "abbr", hProperties: { className: [REFERENCE_CLASS] } },
        }
        : { type: "text", value: item.text }));
    });
  }
  return () => (tree: MarkdownNode) => mark(tree);
}

function textOf(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textOf).join("");
  return "";
}

const conversationComponents: Components = {
  ...markdownComponents,
  abbr: ({ node: _node, className, children, ...props }) => (
    className === REFERENCE_CLASS
      ? <RequirementRef text={textOf(children)} />
      : <abbr className={className} {...props}>{children}</abbr>
  ),
};

/** An LLM message's Markdown, with its requirement IDs marked. */
export function ConversationMarkdown({ children }: { children: string }) {
  const lookup = useContext(LookupContext);
  const plugins = useMemo(
    () => (lookup
      ? [...remarkPlugins, markRequirementIds(lookup.pattern)] as Options["remarkPlugins"]
      : remarkPlugins),
    [lookup],
  );
  return (
    <Markdown remarkPlugins={plugins} components={lookup ? conversationComponents : markdownComponents}>
      {children}
    </Markdown>
  );
}
