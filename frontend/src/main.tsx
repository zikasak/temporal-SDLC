import ReactDOM from "react-dom/client";
import ApiDesign from "./ApiDesign";
import App from "./App";
import Architecture from "./Architecture";
import Onboarding from "./Onboarding";
import Requirements from "./Requirements";
import Flows from "./Flows";
import GitHubSettings from "./GitHubSettings";
import Login, { userRecord, type User } from "./Login";
import Users from "./Users";
import Settings from "./Settings";
import { errorMessage, failure, readBody } from "./api";
import { APP_RETURN_PATH } from "./github";
import { broadcast, installSessionRenewal, onSessionMessage, refreshSession } from "./session";
import "./styles.css";
import React, { useEffect, useRef, useState } from "react";

type Page = "flows" | "github" | "users" | "settings";
// The views of a flow, opened in a panel over the Flows page.
type Panel = "chat" | "requirements" | "architecture" | "api-design" | "onboarding";

const pages: [Page, string][] = [
  ["flows", "Flows"],
  ["github", "GitHub"],
  ["settings", "Settings"],
  // Shown to the first user only.
  ["users", "Users"],
];
// Shown on the sign-in form when a session could not be renewed.
const SESSION_EXPIRED = "Your session expired. Sign in again to continue where you left off.";
// How long before the access token expires it is renewed, at most.
const RENEW_LEAD_MS = 60_000;

const panels: [Panel, string][] = [
  ["chat", "Chat"],
  ["requirements", "Requirements interview"],
  ["architecture", "Architecture"],
  ["api-design", "API design"],
  ["onboarding", "Onboarding"],
];

/** The page and the flow view a URL shows; any other mode, such as the former Workflows
 * tab, shows the list of flows. */
function locate(search: string): { page: Page; panel: Panel | null } {
  if (window.location.pathname === APP_RETURN_PATH) return { page: "github", panel: null };
  const params = new URLSearchParams(search);
  const mode = params.get("mode");
  const page = pages.find(([item]) => item === mode)?.[0];
  if (page) return { page, panel: null };
  const panel = panels.find(([item]) => item === mode)?.[0] ?? (params.has("request_id") ? "chat" : null);
  return { page: "flows", panel };
}

function listSearchOf(search: string) {
  const { page, panel } = locate(search);
  return page === "flows" && !panel ? search || "?" : "?";
}

function Shell() {
  const [search, setSearch] = useState(window.location.search);
  // The list's filters, kept while a flow is open over it.
  const [listSearch, setListSearch] = useState(() => listSearchOf(window.location.search));
  // Changes whenever a flow view is opened, so that it reads its URL afresh.
  const [panelKey, setPanelKey] = useState(0);
  const [listRefresh, setListRefresh] = useState(0);
  // undefined while the saved session is being checked, null when signed out.
  const [user, setUser] = useState<User | null>();
  const [error, setError] = useState("");

  // Why the sign-in form is shown again, if the session ended without signing out.
  const [notice, setNotice] = useState("");
  const userRef = useRef<User | null | undefined>(undefined);
  userRef.current = user;

  // A session that can no longer be renewed: back to the sign-in form, on the same page,
  // which the app shows again once the user has signed in.
  function expire(reason: string, fromOtherTab = false) {
    // Without a refresh token at the first check, the user was simply not signed in.
    const signedIn = Boolean(userRef.current);
    if (signedIn || reason !== "invalid") setNotice(SESSION_EXPIRED);
    if (signedIn && !fromOtherTab) broadcast({ type: "expired", reason });
    setUser(null);
  }
  const expireRef = useRef(expire);
  expireRef.current = expire;

  useEffect(() => {
    installSessionRenewal((reason) => expireRef.current(reason));
    return onSessionMessage((message) => {
      if (message.type === "refreshed") {
        // The cookies are shared: another tab's refresh renewed this one's session too.
        if (userRef.current) setUser(message.user);
      } else if (message.type === "signed-out") {
        setNotice("");
        setUser(null);
      } else if (userRef.current) {
        expireRef.current(message.reason, true);
      }
    });
  }, []);

  useEffect(() => {
    if (user !== undefined) return;
    const controller = new AbortController();
    void (async () => {
      try {
        // An expired access token is renewed by the request itself (installSessionRenewal).
        const response = await fetch("/api/auth/me", { signal: controller.signal });
        const body = await readBody(response);
        if (response.status === 401) setUser(null);
        else if (!response.ok) throw new Error(errorMessage(body, response.status));
        else setUser(userRecord(body));
      } catch (caught) {
        if (!controller.signal.aborted) setError(failure(caught, "Could not check the sign-in."));
      }
    })();
    return () => controller.abort();
  }, [user]);

  // Renew the access token shortly before it expires instead of signing the user out.
  useEffect(() => {
    if (!user) return;
    const remaining = new Date(user.expires_at).getTime() - Date.now();
    const lead = Math.min(RENEW_LEAD_MS, Math.max(remaining / 4, 0));
    let cancelled = false;
    const timer = window.setTimeout(() => {
      void refreshSession().then((outcome) => {
        if (cancelled) return;
        if ("user" in outcome) setUser(outcome.user);
        else expireRef.current(outcome.rejected);
      }, () => {
        // Unreachable for now: the next request that gets a 401 refreshes again.
      });
    }, Math.min(Math.max(remaining - lead, 0), 2 ** 31 - 1));
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [user]);

  async function signOut() {
    try {
      // An expired access token no longer reaches the server: renew it to name the session.
      if (user && new Date(user.expires_at).getTime() - Date.now() < 5_000) {
        await refreshSession().catch(() => null);
      }
      await fetch("/api/auth/logout", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      broadcast({ type: "signed-out" });
    } finally {
      setNotice("");
      setUser(null);
    }
  }

  function signedIn(next: User) {
    setNotice("");
    setUser(next);
  }

  function show(next: string) {
    setSearch(next);
    const { page, panel } = locate(next);
    if (page === "flows" && !panel) setListSearch(next || "?");
    setPanelKey((value) => value + 1);
  }

  function navigate(next: string) {
    const url = new URL(next, window.location.href);
    url.pathname = "/";
    window.history.pushState(null, "", url);
    show(url.search);
  }

  function closePanel() {
    navigate(listSearch);
    setListRefresh((value) => value + 1);
  }

  useEffect(() => {
    const onPopState = () => show(window.location.search);
    // The flow views link to each other with ?… links: they open in place, without a reload.
    const onClick = (event: MouseEvent) => {
      const anchor = event.target instanceof Element ? event.target.closest("a") : null;
      const href = anchor?.getAttribute("href");
      if (!anchor || !href?.startsWith("?") || event.defaultPrevented || event.button !== 0 ||
          event.metaKey || event.ctrlKey || event.shiftKey || event.altKey ||
          anchor.target || anchor.hasAttribute("download")) return;
      event.preventDefault();
      navigate(href);
    };
    window.addEventListener("popstate", onPopState);
    document.addEventListener("click", onClick);
    return () => {
      window.removeEventListener("popstate", onPopState);
      document.removeEventListener("click", onClick);
    };
  }, []);

  const { page, panel } = locate(search);

  useEffect(() => {
    if (!panel) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closePanel();
    };
    document.addEventListener("keydown", onKey);
    document.body.classList.add("flow-panel-open");
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.classList.remove("flow-panel-open");
    };
  }, [panel, listSearch]);

  if (error) return <main className="page-shell"><p className="connector-error" role="alert">{error}</p></main>;
  if (user === undefined) return null;
  if (user === null) return <Login onSignedIn={signedIn} notice={notice} />;
  const available = pages.filter(([item]) => item !== "users" || user.admin);
  const shown = available.some(([item]) => item === page) ? page : "flows";

  return (
    <>
      <nav className="mode-switch" aria-label="Workflow">
        {available.map(([item, label]) => (
          <button
            key={item}
            type="button"
            aria-current={shown === item ? "page" : undefined}
            onClick={() => navigate(item === "flows" ? listSearch : `?mode=${item}`)}
          >
            {label}
          </button>
        ))}
        <span className="mode-switch-user">
          {user.login}
          <button type="button" onClick={() => void signOut()}>Sign out</button>
        </span>
      </nav>
      {shown === "flows" && <Flows search={listSearch} refreshKey={listRefresh} onNavigate={navigate} />}
      {shown === "flows" && panel && (
        <div className="flow-panel-backdrop" onClick={(event) => {
          if (event.target === event.currentTarget) closePanel();
        }}>
          <section className="flow-panel" role="dialog" aria-modal="true"
            aria-label={panels.find(([item]) => item === panel)?.[1]}>
            <div className="flow-panel-bar">
              <button type="button" onClick={closePanel}>← All flows</button>
            </div>
            <div key={panelKey}>
              {panel === "chat" && <App />}
              {panel === "requirements" && <Requirements />}
              {panel === "architecture" && <Architecture />}
              {panel === "api-design" && <ApiDesign />}
              {panel === "onboarding" && <Onboarding />}
            </div>
          </section>
        </div>
      )}
      {shown === "github" && <GitHubSettings />}
      {shown === "users" && <Users />}
      {shown === "settings" && <Settings />}
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Shell />
  </React.StrictMode>,
);
