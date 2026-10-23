import type { Conversation } from './types';

const fileName = (conversation: Conversation, extension: string) =>
  `${conversation.title.replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '').slice(0, 40) || 'conversation'}.${extension}`;

export function conversationMarkdown(conversation: Conversation): string {
  const lines = [`# ${conversation.title}`, '', `_Exported ${new Date().toISOString().slice(0, 16).replace('T', ' ')} UTC_`, ''];
  if (conversation.memory) lines.push('## Memory of earlier messages', '', conversation.memory.text, '');
  for (const turn of conversation.turns) {
    lines.push('## You', '', turn.user, '');
    if (turn.result) {
      const where = turn.result.provider === 'cloud' ? 'cloud' : 'local';
      lines.push(`## UGen (${where}${turn.result.stopped ? ', stopped' : ''})`, '', turn.result.content, '');
    }
  }
  return lines.join('\n');
}

/** Save a conversation as Markdown or JSON through a download. */
export function downloadConversation(conversation: Conversation, format: 'markdown' | 'json'): void {
  const [content, type, extension] = format === 'markdown'
    ? [conversationMarkdown(conversation), 'text/markdown', 'md']
    : [JSON.stringify(conversation, null, 2), 'application/json', 'json'];
  const url = URL.createObjectURL(new Blob([content], { type: `${type};charset=utf-8` }));
  const link = Object.assign(document.createElement('a'), { href: url, download: fileName(conversation, extension) });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
