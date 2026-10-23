import { useState } from 'react';
import type { User } from '../api';
import Avatar from './Avatar';
import type { SettingsPage } from './SettingsDialog';
import type { Conversation } from '../types';
import type { SyncStatus } from '../conversationStore';
import LogoMark from './LogoMark';

type Props = {
  conversations: Conversation[];
  syncStatus: SyncStatus;
  syncError: string;
  activeId: string | null;
  busy: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onClearAll: () => void;
  onClose: () => void;
  onOpenSettings: (page: SettingsPage) => void;
  user: User | null;
};

const SYNC_LABEL: Record<SyncStatus, string> = {
  local: 'history on this device', loading: 'loading history…', synced: 'synced across devices', saving: 'saving…', error: 'not saved, retrying',
};

function Sidebar({ conversations, syncStatus, syncError, activeId, busy, onNew, onSelect, onDelete, onRename, onClearAll, onClose, onOpenSettings, user }: Props) {
  const [query, setQuery] = useState('');
  const [renaming, setRenaming] = useState<{ id: string; title: string } | null>(null);
  const needle = query.trim().toLowerCase();
  // Search titles and message text.
  const shown = needle ? conversations.filter(conversation => conversation.title.toLowerCase().includes(needle)
    || conversation.turns.some(turn => turn.user.toLowerCase().includes(needle) || turn.result?.content.toLowerCase().includes(needle))) : conversations;
  const finishRename = () => { if (renaming?.title.trim()) onRename(renaming.id, renaming.title.trim()); setRenaming(null); };
  return <aside className="sidebar">
    <a className="brand" href="#" aria-label="UGen AI Gateway home"><span className="brand-mark"><LogoMark/></span><span>ugen<span className="brand-dot">.</span><small>AI GATEWAY</small></span></a>
    <button className="icon-button drawer-close" onClick={onClose} aria-label="Close conversation history">×</button>
    <div className="history-label"><span>Recent</span><span className="history-count">{conversations.length}</span>{conversations.length > 0 && <button className="text-action" onClick={onClearAll} disabled={busy}>Clear</button>}</div>
    {conversations.length > 0 && <label className="history-search"><span className="sr-only">Search conversations</span>
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="m20 20-4.5-4.5"/></svg>
      <input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search conversations" onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setQuery(''); } }}/>
    </label>}
    <nav className="history-list" aria-label="Conversation history">
      {syncStatus === 'loading' && !conversations.length && <div className="history-empty"><span className="spinner dark" aria-hidden="true"/><small>Loading your conversations…</small></div>}
      {needle && shown.length === 0 && <div className="history-empty"><b>No matches</b><small>Nothing in your conversations matches “{query}”.</small></div>}
      {conversations.length ? shown.map(conversation => {
        const messages = conversation.turns.reduce((count, turn) => count + (turn.result ? 2 : 1), 0);
        return <div key={conversation.id} className={`history-item ${conversation.id === activeId ? 'active' : ''}`}>
          {renaming?.id === conversation.id
            ? <form className="history-rename" onSubmit={event => { event.preventDefault(); finishRename(); }}>
                <input value={renaming.title} onChange={event => setRenaming({ ...renaming, title: event.target.value })} onBlur={finishRename} maxLength={80} autoFocus aria-label="Conversation title"
                  onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setRenaming(null); } }}/>
              </form>
            : <button className="history-select" title={conversation.title} onClick={() => onSelect(conversation.id)} onDoubleClick={() => setRenaming({ id: conversation.id, title: conversation.title })} disabled={busy}>
                <span className={`history-icon ${conversation.shared ? 'shared' : ''}`} title={conversation.shared ? `Shared with ${conversation.shared.members.join(', ')}` : undefined}>{conversation.shared ? '👥' : '▤'}</span><span className="history-copy"><b>{conversation.title}</b><small>{messages} {messages === 1 ? 'message' : 'messages'}</small></span>
              </button>}
          <button className="icon-button history-rename-button" aria-label={`Rename conversation: ${conversation.title}`} title="Rename" onClick={() => setRenaming({ id: conversation.id, title: conversation.title })} disabled={busy}>✎</button>
          <button className="icon-button history-delete" aria-label={`Delete conversation: ${conversation.title}`} title="Delete conversation" onClick={() => onDelete(conversation.id)} disabled={busy}>×</button>
        </div>;
      }) : syncStatus === 'loading' ? null : <div className="history-empty"><span>✳</span><b>No conversations yet</b><small>Your conversations will appear here.</small></div>}
    </nav>
    <div className="sidebar-bottom"><button className="new-chat" onClick={onNew} disabled={busy}><span>+</span> New conversation <kbd>⌘ K</kbd></button><div className="sidebar-footer">{user
      ? <button className="account-button" onClick={() => onOpenSettings('account')} title="Account settings"><Avatar user={user} className="small"/><span><b>{user.username}</b><small className={syncStatus === 'error' ? 'sync-error' : ''} title={syncError || undefined}>{user.is_admin ? 'Administrator' : 'Member'} · {SYNC_LABEL[syncStatus]}</small></span></button>
      : <span className="account-button static"><span className="sidebar-status-dot"/><span><b>Local privacy</b><small>History stays on this device</small></span></span>}
      {(!user || user.is_admin) && <button className="icon-button settings-button" onClick={() => onOpenSettings(user ? 'users' : 'models')} aria-label="Gateway settings" title="Gateway settings: users, models, privacy and API keys"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z"/><path d="M19.4 13.5a7.6 7.6 0 0 0 0-3l2-1.6-2-3.4-2.4 1a7.6 7.6 0 0 0-2.6-1.5L14 2.5h-4l-.4 2.5A7.6 7.6 0 0 0 7 6.5l-2.4-1-2 3.4 2 1.6a7.6 7.6 0 0 0 0 3l-2 1.6 2 3.4 2.4-1a7.6 7.6 0 0 0 2.6 1.5l.4 2.5h4l.4-2.5a7.6 7.6 0 0 0 2.6-1.5l2.4 1 2-3.4Z"/></svg></button>}
    </div></div>
  </aside>;
}
export default Sidebar;
