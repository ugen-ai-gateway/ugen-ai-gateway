import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch, apiUrl, errorMessage, startSsoLink, type AuthStatus, type User } from '../api';
import Avatar from './Avatar';
import AvatarCropper from './AvatarCropper';
import ModelsPage from './ModelsPage';
import NumberStepper from './NumberStepper';
import PrivacyPage from './PrivacyPage';
import PromptEditor from './PromptEditor';
import UsersPage from './UsersPage';
import { applyTheme, loadTheme, type Theme } from '../theme';

export type SettingsPage = 'account' | 'users' | 'models' | 'privacy' | 'keys';
type Props = {
  user: User | null;
  page: SettingsPage;
  onPageChange: (page: SettingsPage) => void;
  onUserChange: (user: User) => void;
  onSignOut: () => void;
  onAccountDeleted: () => void;
  onClose: () => void;
  onModelsSaved: () => void;
};
type ApiKey = {
  id: string; name: string; prefix: string; created_at: number; revoked_at: number | null; last_used_at: number | null;
  allow_cloud: boolean; monthly_budget_usd: number | null; rate_limit_per_minute: number | null;
  month_cost_usd: number; month_requests: number;
};

const usd = (value: number) => `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`;
const date = (seconds: number | null) => seconds ? new Intl.DateTimeFormat('en-US', { dateStyle: 'short', timeStyle: 'short' }).format(seconds * 1000) : '—';
// Budget arrows move in cents-scale steps for small budgets and whole dollars from $10 up.
const budgetStep = (current: number, direction: 1 | -1) => (direction === 1 ? current < 10 : current <= 10) ? 0.1 : 1;
const AVATAR_SIZE = 160;

function AppearanceSection() {
  const [theme, setTheme] = useState<Theme>(loadTheme);
  return <div className="settings-section">
    <div className="section-head"><h3>Appearance</h3>
      <div className="segmented" role="radiogroup" aria-label="Theme">
        {(['system', 'light', 'dark'] as const).map(value => <button type="button" key={value} role="radio" aria-checked={theme === value} className={theme === value ? 'selected' : ''}
          onClick={() => { applyTheme(value); setTheme(value); }}>{value === 'system' ? 'System' : value === 'light' ? 'Light' : 'Dark'}</button>)}
      </div>
    </div>
  </div>;
}

function SsoAccountSection({ user, onUserChange }: { user: User; onUserChange: (user: User) => void }) {
  const [sso, setSso] = useState<AuthStatus['sso']>(null);
  const [state, setState] = useState<{ ok: boolean; message: string } | null>(null);
  const [working, setWorking] = useState(false);
  useEffect(() => {
    apiFetch('/v1/ugen/auth/status', {}, '').then(async response => { if (response.ok) setSso((await response.json() as AuthStatus).sso ?? null); }).catch(() => undefined);
  }, []);
  // Nothing to link to until an administrator sets up single sign-on.
  if (!sso && !user.sso) return null;
  const provider = sso?.provider === 'google' ? 'Google' : 'single sign-on';

  async function link() {
    setWorking(true); setState(null);
    const error = await startSsoLink().catch(() => 'Cannot reach the gateway');
    // On success the browser has left for the provider.
    if (error) { setState({ ok: false, message: error }); setWorking(false); }
  }

  async function unlink() {
    if (!window.confirm(`Unlink ${provider}? You will sign in with your username and password.`)) return;
    setWorking(true); setState(null);
    try {
      const response = await apiFetch('/v1/ugen/auth/me/sso', { method: 'DELETE' });
      if (!response.ok) { setState({ ok: false, message: await errorMessage(response) }); return; }
      onUserChange(await response.json() as User);
      setState({ ok: true, message: 'Unlinked.' });
    } catch { setState({ ok: false, message: 'Cannot reach the gateway' }); } finally { setWorking(false); }
  }

  return <div className="settings-section">
    <h3>Single sign-on</h3>
    {user.sso
      ? <p>Linked{user.email ? ` to ${user.email}` : ''}. You can sign in with {provider}{user.has_password ? ' or your password' : ''}.</p>
      : <p>Link your {provider} account to sign in to this account with it. If you already signed in with {provider} once, that created a separate account: an administrator must delete it first.</p>}
    <div className="probe-row">
      {user.sso
        ? <button type="button" className="cancel-button" onClick={() => void unlink()} disabled={working || !user.has_password} title={user.has_password ? undefined : 'Set a password first'}>Unlink</button>
        : sso && <button type="button" className="cancel-button sso-link-button" onClick={() => void link()} disabled={working}>{working ? 'Opening…' : `Link ${provider === 'Google' ? 'Google account' : provider}`}</button>}
      {user.sso && !user.has_password && <span className="probe">Set a password below before unlinking.</span>}
      {state && <span className={`probe ${state.ok ? 'ok' : 'error'}`} role="status">{state.message}</span>}
    </div>
  </div>;
}

function PasswordSection({ hasPassword, onPasswordSet }: { hasPassword: boolean; onPasswordSet: () => void }) {
  const [form, setForm] = useState({ current: '', next: '', confirm: '' });
  const [state, setState] = useState<{ ok: boolean; message: string } | null>(null);
  const [working, setWorking] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (form.next.length < 8) { setState({ ok: false, message: 'Use a new password of at least 8 characters.' }); return; }
    if (form.next !== form.confirm) { setState({ ok: false, message: 'The new passwords do not match.' }); return; }
    setWorking(true); setState(null);
    try {
      const response = await apiFetch('/v1/ugen/auth/me/password', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: form.current, new_password: form.next }),
      });
      if (!response.ok) { setState({ ok: false, message: await errorMessage(response) }); return; }
      setForm({ current: '', next: '', confirm: '' });
      setState({ ok: true, message: hasPassword ? 'Password changed. Other browsers signed in to this account were signed out.' : 'Password set. You can now also sign in with your username.' });
      if (!hasPassword) onPasswordSet();
    } catch { setState({ ok: false, message: 'Cannot reach the gateway' }); } finally { setWorking(false); }
  }

  return <form className="settings-section password-form" onSubmit={submit}>
    <h3>Password</h3>
    {!hasPassword && <p>You sign in with single sign-on. Set a password to also sign in with your username and password.</p>}
    {hasPassword && <input type="password" value={form.current} onChange={event => setForm({ ...form, current: event.target.value })} placeholder="Current password" aria-label="Current password" autoComplete="current-password" required/>}
    <input type="password" value={form.next} onChange={event => setForm({ ...form, next: event.target.value })} placeholder="New password (8+ characters)" aria-label="New password" autoComplete="new-password" required/>
    <input type="password" value={form.confirm} onChange={event => setForm({ ...form, confirm: event.target.value })} placeholder="Confirm new password" aria-label="Confirm new password" autoComplete="new-password" required/>
    <div className="probe-row"><button className="cancel-button" type="submit" disabled={working}>{working ? 'Saving…' : hasPassword ? 'Change password' : 'Set password'}</button>
      {state && <span className={`probe ${state.ok ? 'ok' : 'error'}`} role="status">{state.message}</span>}</div>
  </form>;
}

function AccountPage({ user, onUserChange, onSignOut, onAccountDeleted }: { user: User; onUserChange: (user: User) => void; onSignOut: () => void; onAccountDeleted: () => void }) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [avatarError, setAvatarError] = useState('');
  const [saving, setSaving] = useState(false);
  const [cropping, setCropping] = useState<File | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deletePassword, setDeletePassword] = useState('');
  const [deleteError, setDeleteError] = useState('');

  async function saveAvatar(image: string | null) {
    setSaving(true); setAvatarError('');
    try {
      const response = await apiFetch('/v1/ugen/auth/me/avatar', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ image }),
      });
      if (!response.ok) { setAvatarError(await errorMessage(response)); return; }
      onUserChange(await response.json() as User);
      setCropping(null);
    } catch { setAvatarError('Cannot reach the gateway'); } finally { setSaving(false); }
  }

  async function deleteAccount(event: React.FormEvent) {
    event.preventDefault();
    if (!deletePassword || saving) return;
    const confirmation = user.has_password === false ? { username: deletePassword } : { password: deletePassword };
    setSaving(true); setDeleteError('');
    try {
      const response = await apiFetch('/v1/ugen/auth/me', {
        method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(confirmation),
      });
      if (response.status === 204) { onAccountDeleted(); return; }
      setDeleteError(await errorMessage(response));
    } catch { setDeleteError('Cannot reach the gateway'); } finally { setSaving(false); }
  }

  return <>
    <div className="settings-section first">
      <h3>Profile</h3>
      <div className="profile-row">
        <button type="button" className="avatar-picker" onClick={() => fileInput.current?.click()} disabled={saving} aria-label="Change profile picture" title="Change profile picture">
          <Avatar user={user} className="large"/><span className="avatar-edit" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M4 8.5h3l1.6-2.5h6.8L17 8.5h3V18H4z"/><circle cx="12" cy="13" r="3.2"/></svg></span>
        </button>
        <span className="account-copy"><b>{user.username}</b><small>{user.is_admin ? 'Administrator · manages API keys and sees all usage' : 'Member · sees only your own usage'}{user.sso ? ` · single sign-on${user.email ? ` (${user.email})` : ''}` : ''}</small>
          <span className="profile-actions">
            <button type="button" className="cancel-button" onClick={() => fileInput.current?.click()} disabled={saving}>{saving ? 'Saving…' : user.avatar ? 'Change picture' : 'Upload picture'}</button>
            {user.avatar && <button type="button" className="text-action" onClick={() => void saveAvatar(null)} disabled={saving}>Remove</button>}
          </span>
        </span>
        <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden onChange={event => { const file = event.target.files?.[0]; if (file) { setAvatarError(''); setCropping(file); } event.target.value = ''; }}/>
      </div>
      <small className="settings-hint">After choosing a picture you can move and zoom it to fit the circle.</small>
      {avatarError && !cropping && <div className="error-box" role="alert"><span>{avatarError}</span></div>}
      {cropping && <AvatarCropper file={cropping} outputSize={AVATAR_SIZE} saving={saving} saveError={avatarError} onCancel={() => { setCropping(null); setAvatarError(''); }} onSave={image => void saveAvatar(image)}/>}
    </div>

    <AppearanceSection/>

    <PromptEditor title="Custom instructions" path="/v1/ugen/auth/me/instructions" maxLength={4000}
      description="Added to every conversation you have here, for example who you are or how you like answers. It is stored encrypted, and personal data in it is redacted before anything reaches a cloud model, like the rest of your messages."
      placeholder={'e.g. I am a nurse in Taipei. Answer in Traditional Chinese and keep it short.'}/>

    <SsoAccountSection user={user} onUserChange={onUserChange}/>

    <PasswordSection hasPassword={user.has_password !== false} onPasswordSet={() => onUserChange({ ...user, has_password: true })}/>

    <div className="settings-section">
      <h3>Session</h3>
      <div className="settings-line"><p>Signed in on this browser. Signing out keeps your saved conversations here.</p><button className="cancel-button" type="button" onClick={onSignOut}>Sign out</button></div>
    </div>

    <div className="settings-section danger-zone">
      <h3>Delete account</h3>
      <p>Permanently deletes your account, your sign-in sessions and your usage records, plus the conversations saved in this browser. This cannot be undone.{user.is_admin ? ' If you are the last administrator, the oldest remaining account becomes administrator.' : ''}</p>
      {deleting ? <form className="settings-row" onSubmit={deleteAccount}>
        {user.has_password === false
          ? <input value={deletePassword} onChange={event => { setDeletePassword(event.target.value); setDeleteError(''); }} placeholder={`Type ${user.username} to confirm`} aria-label="Username" autoComplete="off" spellCheck={false} autoFocus/>
          : <input type="password" value={deletePassword} onChange={event => { setDeletePassword(event.target.value); setDeleteError(''); }} placeholder="Enter your password to confirm" aria-label="Password" autoComplete="current-password" autoFocus/>}
        <button className="danger-button" type="submit" disabled={saving || !deletePassword}>Delete permanently</button>
        <button className="cancel-button" type="button" onClick={() => { setDeleting(false); setDeletePassword(''); setDeleteError(''); }}>Cancel</button>
      </form> : <button className="danger-outline-button" type="button" onClick={() => setDeleting(true)}>Delete my account…</button>}
      {deleteError && <div className="error-box" role="alert"><span>{deleteError}</span></div>}
    </div>
  </>;
}

function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [keysError, setKeysError] = useState('');
  const [form, setForm] = useState({ name: '', allowCloud: true, budget: '', rateLimit: '' });
  const [created, setCreated] = useState<{ name: string; key: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [working, setWorking] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await apiFetch('/v1/ugen/keys');
      if (response.ok) { setKeys((await response.json() as { data: ApiKey[] }).data); setKeysError(''); }
      else { setKeys(null); setKeysError(await errorMessage(response)); }
    } catch { setKeys(null); setKeysError('Cannot reach the gateway'); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function createKey(event: React.FormEvent) {
    event.preventDefault();
    if (!form.name.trim() || working) return;
    setWorking(true);
    try {
      const response = await apiFetch('/v1/ugen/keys', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: form.name.trim(), allow_cloud: form.allowCloud,
          monthly_budget_usd: form.budget === '' ? null : Number(form.budget),
          rate_limit_per_minute: form.rateLimit === '' ? null : Number(form.rateLimit),
        }),
      });
      if (!response.ok) { setKeysError(await errorMessage(response)); return; }
      const body = await response.json() as { name: string; key: string };
      setCreated({ name: body.name, key: body.key }); setCopied(false);
      setForm({ name: '', allowCloud: true, budget: '', rateLimit: '' });
      await load();
    } finally { setWorking(false); }
  }

  async function revoke(key: ApiKey) {
    if (!window.confirm(`Revoke "${key.name}"? Apps using this key will lose access immediately.`)) return;
    const response = await apiFetch(`/v1/ugen/keys/${encodeURIComponent(key.id)}`, { method: 'DELETE' });
    if (!response.ok) setKeysError(await errorMessage(response));
    await load();
  }

  async function copy(text: string) {
    try { await navigator.clipboard.writeText(text); setCopied(true); } catch { setCopied(false); }
  }

  return <>
    <div className="settings-section first">
      <h3>Create a key</h3>
      <p>Keys let other apps call the OpenAI-compatible API. Create one per team or app, each with its own cloud access, monthly budget and rate limit. Once the budget is used up, local models answer instead.</p>
      {created && <div className="created-key" role="status">
        <b>The key for "{created.name}" is shown only once:</b>
        <code>{created.key}</code>
        <button className="cancel-button" type="button" onClick={() => copy(created.key)}>{copied ? 'Copied' : 'Copy'}</button>
      </div>}
      {keysError && <div className="error-box" role="alert"><span>{keysError}</span></div>}
      {keys && <form className="key-form" onSubmit={createKey}>
        <input value={form.name} onChange={event => setForm({ ...form, name: event.target.value })} placeholder="Name, e.g. Marketing team" aria-label="Key name" maxLength={80} required/>
        <NumberStepper value={form.budget} onChange={budget => setForm(current => ({ ...current, budget }))} step={budgetStep} min={0} decimals={2} placeholder="Budget USD/mo" label="Monthly cloud budget"/>
        <NumberStepper value={form.rateLimit} onChange={rateLimit => setForm(current => ({ ...current, rateLimit }))} step={() => 1} min={1} decimals={0} placeholder="Max req/min" label="Requests per minute limit"/>
        <label className="key-check"><input type="checkbox" checked={form.allowCloud} onChange={event => setForm({ ...form, allowCloud: event.target.checked })}/> Allow cloud models</label>
        <button className="send-button" type="submit" disabled={working || !form.name.trim()}>Create key</button>
      </form>}
      {keys && <small className="settings-hint">Leave budget or rate limit empty for no limit. Budget arrows step $0.10 below $10 and $1 from $10 up.</small>}
    </div>
    {keys && <div className="settings-section">
      <h3>Keys</h3>
      {keys.length ? <ul className="key-list">{keys.map(key => <li key={key.id} className={key.revoked_at ? 'revoked' : ''}>
        <div className="key-main"><b>{key.name}</b><small><code>{key.prefix}…</code> · {key.allow_cloud ? 'Cloud allowed' : 'Local only'}{key.monthly_budget_usd !== null ? ` · budget ${usd(key.monthly_budget_usd)}` : ''}{key.rate_limit_per_minute !== null ? ` · ${key.rate_limit_per_minute}/min` : ''}</small></div>
        <div className="key-usage"><b>{key.month_requests} req · {usd(key.month_cost_usd)}</b><small>{key.revoked_at ? `Revoked ${date(key.revoked_at)}` : `Last used ${date(key.last_used_at)}`}</small></div>
        {!key.revoked_at && <button className="text-action" onClick={() => revoke(key)}>Revoke</button>}
      </li>)}</ul> : <p className="key-empty">No API keys yet.</p>}
      <a className="audit-link" href={`${apiUrl}/v1/ugen/audit?format=csv`} onClick={async event => {
        event.preventDefault();
        const response = await apiFetch('/v1/ugen/audit?format=csv');
        if (!response.ok) { setKeysError(await errorMessage(response)); return; }
        const url = URL.createObjectURL(await response.blob());
        const link = Object.assign(document.createElement('a'), { href: url, download: 'ugen-audit.csv' });
        link.click(); URL.revokeObjectURL(url);
      }}>Export audit log (CSV)</a>
    </div>}
  </>;
}

function SettingsDialog({ user, page, onPageChange, onUserChange, onSignOut, onAccountDeleted, onClose, onModelsSaved }: Props) {
  // Without accounts (no database) the gateway runs open and management is open too.
  const canManage = user ? user.is_admin : true;
  const pages: { id: SettingsPage; label: string }[] = [
    ...(user ? [{ id: 'account' as const, label: 'Account' }] : []),
    ...(canManage ? [...(user ? [{ id: 'users' as const, label: 'Users' }] : []), { id: 'models' as const, label: 'Models' }, { id: 'privacy' as const, label: 'Privacy' }, { id: 'keys' as const, label: 'API keys' }] : []),
  ];
  const current = pages.some(item => item.id === page) ? page : pages[0]?.id;

  return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="confirm-modal settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title">
      <div className="settings-head">
        <div><span className="eyebrow">SETTINGS</span><h2 id="settings-title">{pages.find(item => item.id === current)?.label ?? 'Settings'}</h2></div>
        <button className="icon-button" onClick={onClose} aria-label="Close settings">×</button>
      </div>
      {pages.length > 1 && <div className="settings-tabs" role="tablist" aria-label="Settings pages">
        {pages.map(item => <button key={item.id} type="button" role="tab" aria-selected={current === item.id} className={current === item.id ? 'selected' : ''} onClick={() => onPageChange(item.id)}>{item.label}</button>)}
      </div>}
      {current === 'account' && user && <AccountPage user={user} onUserChange={onUserChange} onSignOut={onSignOut} onAccountDeleted={onAccountDeleted}/>}
      {current === 'users' && <UsersPage currentUserId={user?.id ?? null}/>}
      {current === 'models' && <>
        <PromptEditor first title="System prompt" path="/v1/ugen/system-prompt" maxLength={8000}
          description="Added ahead of every request to every model, from the dashboard and the API alike, before each person's own custom instructions. Use it for house rules such as language, tone or topics to decline. Avoid personal data here: it is redacted like any other text."
          placeholder={'e.g. You are the assistant of Acme Clinic. Answer in Traditional Chinese and never give a diagnosis.'}/>
        <ModelsPage onSaved={onModelsSaved}/>
      </>}
      {current === 'privacy' && <PrivacyPage/>}
      {current === 'keys' && <ApiKeysPage/>}
    </section>
  </div>;
}

export default SettingsDialog;
