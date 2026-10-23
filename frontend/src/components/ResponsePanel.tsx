import { lazy, memo, Suspense, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { User } from '../api';
import { copyText } from '../clipboard';
import type { ConversationMemory, ConversationTurn, SharedInfo } from '../types';
import Avatar from './Avatar';

// The Markdown and LaTeX renderer is large, so it loads with the first reply instead of the page.
const LazyMarkdown = lazy(() => import('./Markdown'));
const Markdown = memo(({ text }: { text: string }) => <Suspense fallback={<div className="markdown plain">{text}</div>}><LazyMarkdown text={text}/></Suspense>);

/** Streamed Markdown split at blank lines into finished blocks and the one
 * still being written. Only the last block is parsed again as text arrives;
 * code fences and $$ math are never split, nor is an indented continuation. */
export function markdownBlocks(text: string): string[] {
  const blocks: string[] = [];
  const lines = text.split('\n');
  let current: string[] = [];
  let fence = '';
  let math = false;
  lines.forEach((line, index) => {
    const opener = /^\s{0,3}(`{3,}|~{3,})/.exec(line)?.[1];
    if (fence) {
      if (opener && opener[0] === fence[0] && opener.length >= fence.length) fence = '';
    } else if (opener) {
      fence = opener;
    } else if (line.trim() === '$$') {
      math = !math;
    } else if (!math && !line.trim() && current.length) {
      const next = lines[index + 1];
      if (next && !/^\s/.test(next)) {
        blocks.push(current.join('\n'));
        current = [];
        return;
      }
    }
    current.push(line);
  });
  blocks.push(current.join('\n'));
  return blocks;
}

const StreamingMarkdown = ({ text }: { text: string }) =>
  <>{markdownBlocks(text).map((block, index) => <Markdown key={index} text={block}/>)}</>;

// Built-in rules that keep a request local, with the reason in plain words.
const POLICY_RULES: Record<string, string> = {
  'privacy.never_cloud': 'Privacy policy: kept local',
  'privacy.unredactable': 'Cannot redact safely: kept local',
  'key.local_only': 'Key restriction: local only',
  'key.budget': 'Budget used up: switched to local',
  'user.budget': 'Your budget is used up: switched to local',
  'guard.injection': 'Possible injection: kept local',
};

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return <button className="message-action" onClick={async () => { if (await copyText(text)) { setCopied(true); setTimeout(() => setCopied(false), 1500); } }} title="Copy">{copied ? 'Copied' : 'Copy'}</button>;
}

type Props = {
  user: User | null; turns: ConversationTurn[]; memory?: ConversationMemory; busy: boolean; memoryBusy: boolean; streamingText: string;
  onFeedback: (turnId: string, rating: 1 | -1) => void; onDeleteTurn: (turnId: string) => void;
  onRegenerate: (turnId: string) => void; onEdit: (turnId: string, text: string) => void;
  onExport: (format: 'markdown' | 'json') => void;
  /** Who sees the first-run guide when no model is set up yet. */
  setup: 'admin' | 'member' | null; onOpenModels: () => void;
  /** Set for a shared chat. */
  shared?: SharedInfo;
  /** Opens sharing; absent without an account. */
  onShare?: () => void;
};

function ResponsePanel({ user, turns, memory, busy, memoryBusy, streamingText, onFeedback, onDeleteTurn, onRegenerate, onEdit, onExport, setup, onOpenModels, shared, onShare }: Props) {
  const threadRef = useRef<HTMLDivElement>(null);
  const [editing, setEditing] = useState<{ turnId: string; text: string } | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  // Follow the answer while the view is at the bottom; scrolling up to read
  // stops that until the next message.
  const followRef = useRef(true);
  useEffect(() => { followRef.current = true; }, [turns.length]);
  // Before paint, so new text never shows below the fold for a frame first.
  useLayoutEffect(() => {
    if (threadRef.current && followRef.current) threadRef.current.scrollTop = threadRef.current.scrollHeight;
  }, [turns.length, turns.at(-1)?.result?.content, streamingText]);
  const hasTurns = turns.length > 0;
  // In a shared chat, messages from other members show their name.
  const otherAuthor = (turn: ConversationTurn) => shared && turn.author && turn.author.toLowerCase() !== user?.username.toLowerCase() ? turn.author : null;

  return <article className="panel response-panel">
    <div className="panel-heading">
      <div><span className="eyebrow">02 / CONVERSATION</span><h2><span className="channel-hash">#</span> Conversation</h2><p>Context is kept, so you can keep asking follow-ups.</p></div>
      <span className="panel-tools">
        {shared && <button className="shared-badge" onClick={onShare} title={`Shared chat: ${shared.members.join(', ')}`}>👥 {shared.members.length}</button>}
        {hasTurns && onShare && !shared && <button className="text-action share-button" onClick={onShare}>Share</button>}
        {hasTurns && <details className="export-menu">
          <summary>Export</summary>
          <div role="menu"><button role="menuitem" onClick={event => { onExport('markdown'); event.currentTarget.closest('details')?.removeAttribute('open'); }}>Markdown (.md)</button>
            <button role="menuitem" onClick={event => { onExport('json'); event.currentTarget.closest('details')?.removeAttribute('open'); }}>JSON (.json)</button></div>
        </details>}
        <span className="conversation-live"><i/> Live</span>
      </span>
    </div>
    {hasTurns ? <div className="conversation-thread" ref={threadRef} onScroll={event => { const el = event.currentTarget; followRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>{turns.map((turn, index) => {
      const result = turn.result;
      const generatedTokens = result?.usage?.generation?.completion_tokens;
      const generationMs = result?.performance?.generation_ms;
      const tokenRate = generatedTokens !== undefined && generationMs !== undefined && generationMs > 0
        ? (generatedTokens / (generationMs / 1000)).toFixed(1)
        : null;
      const isEditing = editing?.turnId === turn.id;
      return <section className={`turn ${memory && index < memory.coveredTurns ? 'remembered' : ''}`} key={turn.id}>
        {memory && index === memory.coveredTurns && <div className="memory-marker">
          <span>↑ Earlier messages are kept as memory</span>
          <button className="text-action" onClick={() => setMemoryOpen(open => !open)}>{memoryOpen ? 'Hide memory' : 'Show memory'}</button>
          {memoryOpen && <div className="memory-text"><Markdown text={memory.text}/></div>}
        </div>}
        <article className="chat-message user-message">
          {otherAuthor(turn) ? <span className="message-avatar user-avatar">{otherAuthor(turn)!.slice(0, 1).toUpperCase()}</span>
            : user ? <Avatar user={user} className="message-avatar user-avatar"/> : <span className="message-avatar user-avatar">You</span>}
          <div className="message-body"><div className="message-author"><b className={otherAuthor(turn) ? 'turn-author' : undefined}>{otherAuthor(turn) ?? 'You'}</b>
            {!isEditing && <span className="message-actions">
              <CopyButton text={turn.user}/>
              <button className="message-action" onClick={() => setEditing({ turnId: turn.id, text: turn.user })} disabled={busy} title="Edit and send again">Edit</button>
              <button className="turn-delete" onClick={() => onDeleteTurn(turn.id)} disabled={busy} aria-label="Delete this message" title="Delete this exchange">Delete</button>
            </span>}</div>
            {isEditing
              ? <form className="edit-form" onSubmit={event => { event.preventDefault(); if (editing.text.trim()) { onEdit(turn.id, editing.text); setEditing(null); } }}>
                  <textarea value={editing.text} onChange={event => setEditing({ turnId: turn.id, text: event.target.value })} rows={Math.min(8, editing.text.split('\n').length + 1)} maxLength={32000} autoFocus
                    onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setEditing(null); } }}/>
                  <small>Sending replaces this message and everything after it.</small>
                  <span><button type="button" className="cancel-button" onClick={() => setEditing(null)}>Cancel</button><button type="submit" className="send-button" disabled={busy || !editing.text.trim()}>Send</button></span>
                </form>
              : <p>{turn.user}</p>}
          </div>
        </article>
        {result ? <article className="chat-message assistant-message">
          <span className="message-avatar assistant-avatar">✳</span>
          <div className="message-body">
            <div className="message-author"><b>UGen</b><span className="message-actions">
              <CopyButton text={result.content}/>
              {index === turns.length - 1 && <button className="message-action" onClick={() => onRegenerate(turn.id)} disabled={busy} title="Answer again">Regenerate</button>}
              {result.id && <span className="feedback" role="group" aria-label="Rate this answer">
                <button className={turn.feedback === 1 ? 'selected' : ''} aria-pressed={turn.feedback === 1} onClick={() => onFeedback(turn.id, 1)} title="Helpful">👍</button>
                <button className={turn.feedback === -1 ? 'selected' : ''} aria-pressed={turn.feedback === -1} onClick={() => onFeedback(turn.id, -1)} title="Not helpful">👎</button>
              </span>}
            </span></div>
            <div className="markdown-wrap"><Markdown text={result.content}/></div>
            <div className="result-foot">
              {result.stopped ? <span className="fallback-note">Stopped</span> : <><span>Task: {result.routing.task_type}</span><span>Complexity: {result.routing.complexity}</span></>}
              {tokenRate !== null && <span className="token-stat">{tokenRate} tok/s · {generatedTokens} tokens</span>}
              {(result.routing.output_findings?.length ?? 0) > 0 && <span className="fallback-note">Redacted {result.routing.output_findings!.length} personal data item(s) you did not provide</span>}
              {result.routing.fallback_from && <span className="fallback-note">{result.routing.fallback_from === 'cloud' ? 'Cloud did not respond; answered locally' : 'Local model did not respond; answered by cloud'}</span>}
              {result.routing.rule && <span className={POLICY_RULES[result.routing.rule] ? 'policy-note' : ''}>{POLICY_RULES[result.routing.rule] ?? `Rule: ${result.routing.rule}`}</span>}
              <span className={`provider-model ${result.provider === 'cloud' ? 'cloud' : ''}`}>{result.provider === 'cloud' ? 'Cloud' : 'Local'}</span>
            </div>
          </div>
        </article> : <article className={`chat-message assistant-message ${busy && index === turns.length - 1 && streamingText ? '' : 'pending-message'}`}>
          <span className="message-avatar assistant-avatar">✳</span><div className="message-body"><div className="message-author"><b>UGen</b></div>{busy && index === turns.length - 1 && streamingText
            ? <div className="markdown-wrap streaming"><StreamingMarkdown text={streamingText}/></div>
            : <p>{busy && index === turns.length - 1 ? (memoryBusy ? 'Saving older messages to memory…' : 'Analyzing and generating a response…') : 'No response received for this message'}</p>}</div>
        </article>}
      </section>;
    })}</div> : setup ? <div className="response-empty setup-guide"><span className="empty-glyph">⚙</span>
      <b>{setup === 'admin' ? 'Set up a model to start chatting' : 'Almost ready'}</b>
      <p>{setup === 'admin'
        ? 'Point the gateway at your local model server (Ollama, LM Studio, vLLM…), and optionally a cloud model. It takes a minute.'
        : 'An administrator needs to set up a model before conversations can start.'}</p>
      {setup === 'admin' && <button className="send-button" onClick={onOpenModels}>Open Models</button>}
    </div> : <div className="response-empty"><span className="empty-glyph">✳</span><b>{busy ? 'The model is working on your request' : 'Start a new conversation'}</b><p>{busy ? 'Once the request is analyzed, the gateway shows where it was routed.' : 'Ask follow-up questions; the gateway sends the conversation context to the model.'}</p></div>}
  </article>;
}
export default ResponsePanel;
