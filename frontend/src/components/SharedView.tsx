import { lazy, Suspense, useEffect, useState } from 'react';
import { apiFetch, errorMessage } from '../api';
import Footer from './Footer';
import type { Conversation } from '../types';
import LogoMark from './LogoMark';

const Markdown = lazy(() => import('./Markdown'));

type SharedTurn = { id: string; user: string; author?: string | null; answer?: string | null; model?: string | null; provider?: string | null; stopped: boolean };
type Shared = { title: string; turns: SharedTurn[]; owner: string; visibility: 'accounts' | 'public'; updated_at: number; is_owner: boolean };

/** Where a copy waits while the dashboard loads; App picks it up once. */
export const IMPORT_STORAGE = 'ugen-import-conversation';

const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
const date = (seconds: number) => new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' }).format(seconds * 1000);

function asConversation(shared: Shared, shareId: string): Conversation {
  return {
    id: newId(), title: shared.title, updatedAt: Date.now(), sharedFrom: { id: shareId, owner: shared.owner },
    turns: shared.turns.map(turn => ({
      id: newId(), user: turn.user,
      ...(turn.answer ? { result: {
        content: turn.answer, model: turn.model ?? '', provider: turn.provider ?? 'local', stopped: turn.stopped,
        routing: { complexity: '', task_type: '', contains_personal_data: false, personal_data_types: [], reason: '' },
      } } : {}),
    })),
  };
}

/** A conversation opened from a share link. Signed-in people get their own
 * copy to continue right away (its author does not see what they add);
 * others can read a public link and sign in to continue it. */
function SharedView({ id, signedIn, onSignIn, onOpenApp }: { id: string; signedIn: boolean; onSignIn: () => void; onOpenApp: () => void }) {
  const [shared, setShared] = useState<Shared | null>(null);
  const [error, setError] = useState<{ message: string; signIn: boolean } | null>(null);

  useEffect(() => {
    apiFetch(`/v1/ugen/shares/${encodeURIComponent(id)}`)
      .then(async response => {
        if (response.ok) {
          const data = await response.json() as Shared;
          setShared(data);
          // Straight into the viewer's own copy, unless it is their own link.
          if (signedIn && !data.is_owner) continueCopy(data);
          return;
        }
        setError({ message: await errorMessage(response), signIn: response.status === 401 && !signedIn });
      })
      .catch(() => setError({ message: 'Cannot reach the gateway.', signIn: false }));
  }, [id, signedIn]);

  function continueCopy(data: Shared) {
    try { sessionStorage.setItem(IMPORT_STORAGE, JSON.stringify(asConversation(data, id))); } catch { /* nothing to carry over */ }
    onOpenApp();
  }

  return <main className="shared-page">
    <header className="shared-header">
      <a className="brand" href={window.location.pathname} onClick={event => { event.preventDefault(); onOpenApp(); }}><span className="brand-mark"><LogoMark/></span><span>ugen<span className="brand-dot">.</span><small>AI GATEWAY</small></span></a>
      {shared && signedIn && <button className="send-button" onClick={() => continueCopy(shared)}>Continue this conversation</button>}
      {!signedIn && <button className="send-button" onClick={onSignIn}>Sign in to continue</button>}
    </header>
    <article className="shared-card">
      {error ? <div className="shared-empty"><b>{error.message}</b>{error.signIn && <button className="send-button" onClick={onSignIn}>Sign in to open it</button>}</div>
        : !shared ? <div className="shared-empty"><span className="spinner dark" aria-label="Loading"/></div>
        : <>
            <span className="eyebrow">SHARED CONVERSATION{shared.visibility === 'public' ? ' · PUBLIC LINK' : ''}</span>
            <h1>{shared.title}</h1>
            <p className="shared-meta">Shared by {shared.owner}{shared.is_owner ? ' (you): people who open this link get their own copy to continue' : ''} · {date(shared.updated_at)}</p>
            {!signedIn && <p className="shared-meta">Sign in to continue this conversation as your own; {shared.owner} will not see what you add.</p>}
            <div className="shared-thread">{shared.turns.map(turn => <section key={turn.id} className="shared-turn">
              <div className="shared-question"><b>{turn.author ?? shared.owner}</b><p>{turn.user}</p></div>
              {turn.answer && <div className="shared-answer"><b>UGen{turn.model ? ` · ${turn.provider === 'cloud' ? 'Cloud' : 'Local'}` : ''}{turn.stopped ? ' · stopped' : ''}</b>
                <div className="markdown-wrap"><Suspense fallback={<div className="markdown plain">{turn.answer}</div>}><Markdown text={turn.answer}/></Suspense></div></div>}
            </section>)}</div>
          </>}
    </article>
    <Footer/>
  </main>;
}

export default SharedView;
