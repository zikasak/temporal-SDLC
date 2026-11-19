/** The value of the page URL's query parameter `name`, or "" when it has none. */
export function urlParam(name: string) {
  return new URLSearchParams(window.location.search).get(name) ?? "";
}

/** Keep the open page (`mode`) and its session in the URL, so a reload restores them. */
export function rememberSessionId(mode: string, sessionId: string) {
  const url = new URL(window.location.href);
  url.searchParams.set("mode", mode);
  if (sessionId) url.searchParams.set("session_id", sessionId);
  else url.searchParams.delete("session_id");
  window.history.replaceState(null, "", url);
}

/** The Flows page listing the steps of the flow `workflowId`, such as a task flow. */
export function flowStepsLink(workflowId: string) {
  return `?parent=${encodeURIComponent(workflowId)}`;
}
