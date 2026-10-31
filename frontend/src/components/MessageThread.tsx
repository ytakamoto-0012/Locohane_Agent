import { useContext } from 'react';
import { useRecoilValue } from 'recoil';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeRaw from 'rehype-raw';
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize';
import { ChainlitContext, elementState, sessionIdState, type IStep } from '@chainlit/react-client';
import type { IMessageElement } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { FileLink } from './FileLink';
import { SUBAGENT_MESSAGE_AUTHOR } from '../utils/messageTree';

function MessageElements({ messageId }: { messageId: string }) {
  const elements = useRecoilValue(elementState);
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const messageElements = elements.filter(
    (e): e is IMessageElement & { type: 'file' | 'image' } =>
      e.forId === messageId && (e.type === 'file' || e.type === 'image')
  );
  if (messageElements.length === 0) return null;

  const resolveUrl = (e: IMessageElement) =>
    e.url || (e.chainlitKey ? client.getElementUrl(e.chainlitKey, sessionId) : undefined);

  const imageElements = messageElements.filter((e) => e.type === 'image');
  const fileElements = messageElements.filter((e) => e.type === 'file');

  return (
    <>
      {imageElements.length > 0 ? (
        <div className="message-images">
          {imageElements.map((e) => (
            <img key={e.id} src={resolveUrl(e)} alt={e.name} className="message-image" />
          ))}
        </div>
      ) : null}
      {fileElements.length > 0 ? (
        <div className="message-files">
          {fileElements.map((e) => (
            <FileLink key={e.id} href={resolveUrl(e)} name={e.name} />
          ))}
        </div>
      ) : null}
    </>
  );
}

/** メッセージの表示カテゴリ。ユーザー発言 / メインエージェントの回答 /
 *  システムメッセージ / サブエージェント（dispatch_agent）由来、の4種。 */
function messageCategory(step: IStep): 'user' | 'system' | 'subagent' | 'assistant' {
  if (step.type === 'user_message') return 'user';
  if (step.type === 'system_message') return 'system';
  if (step.name === SUBAGENT_MESSAGE_AUTHOR) return 'subagent';
  return 'assistant';
}

const AVATAR_LABEL: Record<'assistant' | 'system' | 'subagent', string> = {
  assistant: 'AI',
  system: 'SYS',
  subagent: 'SUB'
};

/** GFMのタスクリスト（- [ ] 項目）が生成する <input type="checkbox"> を
 *  rehype-sanitizeのデフォルトスキーマは許可しないため、明示的に追加する。 */
const markdownSanitizeSchema = {
  ...defaultSchema,
  tagNames: [...(defaultSchema.tagNames ?? []), 'input'],
  attributes: {
    ...defaultSchema.attributes,
    input: ['type', 'checked', 'disabled']
  }
};

function MessageBubble({ step }: { step: IStep }) {
  const category = messageCategory(step);
  const isUser = category === 'user';
  return (
    <div className={`message-row ${isUser ? 'message-row--user' : 'message-row--assistant'}`}>
      {!isUser ? <div className={`avatar avatar--${category}`}>{AVATAR_LABEL[category]}</div> : null}
      <div className={`message-bubble ${isUser ? 'message-bubble--user' : `message-bubble--${category}`}`}>
        <div className="message-bubble-content">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeRaw, [rehypeSanitize, markdownSanitizeSchema]]}
            components={{
              a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />
            }}
          >
            {step.output || ''}
          </ReactMarkdown>
          {step.streaming ? <span className="streaming-cursor" /> : null}
        </div>
        <MessageElements messageId={step.id} />
      </div>
      {isUser ? <div className="avatar avatar--user">You</div> : null}
    </div>
  );
}

function TypingIndicator() {
  return (
    <div className="message-row message-row--assistant">
      <div className="avatar avatar--assistant">AI</div>
      <div className="message-bubble message-bubble--assistant message-bubble--typing">
        <span className="typing-dots">
          <span />
          <span />
          <span />
        </span>
      </div>
    </div>
  );
}

export function MessageThread({ messages, showTyping }: { messages: IStep[]; showTyping: boolean }) {
  const { t } = useI18n();
  if (messages.length === 0 && !showTyping) {
    return <div className="message-thread-empty">{t('messages.empty')}</div>;
  }

  return (
    <div className="message-thread">
      {messages.map((step) => (
        <MessageBubble key={step.id} step={step} />
      ))}
      {showTyping ? <TypingIndicator /> : null}
    </div>
  );
}
