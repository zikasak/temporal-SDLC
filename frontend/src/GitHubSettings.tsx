import { useEffect, useRef, useState, type FormEvent } from "react";
import { failure } from "./api";
import { connectionRecord, createGitHubApp, githubRequest, installUrl, takeAppReturn, type Connection } from "./github";

export default function GitHubSettings() {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [appId, setAppId] = useState("");
  const [account, setAccount] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const keyInput = useRef<HTMLInputElement>(null);
  const [appName, setAppName] = useState(() => `service-generator-${crypto.randomUUID().slice(0, 4)}`);
  const [organization, setOrganization] = useState("");
  const returned = useRef(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<"connect" | "check" | "disconnect" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const operation = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    githubRequest("/connection", { signal: controller.signal })
      .then((body) => { if (!controller.signal.aborted) setConnection(connectionRecord(body)); })
      .catch((caught) => { if (!controller.signal.aborted) setError(failure(caught, "Could not load GitHub settings.")); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); operation.current?.abort(); };
  }, [refresh]);

  // Back from GitHub: save the app it created, or pick up its installation.
  useEffect(() => {
    if (returned.current) return;
    returned.current = true;
    const back = takeAppReturn();
    if (!back) return;
    if ("error" in back) {
      setError(back.error);
      return;
    }
    const created = "code" in back;
    setBusy(created ? "connect" : "check");
    githubRequest(created ? "/connection/manifest" : "/connection/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(created ? { code: back.code } : {}),
    }).then(() => {
      setNotice(created ? "The GitHub App is created. Install it to finish connecting."
        : "GitHub connected. Choose a repository when starting a session.");
      setRefresh((key) => key + 1);
    }).catch((caught) => setError(failure(caught, "Could not update the GitHub connection.")))
      .finally(() => setBusy(null));
  }, []);

  async function mutate(action: "connect" | "check" | "disconnect") {
    if (action === "disconnect" && !window.confirm("Disconnect GitHub? Saved sessions keep their repository selection, but new repository choices will need a new connection.")) return;
    const controller = new AbortController();
    operation.current = controller;
    setBusy(action);
    setError("");
    setNotice("");
    const credentials = { app_id: Number(appId), private_key: privateKey,
      ...(account.trim() ? { account: account.trim() } : {}) };
    if (action === "connect") {
      setPrivateKey("");
      if (keyInput.current) keyInput.current.value = "";
    }
    try {
      const body = await githubRequest(action === "check" ? "/connection/check" : "/connection", {
        method: action === "connect" ? "PUT" : action === "check" ? "POST" : "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "connect" ? credentials : {}),
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (action === "disconnect") setConnection({ configured: false,
        encryption_ready: connection?.encryption_ready ?? false, app: null, installation: null, validated_at: null });
      else setConnection(connectionRecord(body));
      setNotice(action === "disconnect" ? "GitHub disconnected. Existing session selections are unchanged." :
        action === "check" ? "The app is installed and its key is valid." : "GitHub connected. Choose a repository when starting a session.");
    } catch (caught) {
      if (!controller.signal.aborted) setError(failure(caught, "Could not update the GitHub connection."));
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  }

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (ready && !busy) void mutate("connect");
  }

  async function chooseKey(file: File | undefined) {
    setError("");
    try {
      setPrivateKey(file ? await file.text() : "");
    } catch {
      setPrivateKey("");
      setError("Could not read the private key file.");
    }
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (connection?.configured && !window.confirm("Create another GitHub App? It replaces the saved one here once created; the old app stays on GitHub until you delete it there.")) return;
    createGitHubApp(appName.trim(), organization.trim());
  }

  const ready = /^[1-9]\d{0,15}$/.test(appId) && Boolean(privateKey.trim());
  const disabled = loading || Boolean(busy) || !connection?.encryption_ready;

  return <main className="page-shell">
    <header className="page-heading">
      <h1>GitHub connection</h1>
      <p className="page-intro">Connect once, then choose a repository and branch for each session. Derived sessions inherit that choice.</p>
    </header>
    <section className="github-settings" aria-labelledby="github-connection-title" aria-busy={loading || Boolean(busy)}>
      <div className="github-connection-header">
        <h2 id="github-connection-title">{connection?.configured ? "Saved connection" : "Connect GitHub"}</h2>
        <button type="button" className="workflows-refresh" disabled={loading || Boolean(busy)} onClick={() => { setError(""); setRefresh((key) => key + 1); }}>Refresh</button>
      </div>
      {loading && <p className="field-hint" role="status">Loading connection…</p>}
      {connection?.app && <div className="github-account">
        <strong>{connection.app.slug} {connection.installation ? `on ${connection.installation.account}` : "is not installed yet"}</strong>
        {connection.installation
          ? <p>Last validated {connection.validated_at ? new Date(connection.validated_at).toLocaleString() : "not yet"}. This is a saved configuration, not a continuous access check.</p>
          : <p>Install it on the repositories sessions may use. GitHub returns you here; otherwise check the connection afterwards.</p>}
        <a className="github-token-link" href={installUrl(connection.app.slug)}>
          {connection.installation ? "Change its repositories on GitHub" : "Install the app on GitHub"}</a>
        <div className="repository-actions">
          <button type="button" className="workflows-refresh" disabled={Boolean(busy) || !connection.encryption_ready}
            onClick={() => void mutate("check")}>{busy === "check" ? "Checking…" : "Check connection"}</button>
          <button type="button" className="workflows-refresh disconnect-button" disabled={Boolean(busy)}
            onClick={() => void mutate("disconnect")}>{busy === "disconnect" ? "Disconnecting…" : "Disconnect"}</button>
        </div>
      </div>}
      {connection && !connection.encryption_ready && <p className="connector-error" role="alert">
        Set a valid <code>GITHUB_TOKEN_ENCRYPTION_KEY</code> in the backend environment and restart the API. The private key cannot be saved or used until encryption is configured.
      </p>}
      <form className="message-form github-token-form" onSubmit={create}>
        <label htmlFor="github-app-name">{connection?.configured ? "Replace with a new app named" : "Name of the new GitHub App"}</label>
        <input id="github-app-name" type="text" value={appName} autoComplete="off" spellCheck={false} maxLength={34}
          aria-describedby="github-create-help" disabled={disabled} onChange={(event) => setAppName(event.target.value)} />
        <label htmlFor="github-app-organization">Organization (leave empty for your own account)</label>
        <input id="github-app-organization" type="text" value={organization} autoComplete="off" spellCheck={false}
          maxLength={39} disabled={disabled} onChange={(event) => setOrganization(event.target.value)} />
        <p id="github-create-help" className="field-hint">GitHub asks you to confirm a private app with Contents read and write permission (sessions push their verified code to a new branch) and no webhook, then returns here with its key. Install it next on the repositories you need. The key is saved encrypted and never returned to this browser.</p>
        <button type="submit" className="run-button" disabled={disabled || !appName.trim()}>Create GitHub App</button>
      </form>
      <details className="github-existing-app">
      <summary>Use an existing GitHub App</summary>
      <form className="message-form github-token-form" onSubmit={connect}>
        <label htmlFor="github-app-id">App ID</label>
        <input id="github-app-id" type="text" inputMode="numeric" value={appId} autoComplete="off" spellCheck={false}
          maxLength={16} disabled={disabled} onChange={(event) => setAppId(event.target.value.trim())} />
        <label htmlFor="github-app-key">Private key (.pem)</label>
        <input id="github-app-key" ref={keyInput} type="file" accept=".pem,application/x-pem-file"
          aria-describedby="github-token-help" disabled={disabled}
          onChange={(event) => void chooseKey(event.target.files?.[0])} />
        <label htmlFor="github-app-account">Installed on (only if the app is installed on several accounts)</label>
        <input id="github-app-account" type="text" value={account} autoComplete="off" spellCheck={false}
          maxLength={39} placeholder="user or organization" disabled={disabled}
          onChange={(event) => setAccount(event.target.value)} />
        <p id="github-token-help" className="field-hint">The app needs Contents read and write permission, a generated private key and an installation on the repositories you need. No webhook or callback URL is required.</p>
        <button type="submit" className="run-button" disabled={disabled || !ready}>
          {busy === "connect" ? "Connecting…" : connection?.configured ? "Replace app" : "Connect GitHub"}
        </button>
      </form>
      </details>
      {error && <p className="connector-error" role="alert">{error}</p>}
      {notice && <p className="connector-notice" role="status">{notice}</p>}
      <p className="github-boundary">Shared connection for this app instance. Disconnecting removes the saved key here; the app itself stays on GitHub until you delete it there.</p>
    </section>
  </main>;
}
