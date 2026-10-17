import ReactDOM from "react-dom/client";
import ApiDesign from "./ApiDesign";
import App from "./App";
import Requirements from "./Requirements";
import Workflows from "./Workflows";
import GitHubSettings from "./GitHubSettings";
import Login, { userRecord, type User } from "./Login";
import Users from "./Users";
import Settings from "./Settings";
import { errorMessage, failure, readBody } from "./api";
import { APP_RETURN_PATH } from "./github";
import "./styles.css";
import React, { useEffect, useState } from "react";

type Mode = "chat" | "requirements" | "api-design" | "workflows" | "github" | "users" | "settings";

const modes: [Mode, string][] = [
  ["chat", "Chat"],
  ["requirements", "Requirements"],
  ["api-design", "API design"],
  ["workflows", "Workflows"],
  ["github", "GitHub"],
  ["settings", "Settings"],
  // Shown to the first user only.
  ["users", "Users"],
];

function modeFromUrl(): Mode {
  if (window.location.pathname === APP_RETURN_PATH) return "github";
  const mode = new URLSearchParams(window.location.search).get("mode");
  return modes.find(([item]) => item === mode)?.[0] ?? "chat";
}

function Shell() {
  const [mode, setMode] = useState<Mode>(modeFromUrl);
  // undefined while the saved session is being checked, null when signed out.
  const [user, setUser] = useState<User | null>();
  const [error, setError] = useState("");

  useEffect(() => {
    if (user !== undefined) return;
    const controller = new AbortController();
    void (async () => {
      try {
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

  // Return to the sign-in form when the token expires instead of failing request by request.
  useEffect(() => {
    if (!user) return;
    const remaining = new Date(user.expires_at).getTime() - Date.now();
    const timer = window.setTimeout(() => setUser(undefined), Math.min(Math.max(remaining, 0), 2 ** 31 - 1));
    return () => window.clearTimeout(timer);
  }, [user]);

  async function signOut() {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      setUser(undefined);
    }
  }

  function switchMode(next: Mode) {
    const url = new URL(window.location.href);
    url.search = next === "chat" ? "" : `?mode=${next}`;
    window.history.replaceState(null, "", url);
    setMode(next);
  }

  if (error) return <main className="page-shell"><p className="connector-error" role="alert">{error}</p></main>;
  if (user === undefined) return null;
  if (user === null) return <Login onSignedIn={setUser} />;
  const available = modes.filter(([item]) => item !== "users" || user.admin);
  const shown = available.some(([item]) => item === mode) ? mode : "chat";

  return (
    <>
      <nav className="mode-switch" aria-label="Workflow">
        {available.map(([item, label]) => (
          <button
            key={item}
            type="button"
            aria-current={shown === item ? "page" : undefined}
            onClick={() => switchMode(item)}
          >
            {label}
          </button>
        ))}
        <span className="mode-switch-user">
          {user.login}
          <button type="button" onClick={() => void signOut()}>Sign out</button>
        </span>
      </nav>
      {shown === "chat" && <App />}
      {shown === "requirements" && <Requirements />}
      {shown === "api-design" && <ApiDesign />}
      {shown === "workflows" && <Workflows />}
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
