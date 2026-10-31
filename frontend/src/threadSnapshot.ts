import { addMessage } from '@chainlit/react-client';
import type { IMessageElement, IStep, IThread, ITasklistElement } from '@chainlit/react-client';
import { BACKEND_URL } from './chainlitClient';

// 生成完了検知後、window.location.reload() の代わりにこのスレッドの最新
// 永続化済み状態を1回だけ取り直すための軽量スナップショット取得。
// app.py の GET /locohane/threads/{thread_id} は Chainlit公式の
// GET /project/thread/{thread_id} と同じ形（IThread: steps/elements/metadata）を
// 匿名モード対応で返すだけの薄いラッパー。
export async function fetchThreadSnapshot(threadId: string): Promise<IThread> {
  const res = await fetch(`${BACKEND_URL}/locohane/threads/${threadId}`, { credentials: 'include' });
  if (!res.ok) throw new Error(`failed to fetch thread snapshot (status=${res.status})`);
  return res.json() as Promise<IThread>;
}

// @chainlit/react-client 内部の resume_thread ソケットイベントハンドラ
// （steps を addMessage で1件ずつ積み上げてネスト構造を復元する処理）と
// 同じ組み立て方をREST取得結果にも適用する。
export function threadStepsToMessages(thread: IThread): IStep[] {
  let messages: IStep[] = [];
  for (const step of thread.steps) {
    messages = addMessage(messages, step);
  }
  return messages;
}

// 同じく resume_thread ハンドラと同じ振り分け方（tasklist / それ以外の
// 表示要素）。avatar はサイドパネル等に描画する対象ではないため除外する。
export function splitThreadElements(thread: IThread): {
  tasklist: ITasklistElement[];
  display: IMessageElement[];
} {
  const elements = thread.elements ?? [];
  const tasklist = elements.filter((e): e is ITasklistElement => e.type === 'tasklist');
  const display = elements.filter(
    (e): e is IMessageElement => e.type !== 'tasklist' && (e.type as string) !== 'avatar'
  );
  return { tasklist, display };
}
