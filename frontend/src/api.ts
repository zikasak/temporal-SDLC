export async function readBody(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

export function errorMessage(value: unknown, status: number) {
  if (typeof value === "object" && value !== null && "detail" in value) {
    const detail = (value as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return `The backend returned HTTP ${status}.`;
}

export function failure(error: unknown, fallback: string) {
  if (error instanceof TypeError) {
    return "Could not reach the backend. Check that it is running, then try again.";
  }
  return error instanceof Error ? error.message : fallback;
}

export function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
