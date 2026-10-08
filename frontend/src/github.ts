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

export type RepositoryPublication = { branch: string; commit: string; url: string };

export type Connection = {
  configured: boolean;
  encryption_ready: boolean;
  account: { id: number; login: string } | null;
  validated_at: string | null;
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
  return { branch: value.branch, commit: value.commit, url: value.url };
}

export function connectionRecord(value: unknown): Connection {
  if (!object(value) || typeof value.configured !== "boolean" || typeof value.encryption_ready !== "boolean" ||
      !(value.validated_at === null || typeof value.validated_at === "string") ||
      !(value.account === null || (object(value.account) && Number.isSafeInteger(value.account.id) &&
        typeof value.account.login === "string")) || value.configured !== (value.account !== null)) {
    throw new Error("The backend returned invalid GitHub connection metadata.");
  }
  return value as Connection;
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
