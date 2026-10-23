import { useCallback, useEffect, useState } from 'react';
import { apiFetch, errorMessage, type User } from '../api';
import { copyText } from '../clipboard';
import Avatar from './Avatar';
import NumberStepper from './NumberStepper';
import SsoSection from './SsoSection';

type Account = User & {
  created_at: number; last_login_at: number | null; disabled: boolean; monthly_budget_usd: number | null;
  month_requests: number; month_cost_usd: number; month_cloud: number;
};

const usd = (value: number) => `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`;
const date = (seconds: number | null) => seconds ? new Intl.DateTimeFormat('en-US', { dateStyle: 'medium' }).format(seconds * 1000) : 'Never';
const budgetStep = (current: number, direction: 1 | -1) => (direction === 1 ? current < 10 : current <= 10) ? 0.1 : 1;

function UsersPage({ currentUserId }: { currentUserId: string | null }) {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [allowSignUp, setAllowSignUp] = useState<boolean | null>(null);
  const [error, setError] = useState('');
  const [budgets, setBudgets] = useState<Record<string, string>>({});
  const [resetFor, setResetFor] = useState<{ username: string; password: string; copied: boolean } | null>(null);
  const [working, setWorking] = useState('');

  const load = useCallback(async () => {
    try {
      const [users, settings] = await Promise.all([apiFetch('/v1/ugen/users'), apiFetch('/v1/ugen/settings')]);
      if (!users.ok) { setError(await errorMessage(users)); return; }
      const data = (await users.json() as { data: Account[] }).data;
      setAccounts(data);
      setBudgets(Object.fromEntries(data.map(account => [account.id, account.monthly_budget_usd === null ? '' : String(account.monthly_budget_usd)])));
      if (settings.ok) setAllowSignUp((await settings.json() as { allow_registration: boolean }).allow_registration);
    } catch { setError('Cannot reach the gateway'); }
  }, []);

  useEffect(() => { void load(); }, [load]);

  async function act(id: string, request: Promise<Response>) {
    setWorking(id); setError('');
    try {
      const response = await request;
      if (!response.ok) { setError(await errorMessage(response)); return null; }
      return response;
    } catch { setError('Cannot reach the gateway'); return null; } finally { setWorking(''); }
  }

  const patch = async (account: Account, body: Record<string, unknown>) => {
    if (await act(account.id, apiFetch(`/v1/ugen/users/${account.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }))) await load();
  };

  async function resetPassword(account: Account) {
    if (!window.confirm(`Reset ${account.username}'s password? They will be signed out everywhere.`)) return;
    const response = await act(account.id, apiFetch(`/v1/ugen/users/${account.id}/reset-password`, { method: 'POST' }));
    if (response) setResetFor({ username: account.username, password: (await response.json() as { password: string }).password, copied: false });
  }

  async function remove(account: Account) {
    if (!window.confirm(`Delete ${account.username}? Their sessions, usage records and feedback are removed. This cannot be undone.`)) return;
    if (await act(account.id, apiFetch(`/v1/ugen/users/${account.id}`, { method: 'DELETE' }))) await load();
  }

  async function toggleSignUp(allow: boolean) {
    setAllowSignUp(allow);
    const response = await act('settings', apiFetch('/v1/ugen/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ allow_registration: allow }) }));
    if (!response) setAllowSignUp(!allow);
  }

  return <>
    <div className="settings-section first">
      <div className="section-head"><h3>Sign-up</h3>
        {allowSignUp !== null && <label className="switch"><input type="checkbox" checked={allowSignUp} onChange={event => void toggleSignUp(event.target.checked)} disabled={working === 'settings'}/><span aria-hidden="true"/>{allowSignUp ? 'Open' : 'Closed'}</label>}
      </div>
      <p>When closed, only existing accounts can sign in. Close it once everyone who needs an account has one.</p>
    </div>

    <SsoSection/>

    <div className="settings-section">
      <h3>Accounts</h3>
      <p>Sorted by this month’s cloud cost. A budget keeps that person on the local model once their cloud spend this month reaches it.</p>
      {resetFor && <div className="created-key" role="status">
        <b>New password for {resetFor.username}, shown only once:</b>
        <code>{resetFor.password}</code>
        <span className="profile-actions">
          <button className="cancel-button" type="button" onClick={async () => setResetFor({ ...resetFor, copied: await copyText(resetFor.password) })}>{resetFor.copied ? 'Copied' : 'Copy'}</button>
          <button className="text-action" type="button" onClick={() => setResetFor(null)}>Done</button>
        </span>
      </div>}
      {error && <div className="error-box" role="alert"><span>{error}</span></div>}
      {accounts === null ? <p className="key-empty">Loading…</p> : <ul className="user-list">{accounts.map(account => {
        const self = account.id === currentUserId;
        const budget = budgets[account.id] ?? '';
        const savedBudget = account.monthly_budget_usd === null ? '' : String(account.monthly_budget_usd);
        return <li key={account.id} className={account.disabled ? 'disabled' : ''}>
          <div className="user-main">
            <Avatar user={account} className="small"/>
            <span className="user-name"><b>{account.username}{self && ' (you)'}</b>
              <small>{account.is_admin ? 'Administrator' : 'Member'}{account.sso ? ' · SSO' : ''}{account.disabled ? ' · disabled' : ''} · last sign-in {date(account.last_login_at)}</small></span>
            <span className="user-usage"><b>{usd(account.month_cost_usd)}</b><small>{account.month_requests} req · {account.month_cloud} cloud</small></span>
          </div>
          <div className="user-controls">
            <span className="user-budget">
              <NumberStepper value={budget} onChange={value => setBudgets(current => ({ ...current, [account.id]: value }))} step={budgetStep} min={0} decimals={2} placeholder="No budget" label={`Monthly budget for ${account.username}`}/>
              {budget !== savedBudget && <button className="cancel-button" type="button" disabled={working === account.id}
                onClick={() => void patch(account, { monthly_budget_usd: budget === '' ? null : Number(budget) })}>Save</button>}
            </span>
            {!self && <span className="user-actions">
              <button className="text-action" disabled={working === account.id} onClick={() => void patch(account, { is_admin: !account.is_admin })}>{account.is_admin ? 'Remove admin' : 'Make admin'}</button>
              <button className="text-action" disabled={working === account.id} onClick={() => void patch(account, { disabled: !account.disabled })}>{account.disabled ? 'Enable' : 'Disable'}</button>
              <button className="text-action" disabled={working === account.id} onClick={() => void resetPassword(account)}>Reset password</button>
              <button className="text-action danger" disabled={working === account.id} onClick={() => void remove(account)}>Delete</button>
            </span>}
          </div>
        </li>;
      })}</ul>}
    </div>
  </>;
}

export default UsersPage;
