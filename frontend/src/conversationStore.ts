import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch, apiUrl, errorMessage, loadSession, type User } from './api';
import type { Conversation } from './types';

/**
 * Conversation history. With an account it lives on the gateway (encrypted,
 * shared by every browser the person signs in to): changes are saved shortly
 * after they happen, and other devices' changes are fetched when the window
 * regains focus and every half minute. Without accounts it stays in this
 * browser's storage.
 */

const STORAGE_KEY = 'ugen-gateway-conversations-v1';
const SAVE_DELAY_MS = 800;
const REFRESH_MS = 30_000;
// While the account is in a shared chat, others' messages arrive this often.
const SHARED_REFRESH_MS = 5_000;
const RETRY_MS = 10_000;

export type SyncStatus = 'local' | 'loading' | 'synced' | 'saving' | 'error';
type Listing = { conversations: Conversation[]; ids: string[]; server_time: number; unreadable: number };

const byRecent = (items: Conversation[]) => [...items].sort((a, b) => b.updatedAt - a.updatedAt);

function readLocal(key: string): Conversation[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(key) ?? '[]');
    return Array.isArray(stored) ? stored as Conversation[] : [];
  } catch { return []; }
}

/** History saved in this browser before it moved to the gateway: this
 * account's own, plus any from before accounts existed. */
function localHistory(user: User): { items: Conversation[]; keys: string[] } {
  const keys = [`${STORAGE_KEY}:${user.id}`, STORAGE_KEY];
  return { items: keys.flatMap(readLocal), keys };
}

export function useConversations(user: User | null, busy: boolean) {
  const account = user !== null;
  const [conversations, setConversations] = useState<Conversation[]>(() => account ? [] : readLocal(STORAGE_KEY));
  const [status, setStatus] = useState<SyncStatus>(account ? 'loading' : 'local');
  const [syncError, setSyncError] = useState('');
  const latest = useRef(conversations);
  latest.current = conversations;
  const busyRef = useRef(busy);
  busyRef.current = busy;
  // What the gateway holds, by object identity: a conversation whose object
  // differs from this has unsaved changes.
  const synced = useRef(new Map<string, Conversation>());
  const since = useRef<number | null>(null);
  const saving = useRef(false);
  const saveAgain = useRef(false);
  const saveTimer = useRef<number | undefined>(undefined);
  const retryTimer = useRef<number | undefined>(undefined);
  const migration = useRef<string[]>([]);

  const changes = useCallback(() => {
    const current = latest.current;
    const ids = new Set(current.map(item => item.id));
    return {
      save: current.filter(item => synced.current.get(item.id) !== item),
      remove: [...synced.current.keys()].filter(id => !ids.has(id)),
    };
  }, []);

  const pull = useCallback(async () => {
    if (!account || saving.current) return;
    const query = since.current === null ? '' : `?since=${since.current}`;
    try {
      const response = await apiFetch(`/v1/ugen/conversations${query}`);
      if (!response.ok) throw new Error(await errorMessage(response));
      const listing = await response.json() as Listing;
      const first = since.current === null;
      since.current = listing.server_time;
      const current = new Map(latest.current.map(item => [item.id, item]));
      for (const remote of listing.conversations) {
        const local = current.get(remote.id);
        const base = synced.current.get(remote.id);
        const locallyDeleted = base !== undefined && local === undefined;
        // Keep local edits; a conversation only in this browser (from before
        // sync) wins when it is newer.
        const keepLocal = local !== undefined && local !== base && (base !== undefined || local.updatedAt > remote.updatedAt);
        synced.current.set(remote.id, remote);
        if (!locallyDeleted && !keepLocal) current.set(remote.id, remote);
      }
      // Deleted on another device, and not edited here since.
      const remaining = new Set(listing.ids);
      for (const [id, base] of synced.current) {
        if (remaining.has(id)) continue;
        if (current.get(id) === base) current.delete(id);
        synced.current.delete(id);
      }
      if (first || listing.conversations.length || current.size !== latest.current.length) {
        const next = byRecent([...current.values()]);
        // Update the ref now: a save right after this must see the merged
        // list, not the one React has yet to render.
        latest.current = next;
        setConversations(next);
      }
      setStatus(current => current === 'loading' ? 'synced' : current);
      if (listing.unreadable) setSyncError(`${listing.unreadable} saved conversation(s) cannot be decrypted; the gateway's secret key may have changed.`);
    } catch (error) {
      if (since.current === null) { setStatus('error'); setSyncError(error instanceof Error ? error.message : 'Cannot reach the gateway'); }
    }
  }, [account]);

  const push = useCallback(async () => {
    window.clearTimeout(saveTimer.current);
    if (!account || since.current === null) return;
    if (saving.current) { saveAgain.current = true; return; }
    const { save, remove } = changes();
    if (!save.length && !remove.length) return;
    // Shared chats merge turn by turn on the server: say which turns were
    // removed here, or the merge would bring them back.
    const removedTurns: Record<string, string[]> = {};
    for (const item of save) {
      const base = synced.current.get(item.id);
      if (!item.shared || !base) continue;
      const kept = new Set(item.turns.map(turn => turn.id));
      const gone = base.turns.filter(turn => !kept.has(turn.id)).map(turn => turn.id);
      if (gone.length) removedTurns[item.id] = gone;
    }
    saving.current = true; setStatus('saving');
    try {
      const response = await apiFetch('/v1/ugen/conversations/sync', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ save, delete: remove, removed_turns: removedTurns }),
      });
      if (!response.ok) throw new Error(await errorMessage(response));
      for (const item of save) synced.current.set(item.id, item);
      for (const id of remove) synced.current.delete(id);
      setStatus('synced'); setSyncError('');
      if (migration.current.length && !changes().save.length) {
        for (const key of migration.current) { try { localStorage.removeItem(key); } catch { /* already gone */ } }
        migration.current = [];
      }
    } catch (error) {
      setStatus('error'); setSyncError(error instanceof Error ? error.message : 'Cannot reach the gateway');
      window.clearTimeout(retryTimer.current);
      retryTimer.current = window.setTimeout(() => void push(), RETRY_MS);
    } finally {
      saving.current = false;
      if (saveAgain.current) { saveAgain.current = false; void push(); }
    }
  }, [account, changes]);

  // First load, with this browser's older history merged in and uploaded.
  useEffect(() => {
    if (!user) return;
    const { items, keys } = localHistory(user);
    migration.current = items.length ? keys : [];
    if (items.length) setConversations(byRecent(items));
    void pull().then(() => { if (since.current !== null) void push(); });
  }, [user?.id]);

  // Save shortly after each change.
  useEffect(() => {
    if (!account) {
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations)); } catch { /* history lasts for this page only */ }
      return;
    }
    if (since.current === null) return;
    window.clearTimeout(saveTimer.current);
    saveTimer.current = window.setTimeout(() => void push(), SAVE_DELAY_MS);
  }, [account, conversations, push]);

  // Fetch other devices' changes while nothing here is unsaved.
  useEffect(() => {
    if (!account) return;
    const refresh = () => {
      if (document.visibilityState !== 'visible' || busyRef.current) return;
      const { save, remove } = changes();
      if (since.current === null) void pull().then(() => void push());
      else if (!save.length && !remove.length) void pull();
    };
    let ticks = 0;
    const timer = window.setInterval(() => {
      ticks += 1;
      const shared = latest.current.some(item => item.shared);
      if (shared || ticks * SHARED_REFRESH_MS >= REFRESH_MS) { ticks = 0; refresh(); }
    }, SHARED_REFRESH_MS);
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    // Leaving the page: send what is still waiting, if it fits a keepalive request.
    const leave = () => {
      const { save, remove } = changes();
      const token = loadSession()?.token;
      if ((!save.length && !remove.length) || !token) return;
      try {
        void fetch(`${apiUrl}/v1/ugen/conversations/sync`, {
          method: 'POST', keepalive: true,
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
          body: JSON.stringify({ save, delete: remove }),
        }).catch(() => undefined);
      } catch { /* too large for keepalive; saved on the next visit instead */ }
    };
    window.addEventListener('pagehide', leave);
    return () => {
      window.clearInterval(timer); window.clearTimeout(saveTimer.current); window.clearTimeout(retryTimer.current);
      window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh);
      window.removeEventListener('pagehide', leave);
    };
  }, [account, changes, pull, push]);

  /** Save pending changes now, e.g. before signing out. */
  const flush = useCallback(async () => { if (account) await push(); }, [account, push]);
  /** Save, then fetch changes now (after creating or leaving a shared chat). */
  const refreshNow = useCallback(async () => { if (account) { await push(); await pull(); } }, [account, push, pull]);

  return { conversations, setConversations, status, syncError, flush, refreshNow };
}

/** Forget this browser's copy of an account's history. */
export function forgetLocalHistory(user: User | null): void {
  try { localStorage.removeItem(user ? `${STORAGE_KEY}:${user.id}` : STORAGE_KEY); } catch { /* nothing saved */ }
}
