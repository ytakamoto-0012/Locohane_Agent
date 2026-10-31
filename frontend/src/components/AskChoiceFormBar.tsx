import { useEffect, useState } from 'react';
import { useChatData } from '@chainlit/react-client';
import { useI18n } from '../i18n';

interface IMultiChoiceFormProps {
  question?: string;
  choices?: string[];
}

/**
 * ask_user_choice（multi_select=True 指定時）専用の表示コンポーネント。
 * backend では cl.AskElementMessage(spec.type === 'element') として届くため、
 * askUser.spec.element_id に一致するカスタム要素の props（question/choices）を
 * 元にチェックボックス一覧を描画し、送信時に askUser.callback へ
 * { submitted: true, values: 選択された選択肢の文字列配列, other: 自由記述文字列 }
 * を返す。キャンセル時は { submitted: false, values: [] } を返す
 * （backend 側は submitted===false をキャンセルとして扱う）。
 * AskFormBar（labels指定の自由記述フォーム）と同じ askUser.spec.type
 * ('element') を使うが、props に labels が無いため AskFormBar 側は
 * 何も描画しない（互いの props 形状で排他になる）。
 */
export function AskChoiceFormBar() {
  const { askUser, elements } = useChatData();
  const { t } = useI18n();

  const element =
    askUser?.spec.type === 'element'
      ? elements.find((e) => e.type === 'custom' && e.id === askUser.spec.element_id)
      : undefined;
  const props = element?.type === 'custom' ? (element.props as IMultiChoiceFormProps) : undefined;
  const choices = props?.choices ?? [];

  const [checked, setChecked] = useState<boolean[]>(() => choices.map(() => false));
  const [otherText, setOtherText] = useState('');

  useEffect(() => {
    setChecked(choices.map(() => false));
    setOtherText('');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [element?.id]);

  if (!askUser || askUser.spec.type !== 'element' || !element || choices.length === 0) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const values = choices.filter((_, i) => checked[i]);
    askUser.callback({ submitted: true, values, other: otherText.trim() });
  };

  const handleCancel = () => {
    askUser.callback({ submitted: false, values: [] });
  };

  return (
    <form className="ask-form-bar" onSubmit={handleSubmit}>
      {props?.question && <div className="ask-form-question">{props.question}</div>}
      <div className="ask-form-fields">
        {choices.map((choice, i) => (
          <label key={i} className="ask-choice-field">
            <input
              type="checkbox"
              checked={checked[i] ?? false}
              onChange={(e) => {
                const next = [...checked];
                next[i] = e.target.checked;
                setChecked(next);
              }}
            />
            <span>{choice}</span>
          </label>
        ))}
        <label className="ask-form-field">
          <span className="ask-form-label">{t('ask.otherFreeText')}</span>
          <input
            type="text"
            className="ask-form-input"
            value={otherText}
            onChange={(e) => setOtherText(e.target.value)}
          />
        </label>
      </div>
      <div className="ask-form-actions">
        <button type="button" className="ask-form-cancel" onClick={handleCancel}>
          {t('common.cancelWithIcon')}
        </button>
        <button type="submit" className="ask-form-submit">
          {t('common.send')}
        </button>
      </div>
    </form>
  );
}
