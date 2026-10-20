import { useEffect, useState } from 'react';
import PromptPanel from './components/PromptPanel';
import MonitorPanel from './components/MonitorPanel';
import RequestFlow from './components/RequestFlow';
import ResponsePanel from './components/ResponsePanel';
import Sidebar from './components/Sidebar';
import type { ChatResult, Conversation, ConversationTurn, GatewayEvent, Phase, Provider, ProviderSelection } from './types';

const STORAGE_KEY = 'ugen-gateway-conversations-v1';
const PROVIDER_SELECTION_KEY = 'ugen-gateway-provider-selection';
type View = 'chat' | 'monitor';
type DeleteAction = { type: 'conversation'; conversationId: string } | { type: 'turn'; conversationId: string; turnId: string } | { type: 'all' };
function loadConversations(): Conversation[] {
  try {
    const stored: unknown = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '[]');
    return Array.isArray(stored) ? stored as Conversation[] : [];
  } catch { return []; }
}
function loadProviderSelection(): ProviderSelection {
  try {
    const stored = localStorage.getItem(PROVIDER_SELECTION_KEY);
    return stored === 'local' || stored === 'cloud' ? stored : 'auto';
  } catch { return 'auto'; }
}

function App() {
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [provider, setProvider] = useState<Provider | null>(null);
  const [model, setModel] = useState('');
  const [pii, setPii] = useState<{ found: boolean; types: string[] } | null>(null);
  const [configuredModels, setConfiguredModels] = useState({ local: '', cloud: '' });
  const [modelsStatus, setModelsStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const [error, setError] = useState('');
  const [deleteAction, setDeleteAction] = useState<DeleteAction | null>(null);
  const [view, setView] = useState<View>('chat');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [providerSelection, setProviderSelection] = useState<ProviderSelection>(loadProviderSelection);
  const busy = !['idle', 'complete', 'failed'].includes(phase);
  const activeConversation = conversations.find(item => item.id === activeId) ?? null;
  const turns = activeConversation?.turns ?? [];
  const apiUrl = import.meta.env.VITE_GATEWAY_URL || '/api';

  useEffect(() => { localStorage.setItem(STORAGE_KEY, JSON.stringify(conversations)); }, [conversations]);
  useEffect(() => { try { localStorage.setItem(PROVIDER_SELECTION_KEY, providerSelection); } catch { /* selection simply resets next visit */ } }, [providerSelection]);
  useEffect(() => {
    let active = true;
    const loadModels = async () => {
      const baseUrl = apiUrl.replace(/\/$/, '').replace(/\/v1$/, '');
      const primary = `${baseUrl}/v1/models`;
      const endpoints = [...new Set([primary, '/api/v1/models', '/v1/models'])];
      let data: { local_model: string; cloud_model: string } | undefined;
      for (const endpoint of endpoints) {
        try {
          const response = await fetch(endpoint, { headers: { Accept: 'application/json' } });
          if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) continue;
          const payload: unknown = await response.json();
          if (!payload || typeof payload !== 'object') continue;
          const candidate = payload as { local_model?: unknown; cloud_model?: unknown };
          if (typeof candidate.local_model !== 'string' || typeof candidate.cloud_model !== 'string') continue;
          data = { local_model: candidate.local_model, cloud_model: candidate.cloud_model };
          break;
        } catch {
          // Try the next configured route when a proxy or fallback route is unavailable.
        }
      }
      if (!active) return;
      if (!data) { setModelsStatus('error'); return; }
      setConfiguredModels({ local: data.local_model.trim(), cloud: data.cloud_model.trim() });
      setModelsStatus('loaded');
    };
    void loadModels();
    return () => { active = false; };
  }, [apiUrl]);
  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); newConversation(); }
      if (event.key === 'Escape') { setDeleteAction(null); setDrawerOpen(false); }
    };
    window.addEventListener('keydown', handleShortcut);
    return () => window.removeEventListener('keydown', handleShortcut);
  }, [busy]);

  function selectConversation(id: string) {
    if (busy) return;
    const selected = conversations.find(item => item.id === id);
    const latest = selected ? [...selected.turns].reverse().find(turn => turn.result)?.result : undefined;
    setActiveId(id); setPrompt(''); setError(''); setPhase(latest ? 'complete' : 'idle'); setView('chat'); setDrawerOpen(false);
    setProvider(latest ? latest.provider === 'cloud' ? 'cloud' : 'local' : null);
    setModel(latest?.model ?? '');
    setPii(latest ? { found: latest.routing.contains_personal_data, types: latest.routing.personal_data_types ?? [] } : null);
  }
  function newConversation() {
    if (busy) return;
    setActiveId(null); setPrompt(''); setError(''); setPhase('idle'); setProvider(null); setModel(''); setPii(null); setView('chat'); setDrawerOpen(false);
  }
  function restoreConversation(conversation: Conversation | undefined) {
    const latest = conversation ? [...conversation.turns].reverse().find(turn => turn.result)?.result : undefined;
    setActiveId(conversation?.id ?? null); setPrompt(''); setError(''); setPhase(latest ? 'complete' : 'idle');
    setProvider(latest ? latest.provider === 'cloud' ? 'cloud' : 'local' : null); setModel(latest?.model ?? '');
    setPii(latest ? { found: latest.routing.contains_personal_data, types: latest.routing.personal_data_types ?? [] } : null);
  }
  function performDelete() {
    if (!deleteAction || busy) return;
    if (deleteAction.type === 'all') {
      setConversations([]); restoreConversation(undefined);
    } else if (deleteAction.type === 'conversation') {
      const remaining = conversations.filter(item => item.id !== deleteAction.conversationId);
      setConversations(remaining);
      if (activeId === deleteAction.conversationId) restoreConversation(remaining[0]);
    } else {
      const target = conversations.find(item => item.id === deleteAction.conversationId);
      if (target) {
        const turnsAfterDelete = target.turns.filter(turn => turn.id !== deleteAction.turnId);
        if (turnsAfterDelete.length) {
          const updated = { ...target, turns: turnsAfterDelete, updatedAt: Date.now() };
          setConversations(current => current.map(item => item.id === target.id ? updated : item));
          if (activeId === target.id) restoreConversation(updated);
        } else {
          const remaining = conversations.filter(item => item.id !== target.id);
          setConversations(remaining);
          if (activeId === target.id) restoreConversation(remaining[0]);
        }
      }
    }
    setDeleteAction(null);
  }
  function updateTurn(conversationId: string, turnId: string, result?: ChatResult) {
    setConversations(current => current.map(conversation => conversation.id !== conversationId ? conversation : {
      ...conversation, updatedAt: Date.now(), turns: conversation.turns.map(turn => turn.id === turnId ? { ...turn, ...(result ? { result } : {}) } : turn),
    }));
  }

  async function sendPrompt() {
    const userText = prompt.trim();
    if (!userText || busy) return;
    const existing = activeConversation;
    const conversationId = existing?.id ?? crypto.randomUUID();
    const turnId = crypto.randomUUID();
    const conversation: Conversation = existing ?? { id: conversationId, title: userText.slice(0, 36), updatedAt: Date.now(), turns: [] };
    const turn: ConversationTurn = { id: turnId, user: userText };
    const nextTurns = [...conversation.turns, turn];
    const messages = nextTurns.flatMap(item => [
      { role: 'user' as const, content: item.user },
      ...(item.result ? [{ role: 'assistant' as const, content: item.result.content }] : []),
    ]).slice(-62);
    const totalChars = messages.reduce((count, message) => count + message.content.length, 0);
    if (totalChars > 32000) { setError('這段對話累積內容已超過 32,000 字，請開始新對話後再試。'); return; }

    setConversations(current => [
      { ...conversation, updatedAt: Date.now(), turns: nextTurns },
      ...current.filter(item => item.id !== conversationId),
    ]);
    setActiveId(conversationId); setPrompt(''); setPhase('sending'); setProvider(null); setModel(''); setPii(null); setError('');
    try {
      const response = await fetch(`${apiUrl.replace(/\/$/, '')}/v1/chat/completions/stream`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ messages, provider_selection: providerSelection, temperature: 0.2 }),
      });
      if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(data.detail ?? `請求失敗（HTTP ${response.status}）`); }
      if (!response.body) throw new Error('伺服器沒有回傳即時進度資料');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      const received: { result: ChatResult | null } = { result: null }; let streamError = '';
      const onEvent = (event: GatewayEvent) => {
        if (event.type === 'analysis_started') setPhase('analyzing');
        if (event.type === 'analysis_completed') { setPhase('analyzed'); setPii({ found: Boolean(event.contains_personal_data), types: Array.isArray(event.personal_data_types) ? event.personal_data_types.map(String) : [] }); }
        if (event.type === 'routed') { setProvider(event.provider === 'cloud' ? 'cloud' : 'local'); setPhase('routing'); }
        if (event.type === 'generation_started') { setProvider(event.provider === 'cloud' ? 'cloud' : 'local'); setModel(String(event.model ?? '')); setPhase('generating'); }
        if (event.type === 'result') received.result = event.data as ChatResult;
        if (event.type === 'error') streamError = String(event.detail ?? '模型請求失敗');
      };
      while (true) {
        const { value, done } = await reader.read(); buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n'); buffer = lines.pop() ?? '';
        for (const line of lines) if (line.trim()) onEvent(JSON.parse(line) as GatewayEvent);
        if (done) break;
      }
      if (buffer.trim()) onEvent(JSON.parse(buffer) as GatewayEvent);
      if (streamError) throw new Error(streamError);
      const finalResult = received.result;
      if (!finalResult) throw new Error('Gateway 已結束連線，但沒有收到模型回應');
      updateTurn(conversationId, turnId, finalResult);
      setProvider(finalResult.provider === 'cloud' ? 'cloud' : 'local'); setModel(finalResult.model);
      setPii({ found: finalResult.routing.contains_personal_data, types: finalResult.routing.personal_data_types ?? [] }); setPhase('complete');
    } catch (cause) {
      setConversations(current => current.map(item => item.id !== conversationId ? item : { ...item, turns: item.turns.filter(existingTurn => existingTurn.id !== turnId) }));
      setError(cause instanceof Error ? cause.message : '無法連線至 Gateway'); setPrompt(userText); setPhase('failed');
    }
  }

  const flowStatus = busy ? 'active' : phase === 'failed' ? 'failed' : phase === 'complete' ? 'complete' : '';
  return <div className={`app-layout view-${view} ${drawerOpen ? 'drawer-open' : ''}`}>
    <header className="mobile-bar">
      <button className="icon-button menu-button" onClick={() => setDrawerOpen(true)} aria-label="開啟對話紀錄" aria-expanded={drawerOpen}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg></button>
      <span className="mobile-title">ugen<span className="brand-dot">.</span><small>AI GATEWAY</small></span>
      <button className="icon-button new-chat-mini" onClick={newConversation} disabled={busy} aria-label="開始新對話" title="開始新對話">＋</button>
    </header>
    <Sidebar conversations={conversations} activeId={activeId} busy={busy} onNew={newConversation} onSelect={selectConversation}
      onDelete={id => setDeleteAction({ type: 'conversation', conversationId: id })} onClearAll={() => setDeleteAction({ type: 'all' })} onClose={() => setDrawerOpen(false)}/>
    <div className="drawer-scrim" onClick={() => setDrawerOpen(false)} aria-hidden="true"/>
    <nav className="view-tabs" role="tablist" aria-label="切換檢視">
      <button role="tab" aria-selected={view === 'chat'} className={view === 'chat' ? 'selected' : ''} onClick={() => setView('chat')}><i aria-hidden="true">#</i>對話{turns.length > 0 && <small>{turns.length}</small>}</button>
      <button role="tab" aria-selected={view === 'monitor'} className={view === 'monitor' ? 'selected' : ''} onClick={() => setView('monitor')}><i aria-hidden="true">⇄</i>流程監控<span className={`tab-status ${flowStatus}`}/></button>
    </nav>
    <main className="workspace-area"><section className="workspace-title" aria-label="工作區標題"><span className="eyebrow">SECURE MODEL ROUTING</span><h1>讓每個請求，<span>都走對路。</span></h1><p>本機分析後，再交給合適的模型處理。</p></section><section className="workspace-content"><RequestFlow phase={phase} provider={provider} providerSelection={providerSelection} model={model} localModel={configuredModels.local} cloudModel={configuredModels.cloud} modelsStatus={modelsStatus} pii={pii}/><MonitorPanel conversations={conversations} phase={phase}/></section></main>
    <section className="chat-column"><ResponsePanel turns={turns} busy={busy} onDeleteTurn={turnId => activeId && setDeleteAction({ type: 'turn', conversationId: activeId, turnId })}/><PromptPanel prompt={prompt} busy={busy} error={error} providerSelection={providerSelection} cloudAvailable={modelsStatus !== 'loaded' || Boolean(configuredModels.cloud)} onProviderSelectionChange={setProviderSelection} onPromptChange={setPrompt} onSubmit={sendPrompt} onClear={() => setPrompt('')}/></section>
    {deleteAction && <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setDeleteAction(null); }}><section className="confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-description"><span className="modal-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 7h15M9 7V4.8h6V7m2.5 0-.8 12h-9.4l-.8-12m4 3.5v5m3-5v5"/></svg></span><span className="eyebrow">DELETE CONFIRMATION</span><h2 id="delete-title">{deleteAction.type === 'all' ? '清空所有對話紀錄？' : deleteAction.type === 'turn' ? '刪除這一輪對話？' : '刪除這段對話？'}</h2><p id="delete-description">{deleteAction.type === 'all' ? '所有保存在此裝置的對話都會永久刪除。' : '這項操作會從此裝置的對話紀錄中移除內容。'}</p><div className="modal-actions"><button className="cancel-button" onClick={() => setDeleteAction(null)}>取消</button><button className="danger-button" onClick={performDelete}>刪除</button></div></section></div>}
  </div>;
}
export default App;
