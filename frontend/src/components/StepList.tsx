import { useEffect, useRef, useState } from 'react';
import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { StepItem } from './StepItem';
import { Icon } from './Icon';

// ほぼ最下部(この距離px以内)まで戻ってきたときだけオートスクロールを再開する。
// MessagePane.tsx と同じ非対称しきい値方式。
const AUTO_SCROLL_ENGAGE_THRESHOLD_PX = 4;
// wheel イベントを伴わない移動(スクロールバードラッグ、キーボード操作等)で
// 最下部からこの距離(px)を超えて離れたら解除する。
const AUTO_SCROLL_DISENGAGE_THRESHOLD_PX = 64;

export function StepList({ steps }: { steps: IStep[] }) {
  const { t } = useI18n();
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  // ResizeObserver のコールバックや scroll ハンドラは古いクロージャを
  // 参照し続けるため、最新値は ref 経由で読む。ref の更新を useEffect
  // (レンダー確定後)に任せると、その反映前に stream_token による
  // ResizeObserver コールバックが割り込んで古い値のまま強制スナップして
  // しまうことがあるため、setAutoScrollBoth で state と同時に同期更新する
  // (MessagePane.tsx と同じ方式)。
  const autoScrollRef = useRef(true);

  const setAutoScrollBoth = (value: boolean) => {
    autoScrollRef.current = value;
    setAutoScroll(value);
  };

  // stream_token による Step 内容の更新だけでなく、ユーザーが完了済み
  // StepItem を手動で開閉したときの高さ変化（StepItem 内部の useState で
  // StepList 側は再レンダーされない）にも追従させたいため、依存配列で
  // 更新契機を列挙するのではなく ResizeObserver でコンテナ自身の実高さの
  // 変化を直接監視する。scrollIntoView はドキュメント全体を巻き込んで
  // スクロールさせることがあるため、scrollTop を直接操作する。
  useEffect(() => {
    const el = scrollRef.current;
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
  }, [steps]);

  // 再開は「ほぼ最下部に戻ったとき」のみ、解除は「大きく離れたとき」のみ
  // 行う非対称な判定(MessagePane.tsx と同じ方式)。中間の範囲では現状を
  // 維持し、handleWheel による解除直後に勝手に再開されないようにする。
  const handleScroll = () => {
    const el = scrollRef.current;
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
  // てしまうため、ユーザー操作を最優先で反映する(MessagePane.tsx と同じ方式)。
  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    if (e.deltaY < 0) {
      setAutoScrollBoth(false);
    }
  };

  const handleScrollToBottomClick = () => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
    setAutoScrollBoth(true);
  };

  return (
    <div className="step-list-card">
      <div className="step-list-scroll-wrapper">
        <div className="step-list-scroll" ref={scrollRef} onScroll={handleScroll} onWheel={handleWheel}>
          {steps.length === 0 ? (
            <div className="step-list-empty">{t('side.noSteps')}</div>
          ) : (
            <div className="step-list">
              {steps.map((step) => (
                <StepItem key={step.id} step={step} />
              ))}
            </div>
          )}
        </div>
        {!autoScroll ? (
          <button
            type="button"
            className="scroll-to-bottom-button"
            onClick={handleScrollToBottomClick}
            aria-label={t('side.scrollToLatestStep')}
            title={t('side.latestStep')}
          >
            <Icon name="arrow-down" size={16} />
          </button>
        ) : null}
      </div>
    </div>
  );
}
