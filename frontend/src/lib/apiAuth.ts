const STORAGE_KEY = "vibe_trading_api_auth_key";

export function getApiAuthKey(): string {
  return window.localStorage.getItem(STORAGE_KEY) || "";
}

export function setApiAuthKey(value: string): void {
  const trimmed = value.trim();
  if (trimmed) {
    window.localStorage.setItem(STORAGE_KEY, trimmed);
  } else {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}

export function authHeaders(): Record<string, string> {
  const key = getApiAuthKey();
  return key ? { Authorization: `Bearer ${key}` } : {};
}

export async function withAuthTicket(url: string): Promise<string> {
  const key = getApiAuthKey();
  if (!key) return url;

  const response = await fetch("/auth/sse-ticket", {
    method: "POST",
    headers: authHeaders(),
  });
  if (!response.ok) {
    throw new Error(`SSE ticket request failed (HTTP ${response.status})`);
  }

  const data = await response.json() as { ticket?: unknown };
  if (typeof data.ticket !== "string" || !data.ticket) {
    throw new Error("SSE ticket response was invalid");
  }
  return `${url}${url.includes("?") ? "&" : "?"}ticket=${encodeURIComponent(data.ticket)}`;
}
