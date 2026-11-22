import { useCallback, useEffect, useRef, useState } from 'react';
import { useRecoilValue } from 'recoil';
import { currentThreadIdState, sessionIdState, useAuth, useConfig } from '@chainlit/react-client';
import { BACKEND_URL } from '../chainlitClient';
import { useI18n, type MessageKey } from '../i18n';

/*
 * チャット画面の使い方のチュートリアル（ハローワールド）。
 *
 * 会話が1件も無いユーザーが初めて開いたとき（会話履歴の保存が有効で、/locohane/threads が空で、新規チャットを
 * 開いているとき）に自動で出す。画面の各部分を順に強調して説明し、最後に入力欄へ例の指示を入れて、
 * 実際に送信 → AI の回答が届くまでを案内する。終える・スキップすると、ブラウザにユーザー別の印を残して
 * 次からは自動で出さない。表示設定（ThemePicker）の「使い方のチュートリアルを始める」から何度でも始められる
 * （START_TUTORIAL_EVENT）。
 *
 * 説明のカードは右下に出し、画面は覆わない（説明を読みながら実際に操作できるように）。強調は対象の要素へ
 * CSS クラスを付けるだけで、各部品には手を入れない（入力欄へ例の指示を入れるのだけは Composer が
 * FILL_COMPOSER_EVENT を受けて行う）。
 */

/** ThemePicker 等からチュートリアルを始めるイベント。 */
export const START_TUTORIAL_EVENT = 'locohane:start-tutorial';
/** 入力欄に文章を入れるイベント（detail: 文章）。Composer が受ける。 */
export const FILL_COMPOSER_EVENT = 'locohane:fill-composer';

const DONE_KEY_PREFIX = 'la-chat-tutorial-done:';
// 強調する要素を探し直す間隔（ms）。描き直しで要素が入れ替わっても付け直すため。
const POLL_MS = 600;
// 回答が届いた（生成が止まった）と判断するまで待つ時間。承認待ち等の前後で loading が一瞬 false になるため。
const ANSWER_SETTLE_MS = 1500;
const TARGET_CLASS = 'chat-tutorial-target';
// 「〇〇へようこそ」に入れる名前の長さの上限。header.md の1行目がこれより長い（名前ではなく文章）なら名前を入れない。
const APP_NAME_MAX_CHARS = 30;

interface Step {
  title: MessageKey;
  body: MessageKey;
  /** 強調する要素の CSS セレクター（見つからなければ強調しない。スマホ幅で引き出しが閉じている等） */
  targets: string[];
  /** 入力欄に例の指示を入れて送信を待つ手順 */
  tryIt?: boolean;
  /** 回答が届くのを待つ手順 */
  waitAnswer?: boolean;
  /** 定型文のボタンがあるときだけ出す手順 */
  needsStarters?: boolean;
}

const STEPS: Step[] = [
  { title: 'tutorial.welcomeTitle', body: 'tutorial.welcomeBody', targets: [] },
  { title: 'tutorial.inputTitle', body: 'tutorial.inputBody', targets: ['.composer-box'] },
  { title: 'tutorial.attachTitle', body: 'tutorial.attachBody', targets: ['.composer-toolbar-left .composer-icon-button'] },
  { title: 'tutorial.workdirTitle', body: 'tutorial.workdirBody', targets: ['.work-dir-button'] },
  { title: 'tutorial.effortTitle', body: 'tutorial.effortBody', targets: ['.effort-button'] },
  { title: 'tutorial.startersTitle', body: 'tutorial.startersBody', targets: ['.starter-prompts'], needsStarters: true },
  { title: 'tutorial.threadsTitle', body: 'tutorial.threadsBody', targets: ['.thread-sidebar', '.new-chat-button'] },
  { title: 'tutorial.detailsTitle', body: 'tutorial.detailsBody', targets: ['.side-panel'] },
  { title: 'tutorial.themeTitle', body: 'tutorial.themeBody', targets: ['.theme-picker-trigger'] },
  { title: 'tutorial.tryTitle', body: 'tutorial.tryBody', targets: ['.composer-submit-button', '.composer-box'], tryIt: true },
  { title: 'tutorial.waitTitle', body: 'tutorial.waitBody', targets: ['.side-panel', '.composer-stop-button'], waitAnswer: true },
  { title: 'tutorial.doneTitle', body: 'tutorial.doneBody', targets: ['.messages-scroll'] }
];

// 説明のカードと対象の間・画面の端との間（px）。
const CARD_GAP = 14;
const VIEWPORT_MARGIN = 12;
// この幅以下（スマホ）では、カードは画面の下に出す（styles.css の .chat-tutorial-card の @media と合わせる）。
const MOBILE_MAX_WIDTH = 640;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, Math.max(min, max)));
}

/**
 * 説明のカードを対象の要素の近くへ置く。対象の下・上・右・左のうち、カードが画面に収まって対象と重ならない側を、
 * 空いている広さの順に選ぶ（入力欄なら上、左の会話一覧なら右、右の実行の詳細なら左になる）。
 * 対象が無い・どこにも収まらない（会話の欄全体等）・スマホ幅なら、置き場所の指定を外して既定（右下）に戻す。
 */
function placeCard(card: HTMLElement, target: Element | null) {
  const reset = () => {
    card.classList.remove('chat-tutorial-card--anchored');
    card.style.left = '';
    card.style.top = '';
  };
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  if (!target || vw <= MOBILE_MAX_WIDTH) return reset();
  const r = target.getBoundingClientRect();
  if (r.width === 0 && r.height === 0) return reset();
  const w = card.offsetWidth;
  const h = card.offsetHeight;
  const sides = [
    { space: vh - r.bottom, fits: vh - r.bottom >= h + CARD_GAP + VIEWPORT_MARGIN, top: r.bottom + CARD_GAP, left: r.left + r.width / 2 - w / 2 },
    { space: r.top, fits: r.top >= h + CARD_GAP + VIEWPORT_MARGIN, top: r.top - CARD_GAP - h, left: r.left + r.width / 2 - w / 2 },
    { space: vw - r.right, fits: vw - r.right >= w + CARD_GAP + VIEWPORT_MARGIN, top: r.top + r.height / 2 - h / 2, left: r.right + CARD_GAP },
    { space: r.left, fits: r.left >= w + CARD_GAP + VIEWPORT_MARGIN, top: r.top + r.height / 2 - h / 2, left: r.left - CARD_GAP - w }
  ]
    .filter((s) => s.fits)
    .sort((a, b) => b.space - a.space);
  const best = sides[0];
  if (!best) return reset();
  card.classList.add('chat-tutorial-card--anchored');
  card.style.left = `${Math.round(clamp(best.left, VIEWPORT_MARGIN, vw - w - VIEWPORT_MARGIN))}px`;
  card.style.top = `${Math.round(clamp(best.top, VIEWPORT_MARGIN, vh - h - VIEWPORT_MARGIN))}px`;
}

function doneKey(identifier: string | undefined): string {
  return DONE_KEY_PREFIX + (identifier ?? '');
}

export function ChatTutorial({
  hasSentMessage,
  loading,
  hasAnswer,
  hasStarters
}: {
  /** この会話で1回でも送信したか */
  hasSentMessage: boolean;
  /** AI が生成中か */
  loading: boolean;
  /** 最後のメッセージが、書き終わった AI の回答か */
  hasAnswer: boolean;
  /** 定型文のボタンが出ているか */
  hasStarters: boolean;
}) {
  const { t } = useI18n();
  const { config } = useConfig();
  const { user } = useAuth();
  const currentThreadId = useRecoilValue(currentThreadIdState);
  const sessionId = useRecoilValue(sessionIdState);
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const autoCheckedRef = useRef(false);
  const cardRef = useRef<HTMLDivElement>(null);
  // 最初の手順の見出し「〇〇へようこそ」の名前。画面の見出しと同じ表示設定の header.md の1行目
  // （社内の AI の名前にしていることがあるため、アプリの名前は書かない）。取れない・長すぎるなら「ようこそ」だけにする。
  const [appName, setAppName] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    fetch(`${BACKEND_URL}/public/settings/header.md`)
      .then((res) => (res.ok ? res.text() : Promise.reject()))
      .then((text) => {
        const firstLine = text
          .split('\n')
          .map((line) => line.replace(/^#+\s*/, '').trim())
          .find((line) => line.length > 0);
        setAppName(firstLine && firstLine.length <= APP_NAME_MAX_CHARS ? firstLine : null);
      })
      .catch(() => setAppName(null));
  }, [open]);
  const steps = STEPS.filter((s) => !s.needsStarters || hasStarters);
  const step = open ? steps[Math.min(index, steps.length - 1)] : undefined;

  const start = useCallback(() => {
    setIndex(0);
    setCollapsed(false);
    setOpen(true);
  }, []);

  const finish = useCallback(() => {
    setOpen(false);
    try {
      localStorage.setItem(doneKey(user?.identifier), new Date().toISOString());
    } catch {
      /* 保存できなくても閉じる */
    }
  }, [user?.identifier]);

  // 表示設定の「使い方のチュートリアルを始める」から始める。
  useEffect(() => {
    const onStart = () => start();
    window.addEventListener(START_TUTORIAL_EVENT, onStart);
    return () => window.removeEventListener(START_TUTORIAL_EVENT, onStart);
  }, [start]);

  // 初めて開いたユーザー（会話が1件も無い）なら、接続できたところで1回だけ自動で始める。
  // 会話履歴の保存が無効だと、会話が無いかを確かめられないため自動では出さない。
  useEffect(() => {
    if (autoCheckedRef.current || !sessionId || !config) return;
    autoCheckedRef.current = true;
    if (!config.dataPersistence || currentThreadId || hasSentMessage) return;
    try {
      if (localStorage.getItem(doneKey(user?.identifier))) return;
    } catch {
      return;
    }
    fetch(`${BACKEND_URL}/locohane/threads`, { credentials: 'include' })
      .then((res) => (res.ok ? res.json() : Promise.reject()))
      .then((data: { threads?: unknown[] }) => {
        if ((data.threads ?? []).length === 0) start();
      })
      .catch(() => {});
  }, [sessionId, config, currentThreadId, hasSentMessage, user?.identifier, start]);

  // 送ってみる手順: 入力欄に例の指示を入れる。送ったら次（回答待ち）へ進む。
  useEffect(() => {
    if (!step?.tryIt) return;
    if (hasSentMessage) {
      setIndex((i) => i + 1);
      return;
    }
    window.dispatchEvent(new CustomEvent(FILL_COMPOSER_EVENT, { detail: t('tutorial.tryPrompt') }));
    // 手順に入ったときだけ入れる（入れた文章を利用者が書き換えても上書きしない）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step?.tryIt, hasSentMessage]);

  // 回答待ちの手順: 生成が止まって回答が届いたら、少し待ってから完了の手順へ進む。
  useEffect(() => {
    if (!step?.waitAnswer || loading || !hasAnswer) return;
    const timer = setTimeout(() => setIndex((i) => i + 1), ANSWER_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [step?.waitAnswer, loading, hasAnswer]);

  // 対象の要素を強調する（描き直しで入れ替わっても付け直す）。
  useEffect(() => {
    if (!step) return;
    let scrolled = false;
    const apply = () => {
      const found = step.targets.flatMap((sel) => {
        const node = document.querySelector(sel);
        return node ? [node] : [];
      });
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => {
        if (!found.includes(node)) node.classList.remove(TARGET_CLASS);
      });
      found.forEach((node) => node.classList.add(TARGET_CLASS));
      if (!scrolled && found[0]) {
        scrolled = true;
        found[0].scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
      if (cardRef.current) placeCard(cardRef.current, found[0] ?? null);
    };
    apply();
    const timer = setInterval(apply, POLL_MS);
    window.addEventListener('resize', apply);
    return () => {
      clearInterval(timer);
      window.removeEventListener('resize', apply);
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => node.classList.remove(TARGET_CLASS));
    };
  }, [step]);

  // たたむ・広げるで大きさが変わったら、すぐ置き直す（次の見直しを待たない）。
  useEffect(() => {
    if (!step || !cardRef.current) return;
    const target = step.targets.map((sel) => document.querySelector(sel)).find(Boolean) ?? null;
    placeCard(cardRef.current, target);
  }, [collapsed, step]);

  if (!step) return null;
  const position = steps.indexOf(step);
  const last = position === steps.length - 1;
  // 送信・回答を待つ手順では「次へ」を押さなくても進む（押せば待たずに進める）。
  const next = () => (last ? finish() : setIndex(position + 1));

  return (
    <div
      ref={cardRef}
      className={`chat-tutorial-card${collapsed ? ' chat-tutorial-card--collapsed' : ''}`}
      role="dialog"
      aria-label={t('tutorial.heading')}
    >
      <div className="chat-tutorial-head">
        <strong>{t('tutorial.heading')}</strong>
        <span className="chat-tutorial-count">
          {position + 1} / {steps.length}
        </span>
        <button
          type="button"
          className="chat-tutorial-collapse"
          title={collapsed ? t('tutorial.expand') : t('tutorial.collapse')}
          onClick={() => setCollapsed((v) => !v)}
        >
          {collapsed ? '▴' : '▾'}
        </button>
      </div>
      {collapsed ? null : (
        <>
          <div className="chat-tutorial-body">
            <h4>
              {step.title === 'tutorial.welcomeTitle'
                ? appName
                  ? t('tutorial.welcomeTitle', { name: appName })
                  : t('tutorial.welcomeTitlePlain')
                : t(step.title)}
            </h4>
            {t(step.body)
              .split('\n')
              .map((line, i) => (
                <p key={i}>{line}</p>
              ))}
          </div>
          <div className="chat-tutorial-actions">
            <button type="button" className="chat-tutorial-skip" title={t('tutorial.skipTitle')} onClick={finish}>
              {t('tutorial.skip')}
            </button>
            <button type="button" disabled={position === 0} onClick={() => setIndex(position - 1)}>
              {t('tutorial.back')}
            </button>
            <button type="button" className="chat-tutorial-next" onClick={next}>
              {last ? t('tutorial.done') : t('tutorial.next')}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
