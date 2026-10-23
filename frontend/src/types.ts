export type Phase = 'idle' | 'sending' | 'analyzing' | 'analyzed' | 'routing' | 'generating' | 'complete' | 'failed';
export type Provider = 'local' | 'cloud';
export type ProviderSelection = 'auto' | Provider;
export type ChatResult = {
  id?: string;
  content: string;
  model: string;
  provider: string;
  routing: {
    complexity: string;
    task_type: string;
    contains_personal_data: boolean;
    personal_data_types: string[];
    reason: string;
    redacted_values?: number;
    analysis_source?: AnalysisSource;
    fallback_from?: Provider | null;
    provider_name?: string;
    rule?: string | null;
    injection_signals?: string[];
    output_findings?: string[];
  };
  usage?: { generation?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } };
  performance?: { analysis_ms: number; generation_ms: number; total_ms: number };
  /** The user stopped generation; ``content`` is the partial answer. */
  stopped?: boolean;
};
/** ``author``: who asked, in a shared chat (a username). */
export type ConversationTurn = { id: string; user: string; author?: string; result?: ChatResult; feedback?: 1 | -1 };
/** Older turns folded into a summary by the local model; ``coveredTurns`` counts them from the start. */
export type ConversationMemory = { text: string; coveredTurns: number; updatedAt: number };
/** Set by the gateway for a shared chat: its owner, members and the viewer's role. */
export type SharedInfo = { owner: string; members: string[]; role: 'owner' | 'member' };
/** ``sharedFrom``: this is the viewer's own copy of someone's share link. */
export type Conversation = { id: string; title: string; updatedAt: number; turns: ConversationTurn[]; memory?: ConversationMemory; shared?: SharedInfo; sharedFrom?: { id: string; owner: string } };
export type GatewayEvent = { type: string; [key: string]: unknown };
export type PiiState = { found: boolean; types: string[]; redacted?: number };
export type AnalysisSource = 'llm' | 'rules' | 'cache';
export type UsageStats = {
  requests: number; completed: number; failed: number; local: number; cloud: number;
  prompt_tokens: number; completion_tokens: number; tokens_per_second: number | null;
  cost_usd: number; baseline_cost_usd: number; savings_usd: number;
  with_personal_data: number; redacted_values: number; fallbacks: number;
  daily: { day: string; requests: number; cloud: number }[];
  feedback: { up: number; down: number };
};
