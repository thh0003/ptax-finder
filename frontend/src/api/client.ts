import { makeClient, refresh, type Tokens } from "../auth/cognito";
import { getConfig } from "../config";

const STORAGE_KEY = "ptax.session";
const REFRESH_MARGIN_MS = 60_000;

let tokens: Tokens | null = load();
let onUnauthorized: () => void = () => {};

function load(): Tokens | null {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Tokens) : null;
  } catch {
    return null;
  }
}

export function setTokens(next: Tokens | null): void {
  tokens = next;
  try {
    if (next) sessionStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // sessionStorage can be unavailable (private mode); the in-memory copy still works.
  }
}

export function getTokens(): Tokens | null {
  return tokens;
}

/** Called when the API rejects our token; the auth context uses it to sign out. */
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

async function freshAccessToken(): Promise<string | null> {
  if (!tokens) return null;
  if (tokens.expiresAt - Date.now() > REFRESH_MARGIN_MS) return tokens.accessToken;
  if (!tokens.refreshToken) return tokens.accessToken;
  try {
    const config = await getConfig();
    const next = await refresh(makeClient(config), config.cognito_client_id, tokens.refreshToken);
    setTokens(next);
    return next.accessToken;
  } catch {
    return tokens.accessToken;
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
  }
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = await freshAccessToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");

  const response = await fetch(path, { ...init, headers });
  if (response.status === 401) {
    setTokens(null);
    onUnauthorized();
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      else if (body.detail) detail = JSON.stringify(body.detail);
    } catch {
      // non-JSON error body
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
