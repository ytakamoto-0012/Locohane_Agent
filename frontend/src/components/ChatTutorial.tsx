import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRecoilValue } from 'recoil';
import { currentThreadIdState, sessionIdState, useAuth, useConfig } from '@chainlit/react-client';
import { BACKEND_URL } from '../chainlitClient';
import { useI18n, type MessageKey } from '../i18n';
import { useIsMobile } from '../utils/useIsMobile';

/*
 * チャット画面の使い方のチュートリアル（ハローワールド）。
 *
 * まだ終えていないユーザーが新規チャットを開いたとき（会話がすでにあるユーザーにも1回は）に自動で出す。画面の各部分を順に強調して説明し、最後に入力欄へ例の指示を入れて、
 * 実際に送信 → AI の回答が届くまでを案内する。終える・スキップすると、サーバー（POST /locohane/tutorial/done。
 * ユーザー単位）とブラウザに印を残して次からは自動で出さない。表示設定（ThemePicker）の「使い方のチュートリアルを始める」から何度でも始められる
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

// 「新規チャット」・会話の一覧のクリックはページを読み込み直すため、続きの手順を sessionStorage（このタブだけ）に残して、
// 読み込み直した後にそこから再開する。
const RESUME_KEY = 'la-chat-tutorial-resume';

interface ResumeState {
  stepId: string;
  withStarters: boolean;
  user: string;
}

interface Step {
  /** 読み込み直した後に再開する手順の名前（waitClick の next に使う） */
  id?: string;
  title: MessageKey;
  body: MessageKey;
  /** 強調する要素の CSS セレクター（見つからなければ強調しない。スマホ幅で引き出しが閉じている等） */
  targets: string[];
  /** スマホ幅で targets の代わりに強調する要素（会話の一覧・実行の詳細は引き出しの中にあるため、開くボタン等） */
  mobileTargets?: string[];
  /** スマホ幅で body の代わりに出す説明（「左の一覧」「右の詳細」「マウスを乗せる」がスマホに無いため） */
  mobileBody?: MessageKey;
  /** 入力欄に例の指示を入れて送信を待つ手順 */
  tryIt?: boolean;
  /** 回答が届くのを待つ手順 */
  waitAnswer?: boolean;
  /** 回答を書き終えるまで「次へ」を押せない手順（回答が出始めたら入る「回答が届きました」） */
  waitAnswerDone?: boolean;
  /** 定型文のボタンがあるときだけ出す手順 */
  needsStarters?: boolean;
  /** 会話の一覧（[thread_store] enabled）があるときだけ出す手順 */
  needsThreads?: boolean;
  /** この要素のクリックを待つ手順（押すとページを読み込み直すので、next の手順から再開する） */
  waitClick?: { selector: string; ignore?: string; next: string };
}

const STEPS: Step[] = [
  { title: 'tutorial.welcomeTitle', body: 'tutorial.welcomeBody', targets: [] },
  { title: 'tutorial.inputTitle', body: 'tutorial.inputBody', targets: ['.composer-box'] },
  { title: 'tutorial.attachTitle', body: 'tutorial.attachBody', targets: ['.composer-toolbar-left .composer-icon-button'] },
  { title: 'tutorial.workdirTitle', body: 'tutorial.workdirBody', targets: ['.work-dir-button'] },
  { title: 'tutorial.effortTitle', body: 'tutorial.effortBody', targets: ['.effort-button'] },
  { title: 'tutorial.startersTitle', body: 'tutorial.startersBody', targets: ['.starter-prompts'], needsStarters: true },
  {
    title: 'tutorial.detailsTitle',
    body: 'tutorial.detailsBody',
    targets: ['.side-panel'],
    // 引き出しが開いていれば中身、閉じていれば開くボタン
    mobileTargets: ['.side-panel', '.header-details-button'],
    mobileBody: 'tutorial.detailsBodyMobile'
  },
  { title: 'tutorial.themeTitle', body: 'tutorial.themeBody', targets: ['.theme-picker-trigger'] },
  // カードは最初の対象（入力欄全体）の上に置く。送信ボタンを基準にすると入力欄に被り、入れた例の指示が見えないため
  { title: 'tutorial.tryTitle', body: 'tutorial.tryBody', targets: ['.composer-box', '.composer-submit-button'], tryIt: true },
  {
    title: 'tutorial.waitTitle',
    body: 'tutorial.waitBody',
    targets: ['.side-panel', '.composer-stop-button'],
    mobileTargets: ['.composer-stop-button'],
    mobileBody: 'tutorial.waitBodyMobile',
    waitAnswer: true
  },
  { title: 'tutorial.doneTitle', body: 'tutorial.doneBody', targets: ['.messages-scroll'], waitAnswerDone: true },
  // 会話の一覧は、送った会話が一覧に入った後に、実際に操作してもらう:
  // 新規チャットを押す → 一覧から今の会話を開く → もう一度新規チャットを押す → 締め（新しい会話の画面）
  {
    id: 'newChat',
    title: 'tutorial.newChatTitle',
    body: 'tutorial.newChatBody',
    targets: ['.new-chat-button'],
    mobileBody: 'tutorial.newChatBodyMobile',
    needsThreads: true,
    waitClick: { selector: '.new-chat-button', next: 'openThread' }
  },
  {
    id: 'openThread',
    title: 'tutorial.openThreadTitle',
    body: 'tutorial.openThreadBody',
    targets: ['.thread-list-item', '.thread-sidebar'],
    // 引き出しを開くまではその開くボタン、開いたら一覧の会話
    mobileTargets: ['.thread-list-item', '.header-threads-button'],
    mobileBody: 'tutorial.openThreadBodyMobile',
    needsThreads: true,
    waitClick: { selector: '.thread-list-item', ignore: '.thread-list-item-actions', next: 'resumed' }
  },
  {
    id: 'resumed',
    title: 'tutorial.resumedTitle',
    body: 'tutorial.resumedBody',
    // 開いた会話（会話の欄全体）を囲む
    targets: ['.messages-scroll', '.thread-list-item-active'],
    mobileBody: 'tutorial.resumedBodyMobile',
    needsThreads: true
  },
  {
    title: 'tutorial.continueTitle',
    body: 'tutorial.continueBody',
    // 続きはここに書いて送る
    targets: ['.composer-box'],
    needsThreads: true
  },
  {
    id: 'newChatAgain',
    title: 'tutorial.newChatAgainTitle',
    body: 'tutorial.newChatAgainBody',
    targets: ['.new-chat-button'],
    needsThreads: true,
    waitClick: { selector: '.new-chat-button', next: 'finish' }
  },
  { id: 'finish', title: 'tutorial.finishTitle', body: 'tutorial.finishBody', targets: [], needsThreads: true }
];

function readResume(): ResumeState | null {
  try {
    const raw = sessionStorage.getItem(RESUME_KEY);
    return raw ? (JSON.parse(raw) as ResumeState) : null;
  } catch {
    return null;
  }
}

function writeResume(state: ResumeState | null) {
  try {
    if (state) sessionStorage.setItem(RESUME_KEY, JSON.stringify(state));
    else sessionStorage.removeItem(RESUME_KEY);
  } catch {
    /* 残せなければ、読み込み直した後は再開しない */
  }
}

// 説明のカードと対象の間・画面の端との間（px）。
const CARD_GAP = 14;
const VIEWPORT_MARGIN = 12;
// スマホ幅（MOBILE_MEDIA_QUERY と同じ 768px 以下）では、カードを画面の上端か下端に寄せた帯にする（dockCard）。
const MOBILE_MAX_WIDTH = 768;
// 帯の高さの下限（これより狭くしか空いていなければ、対象に重ねてでもこの高さは取る）と、画面の高さに対する上限。
const DOCK_MIN_HEIGHT = 140;
const DOCK_MAX_RATIO = 0.45;
// 上下の空きがほぼ同じとき、行ったり来たりしないよう、今の側を選び続ける幅（px）。
const DOCK_HYSTERESIS = 40;

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, Math.max(min, max)));
}

/**
 * スマホ幅: カードを左右いっぱいの帯にし、対象と反対側の端（対象より上が広ければ上端、下が広ければ下端）に寄せる。
 * 高さは空いている側に収まるまで縮め、本文はカードの中でスクロールする。対象が大きくてどちらにも収まらない
 * （会話の欄全体等）なら、上端に寄せる（新しい回答は会話の欄の下に出るため）。キーボードが出ている間は、
 * 見えている範囲（visualViewport）を基準にする。
 */
function dockCard(card: HTMLElement, target: Element | null) {
  const vv = window.visualViewport;
  const vh = vv ? vv.height : window.innerHeight;
  const offset = vv ? vv.offsetTop : 0;
  const cap = Math.round(vh * DOCK_MAX_RATIO);
  let atTop = false;
  let space = cap;
  const r = target?.getBoundingClientRect();
  if (r && (r.width > 0 || r.height > 0)) {
    const above = r.top - offset - CARD_GAP - VIEWPORT_MARGIN;
    const below = offset + vh - r.bottom - CARD_GAP - VIEWPORT_MARGIN;
    const wasTop = card.dataset.dock === 'top';
    if (Math.max(above, below) >= DOCK_MIN_HEIGHT) {
      atTop = wasTop ? above + DOCK_HYSTERESIS >= below : above > below + DOCK_HYSTERESIS;
      space = atTop ? above : below;
      if (space < DOCK_MIN_HEIGHT) {
        atTop = !atTop;
        space = atTop ? above : below;
      }
    } else {
      atTop = true;
    }
  }
  card.classList.remove('chat-tutorial-card--anchored');
  card.classList.add('chat-tutorial-card--docked');
  card.dataset.dock = atTop ? 'top' : 'bottom';
  card.style.left = '';
  card.style.maxHeight = `${Math.round(clamp(space, DOCK_MIN_HEIGHT, cap))}px`;
  card.style.top = atTop ? `${Math.round(offset + VIEWPORT_MARGIN)}px` : 'auto';
  card.style.bottom = atTop
    ? 'auto'
    : `${Math.round(Math.max(0, window.innerHeight - offset - vh) + VIEWPORT_MARGIN)}px`;
}

/**
 * 説明のカードを対象の要素の近くへ置く。対象の下・上・右・左のうち、カードが画面に収まって対象と重ならない側を、
 * 空いている広さの順に選ぶ（入力欄なら上、左の会話一覧なら右、右の実行の詳細なら左になる）。
 * 対象が無い手順は画面の中央に置く。どこにも収まらない（会話の欄全体等）なら、置き場所の指定を外して既定（右下）に戻す。
 * スマホ幅では、上端か下端の帯にする（dockCard）。
 */
function placeCard(card: HTMLElement, target: Element | null) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  if (vw <= MOBILE_MAX_WIDTH) return dockCard(card, target);
  card.classList.remove('chat-tutorial-card--docked');
  delete card.dataset.dock;
  card.style.maxHeight = '';
  card.style.bottom = '';
  const reset = () => {
    card.classList.remove('chat-tutorial-card--anchored');
    card.style.left = '';
    card.style.top = '';
  };
  if (!target) {
    // 対象の無い手順（最初の「ようこそ」）は、目に留まるよう画面の中央に出す。
    card.classList.add('chat-tutorial-card--anchored');
    card.style.left = `${Math.round(Math.max(VIEWPORT_MARGIN, (vw - card.offsetWidth) / 2))}px`;
    card.style.top = `${Math.round(Math.max(VIEWPORT_MARGIN, (vh - card.offsetHeight) / 2))}px`;
    return;
  }
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

/** このブラウザで終えた印があるか（読めないときも「ある」扱いにして、勝手には出さない）。 */
function readLocalDone(identifier: string | undefined): boolean {
  try {
    return Boolean(localStorage.getItem(doneKey(identifier)));
  } catch {
    return true;
  }
}

// サーバーの準備前（503）・接続の失敗のときに聞き直す回数と間隔
const SERVER_DONE_RETRIES = 10;
const SERVER_DONE_RETRY_MS = 1500;

/** サーバーに記録した「このユーザーは終えたか」。聞けなかったら null。 */
async function readServerDone(): Promise<boolean | null> {
  for (let i = 0; i < SERVER_DONE_RETRIES; i++) {
    try {
      const res = await fetch(`${BACKEND_URL}/locohane/tutorial`, { credentials: 'include' });
      if (res.ok) {
        const data = (await res.json()) as { done?: boolean };
        return Boolean(data.done);
      }
      if (res.status !== 503) return null;
    } catch {
      /* 聞き直す */
    }
    await new Promise((resolve) => setTimeout(resolve, SERVER_DONE_RETRY_MS));
  }
  return null;
}

async function postServerDone(): Promise<void> {
  try {
    await fetch(`${BACKEND_URL}/locohane/tutorial/done`, { method: 'POST', credentials: 'include' });
  } catch {
    /* 記録できなくても閉じる（このブラウザの印は残る） */
  }
}

export function ChatTutorial({
  hasSentMessage,
  loading,
  hasAnswer,
  answerStarted,
  hasStarters,
  messageCount
}: {
  /** この会話で1回でも送信したか */
  hasSentMessage: boolean;
  /** AI が生成中か */
  loading: boolean;
  /** 最後のメッセージが、書き終わった AI の回答か */
  hasAnswer: boolean;
  /** 最後のメッセージが AI の回答か（書いている途中も含む） */
  answerStarted: boolean;
  /** 定型文のボタンが出ているか */
  hasStarters: boolean;
  /** 今の会話のメッセージ数（教程中の送信を、教程が始まる前の会話と区別するため） */
  messageCount: number;
}) {
  const { t } = useI18n();
  const { config } = useConfig();
  const { user } = useAuth();
  const currentThreadId = useRecoilValue(currentThreadIdState);
  const sessionId = useRecoilValue(sessionIdState);
  const [open, setOpen] = useState(false);
  const [index, setIndex] = useState(0);
  const [collapsed, setCollapsed] = useState(false);
  const isMobile = useIsMobile();
  const autoCheckedRef = useRef(false);
  const cardRef = useRef<HTMLDivElement>(null);
  // 定型文の手順を出すかは、始めた時点で決める。送信すると定型文のボタンが消えるため、その場で数え直すと
  // 手順の番号がずれ、「AI が考えています」等の手順を飛ばしてしまう。
  const hasStartersRef = useRef(hasStarters);
  hasStartersRef.current = hasStarters;
  // 「送ってみる」手順は、チュートリアルを始める前からある会話のメッセージで飛ばさない（表示設定から既存の会話で
  // 始めると、hasSentMessage は最初から true なので、例の指示が入る前に次へ進んでいた）。手順に入ったときの
  // メッセージ数を控え、その後に増えたかで送ったことを見る。
  const latestCountRef = useRef(messageCount);
  latestCountRef.current = messageCount;
  const tryBaseRef = useRef<number | null>(null);
  const [withStarters, setWithStarters] = useState(false);
  // 会話の一覧の操作の手順は、会話履歴の保存が有効なときだけ（無効なら会話の一覧が無い）
  const withThreads = Boolean(config?.dataPersistence);
  const steps = useMemo(
    () => STEPS.filter((s) => (!s.needsStarters || withStarters) && (!s.needsThreads || withThreads)),
    [withStarters, withThreads]
  );
  const step = open ? steps[Math.min(index, steps.length - 1)] : undefined;

  const start = useCallback(() => {
    tryBaseRef.current = null;
    setWithStarters(hasStartersRef.current);
    setIndex(0);
    setCollapsed(false);
    setOpen(true);
  }, []);

  const finish = useCallback(() => {
    setOpen(false);
    writeResume(null);
    try {
      localStorage.setItem(doneKey(user?.identifier), new Date().toISOString());
    } catch {
      /* 保存できなくても閉じる */
    }
    if (config?.dataPersistence) void postServerDone();
  }, [user?.identifier, config?.dataPersistence]);

  // 表示設定の「使い方のチュートリアルを始める」から始める。
  useEffect(() => {
    const onStart = () => start();
    window.addEventListener(START_TUTORIAL_EVENT, onStart);
    return () => window.removeEventListener(START_TUTORIAL_EVENT, onStart);
  }, [start]);

  // 「新規チャット」・会話の一覧のクリックで読み込み直した後は、残しておいた手順から再開する（このタブで1回だけ）。
  const resumeCheckedRef = useRef(false);
  useEffect(() => {
    if (resumeCheckedRef.current || !config) return;
    resumeCheckedRef.current = true;
    const saved = readResume();
    if (!saved) return;
    writeResume(null);
    if (saved.user !== (user?.identifier ?? '')) return;
    autoCheckedRef.current = true; // 再開したら、下の自動の開始はしない
    const list = STEPS.filter(
      (s) => (!s.needsStarters || saved.withStarters) && (!s.needsThreads || config.dataPersistence)
    );
    const i = list.findIndex((s) => s.id === saved.stepId);
    if (i < 0) return;
    setWithStarters(saved.withStarters);
    setIndex(i);
    setCollapsed(false);
    setOpen(true);
  }, [config, user?.identifier]);

  // クリックを待つ手順: 対象を押したら、続きの手順を残して進める（押した後にページが読み込み直される）。
  useEffect(() => {
    const wait = step?.waitClick;
    if (!wait) return;
    const onClick = (ev: MouseEvent) => {
      const node = ev.target instanceof Element ? ev.target : null;
      if (!node || (wait.ignore && node.closest(wait.ignore)) || !node.closest(wait.selector)) return;
      writeResume({ stepId: wait.next, withStarters, user: user?.identifier ?? '' });
      const i = steps.findIndex((s) => s.id === wait.next);
      if (i >= 0) setIndex(i);
    };
    document.addEventListener('click', onClick, true);
    return () => document.removeEventListener('click', onClick, true);
  }, [step, steps, withStarters, user?.identifier]);

  // まだ終えていないユーザーなら、新規チャットを開いて接続できたところで1回だけ自動で始める。
  // 会話がすでにあるユーザー（チュートリアルが入る前の版から使っている人）にも1回は出すため、会話の件数は見ない。
  // 終えたかは、会話履歴の保存が有効ならサーバー（ユーザー単位。ブラウザ・PC が変わっても同じ）、無効ならこのブラウザで見る。
  const hasSentRef = useRef(hasSentMessage);
  hasSentRef.current = hasSentMessage;
  const openRef = useRef(open);
  openRef.current = open;
  const unmountedRef = useRef(false);
  useEffect(
    () => () => {
      unmountedRef.current = true;
    },
    []
  );
  useEffect(() => {
    if (autoCheckedRef.current || !sessionId || !config) return;
    autoCheckedRef.current = true;
    if (currentThreadId || hasSentMessage) return;
    // 問い合わせている間に送信した・表示設定から始めていたら始めない
    const startIfStillNew = () => {
      if (!unmountedRef.current && !hasSentRef.current && !openRef.current) start();
    };
    if (!config.dataPersistence) {
      if (!readLocalDone(user?.identifier)) start();
      return;
    }
    void readServerDone().then((done) => {
      if (done === null) return; // 聞けなかったときは出さない（終えた人に出さないことを優先）
      if (done) return;
      if (readLocalDone(user?.identifier)) {
        // このブラウザで終えた印だけある（サーバーに持つ前の版で終えた）ならサーバーへ写す
        void postServerDone();
        return;
      }
      startIfStillNew();
    });
  }, [sessionId, config, currentThreadId, hasSentMessage, user?.identifier, start]);

  // 送ってみる手順: 入ったときに入力欄へ例の指示を入れ、そのときのメッセージ数を控える。
  // 入る前からある会話のメッセージ（表示設定から既存の会話で始めたとき）では進めない。
  useEffect(() => {
    if (!step?.tryIt) return;
    tryBaseRef.current = latestCountRef.current;
    window.dispatchEvent(new CustomEvent(FILL_COMPOSER_EVENT, { detail: t('tutorial.tryPrompt') }));
    // 手順に入ったときだけ入れる（入れた文章を利用者が書き換えても上書きしない）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [step]);

  // 送ったら（手順に入った後にメッセージが増えたら）次（回答待ち）へ進む。
  useEffect(() => {
    if (!step?.tryIt || tryBaseRef.current === null) return;
    if (messageCount > tryBaseRef.current) setIndex((i) => i + 1);
  }, [step?.tryIt, messageCount]);

  // 回答待ちの手順: 送った後の回答が出始めたら、すぐ「回答が届きました」の手順へ進む
  // （送ったメッセージと回答で 2 件増える。前からある回答では進めない）。
  useEffect(() => {
    if (!step?.waitAnswer || !answerStarted) return;
    const base = tryBaseRef.current;
    if (base !== null && messageCount < base + 2) return;
    setIndex((i) => i + 1);
  }, [step?.waitAnswer, answerStarted, messageCount]);

  // 「回答が届きました」の手順: 回答を書き終えて生成が止まったら（承認待ち等で一瞬止まるのと見分けるため少し待って）、
  // 「次へ」を押せるようにする。
  const [answerDone, setAnswerDone] = useState(false);
  useEffect(() => {
    if (!step?.waitAnswerDone || loading || !hasAnswer) {
      setAnswerDone(false);
      return;
    }
    const timer = setTimeout(() => setAnswerDone(true), ANSWER_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [step?.waitAnswerDone, loading, hasAnswer]);

  // 対象の要素を強調する（描き直しで入れ替わっても付け直す）。
  const targets = step ? (isMobile && step.mobileTargets) || step.targets : null;
  useEffect(() => {
    if (!targets) return;
    let scrolled = false;
    const apply = () => {
      const found = targets.flatMap((sel) => {
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
    // スクロール・キーボードの出し入れ（visualViewport）でも、すぐ置き直す（次の見直しを待たない）
    let frame = 0;
    const onMove = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        apply();
      });
    };
    window.addEventListener('resize', onMove);
    window.addEventListener('scroll', onMove, true);
    window.visualViewport?.addEventListener('resize', onMove);
    window.visualViewport?.addEventListener('scroll', onMove);
    return () => {
      clearInterval(timer);
      if (frame) cancelAnimationFrame(frame);
      window.removeEventListener('resize', onMove);
      window.removeEventListener('scroll', onMove, true);
      window.visualViewport?.removeEventListener('resize', onMove);
      window.visualViewport?.removeEventListener('scroll', onMove);
      document.querySelectorAll(`.${TARGET_CLASS}`).forEach((node) => node.classList.remove(TARGET_CLASS));
    };
  }, [targets]);

  // たたむ・広げるで大きさが変わったら、すぐ置き直す（次の見直しを待たない）。
  useEffect(() => {
    if (!targets || !cardRef.current) return;
    const target = targets.map((sel) => document.querySelector(sel)).find(Boolean) ?? null;
    placeCard(cardRef.current, target);
  }, [collapsed, targets]);

  if (!step) return null;
  const position = steps.indexOf(step);
  const last = position === steps.length - 1;
  // 送信・回答を待つ手順は、送信した・回答が終わったときに自動で進む。その間は「次へ」を押せない（送らずに先へ
  // 進むと、初めての人には何が起きたか分からないため）。回答が終わった後の手順から、また「次へ」を押せる。
  const waiting = Boolean(step.tryIt || step.waitAnswer || step.waitClick || (step.waitAnswerDone && !answerDone));
  const prev = steps[position - 1];
  // 「戻る」で送信待ち・回答待ちの手順へは戻らない（戻ってもすぐ自動で進んでしまうため）。回答待ちの間も押せない。
  const backDisabled =
    position === 0 || Boolean(step.waitAnswer) || Boolean(prev?.tryIt || prev?.waitAnswer || prev?.waitClick);
  const waitingLabel = step.tryIt
    ? t('tutorial.waitingSend')
    : step.waitAnswer
      ? t('tutorial.waitingAnswer')
      : step.waitClick
        ? t('tutorial.waitingClick')
        : step.waitAnswerDone && !answerDone
          ? t('tutorial.waitingAnswerDone')
          : null;
  const next = () => {
    if (waiting) return;
    if (last) finish();
    else setIndex(position + 1);
  };

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
              {t(step.title)}
            </h4>
            {t((isMobile && step.mobileBody) || step.body)
              .split('\n')
              .map((line, i) => (
                <p key={i}>{line}</p>
              ))}
          </div>
          <div className="chat-tutorial-actions">
            <button type="button" className="chat-tutorial-skip" title={t('tutorial.skipTitle')} onClick={finish}>
              {t('tutorial.skip')}
            </button>
            <button type="button" disabled={backDisabled} onClick={() => setIndex(position - 1)}>
              {t('tutorial.back')}
            </button>
            <button
              type="button"
              className="chat-tutorial-next"
              disabled={waiting}
              title={waitingLabel ?? undefined}
              onClick={next}
            >
              {waitingLabel ?? (last ? t('tutorial.done') : t('tutorial.next'))}
            </button>
          </div>
        </>
      )}
    </div>
  );
}
