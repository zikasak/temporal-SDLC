import { useEffect, useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";

type Adapter = "pi" | "openai" | "claude";
type ModelSettings = { adapter: Adapter; model: string; thinking_level: string };
type Workflows = Record<string, ModelSettings>;
type ModelOption = { id: string; label: string; thinking_levels: string[] };
type ProviderOptions = { label: string; models: ModelOption[]; error: string | null; hint: string | null };
type Providers = Record<Adapter, ProviderOptions>;
type SettingsResponse = {
  workflows: Workflows;
  workflow_labels: Record<string, string>;
  providers: Providers;
};

const adapters: Adapter[] = ["pi", "openai", "claude"];

function providerResponse(value: unknown): Providers {
  const providers = value as Providers | null;
  if (!providers || !adapters.every((adapter) => {
    const provider = providers[adapter];
    return provider && typeof provider.label === "string" &&
      (provider.error === null || typeof provider.error === "string") &&
      (provider.hint === null || typeof provider.hint === "string") &&
      Array.isArray(provider.models) && provider.models.every((model) =>
        model && typeof model.id === "string" && typeof model.label === "string" &&
        Array.isArray(model.thinking_levels) && model.thinking_levels.includes("default") &&
        model.thinking_levels.every((level) => typeof level === "string"));
  })) throw new Error("The backend returned invalid provider options.");
  return providers;
}

function selectedModel(providers: Providers, item: ModelSettings) {
  return providers[item.adapter].models.find((model) => model.id === item.model);
}

function validSelection(providers: Providers, item: ModelSettings) {
  return !providers[item.adapter].error &&
    !!selectedModel(providers, item)?.thinking_levels.includes(item.thinking_level);
}

function settingsResponse(value: unknown): SettingsResponse {
  if (typeof value !== "object" || value === null) throw new Error("Invalid settings response.");
  const data = value as SettingsResponse;
  providerResponse(data.providers);
  if (!data.workflows || !data.workflow_labels ||
      Object.keys(data.workflows).length !== Object.keys(data.workflow_labels).length ||
      !Object.entries(data.workflows).length ||
      !Object.entries(data.workflows).every(([key, item]) =>
        typeof data.workflow_labels[key] === "string" && item &&
        adapters.includes(item.adapter) &&
        typeof item.model === "string" &&
        typeof item.thinking_level === "string")) {
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
  const [refreshing, setRefreshing] = useState(false);
  const dirty = data !== null && JSON.stringify(draft) !== JSON.stringify(data.workflows);
  const valid = data !== null && Object.values(draft).every((item) => validSelection(data.providers, item));

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
      if (data) {
        const models = data.providers[next.adapter].models;
        if (patch.adapter && !models.some((model) => model.id === next.model)) {
          next.model = models[0]?.id ?? "";
        }
        if (!selectedModel(data.providers, next)?.thinking_levels.includes(next.thinking_level)) {
          next.thinking_level = "default";
        }
      }
      return { ...current, [workflow]: next };
    });
    setNotice("");
  }

  async function refreshProviders() {
    setRefreshing(true);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/api/settings/providers");
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      const providers = providerResponse(body);
      setData((current) => current ? { ...current, providers } : current);
    } catch (caught) {
      setError(failure(caught, "Could not refresh provider options."));
    } finally {
      setRefreshing(false);
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!data || !dirty || !valid || saving || refreshing) return;
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
            <button className="workflows-refresh" type="button" disabled={saving || refreshing}
              onClick={() => void refreshProviders()}>{refreshing ? "Refreshing providers…" : "Refresh provider options"}</button>
          </div>
          {Object.entries(data.providers).map(([adapter, provider]) => provider.error && (
            <p className="connector-error" role="alert" key={adapter}>{provider.error}</p>
          ))}
          <div className="settings-column-labels" aria-hidden="true">
            <span>Workflow</span><span>Adapter</span><span>Model</span><span>Thinking level</span>
          </div>
          {Object.entries(data.workflow_labels).map(([key, label]) => {
            const item = draft[key];
            const provider = data.providers[item.adapter];
            const model = selectedModel(data.providers, item);
            const levels = model?.thinking_levels ?? [];
            const selectionValid = validSelection(data.providers, item);
            return (
              <fieldset className="settings-row" key={key} disabled={saving || refreshing}>
                <legend>{label}</legend>
                <div className="settings-fields">
                  <label htmlFor={`${key}-adapter`}><span className="settings-field-label">Adapter</span>
                    <select id={`${key}-adapter`} aria-label={`${label} adapter`} value={draft[key].adapter}
                      onChange={(event) => update(key, { adapter: event.target.value as Adapter })}>
                      {Object.entries(data.providers).map(([value, options]) => (
                        <option key={value} value={value} disabled={!!options.error || !options.models.length}>{options.label}</option>
                      ))}
                    </select>
                  </label>
                  <label htmlFor={`${key}-model`}><span className="settings-field-label">Model</span>
                    <select id={`${key}-model`} aria-label={`${label} model`} value={item.model}
                      disabled={!!provider.error || !provider.models.length} aria-invalid={!model}
                      aria-describedby={!selectionValid ? `${key}-selection-error` : undefined}
                      onChange={(event) => update(key, { model: event.target.value })}>
                      {!model && <option value={item.model} disabled>{item.model ? `${item.model} (unavailable)` : "Choose a model"}</option>}
                      {provider.models.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                    </select>
                  </label>
                  <label htmlFor={`${key}-thinking`}><span className="settings-field-label">Thinking level</span>
                    <select id={`${key}-thinking`} aria-label={`${label} thinking level`} value={draft[key].thinking_level}
                      disabled={!!provider.error || !model || (levels.length === 1 && levels.includes(item.thinking_level))}
                      aria-invalid={!!model && !levels.includes(item.thinking_level)}
                      aria-describedby={!selectionValid ? `${key}-selection-error` : undefined}
                      onChange={(event) => update(key, { thinking_level: event.target.value })}>
                      {!levels.includes(item.thinking_level) && <option value={item.thinking_level} disabled>{item.thinking_level} (unavailable)</option>}
                      {levels.map((level) => (
                        <option value={level} key={level}>{level === "default" ? "Model default" : level === "xhigh" ? "Extra high" : level.charAt(0).toUpperCase() + level.slice(1)}</option>
                      ))}
                    </select>
                  </label>
                  {!selectionValid && <p id={`${key}-selection-error`} className="settings-selection-error connector-error">
                    {provider.error ? "Refresh provider options or choose another adapter to verify this selection." :
                      !model ? "This model is no longer available. Choose an available model." :
                      "This thinking level is no longer supported. Choose an available level."}
                  </p>}
                </div>
              </fieldset>
            );
          })}
          <p className="settings-hint">Models and thinking levels come from the selected provider. Model default leaves reasoning unchanged. Workflows without model calls have no model settings.</p>
          {Object.entries(data.providers).map(([adapter, provider]) => provider.hint &&
            Object.values(draft).some((item) => item.adapter === adapter) && (
              <p key={adapter} className="settings-hint">{provider.label}: {provider.hint}</p>
          ))}
          <div className="settings-actions">
            <div className="repository-actions">
              <button className="submit-button" type="submit" disabled={!dirty || !valid || saving || refreshing}>{saving ? "Saving…" : "Save settings"}</button>
              <button className="workflows-refresh" type="button" disabled={!dirty || saving || refreshing} onClick={() => {
                setDraft(data.workflows); setError(""); setNotice("");
              }}>Discard changes</button>
            </div>
            <span className="field-hint" role="status">{saving ? "Saving settings…" : refreshing ? "Refreshing provider options…" : !valid ? "Resolve unavailable selections before saving" : dirty ? "Unsaved changes" : "All changes saved"}</span>
          </div>
          {error && <p className="connector-error" role="alert">{error}</p>}
          <p className="connector-notice" role="status">{notice}</p>
        </form>
      )}
    </main>
  );
}
