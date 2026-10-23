import { useEffect, useState } from 'react';
import { apiFetch, errorMessage } from '../api';
import NumberStepper from './NumberStepper';

type Locality = 'local' | 'cloud';
type SavedConnection = {
  base_url: string; model: string; alias: string; has_api_key: boolean; api_key_hint: string;
  analysis_model?: string; embedding_model?: string;
  input_cost_per_million?: number; output_cost_per_million?: number;
};
type SavedExtra = SavedConnection & { id: string; name: string; locality: Locality };
type Condition = { complexity: string[]; task_type: string[]; contains_personal_data: boolean | null; min_chars: number | null; max_chars: number | null };
type Rule = { name: string; when: Condition; target: string };
type ModelsResponse = {
  source: 'database' | 'env' | 'routing_file';
  config: { local: SavedConnection; cloud_enabled: boolean; cloud: SavedConnection; analysis_mode: 'hybrid' | 'llm'; cloud_from?: CloudFrom; extra: SavedExtra[]; rules: Rule[] };
};
/** A connection being edited. ``apiKey`` undefined keeps the saved key; '' removes it. */
type CloudFrom = 'moderate' | 'complex';
type Draft = { baseUrl: string; model: string; alias: string; apiKey?: string; hasKey: boolean; keyHint: string; inputCost: string; outputCost: string; analysisModel: string; embeddingModel: string };
type Extra = { id: string; name: string; locality: Locality; draft: Draft };
type Probe = { state: 'idle' | 'working' | 'ok' | 'error'; message?: string };

const COMPLEXITY = ['simple', 'moderate', 'complex'];
const TASK_TYPES = ['general', 'coding', 'reasoning', 'summarization', 'translation', 'other'];
const blankDraft: Draft = { baseUrl: '', model: '', alias: '', hasKey: false, keyHint: '', inputCost: '', outputCost: '', analysisModel: '', embeddingModel: '' };
const toDraft = (saved: SavedConnection): Draft => ({
  baseUrl: saved.base_url, model: saved.model, alias: saved.alias, hasKey: saved.has_api_key, keyHint: saved.api_key_hint,
  inputCost: saved.input_cost_per_million ? String(saved.input_cost_per_million) : '',
  outputCost: saved.output_cost_per_million ? String(saved.output_cost_per_million) : '',
  analysisModel: saved.analysis_model ?? '', embeddingModel: saved.embedding_model ?? '',
});
const costStep = (current: number, direction: 1 | -1) => (direction === 1 ? current < 10 : current <= 10) ? 0.1 : 1;
const newModelId = () => `m${Array.from(crypto.getRandomValues(new Uint8Array(4)), b => b.toString(16).padStart(2, '0')).join('')}`;
const toggle = (list: string[], value: string) => list.includes(value) ? list.filter(item => item !== value) : [...list, value];

function ConnectionFields({ id, locality, isMain, draft, onChange, models, probe, onProbe }: {
  id: string; locality: Locality; isMain: boolean; draft: Draft; onChange: (draft: Draft) => void; models: string[]; probe: Probe; onProbe: (kind: 'test' | 'list') => void;
}) {
  const set = (patch: Partial<Draft>) => onChange({ ...draft, ...patch });
  const isCloud = locality === 'cloud';
  const listId = `${id.replace(':', '-')}-models`;
  const keyPlaceholder = draft.apiKey === '' ? 'Key will be removed' : draft.hasKey ? `Saved (${draft.keyHint}). Type to replace` : isCloud ? 'Paste your API key' : 'Optional';
  return <div className="model-fields">
    <label>Base URL<input value={draft.baseUrl} onChange={event => set({ baseUrl: event.target.value })}
      placeholder={isCloud ? 'https://api.example.com/v1' : 'http://localhost:11434/v1'} spellCheck={false}/></label>
    <label>API key
      <span className="field-row">
        <input type="password" value={draft.apiKey ?? ''} onChange={event => set({ apiKey: event.target.value })} placeholder={keyPlaceholder} autoComplete="off"/>
        {draft.hasKey && draft.apiKey !== '' && <button type="button" className="text-action" onClick={() => set({ apiKey: '' })}>Remove</button>}
      </span>
    </label>
    <label>Model
      <span className="field-row">
        <input value={draft.model} onChange={event => set({ model: event.target.value })} list={listId} placeholder="Type or load the model name" spellCheck={false}/>
        <button type="button" className="cancel-button" onClick={() => onProbe('list')} disabled={probe.state === 'working' || !draft.baseUrl}>Load models</button>
      </span>
      <datalist id={listId}>{models.map(model => <option key={model} value={model}/>)}</datalist>
    </label>
    <label>Alias <small>Shown instead of the model name (optional)</small><input value={draft.alias} onChange={event => set({ alias: event.target.value })} placeholder={draft.model || 'e.g. House model'}/></label>
    {isMain && !isCloud && <>
      <label>Analysis model <small>A smaller model here makes routing faster (optional; the model above is used otherwise)</small>
        <input value={draft.analysisModel} onChange={event => set({ analysisModel: event.target.value })} list={listId} placeholder={draft.model || 'e.g. qwen3:1.7b'} spellCheck={false}/></label>
      <label>Embedding model <small>Serves /v1/embeddings, always on this machine (optional)</small>
        <input value={draft.embeddingModel} onChange={event => set({ embeddingModel: event.target.value })} list={listId} placeholder="e.g. nomic-embed-text" spellCheck={false}/></label>
    </>}
    {isCloud && <div className="cost-row">
      <label>Input $ / 1M tokens<NumberStepper value={draft.inputCost} onChange={inputCost => set({ inputCost })} step={costStep} min={0} decimals={2} placeholder="0" label="Input price"/></label>
      <label>Output $ / 1M tokens<NumberStepper value={draft.outputCost} onChange={outputCost => set({ outputCost })} step={costStep} min={0} decimals={2} placeholder="0" label="Output price"/></label>
    </div>}
    <div className="probe-row">
      <button type="button" className="cancel-button" onClick={() => onProbe('test')} disabled={probe.state === 'working' || !draft.model || !draft.baseUrl}>{probe.state === 'working' ? 'Testing…' : 'Test connection'}</button>
      {probe.state === 'ok' && <span className="probe ok">✓ {probe.message}</span>}
      {probe.state === 'error' && <span className="probe error">{probe.message}</span>}
    </div>
  </div>;
}

function RuleEditor({ rule, targets, onChange, onMove, onRemove, first, last }: {
  rule: Rule; targets: { id: string; label: string }[]; onChange: (rule: Rule) => void; onMove: (step: -1 | 1) => void; onRemove: () => void; first: boolean; last: boolean;
}) {
  const when = rule.when;
  const setWhen = (patch: Partial<Condition>) => onChange({ ...rule, when: { ...when, ...patch } });
  const number = (value: string) => value === '' ? null : Math.max(0, Math.floor(Number(value)));
  return <div className="rule-card">
    <div className="rule-head">
      <input value={rule.name} onChange={event => onChange({ ...rule, name: event.target.value })} placeholder="Rule name, e.g. Code to the code model" aria-label="Rule name" maxLength={60}/>
      <button type="button" className="icon-button" onClick={() => onMove(-1)} disabled={first} aria-label="Move up" title="Move up">↑</button>
      <button type="button" className="icon-button" onClick={() => onMove(1)} disabled={last} aria-label="Move down" title="Move down">↓</button>
      <button type="button" className="text-action danger" onClick={onRemove}>Remove</button>
    </div>
    <div className="rule-row"><span>Difficulty</span><div className="chips">{COMPLEXITY.map(value => <button type="button" key={value} className={when.complexity.includes(value) ? 'selected' : ''} onClick={() => setWhen({ complexity: toggle(when.complexity, value) })}>{value}</button>)}</div></div>
    <div className="rule-row"><span>Task</span><div className="chips">{TASK_TYPES.map(value => <button type="button" key={value} className={when.task_type.includes(value) ? 'selected' : ''} onClick={() => setWhen({ task_type: toggle(when.task_type, value) })}>{value}</button>)}</div></div>
    <div className="rule-row"><span>Personal data</span>
      <select value={when.contains_personal_data === null ? 'any' : when.contains_personal_data ? 'yes' : 'no'} onChange={event => setWhen({ contains_personal_data: event.target.value === 'any' ? null : event.target.value === 'yes' })} aria-label="Personal data">
        <option value="any">Any</option><option value="yes">Contains personal data</option><option value="no">No personal data</option>
      </select>
    </div>
    <div className="rule-row"><span>Length</span>
      <span className="field-row"><input type="number" min={0} value={when.min_chars ?? ''} onChange={event => setWhen({ min_chars: number(event.target.value) })} placeholder="Min characters" aria-label="Minimum characters"/>
        <input type="number" min={0} value={when.max_chars ?? ''} onChange={event => setWhen({ max_chars: number(event.target.value) })} placeholder="Max characters" aria-label="Maximum characters"/></span>
    </div>
    <div className="rule-row"><span>Send to</span>
      <select value={rule.target} onChange={event => onChange({ ...rule, target: event.target.value })} aria-label="Send to">
        {targets.map(target => <option key={target.id} value={target.id}>{target.label}</option>)}
      </select>
    </div>
  </div>;
}

function ModelsPage({ onSaved }: { onSaved: () => void }) {
  const [source, setSource] = useState<ModelsResponse['source'] | null>(null);
  const [loadError, setLoadError] = useState('');
  const [drafts, setDrafts] = useState<{ local: Draft; cloud: Draft } | null>(null);
  const [extras, setExtras] = useState<Extra[]>([]);
  const [rules, setRules] = useState<Rule[]>([]);
  const [cloudEnabled, setCloudEnabled] = useState(false);
  const [analysisMode, setAnalysisMode] = useState<'hybrid' | 'llm'>('hybrid');
  const [cloudFrom, setCloudFrom] = useState<CloudFrom>('moderate');
  const [models, setModels] = useState<Record<string, string[]>>({});
  const [probes, setProbes] = useState<Record<string, Probe>>({});
  const [saving, setSaving] = useState(false);
  const [saveState, setSaveState] = useState<{ ok: boolean; message: string } | null>(null);

  function apply(response: ModelsResponse) {
    const { config } = response;
    setSource(response.source);
    setDrafts({ local: toDraft(config.local), cloud: toDraft(config.cloud) });
    setExtras((config.extra ?? []).map(extra => ({ id: extra.id, name: extra.name, locality: extra.locality, draft: toDraft(extra) })));
    setRules(config.rules ?? []);
    setCloudEnabled(config.cloud_enabled); setAnalysisMode(config.analysis_mode); setCloudFrom(config.cloud_from ?? 'moderate');
  }

  useEffect(() => {
    apiFetch('/v1/ugen/models')
      .then(async response => response.ok ? apply(await response.json() as ModelsResponse) : setLoadError(await errorMessage(response)))
      .catch(() => setLoadError('Cannot reach the gateway'));
  }, []);

  if (loadError) return <div className="settings-section first"><div className="error-box" role="alert"><span>{loadError}</span></div></div>;
  if (!drafts || !source) return <div className="settings-section first"><p>Loading…</p></div>;
  const readOnly = source === 'routing_file';
  const setProbe = (target: string, probe: Probe) => setProbes(current => ({ ...current, [target]: probe }));
  const draftFor = (target: string) => target === 'local' ? drafts!.local : target === 'cloud' ? drafts!.cloud : extras.find(extra => `extra:${extra.id}` === target)?.draft ?? blankDraft;

  async function probe(target: string, kind: 'test' | 'list') {
    const draft = draftFor(target);
    setProbe(target, { state: 'working' });
    try {
      const response = await apiFetch(`/v1/ugen/models/${kind}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ target, base_url: draft.baseUrl, model: draft.model, ...(draft.apiKey ? { api_key: draft.apiKey } : {}) }),
      });
      if (!response.ok) { setProbe(target, { state: 'error', message: await errorMessage(response) }); return; }
      const body = await response.json() as { ok?: boolean; error?: string; latency_ms?: number; models?: string[] };
      if (kind === 'list') {
        setModels(current => ({ ...current, [target]: body.models ?? [] }));
        setProbe(target, body.error ? { state: 'error', message: `Could not list models: ${body.error}` } : { state: 'ok', message: `${body.models?.length ?? 0} models found; pick one in the Model field` });
      } else {
        setProbe(target, body.ok ? { state: 'ok', message: `Connected (${body.latency_ms} ms)` } : { state: 'error', message: body.error ?? 'Failed' });
      }
    } catch { setProbe(target, { state: 'error', message: 'Cannot reach the gateway' }); }
  }

  function connection(draft: Draft, kind: 'local' | 'cloud' | 'extra') {
    return {
      base_url: draft.baseUrl.trim(), model: draft.model.trim(), alias: draft.alias.trim(),
      ...(draft.apiKey !== undefined ? { api_key: draft.apiKey } : {}),
      ...(kind === 'local' ? { analysis_model: draft.analysisModel.trim(), embedding_model: draft.embeddingModel.trim() } : {}),
      ...(kind !== 'local' ? { input_cost_per_million: Number(draft.inputCost || 0), output_cost_per_million: Number(draft.outputCost || 0) } : {}),
    };
  }

  async function save() {
    setSaving(true); setSaveState(null);
    try {
      const response = await apiFetch('/v1/ugen/models', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          local: connection(drafts!.local, 'local'), cloud: connection(drafts!.cloud, 'cloud'), cloud_enabled: cloudEnabled, analysis_mode: analysisMode, cloud_from: cloudFrom,
          extra: extras.map(extra => ({ id: extra.id, name: extra.name.trim() || 'Model', locality: extra.locality, ...connection(extra.draft, 'extra') })),
          rules: rules.map(rule => ({ ...rule, name: rule.name.trim() || 'Rule' })),
        }),
      });
      if (!response.ok) { setSaveState({ ok: false, message: await errorMessage(response) }); return; }
      apply(await response.json() as ModelsResponse);
      setSaveState({ ok: true, message: 'Saved. New requests use these models.' });
      onSaved();
    } catch { setSaveState({ ok: false, message: 'Cannot reach the gateway' }); } finally { setSaving(false); }
  }

  const fields = (target: 'local' | 'cloud') => <ConnectionFields id={target} locality={target} isMain draft={drafts[target]} onChange={draft => setDrafts(current => current && { ...current, [target]: draft })}
    models={models[target] ?? []} probe={probes[target] ?? { state: 'idle' }} onProbe={kind => void probe(target, kind)}/>;
  const updateExtra = (id: string, patch: Partial<Extra>) => setExtras(current => current.map(extra => extra.id === id ? { ...extra, ...patch } : extra));
  const targets = [{ id: 'local', label: 'Local model' }, { id: 'cloud', label: 'Cloud model' }, ...extras.map(extra => ({ id: extra.id, label: extra.name || 'Unnamed model' }))];
  const moveRule = (index: number, step: -1 | 1) => setRules(current => {
    const next = [...current]; const [rule] = next.splice(index, 1); next.splice(index + step, 0, rule); return next;
  });

  return <fieldset className="models-page" disabled={readOnly}>
    {source === 'env' && (drafts.local.model
      ? <p className="models-note">These values come from <code>.env</code>. Once you save here, these settings are used and the model settings in <code>.env</code> are ignored.</p>
      : <p className="models-note">No models are set up yet. Enter the local model server’s base URL and model, then save.</p>)}
    {readOnly && <p className="models-note">Models come from the <code>ROUTING_CONFIG</code> file, so they can only be changed there.</p>}

    <div className="settings-section first">
      <h3>Local model</h3>
      <p>Any OpenAI-compatible server on your own hardware, such as Ollama, LM Studio or vLLM. It answers simple requests and checks every request for personal data before anything can reach the cloud.</p>
      {fields('local')}
    </div>

    <div className="settings-section">
      <div className="section-head"><h3>Cloud model</h3>
        <label className="switch"><input type="checkbox" checked={cloudEnabled} onChange={event => setCloudEnabled(event.target.checked)}/><span aria-hidden="true"/>{cloudEnabled ? 'On' : 'Off'}</label>
      </div>
      <p>Any OpenAI SDK-compatible API. It handles moderate and complex requests in Auto mode and only ever receives text with personal data replaced by placeholders. Turning it off keeps these settings.</p>
      {fields('cloud')}
    </div>

    <div className="settings-section">
      <h3>More models</h3>
      <p>Extra local or cloud models that routing rules can use, for example a code model or a cheaper cloud model for summaries. Cloud models only ever receive redacted text.</p>
      <div className="rule-list">{extras.map(extra => <div className="rule-card" key={extra.id}>
        <div className="rule-head">
          <input value={extra.name} onChange={event => updateExtra(extra.id, { name: event.target.value })} placeholder="Name, e.g. Code model" aria-label="Model name" maxLength={60}/>
          <div className="segmented" role="radiogroup" aria-label="Where it runs">
            {(['local', 'cloud'] as const).map(value => <button type="button" key={value} role="radio" aria-checked={extra.locality === value} className={extra.locality === value ? 'selected' : ''} onClick={() => updateExtra(extra.id, { locality: value })}>{value === 'local' ? 'Local' : 'Cloud'}</button>)}
          </div>
          <button type="button" className="text-action danger" onClick={() => { setExtras(current => current.filter(item => item.id !== extra.id)); setRules(current => current.filter(rule => rule.target !== extra.id)); }}>Remove</button>
        </div>
        <ConnectionFields id={`extra:${extra.id}`} locality={extra.locality} isMain={false} draft={extra.draft} onChange={draft => updateExtra(extra.id, { draft })}
          models={models[`extra:${extra.id}`] ?? []} probe={probes[`extra:${extra.id}`] ?? { state: 'idle' }} onProbe={kind => void probe(`extra:${extra.id}`, kind)}/>
      </div>)}</div>
      <button type="button" className="cancel-button add-rule" onClick={() => setExtras(current => [...current, { id: newModelId(), name: '', locality: 'cloud', draft: { ...blankDraft } }])}>+ Add a model</button>
    </div>

    <div className="settings-section">
      <h3>Routing rules</h3>
      <p>Checked from the top in Auto mode; the first rule whose conditions all match picks the model. Requests no rule matches go to the cloud model when they are moderate or complex, otherwise stay local. Privacy rules still win: personal data that must stay local never goes to a cloud model.</p>
      <div className="rule-list">{rules.map((rule, index) => <RuleEditor key={index} rule={rule} targets={targets} first={index === 0} last={index === rules.length - 1}
        onChange={next => setRules(current => current.map((item, i) => i === index ? next : item))} onMove={step => moveRule(index, step)} onRemove={() => setRules(current => current.filter((_, i) => i !== index))}/>)}</div>
      <button type="button" className="cancel-button add-rule" onClick={() => setRules(current => [...current, { name: '', when: { complexity: [], task_type: [], contains_personal_data: null, min_chars: null, max_chars: null }, target: 'local' }])}>+ Add a rule</button>
    </div>

    <div className="settings-section">
      <h3>Difficulty check</h3>
      <div className="mode-options" role="radiogroup" aria-label="How requests are judged">
        <label className={analysisMode === 'hybrid' ? 'selected' : ''}><input type="radio" name="analysis-mode" checked={analysisMode === 'hybrid'} onChange={() => setAnalysisMode('hybrid')}/><span><b>Fast</b><small>Rules settle clear cases on their own, such as small talk, definitions, short rewrites or explicit design and debugging work. The local model judges everything else, so a word like “design” alone never decides.</small></span></label>
        <label className={analysisMode === 'llm' ? 'selected' : ''}><input type="radio" name="analysis-mode" checked={analysisMode === 'llm'} onChange={() => setAnalysisMode('llm')}/><span><b>Thorough</b><small>The local model judges every request’s difficulty and task first. Catches short hard questions and makes task-based rules reliable, at the cost of one extra local call each time.</small></span></label>
      </div>
    </div>

    <div className="settings-section">
      <h3>What goes to the cloud</h3>
      <p>In Auto mode, requests at least this difficult go to the cloud model; easier ones stay local. Routing rules above are checked first.</p>
      <div className="mode-options" role="radiogroup" aria-label="Which requests go to the cloud">
        <label className={cloudFrom === 'moderate' ? 'selected' : ''}><input type="radio" name="cloud-from" checked={cloudFrom === 'moderate'} onChange={() => setCloudFrom('moderate')}/><span><b>Moderate and complex</b><small>Anything that needs a real explanation or several steps, such as how HTTPS works or a function with some logic. Best answers; uses the cloud more.</small></span></label>
        <label className={cloudFrom === 'complex' ? 'selected' : ''}><input type="radio" name="cloud-from" checked={cloudFrom === 'complex'} onChange={() => setCloudFrom('complex')}/><span><b>Complex only</b><small>Only design, proofs, debugging and multi-part analysis. Explanations and everyday tasks stay local. Suits a capable local model and saves cost.</small></span></label>
      </div>
    </div>

    {!readOnly && <div className="models-save">
      {saveState && <span className={`probe ${saveState.ok ? 'ok' : 'error'}`} role="status">{saveState.message}</span>}
      <button type="button" className="send-button" onClick={() => void save()} disabled={saving}>{saving ? 'Saving…' : 'Save models'}</button>
    </div>}
  </fieldset>;
}

export default ModelsPage;
