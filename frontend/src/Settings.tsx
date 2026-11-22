import { useEffect, useState, type FormEvent } from "react";
import { errorMessage, failure, readBody } from "./api";

type Adapter = "pi" | "openai" | "claude";
type ModelSettings = { adapter: Adapter; model: string; thinking_level: string };
type Workflows = Record<string, ModelSettings>;
type ModelOption = { id: string; label: string; thinking_levels: string[] };
type ProviderOptions = { label: string; models: ModelOption[]; error: string | null; hint: string | null };
// The openai adapter is listed only while an OpenAI endpoint is set up.
type Providers = Partial<Record<Adapter, ProviderOptions>>;
// Base URL and api_key_set are shown to the administrator only; the key never is.
type OpenAIEndpointView = { base_url?: string | null; display_name: string | null; api_key_set?: boolean };
type SettingsResponse = {
  workflows: Workflows;
  workflow_labels: Record<string, string>;
  providers: Providers;
  openai_endpoint: OpenAIEndpointView;
};

const adapters: Adapter[] = ["pi", "openai", "claude"];
const adapterLabels: Record<Adapter, string> = { pi: "Pi", openai: "OpenAI", claude: "Claude" };

function providerResponse(value: unknown): Providers {
  const providers = value as Providers | null;
  if (!providers || typeof providers !== "object" || !Object.keys(providers).every((key) => {
    if (!adapters.includes(key as Adapter)) return false;
    const provider = providers[key as Adapter];
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
  return providers[item.adapter]?.models.find((model) => model.id === item.model);
}

function validSelection(providers: Providers, item: ModelSettings) {
  const provider = providers[item.adapter];
  return !!provider && !provider.error &&
    !!selectedModel(providers, item)?.thinking_levels.includes(item.thinking_level);
}

function endpointResponse(value: unknown): OpenAIEndpointView {
  const endpoint = value as OpenAIEndpointView | null;
  if (!endpoint || typeof endpoint !== "object" ||
      !(endpoint.display_name === null || typeof endpoint.display_name === "string") ||
      !(endpoint.base_url === undefined || endpoint.base_url === null || typeof endpoint.base_url === "string") ||
      !(endpoint.api_key_set === undefined || typeof endpoint.api_key_set === "boolean")) {
    throw new Error("The backend returned an invalid OpenAI endpoint.");
  }
  return endpoint;
}

function settingsResponse(value: unknown): SettingsResponse {
  if (typeof value !== "object" || value === null) throw new Error("Invalid settings response.");
  const data = value as SettingsResponse;
  providerResponse(data.providers);
  endpointResponse(data.openai_endpoint);
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
        const models = data.providers[next.adapter]?.models ?? [];
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
            // Missing for a saved openai choice while no OpenAI endpoint is set up.
            const provider = data.providers[item.adapter];
            const providerModels = provider?.models ?? [];
            const unavailable = !provider || !!provider.error;
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
                      {!provider && <option value={item.adapter} disabled>{adapterLabels[item.adapter]} (not set up)</option>}
                    </select>
                  </label>
                  <label htmlFor={`${key}-model`}><span className="settings-field-label">Model</span>
                    <select id={`${key}-model`} aria-label={`${label} model`} value={item.model}
                      disabled={unavailable || !providerModels.length} aria-invalid={!model}
                      aria-describedby={!selectionValid ? `${key}-selection-error` : undefined}
                      onChange={(event) => update(key, { model: event.target.value })}>
                      {!model && <option value={item.model} disabled>{item.model ? `${item.model} (unavailable)` : "Choose a model"}</option>}
                      {providerModels.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
                    </select>
                  </label>
                  <label htmlFor={`${key}-thinking`}><span className="settings-field-label">Thinking level</span>
                    <select id={`${key}-thinking`} aria-label={`${label} thinking level`} value={draft[key].thinking_level}
                      disabled={unavailable || !model || (levels.length === 1 && levels.includes(item.thinking_level))}
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
                    {!provider ? "The OpenAI endpoint is not set up, so this choice fails until it is. Choose another adapter, or have the administrator set up the endpoint." :
                      provider.error ? "Refresh provider options or choose another adapter to verify this selection." :
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
      {data && typeof data.openai_endpoint.api_key_set === "boolean" && (
        <OpenAIEndpointSettings settings={data} disabled={saving || refreshing}
          onSaved={(saved) => {
            // Unsaved workflow edits stay in the draft.
            setData(saved);
            setNotice("");
          }} />
      )}
    </main>
  );
}

const MAX_BASE_URL_CHARS = 2048;
const MAX_DISPLAY_NAME_CHARS = 100;

function validBaseUrl(value: string) {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !!url.hostname &&
      value.length <= MAX_BASE_URL_CHARS;
  } catch {
    return false;
  }
}

/** The shared OpenAI endpoint, administrator only: saved with the saved workflow settings
 * (PUT /settings), checked by the backend first; the API key is never shown again. */
function OpenAIEndpointSettings({ settings, disabled, onSaved }: {
  settings: SettingsResponse; disabled: boolean; onSaved: (saved: SettingsResponse) => void;
}) {
  const endpoint = settings.openai_endpoint;
  const savedUrl = endpoint.base_url ?? "";
  const [baseUrl, setBaseUrl] = useState(savedUrl);
  const [apiKey, setApiKey] = useState("");
  const [noKey, setNoKey] = useState(false);
  const [displayName, setDisplayName] = useState(endpoint.display_name ?? "");
  const [busy, setBusy] = useState<"" | "save" | "remove">("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    setBaseUrl(endpoint.base_url ?? "");
    setDisplayName(endpoint.display_name ?? "");
    setApiKey("");
    setNoKey(false);
  }, [endpoint.base_url, endpoint.display_name, endpoint.api_key_set]);

  const url = baseUrl.trim();
  const urlChanged = url !== savedUrl;
  const name = displayName.trim();
  // A new base URL needs its key, or the choice to use it without one.
  const keyMissing = urlChanged && !noKey && !apiKey.trim();
  const changed = urlChanged || noKey || !!apiKey.trim() || name !== (endpoint.display_name ?? "");
  const valid = validBaseUrl(url) && name.length <= MAX_DISPLAY_NAME_CHARS && !keyMissing;
  const inUse = Object.entries(settings.workflows)
    .filter(([, item]) => item.adapter === "openai").map(([key]) => settings.workflow_labels[key] ?? key);

  async function send(section: object | null, action: "save" | "remove") {
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const response = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflows: settings.workflows, openai_endpoint: section }),
      });
      const body = await readBody(response);
      if (!response.ok) throw new Error(errorMessage(body, response.status));
      const saved = settingsResponse(body);
      onSaved(saved);
      setNotice(action === "remove" ? "OpenAI endpoint removed. The openai adapter is no longer offered." :
        "OpenAI endpoint saved. New model runs and coding agents of the openai adapter use it.");
    } catch (caught) {
      setError(failure(caught, action === "remove" ? "Could not remove the OpenAI endpoint." :
        "Could not save the OpenAI endpoint. Your entries are still here; try again."));
    } finally {
      setBusy("");
    }
  }

  function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!valid || !changed || busy || disabled) return;
    const section: Record<string, string> = { base_url: url, display_name: name };
    // Omitted keeps the saved key; an empty one saves the endpoint without a key.
    if (noKey) section.api_key = "";
    else if (apiKey.trim()) section.api_key = apiKey.trim();
    void send(section, "save");
  }

  const off = disabled || !!busy;
  return (
    <section className="workflow-settings openai-endpoint-settings" aria-labelledby="openai-endpoint-heading">
      <div className="settings-heading">
        <h2 id="openai-endpoint-heading">OpenAI endpoint</h2>
        <p>The OpenAI-compatible endpoint every model run and coding agent of the openai adapter uses. Only you, the administrator, can change it. Until a base URL is saved the openai adapter is not offered.</p>
      </div>
      <form className="message-form github-token-form" onSubmit={save}>
        <label htmlFor="openai-base-url">Base URL</label>
        <input id="openai-base-url" type="url" value={baseUrl} autoComplete="off" spellCheck={false}
          maxLength={MAX_BASE_URL_CHARS} placeholder="https://api.openai.com/v1" disabled={off}
          aria-invalid={!!url && !validBaseUrl(url)} onChange={(event) => setBaseUrl(event.target.value)} />
        <label htmlFor="openai-api-key">API key</label>
        <input id="openai-api-key" type="password" value={apiKey} autoComplete="new-password" spellCheck={false}
          disabled={off || noKey} aria-describedby="openai-api-key-help"
          placeholder={endpoint.api_key_set && !urlChanged ? "Saved key kept when left empty" : ""}
          onChange={(event) => setApiKey(event.target.value)} />
        <label className="checkbox-label" htmlFor="openai-no-key">
          <input id="openai-no-key" type="checkbox" checked={noKey} disabled={off}
            onChange={(event) => setNoKey(event.target.checked)} /> This endpoint needs no API key
        </label>
        <p id="openai-api-key-help" className="field-hint">
          {endpoint.api_key_set ? "An API key is saved. " : endpoint.base_url ? "No API key is saved. " : ""}
          The key is stored encrypted and never shown again. Saving a new base URL or key first checks it with GET /models.
        </p>
        <label htmlFor="openai-display-name">Display name (optional; shown instead of OpenAI in the model pickers)</label>
        <input id="openai-display-name" type="text" value={displayName} autoComplete="off" spellCheck={false}
          maxLength={MAX_DISPLAY_NAME_CHARS} placeholder="OpenAI" disabled={off}
          onChange={(event) => setDisplayName(event.target.value)} />
        {keyMissing && <p className="field-hint">Enter the API key for the new base URL, or choose that the endpoint needs none.</p>}
        <div className="repository-actions">
          <button type="submit" className="submit-button" disabled={off || !valid || !changed}>
            {busy === "save" ? "Checking and saving…" : "Save endpoint"}
          </button>
          {endpoint.base_url && <button type="button" className="workflows-refresh disconnect-button" disabled={off}
            onClick={() => void send(null, "remove")}>{busy === "remove" ? "Removing…" : "Remove"}</button>}
        </div>
        {endpoint.base_url && inUse.length > 0 && <p className="field-hint">In use by: {inUse.join(", ")}. Choose another adapter for them before removing the endpoint.</p>}
        <p className="field-hint">Saved together with the saved workflow models; unsaved model changes above are not included.</p>
      </form>
      {error && <p className="connector-error" role="alert">{error}</p>}
      {notice && <p className="connector-notice" role="status">{notice}</p>}
    </section>
  );
}
