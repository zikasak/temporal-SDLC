import { useEffect, useId, useRef, useState } from "react";
import { failure } from "./api";
import { branchPage, githubRequest, repositoryPage, type Repository, type RepositoryCheckout, type RepositorySelection } from "./github";

export function RepositorySummary({ repository, checkout = null, onUpdate }: {
  repository: RepositorySelection | null;
  checkout?: RepositoryCheckout | null;
  // Checks the branch out again at its latest commit; offered only while it may be.
  onUpdate?: () => void;
}) {
  if (!repository) return null;
  return <p className="repository-summary">
    <a href={repository.html_url} target="_blank" rel="noreferrer">{repository.full_name}</a>
    <span className="repository-branch">{repository.branch}</span>
    <small>Fixed for this session · inherited by derived sessions</small>
    {checkout && <small>
      Checked out at <code>{checkout.commit.slice(0, 7)}</code>
      {onUpdate && <button type="button" onClick={onUpdate}
        title={`Check ${repository.branch} out again at its latest commit`}>
        Update to latest commit
      </button>}
    </small>}
  </p>;
}

export default function RepositoryPicker({ value, onChange, disabled = false, required = false }: {
  value: RepositorySelection | null;
  onChange: (value: RepositorySelection | null) => void;
  disabled?: boolean;
  // Chat may run without a repository; every other session works inside one.
  required?: boolean;
}) {
  const id = useId();
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [branches, setBranches] = useState<string[]>([]);
  const [nextRepoPage, setNextRepoPage] = useState<number | null>(null);
  const [nextBranchPage, setNextBranchPage] = useState<number | null>(null);
  const [loadingRepos, setLoadingRepos] = useState(false);
  const [loadingBranches, setLoadingBranches] = useState(false);
  const [repoError, setRepoError] = useState("");
  const [branchError, setBranchError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const repoRequest = useRef<AbortController | null>(null);
  const branchRequest = useRef<AbortController | null>(null);
  // Use current draft state when an async page arrives, not an earlier closure.
  const latest = useRef({ value, onChange });
  latest.current = { value, onChange };

  async function loadRepositories(page: number, append: boolean) {
    repoRequest.current?.abort();
    const controller = new AbortController();
    repoRequest.current = controller;
    setLoadingRepos(true);
    setRepoError("");
    try {
      const result = repositoryPage(await githubRequest(`/repositories?page=${page}`, { signal: controller.signal }));
      if (controller.signal.aborted) return;
      setRepositories((current) => [...new Map(
        [...(append ? current : []), ...result.repositories].map((item) => [item.repository_id, item]),
      ).values()]);
      setNextRepoPage(result.next_page);
    } catch (caught) {
      if (!controller.signal.aborted) {
        setRepoError(failure(caught, "Could not load repositories."));
        if (!append) { setRepositories([]); setNextRepoPage(null); }
      }
    } finally {
      if (!controller.signal.aborted) setLoadingRepos(false);
    }
  }

  async function loadBranches(fullName: string, page: number, append: boolean) {
    branchRequest.current?.abort();
    const controller = new AbortController();
    branchRequest.current = controller;
    setLoadingBranches(true);
    setBranchError("");
    const path = fullName.split("/").map(encodeURIComponent).join("/");
    try {
      const result = branchPage(await githubRequest(`/repositories/${path}/branches?page=${page}`, { signal: controller.signal }));
      if (controller.signal.aborted || latest.current.value?.full_name !== fullName) return;
      setBranches((current) => [...new Set([...(append ? current : []), ...result.branches])]);
      setNextBranchPage(result.next_page);
      const draft = latest.current.value;
      if (draft && !append && !draft.branch && result.branches.length) {
        const repository = repositories.find((item) => item.full_name === fullName);
        if (repository?.default_branch) latest.current.onChange({ ...draft, branch: repository.default_branch });
      }
    } catch (caught) {
      if (!controller.signal.aborted) setBranchError(failure(caught, "Could not load branches."));
    } finally {
      if (!controller.signal.aborted) setLoadingBranches(false);
    }
  }

  useEffect(() => {
    void loadRepositories(1, false);
    return () => repoRequest.current?.abort();
  }, [refresh]);

  useEffect(() => {
    setBranches([]);
    setNextBranchPage(null);
    setBranchError("");
    if (value) void loadBranches(value.full_name, 1, false);
    else setLoadingBranches(false);
    return () => branchRequest.current?.abort();
  }, [value?.full_name, refresh]);

  function chooseRepository(fullName: string) {
    branchRequest.current?.abort();
    const selected = repositories.find((item) => item.full_name === fullName);
    onChange(selected ? { provider: "github", repository_id: selected.repository_id,
      full_name: selected.full_name, html_url: selected.html_url, private: selected.private, branch: "" } : null);
  }

  const branchOptions = [...new Set([...(value?.branch ? [value.branch] : []), ...branches])];
  return <fieldset className="repository-picker" disabled={disabled}>
    <legend>Repository <span>({required ? "required" : "optional"})</span></legend>
    <p className="field-hint">{required
      ? "The session checks this branch out before the LLM starts, works inside it, and pushes verified code to a new branch."
      : "Choose now; this selection stays with the session. Chat does not check out its code."}</p>
    <label htmlFor={`${id}-repository`}>GitHub repository</label>
    <select id={`${id}-repository`} value={value?.full_name ?? ""} required={required}
      onChange={(event) => chooseRepository(event.target.value)}>
      <option value="">{required ? "Choose a repository" : "No repository"}</option>
      {value && !repositories.some((item) => item.full_name === value.full_name) &&
        <option value={value.full_name}>{value.full_name}</option>}
      {repositories.map((item) => <option key={item.repository_id} value={item.full_name}>
        {item.full_name}{item.private ? " · private" : ""}{item.default_branch ? ` · ${item.default_branch}` : ""}
      </option>)}
    </select>
    <div className="repository-actions">
      <button type="button" className="workflows-refresh" onClick={() => {
        if (value) onChange({ ...value, branch: "" });
        setRefresh((key) => key + 1);
      }} disabled={loadingRepos || loadingBranches}>Refresh</button>
      {nextRepoPage && <button type="button" className="workflows-refresh" disabled={loadingRepos}
        onClick={() => void loadRepositories(nextRepoPage, true)}>Load more repositories</button>}
    </div>
    {loadingRepos && <p className="field-hint" role="status">Loading repositories…</p>}
    {!loadingRepos && !repoError && !repositories.length && <p className="field-hint">No repositories are available to this token.</p>}
    {repoError && <p className="connector-error" role="alert">{repoError} <a href="?mode=github">GitHub settings</a>.{!required && " You can continue without a repository."}</p>}
    {value && <>
      <label htmlFor={`${id}-branch`}>Branch</label>
      <select id={`${id}-branch`} value={value.branch} disabled={loadingBranches && !branches.length}
        onChange={(event) => onChange({ ...value, branch: event.target.value })}>
        <option value="">Choose a branch</option>
        {branchOptions.map((branch) => <option key={branch} value={branch}>{branch}</option>)}
      </select>
      {loadingBranches && <p className="field-hint" role="status">Loading branches…</p>}
      {!loadingBranches && !branchError && !branches.length && <p className="field-hint">This repository has no branches. Choose another repository{!required && " or continue without one"}.</p>}
      {branchError && <p className="connector-error" role="alert">{branchError} Use Refresh to try again{!required && ", or choose No repository"}.</p>}
      {nextBranchPage && <button type="button" className="workflows-refresh" disabled={loadingBranches}
        onClick={() => void loadBranches(value.full_name, nextBranchPage, true)}>Load more branches</button>}
    </>}
  </fieldset>;
}
