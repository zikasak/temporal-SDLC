import { useEffect, useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";

// admin: the first registered user, who registers the others.
export type User = { login: string; expires_at: string; admin: boolean };

export function userRecord(value: unknown): User {
  if (typeof value !== "object" || value === null || typeof (value as User).login !== "string" ||
      typeof (value as User).expires_at !== "string") {
    throw new Error("The backend returned an invalid sign-in response.");
  }
  return { login: (value as User).login, expires_at: (value as User).expires_at,
    admin: (value as User).admin === true };
}

export default function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [login, setLogin] = useState("");
  const [password, setPassword] = useState("");
  const [repeated, setRepeated] = useState("");
  // undefined while the backend is asked whether the first account still has to be created.
  const [registering, setRegistering] = useState<boolean>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/api/auth/registration", { signal: controller.signal });
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        setRegistering((body as { open?: unknown } | null)?.open === true);
      } catch (caught) {
        if (controller.signal.aborted) return;
        setRegistering(false);
        setError(failure(caught, "Could not check whether an account exists."));
      }
    })();
    return () => controller.abort();
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    if (registering && password !== repeated) {
      setError("The passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const response = await fetch(registering ? "/api/auth/register" : "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ login, password }),
      });
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      onSignedIn(userRecord(body));
    } catch (caught) {
      setPassword("");
      setRepeated("");
      setError(failure(caught, registering ? "Could not create the account." : "Could not sign in."));
    } finally {
      setBusy(false);
    }
  }

  if (registering === undefined) return null;

  return <main className="page-shell">
    <header className="page-heading">
      <h1>{registering ? "Create the first account" : "Sign in"}</h1>
      {registering && <p className="page-intro">No one has an account yet. This one will register everyone else.</p>}
    </header>
    <section className="github-settings" aria-busy={busy}>
      <form className="message-form login-form" onSubmit={submit}>
        <label htmlFor="login-name">Email or username</label>
        <input id="login-name" type="text" value={login} autoComplete="username" autoCapitalize="none"
          spellCheck={false} maxLength={254} autoFocus required disabled={busy}
          onChange={(event) => setLogin(event.target.value)} />
        <label htmlFor="login-password">Password</label>
        <input id="login-password" type="password" value={password}
          autoComplete={registering ? "new-password" : "current-password"} minLength={registering ? 8 : undefined}
          maxLength={1024} required disabled={busy} onChange={(event) => setPassword(event.target.value)} />
        {registering && <>
          <label htmlFor="login-repeat">Repeat password</label>
          <input id="login-repeat" type="password" value={repeated} autoComplete="new-password"
            maxLength={1024} required disabled={busy} onChange={(event) => setRepeated(event.target.value)} />
        </>}
        <button type="submit" className="run-button"
          disabled={busy || !login.trim() || !password || (registering && !repeated)}>
          {registering ? (busy ? "Creating…" : "Create account") : busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {error && <p className="connector-error" role="alert">{error}</p>}
    </section>
  </main>;
}
