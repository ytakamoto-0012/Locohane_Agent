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

export function MessagePane({ messages, showTyping }: { messages: IStep[]; showTyping: boolean }) {
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
      if (!autoScrollRef.current) return;
      el.scrollTop = el.scrollHeight;
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

  return (
    <div className="messages-scroll-wrapper">
      <div className="messages-scroll" ref={containerRef} onScroll={handleScroll} onWheel={handleWheel}>
        <MessageThread messages={messages} showTyping={showTyping} />
      </div>
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
