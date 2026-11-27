import { userRecord, type User } from "./Login";
import { isObject, readBody } from "./api";

/** Renewal of the sign-in session: the access token (a cookie) is short-lived and renewed
 * with the refresh token (another cookie, sent only to POST /api/auth/refresh). At most one
 * refresh runs at a time across all tabs; the others wait for it and use its result. */

export type RefreshOutcome = { user: User } | { rejected: string };

/** Messages between the tabs of the app. */
export type SessionMessage =
  | { type: "refreshed"; user: User }
  | { type: "signed-out" }
  | { type: "expired"; reason: string };

const LOCK = "temporal-sdlc-auth-refresh";
const CHANNEL = "temporal-sdlc-auth-session";
// The latest refresh of any tab: { user, at }.
const LATEST = "temporal-sdlc-auth-refreshed";
// A refresh another tab made is reused while its token has at least this long left.
const REUSABLE_MS = 30_000;
// Not renewed: sign-in itself and the endpoints that need no session.
const UNRENEWED = new Set(["/api/auth/login", "/api/auth/register", "/api/auth/refresh",
  "/api/auth/logout", "/api/auth/registration"]);

const channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(CHANNEL);
let pending: Promise<RefreshOutcome> | null = null;

export function broadcast(message: SessionMessage) {
  channel?.postMessage(message);
}

/** Listen to the other tabs' session changes; returns the function that stops listening. */
export function onSessionMessage(listener: (message: SessionMessage) => void) {
  if (!channel) return () => undefined;
  const handle = (event: MessageEvent<unknown>) => {
    const data = event.data;
    if (!isObject(data) || typeof data.type !== "string") return;
    if (data.type === "refreshed") {
      try {
        listener({ type: "refreshed", user: userRecord(data.user) });
      } catch {
        // A malformed message is ignored.
      }
    } else if (data.type === "signed-out") {
      listener({ type: "signed-out" });
    } else if (data.type === "expired" && typeof data.reason === "string") {
      listener({ type: "expired", reason: data.reason });
    }
  };
  channel.addEventListener("message", handle);
  return () => channel.removeEventListener("message", handle);
}

function latestRefresh(): { user: User; at: number } | null {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(LATEST) ?? "null");
    if (!isObject(value) || typeof value.at !== "number") return null;
    return { user: userRecord(value.user), at: value.at };
  } catch {
    return null;
  }
}

function recordRefresh(user: User) {
  try {
    window.localStorage.setItem(LATEST, JSON.stringify({ user, at: Date.now() }));
  } catch {
    // Without storage, other tabs refresh on their own.
  }
}

async function exclusively<T>(work: () => Promise<T>): Promise<T> {
  const locks = typeof navigator === "undefined" ? undefined : navigator.locks;
  if (!locks) return work();
  return locks.request(LOCK, work);
}

async function refreshOnce(requested: number): Promise<RefreshOutcome> {
  return exclusively(async () => {
    // Another tab refreshed while this one waited: its cookies are this tab's too.
    const latest = latestRefresh();
    if (latest && latest.at >= requested &&
        new Date(latest.user.expires_at).getTime() - Date.now() > REUSABLE_MS) {
      return { user: latest.user };
    }
    const response = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const body = await readBody(response);
    if (response.status === 401) {
      const reason = isObject(body) && typeof body.reason === "string" ? body.reason : "invalid";
      return { rejected: reason };
    }
    if (!response.ok) throw new Error(`The backend returned HTTP ${response.status}.`);
    const user = userRecord(body);
    recordRefresh(user);
    broadcast({ type: "refreshed", user });
    return { user };
  });
}

/** Renew the access token; concurrent calls share one refresh. Throws when the backend
 * cannot be reached or fails, and resolves with the rejection reason when the session can
 * no longer be renewed. */
export function refreshSession(): Promise<RefreshOutcome> {
  if (!pending) {
    pending = refreshOnce(Date.now()).finally(() => {
      pending = null;
    });
  }
  return pending;
}

function renewable(input: RequestInfo | URL): boolean {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  const url = new URL(raw, window.location.href);
  return url.origin === window.location.origin && url.pathname.startsWith("/api/") &&
    !UNRENEWED.has(url.pathname);
}

let installed = false;

/** Make every API request of the app refresh the session once and retry when it answers
 * 401. onExpired is called with the reason when the session cannot be renewed. */
export function installSessionRenewal(onExpired: (reason: string) => void) {
  if (installed) return;
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    if (!renewable(input)) return original(input, init);
    // A Request's body can be read once: the retry sends a copy.
    const retry = input instanceof Request ? input.clone() : input;
    const response = await original(input, init);
    if (response.status !== 401) return response;
    let outcome: RefreshOutcome;
    try {
      outcome = await refreshSession();
    } catch {
      return response;
    }
    if ("user" in outcome) return original(retry, init);
    onExpired(outcome.rejected);
    return response;
  };
}
