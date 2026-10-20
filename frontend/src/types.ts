export type Phase = 'idle' | 'sending' | 'analyzing' | 'analyzed' | 'routing' | 'generating' | 'complete' | 'failed';
export type Provider = 'local' | 'cloud';
export type ProviderSelection = 'auto' | Provider;
export type ChatResult = {
  content: string;
  model: string;
  provider: string;
  routing: {
    complexity: string;
    task_type: string;
    contains_personal_data: boolean;
    personal_data_types: string[];
    reason: string;
  };
  usage?: { generation?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } };
  performance?: { analysis_ms: number; generation_ms: number; total_ms: number };
};
export type ConversationTurn = { id: string; user: string; result?: ChatResult };
export type Conversation = { id: string; title: string; updatedAt: number; turns: ConversationTurn[] };
export type GatewayEvent = { type: string; [key: string]: unknown };
