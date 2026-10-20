import type { Conversation } from '../types';

type Props = {
  conversations: Conversation[];
  activeId: string | null;
  busy: boolean;
  onNew: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onClearAll: () => void;
  onClose: () => void;
};

function Sidebar({ conversations, activeId, busy, onNew, onSelect, onDelete, onClearAll, onClose }: Props) {
  return <aside className="sidebar">
    <a className="brand" href="#" aria-label="UGen AI Gateway 首頁"><span className="brand-mark"><svg viewBox="0 0 40 40" aria-hidden="true"><path className="brand-gate" d="M11 27V15a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4v12"/><path className="brand-route" d="M20 30v-7m0 0-7-7m7 7 7-7"/><circle className="brand-node" cx="13" cy="16" r="2.3"/><circle className="brand-node" cx="27" cy="16" r="2.3"/><circle className="brand-core" cx="20" cy="30" r="2.2"/></svg></span><span>ugen<span className="brand-dot">.</span><small>AI GATEWAY</small></span></a>
    <button className="icon-button drawer-close" onClick={onClose} aria-label="關閉對話紀錄">×</button>
    <div className="history-label"><span>最近對話</span><span className="history-count">{conversations.length}</span>{conversations.length > 0 && <button className="text-action" onClick={onClearAll} disabled={busy}>清空</button>}</div>
    <nav className="history-list" aria-label="對話紀錄">
      {conversations.length ? conversations.map(conversation => {
        const messages = conversation.turns.reduce((count, turn) => count + (turn.result ? 2 : 1), 0);
        return <div key={conversation.id} className={`history-item ${conversation.id === activeId ? 'active' : ''}`}>
          <button className="history-select" title={conversation.title} onClick={() => onSelect(conversation.id)} disabled={busy}>
            <span className="history-icon">▤</span><span className="history-copy"><b>{conversation.title}</b><small>{messages} 則訊息</small></span>
          </button>
          <button className="icon-button history-delete" aria-label={`刪除對話：${conversation.title}`} title="刪除對話" onClick={() => onDelete(conversation.id)} disabled={busy}>×</button>
        </div>;
      }) : <div className="history-empty"><span>✳</span><b>還沒有對話</b><small>開始新對話後，紀錄會出現在這裡。</small></div>}
    </nav>
    <div className="sidebar-bottom"><button className="new-chat" onClick={onNew} disabled={busy}><span>＋</span> 開始新對話 <kbd>⌘ K</kbd></button><div className="sidebar-footer"><span className="sidebar-status-dot"/><span><b>本機隱私保護</b><small>紀錄僅儲存在這台裝置</small></span><span className="lock-icon">⌑</span></div></div>
  </aside>;
}
export default Sidebar;
