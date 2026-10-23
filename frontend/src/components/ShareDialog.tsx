import { useEffect, useState } from 'react';
import { apiFetch, errorMessage, type User } from '../api';
import { copyText } from '../clipboard';
import type { Conversation } from '../types';

type Share = { id: string; conversation_id: string; visibility: 'accounts' | 'public'; updated_at: number };
type Status = { ok: boolean; message: string } | null;

export const shareUrl = (id: string) => `${window.location.origin}${window.location.pathname}?share=${encodeURIComponent(id)}`;
const parseNames = (text: string) => [...new Set(text.split(/[\s,]+/).map(name => name.trim()).filter(Boolean))];

type Props = {
  conversation: Conversation; user: User;
  onClose: () => void;
  /** The conversation became a shared chat (the gateway gave it a new id). */
  onSharedChat: (chatId: string) => void;
  /** Membership changed, or the viewer left: fetch the latest state. */
  onMembershipChanged: () => void;
};

/** Share a conversation as a read-only link, or chat in it together. */
function ShareDialog({ conversation, user, onClose, onSharedChat, onMembershipChanged }: Props) {
  const [share, setShare] = useState<Share | null | undefined>(undefined);
  const [visibility, setVisibility] = useState<Share['visibility']>('accounts');
  const [linkStatus, setLinkStatus] = useState<Status>(null);
  const [names, setNames] = useState('');
  const [chatStatus, setChatStatus] = useState<Status>(null);
  const [working, setWorking] = useState(false);
  const [copied, setCopied] = useState(false);
  const shared = conversation.shared;
  const owner = shared?.role === 'owner';

  useEffect(() => {
    apiFetch('/v1/ugen/shares')
      .then(async response => {
        if (!response.ok) { setShare(null); return; }
        const found = (await response.json() as { data: Share[] }).data.find(item => item.conversation_id === conversation.id) ?? null;
        setShare(found); if (found) setVisibility(found.visibility);
      })
      .catch(() => setShare(null));
  }, [conversation.id]);

  async function call(path: string, init: RequestInit, onStatus: (status: Status) => void): Promise<Response | null> {
    setWorking(true); onStatus(null);
    try {
      const response = await apiFetch(path, { headers: { 'Content-Type': 'application/json' }, ...init });
      if (!response.ok) { onStatus({ ok: false, message: await errorMessage(response) }); return null; }
      return response;
    } catch { onStatus({ ok: false, message: 'Cannot reach the gateway' }); return null; } finally { setWorking(false); }
  }

  async function saveLink(nextVisibility = visibility) {
    const response = await call('/v1/ugen/shares', { method: 'POST', body: JSON.stringify({ conversation, visibility: nextVisibility }) }, setLinkStatus);
    if (!response) return;
    const saved = await response.json() as Share;
    const created = !share;
    setShare(saved); setVisibility(saved.visibility);
    setLinkStatus({ ok: true, message: created ? 'Link created. It shows the conversation as it is now.' : 'Link updated with the current messages.' });
  }

  async function stopSharing() {
    if (!share || !await call(`/v1/ugen/shares/${share.id}`, { method: 'DELETE' }, setLinkStatus)) return;
    setShare(null); setLinkStatus({ ok: true, message: 'The link no longer works.' });
  }

  async function startSharedChat() {
    const members = parseNames(names).filter(name => name.toLowerCase() !== user.username.toLowerCase());
    if (!members.length) { setChatStatus({ ok: false, message: 'Enter at least one other username.' }); return; }
    const response = await call('/v1/ugen/shared-chats', { method: 'POST', body: JSON.stringify({ conversation, members }) }, setChatStatus);
    if (response) onSharedChat((await response.json() as { id: string }).id);
  }

  async function changeMembers(change: { add?: string[]; remove?: string[] }) {
    if (!shared || !await call(`/v1/ugen/shared-chats/${conversation.id}`, { method: 'PATCH', body: JSON.stringify(change) }, setChatStatus)) return;
    setNames(''); setChatStatus({ ok: true, message: 'Members updated.' }); onMembershipChanged();
  }

  return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="confirm-modal settings-modal share-modal" role="dialog" aria-modal="true" aria-labelledby="share-title">
      <div className="settings-head"><div><span className="eyebrow">SHARE</span><h2 id="share-title">{conversation.title}</h2></div>
        <button className="icon-button" onClick={onClose} aria-label="Close">×</button></div>

      <div className="settings-section first">
        <h3>Share a link</h3>
        <p>People who open the link get their own copy of the messages and answers, as they are when you create or update it, and can continue it. You will not see what they add, and they will not see what you add later. The conversation’s memory and routing details are not included.</p>
        <div className="segmented share-visibility" role="radiogroup" aria-label="Who can open the link">
          <button type="button" role="radio" aria-checked={visibility === 'accounts'} className={visibility === 'accounts' ? 'selected' : ''} onClick={() => { setVisibility('accounts'); if (share) void saveLink('accounts'); }}>People signed in here</button>
          <button type="button" role="radio" aria-checked={visibility === 'public'} className={visibility === 'public' ? 'selected' : ''} onClick={() => { setVisibility('public'); if (share) void saveLink('public'); }}>Anyone with the link</button>
        </div>
        {visibility === 'public' && <small className="settings-hint">Anyone who gets the link can read it without signing in (continuing it needs an account here). Check the conversation for personal data first.</small>}
        {share === undefined ? <p>Loading…</p> : share
          ? <>
              <div className="callback-row"><code>{shareUrl(share.id)}</code>
                <button type="button" className="text-action" onClick={async () => setCopied(await copyText(shareUrl(share.id)))}>{copied ? 'Copied' : 'Copy'}</button></div>
              <div className="probe-row">
                <button type="button" className="cancel-button" onClick={() => void saveLink()} disabled={working}>Update link</button>
                <button type="button" className="text-action danger" onClick={() => void stopSharing()} disabled={working}>Stop sharing</button>
              </div>
            </>
          : <div className="probe-row"><button type="button" className="send-button" onClick={() => void saveLink()} disabled={working || !conversation.turns.length}>Create link</button></div>}
        {linkStatus && <span className={`probe ${linkStatus.ok ? 'ok' : 'error'}`} role="status">{linkStatus.message}</span>}
      </div>

      <div className="settings-section">
        <h3>Chat together</h3>
        {shared
          ? <>
              <p>{owner ? 'Everyone here can read this conversation and keep chatting in it.' : `Shared by ${shared.owner}. Everyone here can read it and keep chatting in it.`}</p>
              <ul className="member-list">{shared.members.map(name => <li key={name}>
                <span>{name}{name === shared.owner ? ' · owner' : ''}{name.toLowerCase() === user.username.toLowerCase() ? ' (you)' : ''}</span>
                {owner && name !== shared.owner && <button type="button" className="text-action danger" onClick={() => void changeMembers({ remove: [name] })} disabled={working}>Remove</button>}
              </li>)}</ul>
              {owner && <div className="field-row">
                <input value={names} onChange={event => setNames(event.target.value)} placeholder="Add usernames, separated by commas" aria-label="Usernames to add"/>
                <button type="button" className="cancel-button" onClick={() => void changeMembers({ add: parseNames(names) })} disabled={working || !names.trim()}>Add</button>
              </div>}
            </>
          : <>
              <p>Invite people by username. They see this conversation in their list and can continue it; each message shows who sent it. Requests are analyzed and billed to whoever sends them.</p>
              <div className="field-row">
                <input value={names} onChange={event => setNames(event.target.value)} placeholder="Usernames, separated by commas" aria-label="Usernames to invite"/>
                <button type="button" className="send-button" onClick={() => void startSharedChat()} disabled={working || !names.trim()}>Start shared chat</button>
              </div>
            </>}
        {chatStatus && <span className={`probe ${chatStatus.ok ? 'ok' : 'error'}`} role="status">{chatStatus.message}</span>}
      </div>
    </section>
  </div>;
}

export default ShareDialog;
