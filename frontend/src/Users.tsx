import { useEffect, useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";

type Account = { login: string; created_at: string };

function accountRecord(value: unknown): Account {
  if (typeof value !== "object" || value === null || typeof (value as Account).login !== "string" ||
      typeof (value as Account).created_at !== "string") {
    throw new Error("The backend returned an invalid account.");
  }
  return { login: (value as Account).login, created_at: (value as Account).created_at };
}

async function usersRequest(init?: RequestInit) {
  const response = await fetch("/api/auth/users", init);
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return body;
}

export default function Users() {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [repeated, setRepeated] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    usersRequest({ signal: controller.signal })
      .then((body) => {
        if (!Array.isArray(body)) throw new Error("The backend returned an invalid account list.");
        setAccounts(body.map(accountRecord));
      })
      .catch((caught) => { if (!controller.signal.aborted) setError(failure(caught, "Could not load the accounts.")); });
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setNotice("");
    if (password !== repeated) {
      setError("The passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const created = accountRecord(await usersRequest({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      }));
      setAccounts((current) => [...(current ?? []), created].sort((a, b) => a.login.localeCompare(b.login)));
      setLogin("");
      setNotice(`${created.login} can now sign in. Share the password with them yourself.`);
    } catch (caught) {
      setError(failure(caught, "Could not create the account."));
    } finally {
      setPassword("");
      setRepeated("");
      setBusy(false);
    }
  }

  return <main className="page-shell">
    <header className="page-heading">
      <h1>Users</h1>
      <p className="page-intro">Only you, as the first user, can register accounts. Everyone signed in shares the same sessions and data.</p>
    </header>
    <section className="github-settings" aria-labelledby="users-title" aria-busy={busy || !accounts}>
      <h2 id="users-title">Register a user</h2>
      <form className="message-form login-form" onSubmit={submit}>
        <label htmlFor="user-login">Email or username</label>
        <input id="user-login" type="text" value={login} autoComplete="off" autoCapitalize="none"
          spellCheck={false} maxLength={254} required disabled={busy}
          onChange={(event) => setLogin(event.target.value)} />
        <label htmlFor="user-password">Password</label>
        <input id="user-password" type="password" value={password} autoComplete="new-password" minLength={8}
          maxLength={1024} required disabled={busy} onChange={(event) => setPassword(event.target.value)} />
        <label htmlFor="user-repeat">Repeat password</label>
        <input id="user-repeat" type="password" value={repeated} autoComplete="new-password"
          maxLength={1024} required disabled={busy} onChange={(event) => setRepeated(event.target.value)} />
        <button type="submit" className="run-button" disabled={busy || !login.trim() || !password || !repeated}>
          {busy ? "Registering…" : "Register user"}
        </button>
      </form>
      {error && <p className="connector-error" role="alert">{error}</p>}
      {notice && <p className="connector-notice" role="status">{notice}</p>}
      {accounts && <ul className="user-list" aria-label="Registered users">
        {accounts.map((account) => <li key={account.login}>
          <strong>{account.login}</strong>
          <span>since {new Date(account.created_at).toLocaleDateString()}</span>
        </li>)}
      </ul>}
    </section>
  </main>;
}
