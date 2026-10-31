import { useContext, useEffect, useRef, useState } from 'react';
import { useRecoilValue } from 'recoil';
import { ChainlitContext, sessionIdState, type IAction } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { Icon } from './Icon';

/**
 * ツールバーの「推論の深さ（reasoning_effort）」選択。
 *
 * 選んだ値はブラウザの localStorage に保存し、送信時（Composer.tsx・StarterPrompts.tsx）に
 * メッセージの metadata.reasoning_effort へ入れる。app.py の on_message がその値で
 * [llm].reasoning_effort を上書きしてグラフを組み直す（空文字なら config.ini の値）。
 * 生成中は変更できないようボタンを無効化する（送信時の値でターンが確定するため、
 * 変えても今の生成には効かず紛らわしい）。
 */

const STORAGE_KEY = 'la-reasoning-effort';

// src/config.py の LLM_REASONING_EFFORTS（"default" は「送らない」と同じなので除く）。
const EFFORT_CHOICES = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;

export function getStoredReasoningEffort(): string {
  const raw = localStorage.getItem(STORAGE_KEY) ?? '';
  return (EFFORT_CHOICES as readonly string[]).includes(raw) ? raw : '';
}

function storeReasoningEffort(value: string): void {
  if (value) localStorage.setItem(STORAGE_KEY, value);
  else localStorage.removeItem(STORAGE_KEY);
}

function makeAction(name: string, payload: Record<string, unknown>): IAction {
  return { id: '', name, payload, label: '', tooltip: '', forId: '', onClick: () => {} };
}

interface ReasoningEffortButtonProps {
  // true: 生成中。変更をブロックし、理由をツールチップで説明する。
  disabled?: boolean;
}

export function ReasoningEffortButton({ disabled = false }: ReasoningEffortButtonProps) {
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(getStoredReasoningEffort);
  // config.ini の [llm].reasoning_effort（null: 未取得、'': 空欄）。
  const [configValue, setConfigValue] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // 開いている間に生成が始まったら閉じる。
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  useEffect(() => {
    if (!open || configValue !== null) return;
    client
      .callAction(makeAction('reasoning_effort_info', {}), sessionId)
      .then((res) => setConfigValue(String(res?.response?.default ?? '')))
      .catch(() => setConfigValue(''));
  }, [open, configValue, client, sessionId]);

  const choose = (next: string) => {
    if (disabled) return;
    storeReasoningEffort(next);
    setValue(next);
    setOpen(false);
  };

  const configLabel = configValue
    ? t('effort.useConfigWithValue', { value: configValue })
    : configValue === ''
      ? t('effort.useConfigWithValue', { value: t('effort.unset') })
      : t('effort.useConfig');

  return (
    <div className="effort-picker" ref={containerRef}>
      <button
        type="button"
        className={'effort-button' + (value ? ' is-set' : '')}
        title={disabled ? t('effort.lockedTitle') : t('effort.setTitle')}
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
      >
        <Icon name="gauge" />
        {value ? <span className="effort-button-label">{value}</span> : null}
      </button>
      {!disabled && open && (
        <div className="effort-popover">
          <div className="effort-popover-title">{t('effort.title')}</div>
          <ul className="effort-options" role="listbox" aria-label={t('effort.title')}>
            {['', ...EFFORT_CHOICES].map((choice) => (
              <li
                key={choice || 'config'}
                role="option"
                aria-selected={choice === value}
                className={'effort-option' + (choice === value ? ' is-active' : '')}
                onClick={() => choose(choice)}
              >
                <span className="effort-option-check">{choice === value ? '✓' : ''}</span>
                {choice === '' ? configLabel : choice === 'none' ? t('effort.noneLabel') : choice}
              </li>
            ))}
          </ul>
          <div className="effort-popover-note">{t('effort.note')}</div>
        </div>
      )}
    </div>
  );
}
