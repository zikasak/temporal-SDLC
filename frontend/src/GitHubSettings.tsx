import { useEffect, useRef, useState, type FormEvent } from "react";
import { failure } from "./api";
import { connectionRecord, githubRequest, type Connection } from "./github";

export default function GitHubSettings() {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"connect" | "check" | "disconnect" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const operation = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    githubRequest("/connection", { signal: controller.signal })
      .then((body) => { if (!controller.signal.aborted) setConnection(connectionRecord(body)); })
      .catch((caught) => { if (!controller.signal.aborted) setError(failure(caught, "Could not load GitHub settings.")); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); operation.current?.abort(); };
  }, [refresh]);

  async function mutate(action: "connect" | "check" | "disconnect") {
    if (action === "disconnect" && !window.confirm("Disconnect GitHub? Saved sessions keep their repository selection, but new repository choices will need a new connection.")) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(action);
    setError("");
    setNotice("");
    const submittedToken = token;
    setToken("");
    try {
      const body = await githubRequest(action === "check" ? "/connection/check" : "/connection", {
        method: action === "connect" ? "PUT" : action === "check" ? "POST" : "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "connect" ? { token: submittedToken } : {}),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (action === "disconnect") setConnection({ configured: false,
        encryption_ready: connection?.encryption_ready ?? false, account: null, validated_at: null });
      else setConnection(connectionRecord(body));
      setNotice(action === "disconnect" ? "GitHub disconnected. Existing session selections are unchanged." :
        action === "check" ? "The saved token is valid." : "GitHub connected. Choose a repository when starting a session.");
    } catch (caught) {
      if (!controller.signal.aborted) setError(failure(caught, "Could not update the GitHub connection."));
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  }

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (token.trim() && !busy) void mutate("connect");
  }

  return <main className="page-shell">
    <header className="page-heading">
      <h1>GitHub connection</h1>
      <p className="page-intro">Connect once, then choose a repository and branch for each session. Derived sessions inherit that choice.</p>
    </header>
    <section className="github-settings" aria-labelledby="github-connection-title" aria-busy={loading || Boolean(busy)}>
      <div className="github-connection-header">
        <h2 id="github-connection-title">{connection?.configured ? "Saved connection" : "Connect GitHub"}</h2>
        <button type="button" className="workflows-refresh" disabled={loading || Boolean(busy)} onClick={() => setRefresh((key) => key + 1)}>Refresh</button>
      </div>
      {loading && <p className="field-hint" role="status">Loading connection…</p>}
      {connection?.account && <div className="github-account">
        <strong>{connection.account.login}</strong>
        <p>Last validated {connection.validated_at ? new Date(connection.validated_at).toLocaleString() : "not yet"}. This is a saved configuration, not a continuous access check.</p>
        <div className="repository-actions">
          <button type="button" className="workflows-refresh" disabled={Boolean(busy) || !connection.encryption_ready}
            onClick={() => void mutate("check")}>{busy === "check" ? "Checking…" : "Check connection"}</button>
          <button type="button" className="workflows-refresh disconnect-button" disabled={Boolean(busy)}
            onClick={() => void mutate("disconnect")}>{busy === "disconnect" ? "Disconnecting…" : "Disconnect"}</button>
        </div>
      </div>}
      {connection && !connection.encryption_ready && <p className="connector-error" role="alert">
        Set a valid <code>GITHUB_TOKEN_ENCRYPTION_KEY</code> in the backend environment and restart the API. The token cannot be saved or used until encryption is configured.
      </p>}
      <form className="message-form github-token-form" onSubmit={connect}>
        <label htmlFor="github-token">{connection?.configured ? "Replace personal access token" : "Personal access token"}</label>
        <input id="github-token" type="password" value={token} autoComplete="off" spellCheck={false} maxLength={512}
          aria-describedby="github-token-help" disabled={loading || Boolean(busy) || !connection?.encryption_ready}
          onChange={(event) => setToken(event.target.value)} />
        <p id="github-token-help" className="field-hint">Use a fine-grained token limited to the repositories you need, with Contents read and write permission: sessions push their verified code to a new branch. Organization approval or SSO may also be required. The saved token is encrypted and never returned to this browser.</p>
        <a className="github-token-link" href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noreferrer">Create a fine-grained token on GitHub</a>
        <button type="submit" className="run-button" disabled={loading || Boolean(busy) || !token.trim() || !connection?.encryption_ready}>
          {busy === "connect" ? "Connecting…" : connection?.configured ? "Replace token" : "Connect GitHub"}
        </button>
      </form>
      {error && <p className="connector-error" role="alert">{error}</p>}
      {notice && <p className="connector-notice" role="status">{notice}</p>}
      <p className="github-boundary">Shared connection for this app instance. For trusted local/internal use only. This step saves session selections; it does not clone, edit, or push repository code.</p>
    </section>
  </main>;
}
