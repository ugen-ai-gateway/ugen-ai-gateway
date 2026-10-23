import { useEffect, useState } from 'react';
import { apiFetch } from '../api';
import type { Conversation, Phase, UsageStats } from '../types';

type Props = { conversations: Conversation[]; phase: Phase; refreshKey: number };
const PHASE_LABEL: Record<Phase, string> = {
  idle: 'Waiting', sending: 'Sending', analyzing: 'Analyzing locally', analyzed: 'Analyzed',
  routing: 'Routing', generating: 'Generating', complete: 'Completed', failed: 'Failed',
};

const dayKey = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
const formatUsd = (value: number) => value >= 1 ? `$${value.toFixed(2)}` : value >= 0.0001 ? `$${value.toFixed(4)}` : value > 0 ? '<$0.0001' : '$0';

/** Gateway-wide usage from the backend; falls back to this browser's history if unavailable. */
function useUsageStats(refreshKey: number): UsageStats | null {
  const [stats, setStats] = useState<UsageStats | null>(null);
  useEffect(() => {
    let active = true;
    const offset = -new Date().getTimezoneOffset();
    apiFetch(`/v1/ugen/stats?days=7&tz_offset_minutes=${offset}`)
      .then(response => response.ok ? response.json() as Promise<UsageStats> : null)
      .then(data => { if (active) setStats(data); })
      .catch(() => { if (active) setStats(null); });
    return () => { active = false; };
  }, [refreshKey]);
  return stats;
}

function MonitorPanel({ conversations, phase, refreshKey }: Props) {
  const stats = useUsageStats(refreshKey);
  const turns = conversations.flatMap(conversation => conversation.turns.map(turn => ({ ...turn, updatedAt: conversation.updatedAt })));
  const results = turns.flatMap(turn => turn.result ? [turn.result] : []);
  const completed = stats?.completed ?? results.length;
  const localCount = stats?.local ?? results.filter(result => result.provider === 'local').length;
  const cloudCount = stats?.cloud ?? results.filter(result => result.provider === 'cloud').length;
  const generationTokens = stats?.completion_tokens ?? results.reduce((sum, result) => sum + (result.usage?.generation?.completion_tokens ?? 0), 0);
  const generationSeconds = results.reduce((sum, result) => sum + (result.performance?.generation_ms ?? 0) / 1000, 0);
  const averageSpeed = stats ? stats.tokens_per_second?.toFixed(1) ?? '—' : generationSeconds > 0 ? (generationTokens / generationSeconds).toFixed(1) : '—';
  const recent = [...turns].filter(turn => turn.result).sort((a, b) => b.updatedAt - a.updatedAt).slice(0, 4);
  const serverDaily = new Map((stats?.daily ?? []).map(day => [day.day, day.requests]));
  const days = Array.from({ length: 7 }, (_, index) => {
    const date = new Date(); date.setHours(0, 0, 0, 0); date.setDate(date.getDate() - (6 - index));
    const count = stats
      ? serverDaily.get(dayKey(date)) ?? 0
      : turns.filter(turn => { const updated = new Date(turn.updatedAt); updated.setHours(0, 0, 0, 0); return updated.getTime() === date.getTime(); }).length;
    return { label: new Intl.DateTimeFormat('en-US', { weekday: 'short' }).format(date), count };
  });
  const maxDay = Math.max(1, ...days.map(day => day.count));

  return <>
    <section className="monitor-stats" aria-label="Request statistics">
      <article className="metric-card">
        <div className="metric-card-heading"><span>Completed requests</span><i className="metric-mark" aria-hidden="true">↗</i></div>
        <b className="metric-card-value">{completed}</b>
        <small className="metric-card-caption">{stats ? `Last 7 days · failed: ${stats.failed}` : 'Exchanges on this device'}</small>
      </article>
      <article className="metric-card">
        <div className="metric-card-heading"><span>Routing split</span><i className="metric-mark local-mark" aria-hidden="true">⌂</i></div>
        <div className="metric-card-split"><span><b>{localCount}</b><small>local</small></span><i>/</i><span><b>{cloudCount}</b><small>cloud</small></span></div>
        <small className="metric-card-caption">By the model that answered</small>
      </article>
      <article className="metric-card">
        <div className="metric-card-heading"><span>Avg. generation speed</span><i className="metric-mark speed-mark" aria-hidden="true">⌁</i></div>
        <b className="metric-card-value">{averageSpeed}<em>{averageSpeed === '—' ? '' : ' tok/s'}</em></b>
        <small className="metric-card-caption">{generationTokens.toLocaleString()} tokens generated</small>
      </article>
      <article className="metric-card savings-card">
        <div className="metric-card-heading"><span>Cost saved</span><i className="metric-mark savings-mark" aria-hidden="true">$</i></div>
        <b className="metric-card-value">{stats ? formatUsd(stats.savings_usd) : '—'}</b>
        <small className="metric-card-caption">{stats ? `Spent ${formatUsd(stats.cost_usd)} · all-cloud ${formatUsd(stats.baseline_cost_usd)}` : 'Needs gateway usage data'}</small>
      </article>
    </section>
    <section className="panel activity-panel">
      <div className="monitor-panel-heading"><div><span className="eyebrow">ACTIVITY / 7 DAYS</span><h2>Request activity</h2></div><span className="activity-total">{days.reduce((sum, day) => sum + day.count, 0)} total</span></div>
      <div className="activity-chart" role="img" aria-label={`${days.reduce((sum, day) => sum + day.count, 0)} requests in the last 7 days`}>
        {days.map((day, index) => <div className="activity-day" key={`${day.label}-${index}`}><div className="activity-bar-track"><i style={{ height: `${Math.max(day.count ? 12 : 4, day.count / maxDay * 100)}%` }}/></div><small>{day.label}</small></div>)}
      </div>
    </section>
    <section className="panel terminal-panel">
      <div className="monitor-panel-heading"><div><span className="eyebrow">LOCAL TELEMETRY</span><h2><i className={`terminal-status ${!['idle', 'complete', 'failed'].includes(phase) ? 'active' : ''}`}/>Request log</h2></div><span className="terminal-live">{PHASE_LABEL[phase]}</span></div>
      <div className="terminal-body">
        <div className="terminal-line system-line"><span>›</span> gateway <i>·</i> {PHASE_LABEL[phase]}</div>
        {recent.length ? recent.map((turn, index) => {
          const result = turn.result!;
          const tokens = result.usage?.generation?.completion_tokens;
          const seconds = (result.performance?.generation_ms ?? 0) / 1000;
          const speed = tokens !== undefined && seconds > 0 ? `${(tokens / seconds).toFixed(1)} tok/s` : 'speed not reported';
          return <div className="terminal-entry" key={turn.id} style={{ animationDelay: `${index * 45}ms` }}>
            <span className={`terminal-provider ${result.provider}`}>{result.provider === 'cloud' ? 'CLOUD' : 'LOCAL'}</span>
            <div className="terminal-entry-copy"><b>{result.model}</b><small>{turn.user.replace(/\s+/g, ' ').slice(0, 46)}{turn.user.length > 46 ? '…' : ''}</small></div>
            <span className="terminal-speed">{speed}</span>
          </div>;
        }) : <div className="terminal-empty">No completed requests yet. Send a message to see where it was routed.</div>}
        <div className="terminal-cursor"><span>›</span><i/></div>
      </div>
    </section>
  </>;
}

export default MonitorPanel;
