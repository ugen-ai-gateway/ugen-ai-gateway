import { useEffect, useRef, useState } from 'react';
import PromptPanel from './components/PromptPanel';
import MonitorPanel from './components/MonitorPanel';
import RequestFlow from './components/RequestFlow';
import ResponsePanel from './components/ResponsePanel';
import SettingsDialog, { type SettingsPage } from './components/SettingsDialog';
import Footer from './components/Footer';
import ShareDialog from './components/ShareDialog';
import { IMPORT_STORAGE } from './components/SharedView';
import Sidebar from './components/Sidebar';
import { apiFetch, errorMessage, type User } from './api';
import { forgetLocalHistory, useConversations } from './conversationStore';
import { downloadConversation } from './exportConversation';
import { FOLD_CHUNK, requestMessages, size, turnMessages, turnsToRemember } from './memory';
import type { ChatResult, Conversation, ConversationTurn, GatewayEvent, AnalysisSource, Phase, PiiState, Provider, ProviderSelection } from './types';

// crypto.randomUUID only exists on secure origins (HTTPS or localhost); getRandomValues works everywhere.
const newId = () => crypto.randomUUID?.() ?? Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');

const PROVIDER_SELECTION_KEY = 'ugen-gateway-provider-selection';
const STREAM_FRAME_MS = 50;
type View = 'chat' | 'monitor';
type DeleteAction = { type: 'conversation'; conversationId: string } | { type: 'turn'; conversationId: string; turnId: string } | { type: 'all' };
function isSource(value: unknown): value is AnalysisSource {
  return value === 'llm' || value === 'rules' || value === 'cache';
}
function piiFromResult(result: ChatResult): PiiState {
  return { found: result.routing.contains_personal_data, types: result.routing.personal_data_types ?? [], redacted: result.routing.redacted_values ?? 0 };
}
function loadProviderSelection(): ProviderSelection {
  try {
    const stored = localStorage.getItem(PROVIDER_SELECTION_KEY);
    return stored === 'local' || stored === 'cloud' ? stored : 'auto';
  } catch { return 'auto'; }
}

type Notice = { ok: boolean; message: string };
type Props = { user: User | null; onSignOut: () => void; onAccountDeleted: () => void; onUserChange: (user: User) => void; notice?: Notice | null; onDismissNotice?: () => void };

function App({ user, onSignOut, onAccountDeleted, onUserChange, notice, onDismissNotice }: Props) {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [prompt, setPrompt] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [provider, setProvider] = useState<Provider | null>(null);
  const [model, setModel] = useState('');
  const [streamingText, setStreamingText] = useState('');
  const [statsVersion, setStatsVersion] = useState(0);
  const [pii, setPii] = useState<PiiState | null>(null);
  const [analysisSource, setAnalysisSource] = useState<AnalysisSource | null>(null);
  const [fallbackFrom, setFallbackFrom] = useState<Provider | null>(null);
  const [injectionSignals, setInjectionSignals] = useState<string[]>([]);
  const [configuredModels, setConfiguredModels] = useState({ local: '', cloud: '' });
  // False only once the gateway reports that no local model is set up yet.
  const [modelsReady, setModelsReady] = useState(true);
  const [modelsStatus, setModelsStatus] = useState<'loading' | 'loaded' | 'error'>('loading');
  const [error, setError] = useState('');
  const [deleteAction, setDeleteAction] = useState<DeleteAction | null>(null);
  const [view, setView] = useState<View>('chat');
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [providerSelection, setProviderSelection] = useState<ProviderSelection>(loadProviderSelection);
  const busy = !['idle', 'complete', 'failed'].includes(phase);
  const { conversations, setConversations, status: syncStatus, syncError, flush, refreshNow } = useConversations(user, busy);
  const [shareOpen, setShareOpen] = useState(false);
  const activeConversation = conversations.find(item => item.id === activeId) ?? null;
  const turns = activeConversation?.turns ?? [];
  const [settingsPage, setSettingsPage] = useState<SettingsPage | null>(null);
  const [modelsVersion, setModelsVersion] = useState(0);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => { try { localStorage.setItem(PROVIDER_SELECTION_KEY, providerSelection); } catch { /* selection simply resets next visit */ } }, [providerSelection]);
  // A copy of someone's share link to continue, added once history has
  // loaded so opening the same link again reuses the earlier copy.
  const [pendingCopy] = useState<Conversation | null>(() => {
    try {
      const stored = sessionStorage.getItem(IMPORT_STORAGE);
      sessionStorage.removeItem(IMPORT_STORAGE);
      const copy = stored ? JSON.parse(stored) as Conversation : null;
      return copy?.id && Array.isArray(copy.turns) ? copy : null;
    } catch { return null; }
  });
  const [copyNotice, setCopyNotice] = useState<string | null>(null);
  const copyHandled = useRef(false);
  useEffect(() => {
    if (!pendingCopy || copyHandled.current || syncStatus === 'loading') return;
    copyHandled.current = true;
    const earlier = conversations.find(item => item.sharedFrom && item.sharedFrom.id === pendingCopy.sharedFrom?.id);
    if (earlier) { selectConversation(earlier.id); setCopyNotice('You already have a copy of this conversation; it is open below.'); return; }
    setConversations(current => [pendingCopy, ...current]);
    setActiveId(pendingCopy.id); setPhase(pendingCopy.turns.at(-1)?.result ? 'complete' : 'idle');
    setCopyNotice(`This is your own copy of ${pendingCopy.sharedFrom?.owner ?? 'a shared'}’s conversation. Continue it below; ${pendingCopy.sharedFrom?.owner ?? 'they'} will not see what you add.`);
  }, [pendingCopy, syncStatus, conversations]);
  useEffect(() => {
    let active = true;
    const loadModels = async () => {
      let data: { local_model: string; cloud_model: string } | undefined;
      try {
        const response = await apiFetch('/v1/ugen/config');
        if (response.ok && response.headers.get('content-type')?.includes('application/json')) {
          const candidate = await response.json() as { local_model?: unknown; cloud_model?: unknown; configured?: unknown };
          if (typeof candidate.local_model === 'string' && typeof candidate.cloud_model === 'string') {
            data = { local_model: candidate.local_model, cloud_model: candidate.cloud_model };
            if (active) setModelsReady(candidate.configured !== false);
          }
        }
      } catch {
        // Reported below as a configuration error.
      }
      if (!active) return;
      if (!data) { setModelsStatus('error'); return; }
      setConfiguredModels({ local: data.local_model.trim(), cloud: data.cloud_model.trim() });
      setModelsStatus('loaded');
    };
    void loadModels();
    return () => { active = false; };
  }, [modelsVersion]);
  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); newConversation(); }
      if (event.key === 'Escape') { setDeleteAction(null); setDrawerOpen(false); setSettingsPage(null); }
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
    setPii(latest ? piiFromResult(latest) : null); setAnalysisSource(latest?.routing.analysis_source ?? null); setFallbackFrom(latest?.routing.fallback_from ?? null); setInjectionSignals(latest?.routing.injection_signals ?? []);
  }
  function newConversation() {
    if (busy) return;
    setActiveId(null); setPrompt(''); setError(''); setPhase('idle'); setProvider(null); setModel(''); setPii(null); setAnalysisSource(null); setFallbackFrom(null); setInjectionSignals([]); setView('chat'); setDrawerOpen(false);
  }
  function restoreConversation(conversation: Conversation | undefined) {
    const latest = conversation ? [...conversation.turns].reverse().find(turn => turn.result)?.result : undefined;
    setActiveId(conversation?.id ?? null); setPrompt(''); setError(''); setPhase(latest ? 'complete' : 'idle');
    setProvider(latest ? latest.provider === 'cloud' ? 'cloud' : 'local' : null); setModel(latest?.model ?? '');
    setPii(latest ? piiFromResult(latest) : null); setAnalysisSource(latest?.routing.analysis_source ?? null); setFallbackFrom(latest?.routing.fallback_from ?? null); setInjectionSignals(latest?.routing.injection_signals ?? []);
  }
  const deletingShared = deleteAction && deleteAction.type !== 'all' ? conversations.find(item => item.id === deleteAction.conversationId)?.shared : undefined;

  function performDelete() {
    if (!deleteAction || busy) return;
    if (deleteAction.type === 'all') {
      // Shared chats stay: clearing your history never deletes them for others.
      const kept = conversations.filter(item => item.shared);
      setConversations(kept); restoreConversation(kept[0]);
    } else if (deleteAction.type === 'conversation') {
      const remaining = conversations.filter(item => item.id !== deleteAction.conversationId);
      setConversations(remaining);
      if (activeId === deleteAction.conversationId) restoreConversation(remaining[0]);
    } else {
      const target = conversations.find(item => item.id === deleteAction.conversationId);
      if (target) {
        const turnsAfterDelete = target.turns.filter(turn => turn.id !== deleteAction.turnId);
        // A shared chat stays even when empty; leaving it is a separate step.
        if (turnsAfterDelete.length || target.shared) {
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
  async function sendFeedback(turnId: string, rating: 1 | -1) {
    const conversationId = activeId;
    const requestId = turns.find(turn => turn.id === turnId)?.result?.id;
    if (!conversationId || !requestId) return;
    setConversations(current => current.map(conversation => conversation.id !== conversationId ? conversation : {
      ...conversation, turns: conversation.turns.map(turn => turn.id === turnId ? { ...turn, feedback: rating } : turn),
    }));
    try {
      await apiFetch('/v1/ugen/feedback', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: requestId, rating }),
      });
      setStatsVersion(version => version + 1);
    } catch {
      // Feedback is best effort; the local choice is kept either way.
    }
  }
  function updateTurn(conversationId: string, turnId: string, result?: ChatResult) {
    setConversations(current => current.map(conversation => conversation.id !== conversationId ? conversation : {
      ...conversation, updatedAt: Date.now(), turns: conversation.turns.map(turn => turn.id === turnId ? { ...turn, ...(result ? { result } : {}) } : turn),
    }));
  }

  /** Stop the request in flight; a partial answer is kept. */
  function stopGeneration() { abortRef.current?.abort(); }

  /** Send ``text`` as a new turn. ``replaceFrom`` first drops that turn and every
   * later one (to regenerate or edit); older turns may be folded into memory. */
  async function sendTurn(text: string, replaceFrom?: number) {
    const userText = text.trim();
    if (!userText || busy) return;
    const existing = activeConversation;
    const conversationId = existing?.id ?? newId();
    const turnId = newId();
    const base: Conversation = existing ?? { id: conversationId, title: userText.slice(0, 36), updatedAt: Date.now(), turns: [] };
    const keptTurns = replaceFrom === undefined ? base.turns : base.turns.slice(0, replaceFrom);
    // Editing a turn that is already in memory makes that memory stale.
    let conversation: Conversation = { ...base, turns: keptTurns, memory: base.memory && base.memory.coveredTurns <= keptTurns.length ? base.memory : undefined };
    const turn: ConversationTurn = { id: turnId, user: userText, ...(base.shared && user ? { author: user.username } : {}) };
    const save = (next: Conversation) => setConversations(current => [{ ...next, updatedAt: Date.now() }, ...current.filter(item => item.id !== conversationId)]);

    save({ ...conversation, turns: [...keptTurns, turn] });
    setActiveId(conversationId); setPrompt(''); setStreamingText(''); setPhase('sending'); setProvider(null); setModel(''); setPii(null); setAnalysisSource(null); setFallbackFrom(null); setInjectionSignals([]); setError('');
    const controller = new AbortController();
    abortRef.current = controller;
    let streamed = '';
    // Show streamed text at most every STREAM_FRAME_MS: re-rendering the page
    // for every token made long answers stutter.
    let streamTimer = 0;
    const showStreamed = () => { streamTimer = 0; setStreamingText(streamed); };
    // What this request was answered by, for a partial answer kept after Stop
    // or an error (the state values belong to the previous render).
    let streamModel = ''; let streamProvider: Provider = 'local';
    try {
      const fold = turnsToRemember(conversation, turn);
      if (fold) {
        setMemoryBusy(true);
        const [from, to] = fold;
        // Fold in chunks so each update stays within the endpoint's limit.
        for (let start = from; start < to;) {
          let end = start + 1;
          for (let chars = size(conversation.turns[start]); end < to && chars + size(conversation.turns[end]) <= FOLD_CHUNK; end += 1) chars += size(conversation.turns[end]);
          const response = await apiFetch('/v1/ugen/memory', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
            body: JSON.stringify({ previous: conversation.memory?.text ?? '', messages: turnMessages(conversation.turns.slice(start, end)) }),
          });
          if (!response.ok) throw new Error(`Could not update the conversation memory: ${await errorMessage(response)}`);
          const { memory } = await response.json() as { memory: string };
          conversation = { ...conversation, memory: { text: memory, coveredTurns: end, updatedAt: Date.now() } };
          save({ ...conversation, turns: [...keptTurns, turn] });
          start = end;
        }
        setMemoryBusy(false);
      }
      const messages = requestMessages(conversation, [...keptTurns, turn]);
      const response = await apiFetch('/v1/ugen/chat/stream', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
        body: JSON.stringify({ messages, provider_selection: providerSelection, temperature: 0.2 }),
      });
      if (!response.ok) throw new Error(response.status === 401 ? 'Your session has ended. Sign in again.' : await errorMessage(response));
      if (!response.body) throw new Error('The server did not return a progress stream.');
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      const received: { result: ChatResult | null } = { result: null }; let streamError = '';
      const onEvent = (event: GatewayEvent) => {
        if (event.type === 'analysis_started') setPhase('analyzing');
        if (event.type === 'analysis_completed') { setPhase('analyzed'); setAnalysisSource(isSource(event.source) ? event.source : null); setPii({ found: Boolean(event.contains_personal_data), types: Array.isArray(event.personal_data_types) ? event.personal_data_types.map(String) : [] }); }
        if (event.type === 'routed') { streamProvider = event.provider === 'cloud' ? 'cloud' : 'local'; setProvider(streamProvider); setPhase('routing'); setPii(current => current && { ...current, redacted: Number(event.redacted_values ?? 0) }); }
        if (event.type === 'guard' && event.kind === 'injection') setInjectionSignals(Array.isArray(event.signals) ? event.signals.map(String) : []);
        if (event.type === 'fallback') { setFallbackFrom(event.from === 'cloud' ? 'cloud' : 'local'); streamProvider = event.to === 'cloud' ? 'cloud' : 'local'; setProvider(streamProvider); }
        if (event.type === 'generation_started') { streamProvider = event.provider === 'cloud' ? 'cloud' : 'local'; streamModel = String(event.model ?? ''); setProvider(streamProvider); setModel(streamModel); setPhase('generating'); }
        if (event.type === 'delta') { streamed += String(event.content ?? ''); if (!streamTimer) streamTimer = window.setTimeout(showStreamed, STREAM_FRAME_MS); }
        if (event.type === 'result') received.result = event.data as ChatResult;
        if (event.type === 'error') streamError = String(event.detail ?? 'The model request failed.');
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
      if (!finalResult) throw new Error('The gateway closed the connection without a model response.');
      updateTurn(conversationId, turnId, finalResult); setStreamingText(''); setStatsVersion(version => version + 1);
      setProvider(finalResult.provider === 'cloud' ? 'cloud' : 'local'); setModel(finalResult.model);
      setPii(piiFromResult(finalResult)); setAnalysisSource(finalResult.routing.analysis_source ?? null); setFallbackFrom(finalResult.routing.fallback_from ?? null); setPhase('complete');
    } catch (cause) {
      setMemoryBusy(false); setStreamingText(''); setStatsVersion(version => version + 1);
      const message = cause instanceof Error ? cause.message : 'Could not reach the gateway.';
      if (streamed) {
        // Keep what was generated before Stop or the error.
        updateTurn(conversationId, turnId, {
          content: streamed, model: streamModel, provider: streamProvider, stopped: true,
          routing: { complexity: '', task_type: '', contains_personal_data: false, personal_data_types: [], reason: '' },
        });
        if (controller.signal.aborted) { setPhase('complete'); return; }
        setError(`${message} The answer above is incomplete.`); setPhase('failed');
        return;
      }
      // Nothing was generated: drop the new turn, and after a failed
      // regenerate or edit bring back the turns it replaced.
      setConversations(current => current.map(item => item.id !== conversationId ? item
        : replaceFrom === undefined ? { ...item, turns: item.turns.filter(existingTurn => existingTurn.id !== turnId) }
        : { ...item, turns: base.turns, memory: base.memory }));
      setPrompt(userText);
      if (controller.signal.aborted) { setPhase('idle'); return; }
      setError(message); setPhase('failed');
    } finally {
      window.clearTimeout(streamTimer);
      abortRef.current = null;
    }
  }

  function sendPrompt() { void sendTurn(prompt); }
  function regenerate(turnId: string) {
    const index = turns.findIndex(turn => turn.id === turnId);
    if (index >= 0) void sendTurn(turns[index].user, index);
  }
  function editTurn(turnId: string, text: string) {
    const index = turns.findIndex(turn => turn.id === turnId);
    if (index >= 0) void sendTurn(text, index);
  }

  const flowStatus = busy ? 'active' : phase === 'failed' ? 'failed' : phase === 'complete' ? 'complete' : '';
  return <div className={`app-layout view-${view} ${drawerOpen ? 'drawer-open' : ''}`}>
    <header className="mobile-bar">
      <button className="icon-button menu-button" onClick={() => setDrawerOpen(true)} aria-label="Open conversation history" aria-expanded={drawerOpen}><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h10"/></svg></button>
      <span className="mobile-title">ugen<span className="brand-dot">.</span><small>AI GATEWAY</small></span>
      <button className="icon-button new-chat-mini" onClick={newConversation} disabled={busy} aria-label="New conversation" title="New conversation">+</button>
    </header>
    <Sidebar conversations={conversations} syncStatus={syncStatus} syncError={syncError} activeId={activeId} busy={busy} onNew={newConversation} onSelect={selectConversation}
      onDelete={id => setDeleteAction({ type: 'conversation', conversationId: id })} onRename={(id, title) => setConversations(current => current.map(item => item.id === id ? { ...item, title } : item))} onClearAll={() => setDeleteAction({ type: 'all' })} onClose={() => setDrawerOpen(false)} user={user} onOpenSettings={page => { setSettingsPage(page); setDrawerOpen(false); }}/>
    <div className="drawer-scrim" onClick={() => setDrawerOpen(false)} aria-hidden="true"/>
    <nav className="view-tabs" role="tablist" aria-label="Switch view">
      <button role="tab" aria-selected={view === 'chat'} className={view === 'chat' ? 'selected' : ''} onClick={() => setView('chat')}><i aria-hidden="true">#</i>Chat{turns.length > 0 && <small>{turns.length}</small>}</button>
      <button role="tab" aria-selected={view === 'monitor'} className={view === 'monitor' ? 'selected' : ''} onClick={() => setView('monitor')}><i aria-hidden="true">⇄</i>Routing monitor<span className={`tab-status ${flowStatus}`}/></button>
    </nav>
    <main className="workspace-area"><section className="workspace-title" aria-label="Workspace title"><span className="eyebrow">SECURE MODEL ROUTING</span><h1>Every request, <span>on the right route.</span></h1><p>Analyzed on this machine first, then handled by the right model.</p></section><section className="workspace-content"><RequestFlow phase={phase} provider={provider} providerSelection={providerSelection} model={model} localModel={configuredModels.local} cloudModel={configuredModels.cloud} modelsStatus={modelsStatus} pii={pii} analysisSource={analysisSource} fallbackFrom={fallbackFrom} injectionSignals={injectionSignals}/><MonitorPanel conversations={conversations} phase={phase} refreshKey={statsVersion}/></section><Footer/></main>
    <section className="chat-column"><ResponsePanel shared={activeConversation?.shared} onShare={user ? () => setShareOpen(true) : undefined} setup={modelsReady ? null : user && !user.is_admin ? 'member' : 'admin'} onOpenModels={() => setSettingsPage('models')} onExport={format => activeConversation && downloadConversation(activeConversation, format)} user={user} turns={turns} memory={activeConversation?.memory} busy={busy} memoryBusy={memoryBusy} streamingText={streamingText} onRegenerate={regenerate} onEdit={editTurn} onFeedback={sendFeedback} onDeleteTurn={turnId => activeId && setDeleteAction({ type: 'turn', conversationId: activeId, turnId })}/><PromptPanel prompt={prompt} busy={busy} error={error} providerSelection={providerSelection} cloudAvailable={modelsStatus !== 'loaded' || Boolean(configuredModels.cloud)} onProviderSelectionChange={setProviderSelection} onPromptChange={setPrompt} onSubmit={sendPrompt} onStop={stopGeneration} onClear={() => setPrompt('')}/></section>
    {settingsPage && <SettingsDialog user={user} page={settingsPage} onPageChange={setSettingsPage} onUserChange={onUserChange} onSignOut={() => void flush().finally(onSignOut)} onClose={() => setSettingsPage(null)} onModelsSaved={() => setModelsVersion(version => version + 1)}
      onAccountDeleted={() => { forgetLocalHistory(user); onAccountDeleted(); }}/>}
    {shareOpen && activeConversation && user && <ShareDialog conversation={activeConversation} user={user} onClose={() => setShareOpen(false)}
      onSharedChat={chatId => {
        // The shared chat replaces this conversation in everyone's list.
        setConversations(current => current.filter(item => item.id !== activeConversation.id));
        setActiveId(chatId); setShareOpen(false);
        void refreshNow();
      }}
      onMembershipChanged={() => void refreshNow()}/>}
    {notice && <div className={`toast ${notice.ok ? 'ok' : 'error'}`} role="status"><span>{notice.message}</span><button className="icon-button" onClick={onDismissNotice} aria-label="Dismiss">×</button></div>}
    {!notice && copyNotice && <div className="toast ok" role="status"><span>{copyNotice}</span><button className="icon-button" onClick={() => setCopyNotice(null)} aria-label="Dismiss">×</button></div>}
    {deleteAction && <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) setDeleteAction(null); }}><section className="confirm-modal" role="alertdialog" aria-modal="true" aria-labelledby="delete-title" aria-describedby="delete-description"><span className="modal-icon"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4.5 7h15M9 7V4.8h6V7m2.5 0-.8 12h-9.4l-.8-12m4 3.5v5m3-5v5"/></svg></span><span className="eyebrow">DELETE CONFIRMATION</span><h2 id="delete-title">{deleteAction.type === 'all' ? 'Clear all conversations?' : deleteAction.type === 'turn' ? 'Delete this exchange?' : deletingShared ? deletingShared.role === 'owner' ? 'Delete this shared chat for everyone?' : 'Leave this shared chat?' : 'Delete this conversation?'}</h2><p id="delete-description">{deleteAction.type === 'all'
      ? user ? 'Every conversation in your account will be permanently deleted, on every device. Shared chats are kept.' : 'Every conversation saved on this device will be permanently deleted.'
      : deletingShared && deleteAction.type === 'conversation' ? deletingShared.role === 'owner' ? `It is removed for ${deletingShared.members.length - 1} other member(s) too.` : 'It disappears from your list; the others keep it.'
      : user ? 'This removes it from your history on every device.' : 'This removes it from the history saved on this device.'}</p><div className="modal-actions"><button className="cancel-button" onClick={() => setDeleteAction(null)}>Cancel</button><button className="danger-button" onClick={performDelete}>Delete</button></div></section></div>}
  </div>;
}
export default App;
