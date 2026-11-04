// ブラウザ通知（タスク完了・質問待ち・Plan 承認待ち）。表示設定（ThemePicker.tsx）で
// ON/OFF を切り替え、値は localStorage に保存する（既定は OFF）。
//
// 通知は次の組み合わせで出す（気づきやすさ優先）:
// 1. デスクトップ通知（Web Notifications API）。localhost / HTTPS の安全なコンテキストで
//    許可済みのときだけ出す（LAN 内の http://<IP> では使えない）。質問・承認待ちは
//    閉じるまで画面に残す（requireInteraction）。
// 2. 通知音（Web Audio で合成。音声ファイル不要・オフラインで動く）。
// 3. タブタイトルの点滅（「● 質問への回答を待っています」と元のタイトルを交互に表示）。
// 4. favicon に赤い丸のバッジを付ける。
// 2〜4 は権限不要で HTTP でも動く。どれもタブを見ていないとき（非表示・別ウィンドウに
// フォーカス）だけ出し、タブに戻ったら元に戻す。
import { useEffect, useRef } from 'react';
import { useChatData } from '@chainlit/react-client';
import { useI18n } from './i18n';

const STORAGE_KEY = 'la-notify';
const TITLE_MARK = '● ';
const BLINK_MS = 1000;

// ask の前後で loading が一瞬 false になる（session.emit_call 経由の "ask" は
// task_end/task_start を挟む。app.py の /locohane/threads/{id}/status 参照）ため、
// loading が false になってからこの時間だけ待ち、ask が来ないことを確かめてから完了とみなす。
const COMPLETE_SETTLE_MS = 1500;

export type NotifyKind = 'complete' | 'question' | 'approval';

export function getNotifyEnabled(): boolean {
  return localStorage.getItem(STORAGE_KEY) === '1';
}

export function setNotifyEnabled(enabled: boolean): void {
  if (enabled) localStorage.setItem(STORAGE_KEY, '1');
  else localStorage.removeItem(STORAGE_KEY);
}

/** デスクトップ通知が使えるか（安全なコンテキストかつブラウザが対応しているか）。 */
export function desktopNotifySupported(): boolean {
  return typeof window !== 'undefined' && window.isSecureContext && 'Notification' in window;
}

export function currentNotifyPermission(): NotificationPermission | 'unsupported' {
  return desktopNotifySupported() ? Notification.permission : 'unsupported';
}

/** 通知の許可を求める。ユーザー操作（クリック）の中から呼ぶこと。 */
export async function requestNotifyPermission(): Promise<NotificationPermission | 'unsupported'> {
  if (!desktopNotifySupported()) return 'unsupported';
  // ユーザー操作の中で AudioContext を作っておくと、後で操作なしでも音を鳴らせる
  getAudioContext();
  if (Notification.permission !== 'default') return Notification.permission;
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

function isUserAway(): boolean {
  return document.hidden || !document.hasFocus();
}

// ---- 通知音 ----

let audioCtx: AudioContext | null = null;

function getAudioContext(): AudioContext | null {
  if (audioCtx) return audioCtx;
  const Ctor = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!Ctor) return null;
  try {
    audioCtx = new Ctor();
  } catch {
    audioCtx = null;
  }
  return audioCtx;
}

// ページを一度でも操作していれば AudioContext を再開できる（自動再生ポリシー対策）
if (typeof window !== 'undefined') {
  const unlock = () => {
    const ctx = getAudioContext();
    if (ctx && ctx.state === 'suspended') void ctx.resume();
  };
  window.addEventListener('pointerdown', unlock, { capture: true });
  window.addEventListener('keydown', unlock, { capture: true });
}

// 完了は上がる2音、質問・承認待ちは高めの3音を2回（呼ばれている感じを強める）
const CHIMES: Record<NotifyKind, number[]> = {
  complete: [660, 880],
  question: [880, 1175, 880, 1175],
  approval: [880, 1175, 880, 1175]
};

function playChime(kind: NotifyKind): void {
  const ctx = getAudioContext();
  if (!ctx) return;
  if (ctx.state === 'suspended') void ctx.resume();
  const step = 0.16;
  const start = ctx.currentTime + 0.02;
  CHIMES[kind].forEach((freq, i) => {
    const t0 = start + i * step;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, t0);
    gain.gain.exponentialRampToValueAtTime(0.35, t0 + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, t0 + step * 0.95);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t0);
    osc.stop(t0 + step);
  });
}

// ---- タブタイトルの点滅 ----

let baseTitle: string | null = null;
let blinkTimer: number | null = null;

function startBlink(message: string): void {
  if (baseTitle === null) baseTitle = document.title;
  if (blinkTimer !== null) window.clearInterval(blinkTimer);
  const alert = TITLE_MARK + message;
  let on = true;
  document.title = alert;
  blinkTimer = window.setInterval(() => {
    // 点滅中に tab_title.md の読み込み等でタイトルが変わったら、それを新しい元タイトルにする
    if (document.title !== alert && document.title !== baseTitle) baseTitle = document.title;
    on = !on;
    document.title = on ? alert : (baseTitle ?? '');
  }, BLINK_MS);
}

function stopBlink(): void {
  if (blinkTimer !== null) {
    window.clearInterval(blinkTimer);
    blinkTimer = null;
  }
  if (baseTitle === null) return;
  if (document.title.startsWith(TITLE_MARK)) document.title = baseTitle;
  baseTitle = null;
}

// ---- favicon のバッジ ----

let originalFavicon: string | null = null;

function faviconLink(): HTMLLinkElement | null {
  return document.querySelector<HTMLLinkElement>('link[rel~="icon"]');
}

function setFaviconBadge(): void {
  const link = faviconLink();
  if (!link) return;
  if (originalFavicon === null) originalFavicon = link.href;
  const size = 64;
  const draw = (img: HTMLImageElement | null) => {
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const c = canvas.getContext('2d');
    if (!c) return;
    if (img) c.drawImage(img, 0, 0, size, size);
    c.beginPath();
    c.arc(size * 0.72, size * 0.28, size * 0.26, 0, Math.PI * 2);
    c.fillStyle = '#e5322d';
    c.fill();
    c.lineWidth = size * 0.06;
    c.strokeStyle = '#ffffff';
    c.stroke();
    // バッジを付けている間にタブへ戻っていたら付けない
    if (originalFavicon !== null) link.href = canvas.toDataURL('image/png');
  };
  const img = new Image();
  img.onload = () => draw(img);
  img.onerror = () => draw(null);
  img.src = originalFavicon;
}

function clearFaviconBadge(): void {
  if (originalFavicon === null) return;
  const link = faviconLink();
  const href = originalFavicon;
  originalFavicon = null;
  if (link) link.href = href;
}

// ---- まとめ ----

function clearAlerts(): void {
  stopBlink();
  clearFaviconBadge();
}

function showDesktopNotification(kind: NotifyKind, title: string, body: string): boolean {
  if (!desktopNotifySupported() || Notification.permission !== 'granted') return false;
  try {
    // 同じ tag の通知は既定で「黙って差し替え」られポップアップしないため、renotify で毎回出す
    const options = {
      body,
      tag: `locohane-${kind}`,
      renotify: true,
      requireInteraction: kind !== 'complete',
      icon: originalFavicon ?? faviconLink()?.href
    } as NotificationOptions;
    const n = new Notification(title, options);
    n.onclick = () => {
      window.focus();
      n.close();
    };
    return true;
  } catch {
    // 一部の環境（Android Chrome 等）は new Notification を許さない。音とタイトルだけで済ませる
    return false;
  }
}

function notify(kind: NotifyKind, title: string, body: string): void {
  if (!getNotifyEnabled() || !isUserAway()) return;
  startBlink(body);
  setFaviconBadge();
  playChime(kind);
  showDesktopNotification(kind, title, body);
}

export type TestNotifyResult = 'shown' | 'unsupported' | 'denied' | 'default' | 'failed';

/**
 * 設定画面の「テスト」ボタン用。タブを見ていても出す。デスクトップ通知が出せたかを返す
 * （'shown' でも画面に出ない場合は OS 側の通知設定・応答不可モードが原因）。
 */
export async function sendTestNotify(title: string, body: string): Promise<TestNotifyResult> {
  playChime('question');
  // Edge の「静かな通知リクエスト」等では確認がアドレスバーの小さなアイコンに格下げされ、
  // 応答するまで Promise が解決しないため、待ち切れなければ未許可として結果を返す
  const permission = await Promise.race([
    requestNotifyPermission(),
    new Promise<NotificationPermission>((resolve) => setTimeout(() => resolve(Notification.permission), 5000))
  ]);
  if (permission === 'unsupported') return 'unsupported';
  if (permission !== 'granted') return permission;
  return showDesktopNotification('complete', title, body) ? 'shown' : 'failed';
}

/**
 * このセッションのターンの状態を見て、完了・質問待ち・Plan 承認待ちを通知する。
 * 他セッションで動いていたターンの完了は App.tsx のポーリングから notifyRemoteComplete を呼ぶ。
 */
export function useBrowserNotify(): { notifyRemoteComplete: () => void } {
  const { loading, askUser } = useChatData();
  const { t } = useI18n();
  const tRef = useRef(t);
  tRef.current = t;
  const askUserRef = useRef(askUser);
  askUserRef.current = askUser;

  const send = (kind: NotifyKind) => {
    const title = baseTitle ?? document.title;
    notify(kind, title, tRef.current(`notify.${kind}`));
  };

  // タブに戻ったら点滅・バッジを消す
  useEffect(() => {
    const onBack = () => {
      if (!isUserAway()) clearAlerts();
    };
    window.addEventListener('focus', onBack);
    document.addEventListener('visibilitychange', onBack);
    return () => {
      window.removeEventListener('focus', onBack);
      document.removeEventListener('visibilitychange', onBack);
    };
  }, []);

  // 質問・Plan 承認待ち（askUser が新しく出たとき）
  const prevAskRef = useRef(askUser);
  useEffect(() => {
    const prev = prevAskRef.current;
    prevAskRef.current = askUser;
    if (!askUser || askUser === prev) return;
    send(askUser.spec.type === 'action' ? 'approval' : 'question');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [askUser]);

  // 完了（loading が true→false になり、少し待っても ask が来ない）
  const wasLoadingRef = useRef(loading);
  useEffect(() => {
    const wasLoading = wasLoadingRef.current;
    wasLoadingRef.current = loading;
    if (loading || !wasLoading) return;
    const timer = setTimeout(() => {
      if (!wasLoadingRef.current && !askUserRef.current) send('complete');
    }, COMPLETE_SETTLE_MS);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loading]);

  return { notifyRemoteComplete: () => send('complete') };
}
