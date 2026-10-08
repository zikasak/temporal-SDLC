import { useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";

export type User = { login: string; expires_at: string };

export function userRecord(value: unknown): User {
  if (typeof value !== "object" || value === null || typeof (value as User).login !== "string" ||
      typeof (value as User).expires_at !== "string") {
    throw new Error("The backend returned an invalid sign-in response.");
  }
  return { login: (value as User).login, expires_at: (value as User).expires_at };
}

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      onSignedIn(userRecord(body));
    } catch (caught) {
      setPassword("");
      setError(failure(caught, "Could not sign in."));
    } finally {
      setBusy(false);
    }
  }

  return <main className="page-shell">
    <header className="page-heading">
      <h1>Sign in</h1>
    </header>
    <section className="github-settings" aria-busy={busy}>
      <form className="message-form login-form" onSubmit={submit}>
        <label htmlFor="login-name">Email or username</label>
        <input id="login-name" type="text" value={login} autoComplete="username" autoCapitalize="none"
          spellCheck={false} maxLength={254} autoFocus required disabled={busy}
          onChange={(event) => setLogin(event.target.value)} />
        <label htmlFor="login-password">Password</label>
        <input id="login-password" type="password" value={password} autoComplete="current-password"
          maxLength={1024} required disabled={busy} onChange={(event) => setPassword(event.target.value)} />
        <button type="submit" className="run-button" disabled={busy || !login.trim() || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {error && <p className="connector-error" role="alert">{error}</p>}
    </section>
  </main>;
}
