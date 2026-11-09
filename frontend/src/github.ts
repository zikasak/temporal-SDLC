import { errorMessage, readBody } from "./api";

export type Repository = {
  repository_id: number;
  full_name: string;
  html_url: string;
  private: boolean;
  default_branch: string;
};

export type RepositorySelection = Omit<Repository, "default_branch"> & {
  provider: "github";
  branch: string;
};

export type RepositoryCheckout = { commit: string };

export type RepositoryPublication = {
  branch: string; commit: string; url: string; ignored: string[]; ignored_count: number;
  // False when the sources were pushed without a check of them.
  verified: boolean;
};

export type Connection = {
  configured: boolean;
  encryption_ready: boolean;
  app: { id: number; slug: string } | null;
  // Null while the app is created but not installed yet.
  installation: { id: number; account: string } | null;
  validated_at: string | null;
  // Whether webhook deliveries can be verified, and so mentions handled.
  webhook_secret_set: boolean;
  // The app misses a comment event or the installation the pull_requests: write permission.
  webhook_events_missing: boolean;
};

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function repositoryFields(value: unknown): value is Omit<Repository, "default_branch"> {
  return object(value) && Number.isSafeInteger(value.repository_id) && Number(value.repository_id) > 0 &&
    typeof value.full_name === "string" && /^[A-Za-z0-9-]+\/[A-Za-z0-9_.-]+$/.test(value.full_name) &&
    value.html_url === `https://github.com/${value.full_name}` && typeof value.private === "boolean";
}

export function repositorySelection(value: unknown): RepositorySelection | null {
  if (value == null) return null;
  if (!repositoryFields(value) || !("provider" in value) || value.provider !== "github" ||
      !("branch" in value) || typeof value.branch !== "string" || !value.branch) {
    throw new Error("The backend returned invalid repository metadata.");
  }
  return value as RepositorySelection;
}

export function repositoryCheckout(value: unknown): RepositoryCheckout | null {
  return object(value) && typeof value.commit === "string" ? { commit: value.commit } : null;
}

export function repositoryPublication(value: unknown, repository: RepositorySelection | null): RepositoryPublication | null {
  if (!object(value) || !repository) return null;
  // Only ever link to the session's own repository.
  if (typeof value.branch !== "string" || typeof value.commit !== "string" ||
      value.url !== `${repository.html_url}/tree/${value.branch}`) {
    throw new Error("The backend returned invalid repository publication metadata.");
  }
  const ignored = Array.isArray(value.ignored)
    ? value.ignored.filter((item): item is string => typeof item === "string") : [];
  return { branch: value.branch, commit: value.commit, url: value.url, ignored,
    ignored_count: typeof value.ignored_count === "number" ? value.ignored_count : ignored.length,
    verified: value.verified !== false };
}

export function connectionRecord(value: unknown): Connection {
  if (!object(value) || typeof value.configured !== "boolean" || typeof value.encryption_ready !== "boolean" ||
      !(value.validated_at === null || typeof value.validated_at === "string") ||
      !(value.app === null || (object(value.app) && Number.isSafeInteger(value.app.id) &&
        typeof value.app.slug === "string")) ||
      !(value.installation === null || (object(value.installation) &&
        Number.isSafeInteger(value.installation.id) && typeof value.installation.account === "string")) ||
      value.configured !== (value.app !== null) || (!value.configured && value.installation !== null)) {
    throw new Error("The backend returned invalid GitHub connection metadata.");
  }
  return { ...value, webhook_secret_set: value.webhook_secret_set === true,
    webhook_events_missing: value.webhook_events_missing === true } as Connection;
}

function nextPage(value: unknown): number | null {
  if (value === null || (Number.isSafeInteger(value) && Number(value) > 0)) return value as number | null;
  throw new Error("The backend returned invalid pagination.");
}

export function repositoryPage(value: unknown): { repositories: Repository[]; next_page: number | null } {
  if (!object(value) || !Array.isArray(value.repositories) || !value.repositories.every(
    (item) => repositoryFields(item) && "default_branch" in item && typeof item.default_branch === "string",
  )) throw new Error("The backend returned an invalid repository list.");
  return { repositories: value.repositories as Repository[], next_page: nextPage(value.next_page) };
}

export function branchPage(value: unknown): { branches: string[]; next_page: number | null } {
  if (!object(value) || !Array.isArray(value.branches) || !value.branches.every(
    (item) => typeof item === "string" && item.length > 0,
  )) throw new Error("The backend returned an invalid branch list.");
  return { branches: value.branches as string[], next_page: nextPage(value.next_page) };
}

export async function githubRequest(path: string, init?: RequestInit): Promise<unknown> {
  const response = await fetch(`/api/connectors/github${path}`, init);
  const body = await readBody(response);
  if (!response.ok) throw new Error(errorMessage(body, response.status));
  return body;
}

export function selectionInput(repository: RepositorySelection) {
  return { full_name: repository.full_name, branch: repository.branch };
}

// Where GitHub sends the browser back after creating or installing the app.
export const APP_RETURN_PATH = "/github-app";
const APP_STATE_KEY = "github-app-state";

export function installUrl(slug: string): string {
  return `https://github.com/apps/${encodeURIComponent(slug)}/installations/new`;
}

/** Leave for GitHub's confirmation page of an app described by this instance's manifest. */
export function createGitHubApp(name: string, organization: string) {
  const state = crypto.randomUUID();
  window.sessionStorage.setItem(APP_STATE_KEY, state);
  const origin = window.location.origin;
  const manifest = {
    name, url: origin, public: false,
    redirect_url: origin + APP_RETURN_PATH, setup_url: origin + APP_RETURN_PATH,
    // Mentions of the app in pull request comments arrive here, signed with the webhook
    // secret GitHub returns with the created app.
    hook_attributes: { url: `${origin}/api/connectors/github/events`, active: true },
    default_permissions: { contents: "write", metadata: "read", pull_requests: "write", issues: "read" },
    // Pull request events tell the app when a delivery's pull request is merged or closed.
    default_events: ["issue_comment", "pull_request_review_comment", "pull_request"],
  };
  const form = document.createElement("form");
  form.method = "post";
  form.action = `https://github.com/${organization ? `organizations/${encodeURIComponent(organization)}/` : ""}settings/apps/new?state=${state}`;
  const input = document.createElement("input");
  input.type = "hidden";
  input.name = "manifest";
  input.value = JSON.stringify(manifest);
  form.append(input);
  document.body.append(form);
  form.submit();
}

export type AppReturn = { code: string } | { installed: true } | { error: string };

/** What GitHub returned to this page, once: the address is cleaned as it is read. */
export function takeAppReturn(): AppReturn | null {
  if (window.location.pathname !== APP_RETURN_PATH) return null;
  const params = new URLSearchParams(window.location.search);
  window.history.replaceState(null, "", "/?mode=github");
  const code = params.get("code");
  if (!code) return { installed: true };
  const expected = window.sessionStorage.getItem(APP_STATE_KEY);
  window.sessionStorage.removeItem(APP_STATE_KEY);
  // The code of an app someone else created must not be connected here.
  return expected && params.get("state") === expected ? { code }
    : { error: "This GitHub App was not created from this browser tab. Create it again." };
}
