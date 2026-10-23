import { useEffect, useState } from 'react';
import { apiFetch, errorMessage } from '../api';

type Rule = { name: string; kind: 'keyword' | 'regex'; pattern: string; case_sensitive: boolean; keep_local: boolean };
type Privacy = { rules: Rule[] };
type Preview = { redacted: string; matches: { type: string; label: string; value: string }[]; keeps_local: string[] };

const blankRule = (): Rule => ({ name: '', kind: 'keyword', pattern: '', case_sensitive: false, keep_local: false });

function PrivacyPage() {
  const [data, setData] = useState<Privacy | null>(null);
  const [rules, setRules] = useState<Rule[]>([]);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveState, setSaveState] = useState<{ ok: boolean; message: string } | null>(null);
  const [sample, setSample] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [previewError, setPreviewError] = useState('');

  function apply(privacy: Privacy) { setData(privacy); setRules(privacy.rules); }

  useEffect(() => {
    apiFetch('/v1/ugen/privacy')
      .then(async response => response.ok ? apply(await response.json() as Privacy) : setError(await errorMessage(response)))
      .catch(() => setError('Cannot reach the gateway'));
  }, []);

  if (error) return <div className="settings-section first"><div className="error-box" role="alert"><span>{error}</span></div></div>;
  if (!data) return <div className="settings-section first"><p>Loading…</p></div>;
  const settings = () => ({ rules: rules.map(rule => ({ ...rule, name: rule.name.trim() })) });
  const update = (index: number, patch: Partial<Rule>) => setRules(current => current.map((rule, i) => i === index ? { ...rule, ...patch } : rule));

  async function save() {
    setSaving(true); setSaveState(null);
    try {
      const response = await apiFetch('/v1/ugen/privacy', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(settings()) });
      if (!response.ok) { setSaveState({ ok: false, message: await errorMessage(response) }); return; }
      apply(await response.json() as Privacy);
      setSaveState({ ok: true, message: 'Saved. New requests use these rules.' });
    } catch { setSaveState({ ok: false, message: 'Cannot reach the gateway' }); } finally { setSaving(false); }
  }

  async function tryIt() {
    setPreviewError(''); setPreview(null);
    try {
      const response = await apiFetch('/v1/ugen/privacy/test', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: sample, settings: settings() }) });
      if (!response.ok) { setPreviewError(await errorMessage(response)); return; }
      setPreview(await response.json() as Preview);
    } catch { setPreviewError('Cannot reach the gateway'); }
  }

  return <>
    <p className="models-note">Personal data (emails, phone numbers, ID, passport and card numbers, keys, passwords and more) is always redacted automatically before any model sees it, and restored in the reply. Add your own terms below to redact them too.</p>
    <div className="settings-section first">
      <h3>Custom terms</h3>
      <p>Words or patterns that must not reach the cloud as written, such as project code names, client names or internal hostnames. They are replaced by placeholders before any cloud request and restored in the reply.</p>
      <div className="rule-list">{rules.map((rule, index) => <div className="rule-card" key={index}>
        <div className="rule-head">
          <input value={rule.name} onChange={event => update(index, { name: event.target.value })} placeholder="Name, e.g. Project" aria-label="Rule name" maxLength={40}/>
          <select value={rule.kind} onChange={event => update(index, { kind: event.target.value as Rule['kind'] })} aria-label="Rule type">
            <option value="keyword">Keywords</option><option value="regex">Regular expression</option>
          </select>
          <button type="button" className="text-action danger" onClick={() => setRules(current => current.filter((_, i) => i !== index))}>Remove</button>
        </div>
        {rule.kind === 'keyword'
          ? <textarea value={rule.pattern} onChange={event => update(index, { pattern: event.target.value })} rows={Math.min(6, Math.max(2, rule.pattern.split('\n').length))} placeholder={'One term per line, e.g.\nFalcon\nBlue Sky'} aria-label="Keywords" spellCheck={false}/>
          : <input value={rule.pattern} onChange={event => update(index, { pattern: event.target.value })} placeholder="e.g. OPS-\d{4}" aria-label="Regular expression" spellCheck={false} className="mono"/>}
        <div className="rule-options">
          <label><input type="checkbox" checked={rule.case_sensitive} onChange={event => update(index, { case_sensitive: event.target.checked })}/> Match case</label>
          <label><input type="checkbox" checked={rule.keep_local} onChange={event => update(index, { keep_local: event.target.checked })}/> Keep requests containing it on the local model</label>
        </div>
      </div>)}</div>
      <button type="button" className="cancel-button add-rule" onClick={() => setRules(current => [...current, blankRule()])}>+ Add a term</button>
    </div>

    <div className="settings-section">
      <h3>Try it</h3>
      <p>See what the cloud would receive for some text, using the rules on this page (saved or not).</p>
      <textarea className="try-text" value={sample} onChange={event => setSample(event.target.value)} rows={3} placeholder="e.g. Email the Falcon plan to alex@example.com" maxLength={5000}/>
      <div className="probe-row"><button type="button" className="cancel-button" onClick={() => void tryIt()} disabled={!sample.trim()}>Preview</button>
        {previewError && <span className="probe error">{previewError}</span>}</div>
      {preview && <div className="preview-box">
        <code>{preview.redacted}</code>
        <small>{preview.matches.length ? `${preview.matches.length} item(s) replaced` : 'Nothing would be replaced'}{preview.keeps_local.length ? ' · this request would stay on the local model' : ''}</small>
      </div>}
    </div>

    <div className="models-save">
      {saveState && <span className={`probe ${saveState.ok ? 'ok' : 'error'}`} role="status">{saveState.message}</span>}
      <button type="button" className="send-button" onClick={() => void save()} disabled={saving}>{saving ? 'Saving…' : 'Save privacy rules'}</button>
    </div>
  </>;
}

export default PrivacyPage;
