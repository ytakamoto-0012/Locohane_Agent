import { useEffect, useState, type KeyboardEvent } from 'react';
import { v4 as uuidv4 } from 'uuid';
import { useChatData, useChatInteract } from '@chainlit/react-client';
import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';

/**
 * AskUserQuestion（labels省略時の自由記述単発質問）専用の表示コンポーネント。
 * backend では cl.AskUserMessage(spec.type === 'text') として届く。
 * 質問文自体は既にアシスタントメッセージとしてスレッドに表示済みのため、
 * ここでは入力欄と送信ボタンのみを提供する。送信は Composer の
 * isReplying 経路と同じ replyMessage を使う（返信経路を増やさない）。
 * キャンセル時も同じ経路で「❌ キャンセル」を metadata.askCancel=true 付きで返し、
 * backend 側（src/tools/_ask_echo.py の _is_text_ask_cancelled）がキャンセルとして扱う
 * （AskUserMessage の応答は user_message 形式しか返せないため）。
 */
export function AskTextBar() {
  const { askUser } = useChatData();
  const { replyMessage } = useChatInteract();
  const { t } = useI18n();
  const [value, setValue] = useState('');

  const stepId = askUser?.spec.type === 'text' ? askUser.spec.step_id : undefined;

  useEffect(() => {
    setValue('');
  }, [stepId]);

  if (!askUser || askUser.spec.type !== 'text') return null;

  const reply = (output: string, metadata: Record<string, unknown> = {}) => {
    const message: IStep = {
      threadId: '',
      id: uuidv4(),
      name: 'あなた',
      type: 'user_message',
      output,
      createdAt: new Date().toISOString(),
      metadata
    };
    replyMessage(message);
    setValue('');
  };

  const submit = () => {
    if (!value.trim()) return;
    reply(value);
  };

  const handleCancel = () => {
    reply(t('common.cancelWithIcon'), { askCancel: true });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    submit();
  };

  return (
    <form className="ask-text-bar" onSubmit={handleSubmit}>
      <textarea
        className="ask-text-input"
        value={value}
        placeholder={t('ask.answerPlaceholder')}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        rows={3}
        autoFocus
      />
      <div className="ask-form-actions">
        <button type="button" className="ask-form-cancel" onClick={handleCancel}>
          {t('common.cancelWithIcon')}
        </button>
        <button type="submit" className="ask-text-submit">
          {t('common.send')}
        </button>
      </div>
    </form>
  );
}
