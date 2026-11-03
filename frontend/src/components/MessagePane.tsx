import { useEffect, useRef, useState } from 'react';
import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { MessageThread } from './MessageThread';
import { Icon } from './Icon';

// ほぼ最下部(この距離px以内)まで戻ってきたときだけオートスクロールを再開する。
// 「解除」は handleWheel が即座に行うため、ここは狭く保つ。64px 等の広い
// 閾値を再開判定にも使うと、ホイールで解除した直後(まだ最下部から数十px
// しか離れていない)に handleScroll がすぐ再開してしまい、解除が事実上
// 無効化されてしまう。
const AUTO_SCROLL_ENGAGE_THRESHOLD_PX = 4;
// wheel イベントを伴わない移動(スクロールバードラッグ、キーボード操作等)で
// 最下部からこの距離(px)を超えて離れたら解除する。
const AUTO_SCROLL_DISENGAGE_THRESHOLD_PX = 64;
// 固定表示したユーザー指示をクリックして戻ったとき、その発言の上に空ける余白(px)。
const PINNED_JUMP_MARGIN_PX = 12;

/** 表示中の位置より上で一番新しいユーザー指示のうち、上端より上へ完全に隠れたもの（今見えている応答の指示）。
 *  見えている（一部でも）間は固定表示しない。 */
function findHiddenUserMessageId(container: HTMLElement): string | null {
  const top = container.getBoundingClientRect().top;
  let found: string | null = null;
  for (const row of container.querySelectorAll<HTMLElement>('[data-user-message-id]')) {
    const rect = row.getBoundingClientRect();
    if (rect.top >= top) break;
    found = rect.bottom <= top ? row.dataset.userMessageId ?? null : null;
  }
  return found;
}

export function MessagePane({
  messages,
  showTyping,
  runningSteps
}: {
  messages: IStep[];
  showTyping: boolean;
  runningSteps: IStep[];
}) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // ResizeObserver のコールバックや scroll ハンドラは古いクロージャを
  // 参照し続けるため、最新値は ref 経由で読む。ref の更新を useEffect
  // (レンダー確定後)に任せると、その反映前に stream_token による
  // ResizeObserver コールバックが割り込んで古い値のまま強制スナップして
  // しまうことがあるため、setAutoScrollBoth で state と同時に同期更新する。
  const autoScrollRef = useRef(true);
  const lastMessageIdRef = useRef<string | null>(null);
  // スクロールで上に隠れたユーザー指示（Claude Code のように上端に固定表示する）。
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  const updatePinned = () => {
    const el = containerRef.current;
    if (el) setPinnedId(findHiddenUserMessageId(el));
  };
  const pinnedMessage = pinnedId ? messages.find((m) => m.id === pinnedId) : undefined;

  const setAutoScrollBoth = (value: boolean) => {
    autoScrollRef.current = value;
    setAutoScroll(value);
  };

  // 新しいユーザーメッセージが追加されたら、直前にオートスクロールを
  // 解除していても最新のやり取りへ強制的に追従を再開する。
  useEffect(() => {
    const last = messages[messages.length - 1];
    if (last && last.id !== lastMessageIdRef.current && last.type === 'user_message') {
      setAutoScrollBoth(true);
    }
    lastMessageIdRef.current = last?.id ?? null;
  }, [messages]);

  // stream_token によるコンテンツの高さ変化に追従する。scrollIntoView は
  // ドキュメント全体を巻き込むことがあるため scrollTop を直接操作する
  // (StepList.tsx と同じ方式)。
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const scrollToBottom = () => {
      if (autoScrollRef.current) el.scrollTop = el.scrollHeight;
      // 高さが変わる（応答が伸びる・スレッドを切り替える）と、隠れている指示も変わる。
      updatePinned();
    };
    scrollToBottom();
    const ro = new ResizeObserver(scrollToBottom);
    ro.observe(el);
    if (el.firstElementChild) {
      ro.observe(el.firstElementChild);
    }
    return () => ro.disconnect();
  }, [messages, showTyping]);

  // 再開は「ほぼ最下部に戻ったとき」のみ、解除は「大きく離れたとき」のみ
  // 行う非対称な判定。中間の範囲(ENGAGE〜DISENGATEの間)では現状を維持し、
  // handleWheel による解除直後の中途半端な位置で勝手に再開されないようにする。
  const handleScroll = () => {
    const el = containerRef.current;
    if (!el) return;
    updatePinned();
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom <= AUTO_SCROLL_ENGAGE_THRESHOLD_PX) {
      setAutoScrollBoth(true);
    } else if (distanceFromBottom > AUTO_SCROLL_DISENGAGE_THRESHOLD_PX) {
      setAutoScrollBoth(false);
    }
  };

  // 上方向へのホイール操作があった時点で即座にオートスクロールを解除する。
  // handleScroll の距離判定だけに頼ると、ストリーミング中の ResizeObserver
  // による強制スナップ(下記 useEffect)と競合し、閾値を超えるまで押し戻され
  // てしまうため、ユーザー操作を最優先で反映する。
  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (e.deltaY < 0) {
      setAutoScrollBoth(false);
    }
  };

  const handleScrollToBottomClick = () => {
    const el = containerRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    setAutoScrollBoth(true);
  };

  // 固定表示した指示をクリックしたら、その発言の位置まで戻る（戻った先で追従を止める）。
  const handlePinnedClick = () => {
    const el = containerRef.current;
    if (!el || !pinnedId) return;
    const row = Array.from(el.querySelectorAll<HTMLElement>('[data-user-message-id]')).find(
      (r) => r.dataset.userMessageId === pinnedId
    );
    if (!row) return;
    const offset = row.getBoundingClientRect().top - el.getBoundingClientRect().top;
    setAutoScrollBoth(false);
    el.scrollTo({ top: el.scrollTop + offset - PINNED_JUMP_MARGIN_PX, behavior: 'smooth' });
  };

  return (
    <div className="messages-scroll-wrapper">
      <div className="messages-scroll" ref={containerRef} onScroll={handleScroll} onWheel={handleWheel}>
        <MessageThread messages={messages} showTyping={showTyping} runningSteps={runningSteps} />
      </div>
      {pinnedMessage ? (
        <button
          type="button"
          className="pinned-user-message"
          onClick={handlePinnedClick}
          title={t('messages.pinnedPromptTitle')}
        >
          <span className="pinned-user-message-text">{(pinnedMessage.output || '').replace(/\s+/g, ' ').trim()}</span>
        </button>
      ) : null}
      {!autoScroll ? (
        <button
          type="button"
          className="scroll-to-bottom-button"
          onClick={handleScrollToBottomClick}
          aria-label={t('messages.scrollToLatest')}
          title={t('messages.latest')}
        >
          <Icon name="arrow-down" size={16} />
        </button>
      ) : null}
    </div>
  );
}
