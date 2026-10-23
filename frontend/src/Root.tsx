import { useCallback, useEffect, useState } from 'react';
import App from './App';
import AuthScreen from './components/AuthScreen';
import SharedView from './components/SharedView';
import { apiFetch, errorMessage, loadSession, takeSsoBrowser, saveSession, SIGNED_OUT_EVENT, type AuthStatus, type Session, type User } from './api';

/** Shows the sign-in screen until there is a session, then the dashboard. */
function Root() {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [statusError, setStatusError] = useState(false);
  const [session, setSession] = useState<Session | null>(loadSession);
  // A single sign-on result arrives in the URL fragment: #sso=<token> or #sso_error=<message>.
  const [sso] = useState(readSsoResult);
  const ssoToken = sso.token;
  const [ssoError, setSsoError] = useState(sso.error);
  // A share link: ?share=<id> opens that conversation read-only.
  const [shareId, setShareId] = useState(() => new URLSearchParams(window.location.search).get('share'));
  const [shareSignIn, setShareSignIn] = useState(false);
  // Shown in the dashboard: the outcome of linking single sign-on, or an
  // error that came back while signed in.
  const [notice, setNotice] = useState<{ ok: boolean; message: string } | null>(() => session && sso.error ? { ok: false, message: sso.error } : null);

  const updateSession = useCallback((next: Session | null) => {
    saveSession(next); setSession(next);
    // Signing in means an account exists, whatever the status said at page load.
    if (next) setStatus(current => current && { ...current, has_users: true });
  }, []);

  const loadStatus = useCallback(async () => {
    setStatusError(false);
    try {
      const response = await apiFetch('/v1/ugen/auth/status', {}, '');
      if (!response.ok) throw new Error();
      setStatus(await response.json() as AuthStatus);
    } catch { setStatusError(true); }
  }, []);

  useEffect(() => { void loadStatus(); }, [loadStatus]);

  useEffect(() => {
    if (!sso.linkCode || !session) return;
    apiFetch('/v1/ugen/auth/oidc/link/complete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code: sso.linkCode }) }, session.token)
      .then(async response => {
        if (!response.ok) { setNotice({ ok: false, message: await errorMessage(response) }); return; }
        updateSession({ ...session, user: await response.json() as User });
        setNotice({ ok: true, message: 'Single sign-on is linked. You can now sign in with it.' });
      })
      .catch(() => setNotice({ ok: false, message: 'Cannot reach the gateway.' }));
    // Once, for the result in the address bar.
  }, []);

  useEffect(() => {
    if (!ssoToken) return;
    apiFetch('/v1/ugen/auth/me', {}, ssoToken)
      .then(async response => response.ok ? updateSession({ token: ssoToken, user: await response.json() as User }) : setSsoError('Single sign-on did not finish; try again.'))
      .catch(() => setSsoError('Cannot reach the gateway.'));
  }, [ssoToken, updateSession]);

  // Refresh the saved user (an administrator may have changed) and drop a dead session.
  useEffect(() => {
    if (!session) return;
    let active = true;
    apiFetch('/v1/ugen/auth/me', {}, session.token)
      .then(async response => {
        if (!active) return;
        if (response.ok) {
          const user = await response.json() as User;
          if (JSON.stringify(user) !== JSON.stringify(session.user)) updateSession({ ...session, user });
        }
      })
      .catch(() => { /* Offline: keep the saved session and let requests report it. */ });
    return () => { active = false; };
    // Only when the signed-in account changes, not on every user refresh.
  }, [session?.token]);

  useEffect(() => {
    const signedOut = () => { updateSession(null); void loadStatus(); };
    window.addEventListener(SIGNED_OUT_EVENT, signedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, signedOut);
  }, [updateSession, loadStatus]);

  async function signOut() {
    const token = session?.token;
    updateSession(null);
    if (token) await apiFetch('/v1/ugen/auth/logout', { method: 'POST' }, token).catch(() => undefined);
    void loadStatus();
  }

  if (!status) {
    return <main className="auth-page"><section className="auth-card auth-loading">
      {statusError
        ? <><b>Cannot reach the gateway.</b><p>Check that the API is running, then try again.</p><button className="send-button" onClick={() => void loadStatus()}>Try again</button></>
        : <span className="spinner dark" aria-label="Loading"/>}
    </section></main>;
  }
  if (shareId) {
    const openApp = () => { window.history.replaceState(null, '', window.location.pathname); setShareId(null); setShareSignIn(false); };
    if (shareSignIn && status.accounts_enabled && !session) return <AuthScreen key="share" status={status} onSignedIn={next => { updateSession(next); setShareSignIn(false); }}/>;
    return <SharedView key={session?.token ?? 'signed-out'} id={shareId} signedIn={Boolean(session)} onSignIn={() => setShareSignIn(true)} onOpenApp={openApp}/>;
  }
  if (status.accounts_enabled && !session) {
    if (ssoToken && !ssoError) return <main className="auth-page"><section className="auth-card auth-loading"><span className="spinner dark" aria-label="Signing in"/></section></main>;
    return <AuthScreen key={String(status.has_users)} status={status} onSignedIn={updateSession} initialError={ssoError ?? ''}/>;
  }
  // key: each account gets a fresh dashboard with its own saved history.
  return <App key={session?.user.id ?? 'open'} user={session?.user ?? null} onSignOut={signOut} notice={notice} onDismissNotice={() => setNotice(null)}
    onUserChange={user => session && updateSession({ ...session, user })}
    onAccountDeleted={() => { updateSession(null); void loadStatus(); }}/>;
}

/** A single sign-on result, accepted only in the tab that started it. */
function readSsoResult(): { token: string | null; linkCode: string | null; error: string | null } {
  const token = readFragment('sso');
  const linkCode = readFragment('sso_link');
  const browser = readFragment('browser');
  const error = readFragment('sso_error');
  const expected = takeSsoBrowser();
  if ((token || linkCode) && (!expected || browser !== expected)) return { token: null, linkCode: null, error: 'This sign-in was not started in this browser tab. Try again here.' };
  return { token, linkCode, error };
}

/** Read and remove one value from the URL fragment, so a token is not left in the address bar or history. */
function readFragment(name: string): string | null {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const value = params.get(name);
  if (value === null) return null;
  params.delete(name);
  const rest = params.toString();
  window.history.replaceState(null, '', window.location.pathname + window.location.search + (rest ? `#${rest}` : ''));
  return value;
}

export default Root;
