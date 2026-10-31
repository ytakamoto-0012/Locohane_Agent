import { useEffect, useState } from 'react';
import { BACKEND_URL } from '../chainlitClient';
import { useI18n } from '../i18n';
import { ThemePicker } from './ThemePicker';
import { Icon } from './Icon';

const DEFAULT_TITLE = 'Locohane';
const ICON_EXTENSIONS = ['png', 'svg', 'jpg', 'jpeg'];

export function Header({
  mobile,
  onOpenThreads,
  onOpenDetails
}: {
  mobile: boolean;
  /** 会話一覧の引き出しを開く（[thread_store] 無効で会話一覧が無いときは undefined） */
  onOpenThreads?: () => void;
  onOpenDetails: () => void;
}) {
  const { t } = useI18n();
  const [title, setTitle] = useState(DEFAULT_TITLE);
  const [iconExtIndex, setIconExtIndex] = useState(0);
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${BACKEND_URL}/public/settings/header.md`)
      .then((res) => (res.ok ? res.text() : Promise.reject()))
      .then((text) => {
        const firstLine = text
          .split('\n')
          .map((line) => line.replace(/^#+\s*/, '').trim())
          .find((line) => line.length > 0);
        if (firstLine) setTitle(firstLine);
      })
      .catch(() => {});
    // タイトルは header.md で差し替えられるため、バージョンは常時表示せずツールチップに出す
    fetch(`${BACKEND_URL}/locohane/version`)
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data: { version?: string }) => {
        if (data.version) setVersion(data.version);
      })
      .catch(() => {});
  }, []);

  const handleNewChat = () => {
    // 会話スレッド一覧（左サイドバー）が有効な場合、現在の会話は失われず
    // 一覧に残り続けるため、以前のような確認ダイアログは不要
    // （[thread_store].enabled=false でサイドバー自体が無い場合は、
    // 会話が本当に失われるが、その場合も従来通り実害は小さいため確認は省略する）。
    // ?thread=<id> を落とした素のURLへ遷移するだけで新規チャットになる
    // （Sidebar.tsx の goToThread(null) と同じ仕組み）。
    const url = new URL(window.location.href);
    url.searchParams.delete('thread');
    window.location.href = url.pathname + url.search;
  };

  return (
    <header className="app-header">
      <div className="app-header-brand" title={version ? `Locohane v${version}` : undefined}>
        {mobile && onOpenThreads ? (
          <button type="button" className="header-icon-button" title={t('header.openThreads')} onClick={onOpenThreads}>
            <Icon name="menu" size={16} />
          </button>
        ) : null}
        {iconExtIndex < ICON_EXTENSIONS.length ? (
          <img
            className="app-header-icon"
            src={`${BACKEND_URL}/public/settings/icon.${ICON_EXTENSIONS[iconExtIndex]}`}
            alt=""
            onError={() => setIconExtIndex((index) => index + 1)}
          />
        ) : null}
        <span className="app-header-title">{title}</span>
      </div>
      <div className="app-header-actions">
        <button type="button" className="new-chat-button" title={t('common.newChat')} onClick={handleNewChat}>
          <Icon name="plus" size={14} />
          <span className="new-chat-button-label">{t('common.newChat')}</span>
        </button>
        <ThemePicker />
        {mobile ? (
          <button type="button" className="header-icon-button" title={t('header.openDetails')} onClick={onOpenDetails}>
            <Icon name="panel-right" size={16} />
          </button>
        ) : null}
      </div>
    </header>
  );
}
