const SESSION_STORAGE = 'ugen-gateway-session';
/** Fired when the gateway rejects the saved session, so the app can sign out. */
export const SIGNED_OUT_EVENT = 'ugen:signed-out';

export const apiUrl = (import.meta.env.VITE_GATEWAY_URL || '/api').replace(/\/$/, '');

export type User = { id: string; username: string; is_admin: boolean; avatar?: string | null; sso?: boolean; has_password?: boolean; email?: string | null };
export type Session = { token: string; user: User };
export type AuthStatus = { accounts_enabled: boolean; has_users: boolean; registration_open: boolean; sso?: { label: string; provider?: 'google' | 'oidc' } | null };

const SSO_BROWSER_STORAGE = 'ugen-sso-browser';

/** Start single sign-on: the gateway sends the browser to the identity
 * provider. A random value kept in this tab must come back with the session,
 * so a session handed over in someone else's link is refused. */
export function startSso(linkTicket?: string): void {
  const value = Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
  try { sessionStorage.setItem(SSO_BROWSER_STORAGE, value); } catch { /* the result will be refused; nothing else to do */ }
  const link = linkTicket ? `&link=${encodeURIComponent(linkTicket)}` : '';
  window.location.assign(`${apiUrl}/v1/ugen/auth/oidc/start?browser=${value}${link}`);
}

/** Link single sign-on to the signed-in account: get a ticket, then sign in. */
export async function startSsoLink(): Promise<string | null> {
  const response = await apiFetch('/v1/ugen/auth/oidc/link', { method: 'POST' });
  if (!response.ok) return errorMessage(response);
  startSso((await response.json() as { ticket: string }).ticket);
  return null;
}

/** The value startSso kept, once. */
export function takeSsoBrowser(): string | null {
  try {
    const value = sessionStorage.getItem(SSO_BROWSER_STORAGE);
    sessionStorage.removeItem(SSO_BROWSER_STORAGE);
    return value;
  } catch { return null; }
}

/** Tokens are sent in an HTTP header, which only carries printable ASCII. */
const isHeaderSafe = (token: string) => /^[\x21-\x7e]*$/.test(token);

export function loadSession(): Session | null {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(SESSION_STORAGE) ?? 'null');
    if (!stored || typeof stored !== 'object') return null;
    const session = stored as Partial<Session>;
    if (typeof session.token !== 'string' || !isHeaderSafe(session.token) || !session.user?.id) return null;
    return session as Session;
  } catch { return null; }
}

export function saveSession(session: Session | null): void {
  try {
    if (session) localStorage.setItem(SESSION_STORAGE, JSON.stringify(session));
    else localStorage.removeItem(SESSION_STORAGE);
  } catch {
    // Storage can be unavailable (private mode); the session then lasts for this page only.
  }
}

/** Fetch a gateway path, sending the signed-in session as a bearer token. */
export async function apiFetch(path: string, init: RequestInit = {}, token = loadSession()?.token): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/json');
  const sent = Boolean(token && isHeaderSafe(token));
  if (sent) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(`${apiUrl}${path}`, { ...init, headers });
  if (response.status === 401 && sent) window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
  return response;
}

/** The error message from a failed gateway response, in the gateway's own words. */
export async function errorMessage(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  if (body && typeof body === 'object') {
    const data = body as { detail?: unknown; error?: { message?: unknown } };
    if (typeof data.detail === 'string') return data.detail;
    // Validation errors arrive as a list; show the first one.
    if (Array.isArray(data.detail) && typeof data.detail[0]?.msg === 'string') return String(data.detail[0].msg).replace(/^Value error, /, '');
    if (typeof data.error?.message === 'string') return data.error.message;
  }
  return `Request failed (HTTP ${response.status})`;
}
