// ブラウザ通知（タスク完了・質問待ち・Plan 承認待ち）。表示設定（ThemePicker.tsx）で
// ON/OFF を切り替え、値は localStorage に保存する（既定は OFF）。
//
// 通知は2段構え:
// 1. デスクトップ通知（Web Notifications API）。localhost / HTTPS の安全なコンテキストで
//    許可済みのときだけ出す（LAN 内の http://<IP> では使えない）。
// 2. タブタイトルの先頭に印を付ける。権限不要で HTTP でも動く。
// どちらもタブを見ていないとき（非表示・別ウィンドウにフォーカス）だけ出し、
// タブに戻ったらタイトルを元に戻す。
import { useEffect, useRef } from 'react';
import { useChatData } from '@chainlit/react-client';
import { useI18n } from './i18n';

const STORAGE_KEY = 'la-notify';
const TITLE_MARK = '(●) ';

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

/** 通知の許可を求める。ユーザー操作（クリック）の中から呼ぶこと。 */
export async function requestNotifyPermission(): Promise<NotificationPermission | 'unsupported'> {
  if (!desktopNotifySupported()) return 'unsupported';
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

let baseTitle: string | null = null;

function markTitle(): void {
  if (baseTitle === null) baseTitle = document.title;
  document.title = TITLE_MARK + baseTitle;
}

function restoreTitle(): void {
  if (baseTitle === null) return;
  // 印を付けている間に tab_title.md の読み込み等でタイトルが変わった場合は、そちらを優先する
  if (document.title.startsWith(TITLE_MARK)) document.title = baseTitle;
  baseTitle = null;
}

function notify(title: string, body: string): void {
  if (!getNotifyEnabled() || !isUserAway()) return;
  markTitle();
  if (!desktopNotifySupported() || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title, { body, tag: 'locohane' });
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch {
    // 一部の環境（Android Chrome 等）は new Notification を許さない。タイトルの印だけで済ませる
  }
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
    notify(title, tRef.current(`notify.${kind}`));
  };

  // タブに戻ったら印を消す
  useEffect(() => {
    const onBack = () => {
      if (!isUserAway()) restoreTitle();
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
