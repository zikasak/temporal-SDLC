import { useEffect, useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";

type Adapter = "pi" | "openai" | "claude";
type ModelSettings = { adapter: Adapter; model: string; thinking_level: string };
type Workflows = Record<string, ModelSettings>;
type SettingsResponse = {
  workflows: Workflows;
  workflow_labels: Record<string, string>;
  thinking_levels: Record<Adapter, string[]>;
};

const adapters: [Adapter, string][] = [["pi", "Pi"], ["openai", "OpenAI"], ["claude", "Claude"]];

function settingsResponse(value: unknown): SettingsResponse {
  if (typeof value !== "object" || value === null) throw new Error("Invalid settings response.");
  const data = value as SettingsResponse;
  if (!data.workflows || !data.workflow_labels || !data.thinking_levels ||
      !adapters.every(([adapter]) => Array.isArray(data.thinking_levels[adapter])) ||
      Object.keys(data.workflows).length !== Object.keys(data.workflow_labels).length ||
      !Object.entries(data.workflows).length ||
      !Object.entries(data.workflows).every(([key, item]) =>
        typeof data.workflow_labels[key] === "string" && item &&
        adapters.some(([adapter]) => adapter === item.adapter) &&
        typeof item.model === "string" &&
        data.thinking_levels[item.adapter].includes(item.thinking_level))) {
    throw new Error("The backend returned invalid workflow settings.");
  }
  return data;
}

export default function Settings() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [draft, setDraft] = useState<Workflows>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reload, setReload] = useState(0);
  const dirty = data !== null && JSON.stringify(draft) !== JSON.stringify(data.workflows);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void (async () => {
      try {
        const response = await fetch("/api/settings", { signal: controller.signal });
        const body = await readBody(response);
        if (!response.ok) throw new Error(errorMessage(body, response.status));
        const settings = settingsResponse(body);
        if (!controller.signal.aborted) {
          setData(settings);
          setDraft(settings.workflows);
        }
      } catch (caught) {
        if (!controller.signal.aborted) setError(failure(caught, "Could not load settings."));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    })();
    return () => controller.abort();
  }, [reload]);

  useEffect(() => {
    if (!dirty) return;
    function warn(event: BeforeUnloadEvent) { event.preventDefault(); }
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  function update(workflow: string, patch: Partial<ModelSettings>) {
    setDraft((current) => {
      const next = { ...current[workflow], ...patch };
      if (data && !data.thinking_levels[next.adapter].includes(next.thinking_level)) {
        next.thinking_level = "default";
      }
      return { ...current, [workflow]: next };
    });
    setNotice("");
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!data || !dirty || saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    const workflows = Object.fromEntries(Object.entries(draft).map(([key, value]) =>
      [key, { ...value, model: value.model.trim() }]));
    try {
      const response = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflows }),
      });
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      const saved = settingsResponse({ ...data, ...(body as object) });
      setData(saved);
      setDraft(saved.workflows);
      setNotice("Settings saved. New model runs will use these values.");
    } catch (caught) {
      setError(failure(caught, "Could not save settings. Your edits are still here; try again."));
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="page-shell">
      <header className="page-heading">
        <h1>Settings</h1>
        <p className="page-intro">Choose the adapter, model, and thinking level for each workflow.</p>
      </header>
      {loading ? <p role="status">Loading settings…</p> : !data ? (
        <div className="settings-load-error">
          <p className="connector-error" role="alert">{error}</p>
          <button className="workflows-refresh" type="button" onClick={() => setReload((value) => value + 1)}>Retry loading</button>
        </div>
      ) : (
        <form className="workflow-settings" onSubmit={(event) => void save(event)}>
          <div className="settings-heading">
            <h2>Workflow models</h2>
            <p>Shared across the application. Changes apply to the next model run; running agents keep their current settings.</p>
          </div>
          <div className="settings-column-labels" aria-hidden="true">
            <span>Workflow</span><span>Adapter</span><span>Model</span><span>Thinking level</span>
          </div>
          {Object.entries(data.workflow_labels).map(([key, label]) => (
            <fieldset className="settings-row" key={key} disabled={saving}>
              <legend>{label}</legend>
              <div className="settings-fields">
                <label htmlFor={`${key}-adapter`}><span className="settings-field-label">Adapter</span>
                  <select id={`${key}-adapter`} aria-label={`${label} adapter`} value={draft[key].adapter}
                    onChange={(event) => update(key, { adapter: event.target.value as Adapter })}>
                    {adapters.map(([value, name]) => <option key={value} value={value}>{name}</option>)}
                  </select>
                </label>
                <label htmlFor={`${key}-model`}><span className="settings-field-label">Model</span>
                  <input id={`${key}-model`} aria-label={`${label} model`} value={draft[key].model}
                    required maxLength={300} pattern=".*\S.*" autoComplete="off" spellCheck={false}
                    onChange={(event) => update(key, { model: event.target.value })} />
                </label>
                <label htmlFor={`${key}-thinking`}><span className="settings-field-label">Thinking level</span>
                  <select id={`${key}-thinking`} aria-label={`${label} thinking level`} value={draft[key].thinking_level}
                    onChange={(event) => update(key, { thinking_level: event.target.value })}>
                    {data.thinking_levels[draft[key].adapter].map((level) => (
                      <option value={level} key={level}>{level === "default" ? "Model default" : level === "xhigh" ? "Extra high" : level.charAt(0).toUpperCase() + level.slice(1)}</option>
                    ))}
                  </select>
                </label>
              </div>
            </fieldset>
          ))}
          <p className="settings-hint">Enter a model ID supported by the selected adapter. Thinking levels depend on the model; use Model default to leave its reasoning unchanged. Workflows without model calls have no model settings.</p>
          <div className="settings-actions">
            <div className="repository-actions">
              <button className="submit-button" type="submit" disabled={!dirty || saving}>{saving ? "Saving…" : "Save settings"}</button>
              <button className="workflows-refresh" type="button" disabled={!dirty || saving} onClick={() => {
                setDraft(data.workflows); setError(""); setNotice("");
              }}>Discard changes</button>
            </div>
            <span className="field-hint" role="status">{saving ? "Saving settings…" : dirty ? "Unsaved changes" : "All changes saved"}</span>
          </div>
          {error && <p className="connector-error" role="alert">{error}</p>}
          <p className="connector-notice" role="status">{notice}</p>
        </form>
      )}
    </main>
  );
}
