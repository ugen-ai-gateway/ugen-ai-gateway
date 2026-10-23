import type { Conversation, ConversationTurn } from './types';

// The model sees the conversation's memory plus only the last few exchanges.
// Older exchanges are folded into the memory (a summary written by the local
// model) a few at a time, so the summary is not rewritten on every message.
// KEEP_RECENT_TURNS exchanges are always sent word for word; once
// FOLD_BATCH_TURNS more older ones have built up (or FOLD_BATCH_CHARS of
// text), they are folded together.
const KEEP_RECENT_TURNS = 2;
const FOLD_BATCH_TURNS = 3;
const FOLD_BATCH_CHARS = 4_000;
// Recent exchanges are folded too when they alone would exceed this; the
// gateway's request limit is 32,000 characters.
const HARD_LIMIT = 12_000;
// Text folded per memory update, below the endpoint's limit.
export const FOLD_CHUNK = 40_000;

export type ChatMessage = { role: 'system' | 'user' | 'assistant'; content: string };

export function turnMessages(turns: ConversationTurn[]): ChatMessage[] {
  return turns.flatMap(turn => [
    { role: 'user' as const, content: turn.user },
    ...(turn.result ? [{ role: 'assistant' as const, content: turn.result.content }] : []),
  ]);
}

export const size = (turn: ConversationTurn) => turn.user.length + (turn.result?.content.length ?? 0);

/** The turns to fold into memory before sending, as [from, to), or null. */
export function turnsToRemember(conversation: Conversation, pending: ConversationTurn): [number, number] | null {
  const from = conversation.memory?.coveredTurns ?? 0;
  const count = conversation.turns.length;
  const keepFrom = Math.max(from, count - KEEP_RECENT_TURNS);
  const older = conversation.turns.slice(from, keepFrom);
  const olderChars = older.reduce((sum, turn) => sum + size(turn), 0);
  let to = from;
  if (older.length >= FOLD_BATCH_TURNS || olderChars >= FOLD_BATCH_CHARS) to = keepFrom;
  // Never fold the turn being sent; fold recent ones only when they are huge.
  let remaining = conversation.turns.slice(to).reduce((sum, turn) => sum + size(turn), size(pending));
  while (to < count && remaining > HARD_LIMIT) {
    remaining -= size(conversation.turns[to]);
    to += 1;
  }
  return to > from ? [from, to] : null;
}

/** What the model receives: the memory as a system message, then recent turns. */
export function requestMessages(conversation: Conversation, turns: ConversationTurn[]): ChatMessage[] {
  const memory = conversation.memory;
  const recent = turnMessages(turns.slice(memory?.coveredTurns ?? 0));
  return memory
    ? [{ role: 'system', content: `Memory of the earlier conversation (older messages were summarized):\n${memory.text}` }, ...recent]
    : recent;
}
