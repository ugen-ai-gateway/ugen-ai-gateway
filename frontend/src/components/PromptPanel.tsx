import type { ProviderSelection } from '../types';

type Props = {
  prompt: string; busy: boolean; error: string; providerSelection: ProviderSelection; cloudAvailable: boolean;
  onPromptChange: (value: string) => void; onProviderSelectionChange: (value: ProviderSelection) => void; onSubmit: () => void; onClear: () => void;
};
const PROVIDER_OPTIONS: { value: ProviderSelection; label: string; glyph: string; hint: string }[] = [
  { value: 'auto', label: '自動', glyph: '⇄', hint: '依本機分析結果自動選擇模型' },
  { value: 'local', label: '本機', glyph: '⌂', hint: '一律使用本機模型' },
  { value: 'cloud', label: '雲端', glyph: '✳', hint: '一律使用雲端模型（個資仍會先遮蔽）' },
];

function PromptPanel({ prompt, busy, error, providerSelection, cloudAvailable, onPromptChange, onProviderSelectionChange, onSubmit, onClear }: Props) {
  return <article className="panel prompt-panel">
    <div className="composer-topline">
      <span className="eyebrow">YOUR MESSAGE</span>
      <span className="char-count">{prompt.length.toLocaleString()} <i>/ 32,000</i></span>
    </div>
    <label className="sr-only" htmlFor="request-prompt">輸入你的請求</label>
    <textarea id="request-prompt" rows={3} value={prompt} onChange={event => onPromptChange(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); onSubmit(); } }} maxLength={32000} disabled={busy} placeholder="輸入訊息…（Enter 傳送，Shift + Enter 換行）"/>
    <div className="prompt-footer">
      <div className="provider-switch" role="radiogroup" aria-label="模型選擇">
        {PROVIDER_OPTIONS.map(option => {
          const unavailable = option.value === 'cloud' && !cloudAvailable;
          return <button key={option.value} type="button" role="radio" aria-checked={providerSelection === option.value}
            className={`provider-option ${option.value} ${providerSelection === option.value ? 'selected' : ''}`}
            title={unavailable ? '尚未設定雲端模型' : option.hint} disabled={busy || unavailable}
            onClick={() => onProviderSelectionChange(option.value)}>
            <i aria-hidden="true">{option.glyph}</i>{option.label}
          </button>;
        })}
      </div>
      <div className="prompt-actions">{prompt && <button className="clear-draft" onClick={onClear} disabled={busy}>清除草稿</button>}<button className="send-button" onClick={onSubmit} disabled={busy || !prompt.trim()}>{busy ? <><i className="spinner"/> 正在處理</> : <>傳送 <span>↗</span></>}</button></div></div>
    {error && <div className="error-box" role="alert"><b>請求未完成</b><span>{error}</span></div>}
  </article>;
}
export default PromptPanel;
