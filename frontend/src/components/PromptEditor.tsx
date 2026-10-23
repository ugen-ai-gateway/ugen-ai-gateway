import { useEffect, useState } from 'react';
import { apiFetch, errorMessage } from '../api';

type Props = { title: string; description: string; path: string; maxLength: number; placeholder: string; first?: boolean };

/** A text box for a prompt saved on the gateway; ``path`` answers GET and PUT with { text }. */
function PromptEditor({ title, description, path, maxLength, placeholder, first = false }: Props) {
  const [saved, setSaved] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [state, setState] = useState<{ ok: boolean; message: string } | null>(null);
  const [working, setWorking] = useState(false);

  useEffect(() => {
    apiFetch(path).then(async response => {
      if (!response.ok) { setState({ ok: false, message: await errorMessage(response) }); return; }
      const body = await response.json() as { text: string };
      setSaved(body.text); setText(body.text);
    }).catch(() => setState({ ok: false, message: 'Cannot reach the gateway' }));
  }, [path]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setWorking(true); setState(null);
    try {
      const response = await apiFetch(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) });
      if (!response.ok) { setState({ ok: false, message: await errorMessage(response) }); return; }
      const body = await response.json() as { text: string };
      setSaved(body.text); setText(body.text);
      setState({ ok: true, message: body.text ? 'Saved. New messages use it.' : 'Removed.' });
    } catch { setState({ ok: false, message: 'Cannot reach the gateway' }); } finally { setWorking(false); }
  }

  return <form className={`settings-section prompt-editor${first ? ' first' : ''}`} onSubmit={save}>
    <h3>{title}</h3>
    <p>{description}</p>
    <textarea value={text} onChange={event => { setText(event.target.value); setState(null); }} rows={5} maxLength={maxLength} placeholder={placeholder} aria-label={title} disabled={saved === null}/>
    <div className="probe-row">
      <button className="cancel-button" type="submit" disabled={working || saved === null || text.trim() === saved}>{working ? 'Saving…' : 'Save'}</button>
      <span className="char-count">{text.length.toLocaleString('en-US')} / {maxLength.toLocaleString('en-US')}</span>
      {state && <span className={`probe ${state.ok ? 'ok' : 'error'}`} role="status">{state.message}</span>}
    </div>
  </form>;
}

export default PromptEditor;
