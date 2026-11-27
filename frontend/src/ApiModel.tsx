import type { ReactNode } from "react";
import { isObject } from "./api";
import { SpoilerSection } from "./Spoiler";

/** The designed endpoints and schemas of an API design or an onboarding's drafts. */

export type Property = {
  name: string;
  type: string;
  required: boolean;
  array: boolean;
  format?: string;
  enum?: string[];
  description?: string;
};

export type Schema = { name: string; description: string; properties: Property[] };

export type Parameter = { name: string; location: string; type: string; required: boolean };

export type Endpoint = {
  method: string;
  path: string;
  summary: string;
  parameters: Parameter[];
  request_schema: string | null;
  response_status: number;
  response_schema: string | null;
  response_array: boolean;
  error_statuses: number[];
  requirement_ids: string[];
};

export type ApiModel = {
  info: { title: string; version: string; description: string; auth: string };
  schemas: Schema[];
  endpoints: Endpoint[];
};

const authLabels: Record<string, string> = {
  none: "No authentication",
  bearer: "Bearer token",
  api_key: "API key",
  basic: "Basic auth",
};

/** Whether a session's api field has the shape the page shows. */
export function isApiModel(value: unknown): value is ApiModel {
  return isObject(value) && isObject(value.info) && Array.isArray(value.schemas) &&
    Array.isArray(value.endpoints);
}

export function typeLabel(type: string | null, array: boolean) {
  if (!type) return "—";
  return array ? `${type}[]` : type;
}

export function ApiModelSection({ api, problems, requirements, children }: {
  api: ApiModel;
  problems: string[];
  // Functional requirements no endpoint covers are listed below the schemas.
  requirements: { id: string; category: string; title: string }[];
  // Shown below the heading, such as the implementation language.
  children?: ReactNode;
}) {
  const linked = new Set(api.endpoints.flatMap((item) => item.requirement_ids));
  const uncovered = requirements.filter(
    (item) => item.category === "functional" && !linked.has(item.id),
  );
  return (
    <SpoilerSection className="requirements-list api-model" label="API model"
      heading={<span>{api.info.title} <small>v{api.info.version}</small></span>}>
      <p className="api-auth">
        {authLabels[api.info.auth] ?? api.info.auth}
      </p>
      {children}

      {problems.length > 0 && (
        <div className="requirement-group api-problems">
          <h4>Open problems</h4>
          <ul>
            {problems.map((item) => (
              <li key={item}>
                <p>{item}</p>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="requirement-group">
        <h4>Endpoints ({api.endpoints.length})</h4>
        {api.endpoints.length === 0 ? (
          <p className="requirement-empty">None yet.</p>
        ) : (
          <ul>
            {api.endpoints.map((item) => (
              <li key={`${item.method} ${item.path}`}>
                <div className="requirement-heading">
                  <span className={`method method-${item.method.toLowerCase()}`}>
                    {item.method}
                  </span>
                  <code className="endpoint-path">{item.path}</code>
                  <strong>{item.summary}</strong>
                </div>
                <p className="endpoint-io">
                  {item.request_schema && <>body {item.request_schema} → </>}
                  {item.response_status}{" "}
                  {typeLabel(item.response_schema, item.response_array)}
                  {item.error_statuses.length > 0 &&
                    ` · errors ${item.error_statuses.join(", ")}`}
                  {item.parameters
                    .filter((parameter) => parameter.location !== "path")
                    .map((parameter) => ` · ${parameter.location} ${parameter.name}`)
                    .join("")}
                </p>
                {item.requirement_ids.length > 0 && (
                  <small>{item.requirement_ids.join(", ")}</small>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="requirement-group">
        <h4>Schemas ({api.schemas.length})</h4>
        {api.schemas.length === 0 ? (
          <p className="requirement-empty">None yet.</p>
        ) : (
          <ul>
            {api.schemas.map((schema) => (
              <li key={schema.name}>
                <div className="requirement-heading">
                  <strong>{schema.name}</strong>
                </div>
                {schema.description && <small>{schema.description}</small>}
                <table className="schema-properties">
                  <tbody>
                    {schema.properties.map((item) => (
                      <tr key={item.name}>
                        <td>
                          <code>{item.name}</code>
                          {item.required && <span title="Required">*</span>}
                        </td>
                        <td>
                          {typeLabel(item.type, item.array)}
                          {item.format && ` (${item.format})`}
                          {item.enum && ` ${item.enum.join(" | ")}`}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </li>
            ))}
          </ul>
        )}
      </div>

      {uncovered.length > 0 && (
        <div className="requirement-group">
          <h4>Functional requirements without endpoints</h4>
          <ul>
            {uncovered.map((item) => (
              <li key={item.id}>
                <div className="requirement-heading">
                  <code>{item.id}</code>
                  <span>{item.title}</span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}
    </SpoilerSection>
  );
}
