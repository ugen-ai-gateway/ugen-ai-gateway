import { useState } from 'react';
import Footer from './Footer';
import { apiFetch, errorMessage, startSso, type AuthStatus, type Session } from '../api';
import LogoMark from './LogoMark';

type Mode = 'login' | 'register';
const USERNAME = /^[A-Za-z0-9_.-]{3,32}$/;

function AuthScreen({ status, onSignedIn, initialError = '' }: { status: AuthStatus; onSignedIn: (session: Session) => void; initialError?: string }) {
  // The very first account must be created before anyone can sign in.
  const firstAccount = !status.has_users;
  const [mode, setMode] = useState<Mode>(firstAccount ? 'register' : 'login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState(initialError);
  const [working, setWorking] = useState(false);
  const registering = mode === 'register';

  function switchMode(next: Mode) { setMode(next); setError(''); setPassword(''); setConfirm(''); }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (working) return;
    if (registering) {
      if (!USERNAME.test(username)) { setError('Usernames use 3–32 letters, digits, dots, dashes or underscores.'); return; }
      if (password.length < 8) { setError('Use a password of at least 8 characters.'); return; }
      if (password !== confirm) { setError('The passwords do not match.'); return; }
    }
    setWorking(true); setError('');
    try {
      const response = await apiFetch(`/v1/ugen/auth/${registering ? 'register' : 'login'}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: username.trim(), password }),
      }, '');
      if (!response.ok) { setError(await errorMessage(response)); return; }
      const body = await response.json() as Session;
      onSignedIn({ token: body.token, user: body.user });
    } catch {
      setError('Cannot reach the gateway.');
    } finally { setWorking(false); }
  }

  return <main className="auth-page">
    <section className="auth-card" aria-labelledby="auth-title">
      <div className="auth-brand"><span className="brand-mark"><LogoMark/></span><span>ugen<span className="brand-dot">.</span><small>AI GATEWAY</small></span></div>
      <span className="eyebrow">{firstAccount ? 'FIRST-TIME SETUP' : registering ? 'NEW ACCOUNT' : 'WELCOME BACK'}</span>
      <h1 id="auth-title">{firstAccount ? 'Create the administrator account' : registering ? 'Create an account' : 'Sign in'}</h1>
      <p>{firstAccount ? 'This first account manages the gateway: API keys, the audit log and other accounts’ usage.' : 'Requests are analyzed on this machine before any model sees them.'}</p>
      {!firstAccount && status.registration_open && <div className="auth-tabs" role="tablist" aria-label="Sign in or sign up">
        <button type="button" role="tab" aria-selected={!registering} className={!registering ? 'selected' : ''} onClick={() => switchMode('login')}>Sign in</button>
        <button type="button" role="tab" aria-selected={registering} className={registering ? 'selected' : ''} onClick={() => switchMode('register')}>Create account</button>
      </div>}
      {status.sso && <>
        <button type="button" className="cancel-button sso-button" onClick={() => startSso()}>{status.sso.provider === 'google' && <GoogleLogo/>}{status.sso.label}</button>
        <div className="auth-divider"><span>or use a username</span></div>
      </>}
      <form className="auth-form" onSubmit={submit}>
        <label>Username<input value={username} onChange={event => setUsername(event.target.value)} autoComplete="username" autoCapitalize="none" spellCheck={false} maxLength={32} required autoFocus/></label>
        <label>Password<input type="password" value={password} onChange={event => setPassword(event.target.value)} autoComplete={registering ? 'new-password' : 'current-password'} maxLength={128} required/></label>
        {registering && <label>Confirm password<input type="password" value={confirm} onChange={event => setConfirm(event.target.value)} autoComplete="new-password" maxLength={128} required/></label>}
        {registering && <small className="auth-hint">At least 8 characters. Usernames are not case-sensitive.</small>}
        {error && <div className="error-box" role="alert"><span>{error}</span></div>}
        <button className="send-button auth-submit" type="submit" disabled={working}>{working ? 'Working…' : registering ? 'Create account' : 'Sign in'}</button>
      </form>
      {!status.registration_open && <small className="auth-hint">Sign-up is closed. Ask an administrator for an account.</small>}
    </section>
    <Footer/>
  </main>;
}

function GoogleLogo() {
  return <svg className="sso-logo" viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
    <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
    <path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 7.54 2.56 10.78l7.97-6.19z"/>
    <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
  </svg>;
}

export default AuthScreen;
