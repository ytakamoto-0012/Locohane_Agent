import { useContext, useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { useRecoilValue } from 'recoil';
import { ChainlitContext, sessionIdState, type IAction } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { Icon } from './Icon';
import { isLoopbackHost } from '../utils/serverHost';

/**
 * ツールバーの「作業フォルダ」アイコン。app.py の @cl.action_callback("pick_work_dir") を呼ぶ。
 *
 * OSネイティブのフォルダ選択ダイアログはバックエンド側（サーバーPC）にしか
 * 表示できず、サーバーとブラウザが別マシンの構成だと気づかれずクライアントが
 * 固まって見える問題があったため、パス文字列をこのポップオーバーで直接
 * 入力させる方式にしている（2026-08-19 ユーザー報告）。
 *
 * 入力するのはブラウザのPCではなくサーバーPC（Locohane が動いているPC）から見た
 * パスであることを見出し・説明文で明示し、数文字入力するとサーバーPC上の実在する
 * フォルダ候補（app.py の suggest_work_dir）を出す。存在しない・読み取れない
 * フォルダは pick_work_dir がエラーを返すので、ポップオーバーを閉じずに表示する。
 * localhost / 127.0.0.1 で開いている（ブラウザとサーバーが同じPC）場合は
 * パスの区別が無いので、見出し・注意書きは出さず従来の表示にする。それ以外
 * （0.0.0.0 で待ち受けて LAN の IP 等で開いた場合）は、サーバーPCのホスト名
 * （app.py の work_dir_server_info）を、取得できなければ接続先の IP を出す。
 */

// この文字数以上入力したら候補を問い合わせる。
const SUGGEST_MIN_CHARS = 2;
// 入力が止まってから問い合わせるまでの待ち時間（ミリ秒）。
const SUGGEST_DEBOUNCE_MS = 250;

function makeAction(name: string, payload: Record<string, unknown>): IAction {
  return { id: '', name, payload, label: '', tooltip: '', forId: '', onClick: () => {} };
}
interface WorkDirButtonProps {
  // true: このチャットは既に1回以上送信済み（または再開したスレッド）。
  // 作業ディレクトリの変更はUI上ブロックし、理由をツールチップで説明する。
  disabled?: boolean;
}

export function WorkDirButton({ disabled = false }: WorkDirButtonProps) {
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const { t } = useI18n();
  const [pending, setPending] = useState(false);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [highlight, setHighlight] = useState(-1);
  const [serverHostname, setServerHostname] = useState<string | null>(null);
  const remote = !isLoopbackHost();
  const suggestSeq = useRef(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setError(null);
      setSuggestions([]);
      return;
    }
    inputRef.current?.focus();
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  // 別PCから開いているときだけ、初回に開いた時点でサーバーPCのホスト名を問い合わせる。
  useEffect(() => {
    if (!open || !remote || serverHostname !== null) return;
    client
      .callAction(makeAction('work_dir_server_info', {}), sessionId)
      .then((res) => setServerHostname(String(res?.response?.hostname ?? '')))
      .catch(() => setServerHostname(''));
  }, [open, remote, serverHostname, client, sessionId]);

  useEffect(() => {
    if (!open) return;
    const text = value.trim();
    const seq = ++suggestSeq.current;
    if (text.length < SUGGEST_MIN_CHARS) {
      setSuggestions([]);
      setHighlight(-1);
      return;
    }
    const timer = window.setTimeout(async () => {
      try {
        const res = await client.callAction(makeAction('suggest_work_dir', { text }), sessionId);
        if (seq !== suggestSeq.current) return; // 古い問い合わせの結果は捨てる
        setSuggestions(Array.isArray(res?.response?.suggestions) ? res.response.suggestions : []);
        setHighlight(-1);
      } catch {
        if (seq === suggestSeq.current) setSuggestions([]);
      }
    }, SUGGEST_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [value, open, client, sessionId]);

  const submit = async (path: string) => {
    if (pending) return;
    setPending(true);
    setError(null);
    try {
      const res = await client.callAction(makeAction('pick_work_dir', { path }), sessionId);
      const response = res?.response;
      if (response && response.ok === false) {
        setError(response.error || t('workdir.setFailed'));
        return;
      }
      setOpen(false);
      setValue('');
    } catch {
      setError(t('workdir.networkError'));
    } finally {
      setPending(false);
    }
  };

  // 候補を選ぶと末尾に区切りを付けて入力欄へ入れ、続けてその中のフォルダ候補を出す。
  const pickSuggestion = (path: string) => {
    const sep = path.includes('\\') ? '\\' : '/';
    setValue(path.endsWith(sep) ? path : path + sep);
    setError(null);
    inputRef.current?.focus();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'ArrowDown' && suggestions.length > 0) {
      e.preventDefault();
      setHighlight((h) => (h + 1) % suggestions.length);
    } else if (e.key === 'ArrowUp' && suggestions.length > 0) {
      e.preventDefault();
      setHighlight((h) => (h <= 0 ? suggestions.length - 1 : h - 1));
    } else if (e.key === 'Tab' && suggestions.length > 0) {
      e.preventDefault();
      pickSuggestion(suggestions[highlight >= 0 ? highlight : 0]);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (highlight >= 0 && highlight < suggestions.length) {
        pickSuggestion(suggestions[highlight]);
      } else {
        submit(value.trim());
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if (suggestions.length > 0) {
        setSuggestions([]);
      } else {
        setOpen(false);
      }
    }
  };

  // ホスト名の問い合わせ中・取得失敗時は接続先のアドレス（IP 等）を出す。
  const serverHost = serverHostname || window.location.hostname;

  return (
    <div className="work-dir-picker" ref={containerRef}>
      <button
        type="button"
        className="work-dir-button"
        title={
          disabled
            ? t('workdir.lockedTitle')
            : remote
              ? t('workdir.setRemoteTitle')
              : t('workdir.setTitle')
        }
        disabled={disabled}
        onClick={() => setOpen((v) => !v)}
      >
        <Icon name="folder" />
      </button>
      {!disabled && open && (
        <div className="work-dir-popover">
          {remote && (
            <>
              <div className="work-dir-popover-title">
                {t('workdir.popoverTitleBefore')}
                <span className="work-dir-popover-host">{serverHost}</span>
                {t('workdir.popoverTitleAfter')}
              </div>
              <div className="work-dir-popover-note">
                {t('workdir.popoverNote1')}
                {t('workdir.popoverNote2')}
              </div>
            </>
          )}
          <input
            ref={inputRef}
            type="text"
            className={'work-dir-popover-input' + (error ? ' is-error' : '')}
            value={value}
            placeholder={
              remote
                ? t('workdir.placeholderRemote')
                : t('workdir.placeholder')
            }
            onChange={(e) => {
              setValue(e.target.value);
              setError(null);
            }}
            onKeyDown={onKeyDown}
            aria-invalid={error ? true : undefined}
            autoComplete="off"
            spellCheck={false}
          />
          {suggestions.length > 0 && (
            <ul className="work-dir-suggestions" role="listbox" aria-label={t('workdir.suggestions')}>
              {suggestions.map((s, i) => (
                <li
                  key={s}
                  role="option"
                  aria-selected={i === highlight}
                  className={'work-dir-suggestion' + (i === highlight ? ' is-active' : '')}
                  title={s}
                  onMouseDown={(e) => {
                    // input のフォーカスを外さずに選択を確定させる
                    e.preventDefault();
                    pickSuggestion(s);
                  }}
                  onMouseEnter={() => setHighlight(i)}
                >
                  <Icon name="folder" />
                  <span className="work-dir-suggestion-path">{s}</span>
                </li>
              ))}
            </ul>
          )}
          {error && (
            <div className="work-dir-popover-error" role="alert">
              {error}
            </div>
          )}
          <div className="work-dir-popover-actions">
            <button
              type="button"
              className="work-dir-popover-reset"
              onClick={() => submit('')}
              disabled={pending}
              title={t('workdir.resetTitle')}
            >
              {t('workdir.reset')}
            </button>
            <div className="work-dir-popover-actions-right">
              <button type="button" className="work-dir-popover-cancel" onClick={() => setOpen(false)}>
                {t('common.cancel')}
              </button>
              <button
                type="button"
                className="work-dir-popover-submit"
                onClick={() => submit(value.trim())}
                disabled={pending || !value.trim()}
              >
                {t('workdir.apply')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
