// Access-token storage.
//
// The backend issues a short-lived access token in the login response body and
// a long-lived refresh token as an HttpOnly cookie (see app/api/auth_routes.py
// `_set_refresh_cookie`). The refresh token is therefore never readable by
// JavaScript; the access token is held in sessionStorage (cleared when the tab
// closes) rather than localStorage, so it survives reloads but not the session.

const ACCESS_TOKEN_KEY = "voxdesk.access_token";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage.getItem(ACCESS_TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(ACCESS_TOKEN_KEY, token);
  } catch {
    // Storage is unavailable (private mode / disabled); the caller still holds
    // the token in memory for the life of the page.
  }
}

export function clearToken(): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.removeItem(ACCESS_TOKEN_KEY);
  } catch {
    // Nothing to do; a disabled store cannot hold a stale token.
  }
}
