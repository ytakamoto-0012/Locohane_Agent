import { useCallback, useEffect, useState, type MouseEvent } from 'react';
import { useRecoilValue } from 'recoil';
import { currentThreadIdState, firstUserInteraction, sessionIdState, useAuth, useConfig } from '@chainlit/react-client';
import { BACKEND_URL } from '../chainlitClient';
import { useI18n } from '../i18n';
import { Icon } from './Icon';

interface ThreadSummary {
  id: string;
  name: string | null;
  updatedAt: string;
  isGenerating: boolean;
  isWaitingForUser: boolean;
}

// 他のセッション（別スレッドを開いた同じタブ・別タブ）が裏で処理中かどうかを
// 知るライブpushの仕組みが無いため、ポーリングで代替する（app.pyの
// _generating_thread_ids/on_message参照）。
const GENERATING_POLL_INTERVAL_MS = 3000;

/** ?thread=<id> の付け外しだけを行い、リロードで新規/再開を切り替える。
 * useChatSession().connect は threadIdToResumeState を読むdebounce関数のため、
 * setIdToResume 直後に同期的に connect し直すとReactのstate反映タイミングと
 * 競合しうる（App.tsx参照）。URL+リロードなら常にマウント時の1回読みで確定する。 */
function goToThread(threadId: string | null) {
  const url = new URL(window.location.href);
  if (threadId) {
    url.searchParams.set('thread', threadId);
  } else {
    url.searchParams.delete('thread');
  }
  window.location.href = url.pathname + url.search;
}

export function Sidebar({
  mobile,
  open,
  onClose
}: {
  /** スマホ幅では折りたたみボタンの代わりに引き出し（open/onClose は App が持つ）で表示する */
  mobile: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const { config } = useConfig();
  const { user, logout } = useAuth();
  const { t } = useI18n();
  const currentThreadId = useRecoilValue(currentThreadIdState);
  const firstInteraction = useRecoilValue(firstUserInteraction);
  const sessionId = useRecoilValue(sessionIdState);
  const [collapsed, setCollapsed] = useState(false);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);

  const refresh = useCallback(() => {
    // session_id を渡すと、自分自身が生成中のスレッドにはパルスドットを
    // 出さない（app.py の /locohane/threads のdocstring参照）。
    const query = sessionId ? `?session_id=${encodeURIComponent(sessionId)}` : '';
    fetch(`${BACKEND_URL}/locohane/threads${query}`, { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data) => setThreads(data.threads ?? []))
      .catch(() => {});
  }, [sessionId]);

  // マウント時、および新規スレッドがChainlit本体により命名された直後
  // （firstUserInteractionが変化するタイミング）に再取得する。
  useEffect(() => {
    refresh();
  }, [refresh, firstInteraction]);

  // 「生成中」インジケーターは他セッションの状態のためライブpushできず、
  // ポーリングで更新する（config取得前でも[thread_store]有効なら回してよい
  // ため、後述のearly returnより前に置く）。
  useEffect(() => {
    if (!config?.dataPersistence) return;
    const timer = setInterval(refresh, GENERATING_POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refresh, config?.dataPersistence]);

  // [thread_store].enabled=false（データレイヤー未登録）なら何も描画しない。
  if (!config?.dataPersistence) return null;

  if (mobile && !open) return null;

  if (!mobile && collapsed) {
    return (
      <button
        type="button"
        className="thread-sidebar-collapsed"
        title={t('header.openThreads')}
        onClick={() => setCollapsed(false)}
      >
        <Icon name="panel-left" size={14} />
      </button>
    );
  }

  const handleRename = async (thread: ThreadSummary, event: MouseEvent) => {
    event.stopPropagation();
    const name = window.prompt(t('sidebar.renamePrompt'), thread.name ?? '');
    if (!name || name === thread.name) return;
    await fetch(`${BACKEND_URL}/locohane/threads/${thread.id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ name })
    });
    refresh();
  };

  const handleDelete = async (thread: ThreadSummary, event: MouseEvent) => {
    event.stopPropagation();
    // isGenerating は自分自身が今まさに生成中のスレッドでは false になる
    // （app.py の /locohane/threads docstring参照）ため、ここでの弾きは
    // 見た目のヒントに過ぎない。実際の拒否はサーバー側（409）で行う
    // （生成中に削除されるとタスクを止める手段を失うため）。
    if (thread.isGenerating) {
      window.alert(t('sidebar.deleteBusy'));
      return;
    }
    if (!window.confirm(t('sidebar.deleteConfirm', { name: thread.name ?? t('common.untitledThread') }))) return;
    const res = await fetch(`${BACKEND_URL}/locohane/threads/${thread.id}`, {
      method: 'DELETE',
      credentials: 'include'
    });
    if (!res.ok) {
      if (res.status === 409) {
        window.alert(t('sidebar.deleteBusy'));
      }
      refresh();
      return;
    }
    if (thread.id === currentThreadId) {
      // 開いている会話を削除した場合、次のメッセージで空スタブが復活しないよう
      // 新規チャットへフォールバックする。
      goToThread(null);
      return;
    }
    refresh();
  };

  return (
    <aside className={mobile ? 'thread-sidebar thread-sidebar--drawer' : 'thread-sidebar'}>
      <div className="thread-sidebar-header">
        <button type="button" className="new-chat-button" onClick={() => goToThread(null)}>
          <Icon name="plus" size={14} />
          {t('common.newChat')}
        </button>
        <button
          type="button"
          className="thread-sidebar-toggle"
          title={mobile ? t('sidebar.close') : t('sidebar.collapse')}
          onClick={() => (mobile ? onClose() : setCollapsed(true))}
        >
          <Icon name={mobile ? 'x' : 'panel-left'} size={14} />
        </button>
      </div>
      <ul className="thread-list">
        {threads.map((thread) => (
          <li
            key={thread.id}
            className={
              thread.id === currentThreadId ? 'thread-list-item thread-list-item-active' : 'thread-list-item'
            }
            onClick={() => goToThread(thread.id)}
          >
            {thread.isWaitingForUser ? (
              <span
                className="thread-list-item-waiting"
                title={t('sidebar.waitingForUser')}
                aria-label={t('sidebar.waitingForUser')}
              />
            ) : (
              thread.isGenerating && (
                <span
                  className="thread-list-item-generating"
                  title={t('sidebar.generating')}
                  aria-label={t('sidebar.generating')}
                />
              )
            )}
            <span className="thread-list-item-name">{thread.name || t('common.untitledThread')}</span>
            <span className="thread-list-item-actions">
              <button type="button" title={t('sidebar.rename')} onClick={(event) => handleRename(thread, event)}>
                <Icon name="pencil" size={12} />
              </button>
              <button
                type="button"
                title={thread.isGenerating ? t('sidebar.cannotDeleteWhileGenerating') : t('sidebar.delete')}
                disabled={thread.isGenerating}
                onClick={(event) => handleDelete(thread, event)}
              >
                <Icon name="trash" size={12} />
              </button>
            </span>
          </li>
        ))}
      </ul>
      {user && (
        <div className="thread-sidebar-user" title={user.identifier}>
          <Icon name="user" size={14} />
          <span className="thread-sidebar-user-name">{user.display_name || user.identifier}</span>
          <button
            type="button"
            className="thread-sidebar-logout"
            title={t('sidebar.logout')}
            onClick={() => logout(true)}
          >
            <Icon name="log-out" size={14} />
          </button>
        </div>
      )}
    </aside>
  );
}
