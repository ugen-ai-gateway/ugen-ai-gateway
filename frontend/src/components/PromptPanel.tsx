import type { ProviderSelection } from '../types';

type Props = {
  prompt: string; busy: boolean; error: string; providerSelection: ProviderSelection; cloudAvailable: boolean;
  onPromptChange: (value: string) => void; onProviderSelectionChange: (value: ProviderSelection) => void; onSubmit: () => void; onStop: () => void; onClear: () => void;
};
const PROVIDER_OPTIONS: { value: ProviderSelection; label: string; glyph: string; hint: string }[] = [
  { value: 'auto', label: 'Auto', glyph: '⇄', hint: 'Pick the model from the local analysis' },
  { value: 'local', label: 'Local', glyph: '⌂', hint: 'Always use the local model' },
  { value: 'cloud', label: 'Cloud', glyph: '✳', hint: 'Always use the cloud model (personal data is still redacted first)' },
];

function PromptPanel({ prompt, busy, error, providerSelection, cloudAvailable, onPromptChange, onProviderSelectionChange, onSubmit, onStop, onClear }: Props) {
  return <article className="panel prompt-panel">
    <div className="composer-topline">
      <span className="eyebrow">YOUR MESSAGE</span>
      <span className="char-count">{prompt.length.toLocaleString()} <i>/ 32,000</i></span>
    </div>
    <label className="sr-only" htmlFor="request-prompt">Your message</label>
    <textarea id="request-prompt" rows={3} value={prompt} onChange={event => onPromptChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); onSubmit(); } }} maxLength={32000} disabled={busy} placeholder="Type a message… (Enter to send, Shift + Enter for a new line)"/>
    <div className="prompt-footer">
      <div className="provider-switch" role="radiogroup" aria-label="Model selection">
        {PROVIDER_OPTIONS.map(option => {
          const unavailable = option.value === 'cloud' && !cloudAvailable;
          return <button key={option.value} type="button" role="radio" aria-checked={providerSelection === option.value}
            className={`provider-option ${option.value} ${providerSelection === option.value ? 'selected' : ''}`}
            title={unavailable ? 'No cloud model is configured' : option.hint} disabled={busy || unavailable}
            onClick={() => onProviderSelectionChange(option.value)}>
            <i aria-hidden="true">{option.glyph}</i>{option.label}
          </button>;
        })}
      </div>
      <div className="prompt-actions">{prompt && <button className="clear-draft" onClick={onClear} disabled={busy}>Clear draft</button>}{busy
        ? <button className="send-button stop-button" onClick={onStop} title="Stop generating"><i className="spinner"/> Stop</button>
        : <button className="send-button" onClick={onSubmit} disabled={!prompt.trim()}>Send <span>↗</span></button>}</div></div>
    {error && <div className="error-box" role="alert"><b>Request not completed</b><span>{error}</span></div>}
  </article>;
}
export default PromptPanel;
