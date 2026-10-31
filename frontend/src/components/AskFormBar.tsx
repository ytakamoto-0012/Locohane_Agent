import { useEffect, useState } from 'react';
import { useChatData } from '@chainlit/react-client';
import { useI18n } from '../i18n';

interface IMultiTextFormProps {
  question?: string;
  labels?: string[];
}

/**
 * AskUserQuestion（labels指定時の複数項目自由記述フォーム）専用の表示コンポーネント。
 * backend では cl.AskElementMessage(spec.type === 'element') として届くため、
 * askUser.spec.element_id に一致するカスタム要素の props（question/labels）を
 * 元にラベル付き入力欄をまとめて描画し、送信時に askUser.callback へ
 * { submitted: true, values } をそのまま返す（AskElementResponse は任意キー可）。
 * キャンセル時は { submitted: false, values: [] } を返す
 * （AskChoiceFormBar と同じく backend 側は submitted===false をキャンセルとして扱う）。
 */
export function AskFormBar() {
  const { askUser, elements } = useChatData();
  const { t } = useI18n();

  const element =
    askUser?.spec.type === 'element'
      ? elements.find((e) => e.type === 'custom' && e.id === askUser.spec.element_id)
      : undefined;
  const props = element?.type === 'custom' ? (element.props as IMultiTextFormProps) : undefined;
  const labels = props?.labels ?? [];

  const [values, setValues] = useState<string[]>(() => labels.map(() => ''));

  useEffect(() => {
    setValues(labels.map(() => ''));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [element?.id]);

  if (!askUser || askUser.spec.type !== 'element' || !element || labels.length === 0) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    askUser.callback({ submitted: true, values });
  };

  const handleCancel = () => {
    askUser.callback({ submitted: false, values: [] });
  };

  return (
    <form className="ask-form-bar" onSubmit={handleSubmit}>
      {props?.question && <div className="ask-form-question">{props.question}</div>}
      <div className="ask-form-fields">
        {labels.map((label, i) => (
          <label key={i} className="ask-form-field">
            <span className="ask-form-label">{label}</span>
            <input
              type="text"
              className="ask-form-input"
              value={values[i] ?? ''}
              onChange={(e) => {
                const next = [...values];
                next[i] = e.target.value;
                setValues(next);
              }}
            />
          </label>
        ))}
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
