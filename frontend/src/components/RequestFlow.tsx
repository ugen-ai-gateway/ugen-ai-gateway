import type { AnalysisSource, Phase, PiiState, Provider, ProviderSelection } from '../types';

type Props = {
  phase: Phase;
  provider: Provider | null;
  providerSelection: ProviderSelection;
  model: string;
  localModel: string;
  cloudModel: string;
  modelsStatus: 'loading' | 'loaded' | 'error';
  pii: PiiState | null;
  analysisSource: AnalysisSource | null;
  fallbackFrom: Provider | null;
  injectionSignals: string[];
};

function ProcessCard({ glyph, label, title, className = '' }: {
  glyph: string; label: string; title: string; className?: string;
}) {
  return <div className={`flow-v4-card ${className}`}>
    <span className="flow-v4-icon" aria-hidden="true">{glyph}</span>
    <span className="flow-v4-copy"><small>{label}</small><b title={title}>{title}</b></span>
  </div>;
}

function FlowBranch({ provider, animated }: { provider: Provider | null; animated: boolean }) {
  return <div className={`flow-v4-branch ${provider ? `selected ${provider}` : ''}`} aria-hidden="true">
    <i className="flow-v4-branch-stem" />
    <i className={`flow-v4-branch-route local ${provider === 'local' ? 'chosen' : ''}`} />
    <i className={`flow-v4-branch-route cloud ${provider === 'cloud' ? 'chosen' : ''}`} />
    <i className={`flow-v4-tip local ${provider === 'local' ? 'chosen' : ''}`} />
    <i className={`flow-v4-tip cloud ${provider === 'cloud' ? 'chosen' : ''}`} />
    {provider && animated && <i className={`flow-v4-branch-dot ${provider}`} />}
  </div>;
}

const PII_LABEL: Record<string, string> = {
  email: 'email', taiwan_mobile: 'mobile number', taiwan_landline: 'landline', taiwan_id: 'national ID',
  taiwan_resident_certificate: 'resident certificate', taiwan_ubn: 'business ID', passport: 'passport', nhi_card: 'NHI card',
  credit_card: 'credit card', api_key: 'API key', jwt: 'access token', private_key: 'private key', password: 'password',
  person_name: 'name', location: 'location',
};

function piiSummary(pii: PiiState): string {
  const types = pii.types.map(type => PII_LABEL[type] ?? type).join(', ') || 'identified data';
  return pii.redacted ? `${types} · ${pii.redacted} redacted, restored locally in the reply` : `${types} · handled by the privacy policy`;
}

const SELECTION_LABEL: Record<ProviderSelection, string> = { auto: 'Auto routing', local: 'Local only', cloud: 'Cloud only' };

const SOURCE_LABEL: Record<AnalysisSource, string> = { llm: 'LOCAL MODEL', rules: 'FAST RULES', cache: 'CACHED' };

function RequestFlow({ phase, provider, providerSelection, model, localModel, cloudModel, modelsStatus, pii, analysisSource, fallbackFrom, injectionSignals }: Props) {
  const busy = !['idle', 'complete', 'failed'].includes(phase);
  const analyzed = ['analyzed', 'routing', 'generating', 'complete'].includes(phase);
  const routing = phase === 'routing' || phase === 'generating';
  const localName = provider === 'local' && model ? model : localModel || (modelsStatus === 'loading' ? 'Loading model settings…' : modelsStatus === 'error' ? 'Could not load model settings' : 'Not configured');
  const cloudName = provider === 'cloud' && model ? model : cloudModel || (modelsStatus === 'loading' ? 'Loading model settings…' : modelsStatus === 'error' ? 'Could not load model settings' : 'Not configured');

  return <section className="flow-card flow-v4" aria-label="Request routing flow">
    <div className="flow-heading">
      <div><span className="eyebrow">LIVE ROUTING</span><h2>Request routing</h2></div>
      <div className="flow-heading-status">
        <span className={`route-mode ${providerSelection}`}>{SELECTION_LABEL[providerSelection]}</span>
        {injectionSignals.length > 0 && <span className="route-mode injection" title={`Possible prompt injection: ${injectionSignals.join(', ')}`}>Possible injection</span>}
        {fallbackFrom && <span className="route-mode fallback" title={fallbackFrom === 'cloud' ? 'The cloud did not respond; the local model answered' : 'The local model did not respond; the cloud answered'}>Fallback used</span>}
        <span className={`live-status ${busy ? 'active' : ''}`}><i />{busy ? 'Working' : phase === 'complete' ? 'Completed' : phase === 'failed' ? 'Failed' : 'Waiting'}</span>
      </div>
    </div>

    <div className="flow-v4-scroll">
    <div className="flow-v4-steps">
      <ProcessCard glyph="↗" label="STEP 01 · INPUT" title="User request" className={busy || phase === 'complete' ? 'done' : ''} />
      <div className={`flow-v4-link ${phase === 'sending' || phase === 'analyzing' ? 'active' : analyzed ? 'done' : ''}`} aria-hidden="true"><i /></div>
      <ProcessCard glyph="⌕" label={`STEP 02 · ${analysisSource ? SOURCE_LABEL[analysisSource] : 'LOCAL CHECK'}`} title="Local analysis and privacy check" className={`analysis ${phase === 'analyzing' ? 'working' : analyzed ? 'done' : ''}`} />
      <FlowBranch provider={provider} animated={routing} />
      <div className="flow-v4-targets">
        <ProcessCard glyph="⌂" label="STEP 03A · LOCAL" title={localName} className={`local ${provider === 'local' ? 'chosen' : ''} ${phase === 'generating' && provider === 'local' ? 'working' : ''} ${modelsStatus === 'error' && !localModel && !(provider === 'local' && model) ? 'config-error' : ''}`} />
        <ProcessCard glyph="✳" label="STEP 03B · CLOUD" title={cloudName} className={`cloud ${provider === 'cloud' ? 'chosen' : ''} ${phase === 'generating' && provider === 'cloud' ? 'working' : ''} ${modelsStatus === 'error' && !cloudModel && !(provider === 'cloud' && model) ? 'config-error' : ''}`} />
      </div>
    </div>
    </div>

    <div className={`privacy-result ${pii ? pii.found ? 'found' : 'clear' : ''}`}><span className="privacy-symbol">{pii?.found ? '!' : '⌑'}</span><span><b>{pii ? pii.found ? 'Personal data detected' : 'No personal data detected' : 'Privacy check'}</b>{pii && <small>{pii.found ? piiSummary(pii) : 'Checked on this machine'}</small>}</span><span className="privacy-lock">LOCAL CHECK</span></div>
  </section>;
}

export default RequestFlow;
