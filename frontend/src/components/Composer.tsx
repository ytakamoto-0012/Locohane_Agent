import { useEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react';
import { useRecoilValue } from 'recoil';
import { v4 as uuidv4 } from 'uuid';
import { currentThreadIdState, sessionIdState, useChatData, useChatInteract, useChatMessages } from '@chainlit/react-client';
import type { IFileRef, IStep } from '@chainlit/react-client';
import { BACKEND_URL } from '../chainlitClient';
import { useI18n } from '../i18n';
import { WorkDirButton } from './WorkDirButton';
import { ReasoningEffortButton, getStoredReasoningEffort } from './ReasoningEffortButton';
import { PlanModeBadge } from './PlanModeBadge';
import { Icon } from './Icon';
import { selectAppliedInjectionIds, type InputLimits } from '../utils/messageTree';

export interface PendingAttachment {
  name: string;
  fileRef?: IFileRef;
  uploading: boolean;
  // 入力欄への長文貼り付けから生成した添付の場合、その本文（カード表示・文字数
  // カウント・askUser返信時の本文展開に使う）。通常のファイル添付では undefined。
  pastedText?: string;
}

// 貼り付けテキストカードに表示する冒頭プレビューの最大文字数（全文を
// DOMへ流し込むと貼り付け時と同じくブラウザが固まるため切り詰める）。
const PASTED_PREVIEW_CHARS = 200;
// 文字数カウンターを表示し始める、最大入力文字数に対する割合。
const CHAR_COUNTER_SHOW_RATIO = 0.8;
// ターンが終わった（loading が false になった）と判断するまで待つ時間。承認待ち等の "ask" の
// 前後で loading が一瞬 false になるため、すぐには「終わった」とみなさない。
const TURN_END_SETTLE_MS = 1500;
// 追加指示の送信がこれより長くかかったら打ち切る（応答待ちのまま、ターン終了後の処理が止まらないように）。
const INJECT_TIMEOUT_MS = 10000;

/**
 * 生成中に送った追加指示（app.py の /locohane/threads/{id}/inject、src/user_injection.py）。
 * id はここで決めてバックエンドへ渡し、チャット欄に届いたユーザー発言（metadata.injection_id）と突き合わせる。
 * - sending: 送信中
 * - queued: バックエンドが受け付けた（次にモデルを呼ぶ前に会話へ入る）
 * - deferred: ターンが終わりかけで受け付けられなかった（ターンの終了後に通常のメッセージとして送る）
 * - unknown: 通信エラー・タイムアウトで、受け付けられたか分からない（二重送信を避けるため、
 *   会話へ入らないままターンが終わったら自動では送らず入力欄へ戻す）
 * attachments は一緒に送った添付（送り直す・入力欄へ戻すときに使う。バックエンドは受け付けた
 * 時点でスレッド専用フォルダへコピーするため、受け付けられた後はこのセッションの一時ファイルに依存しない）。
 */
interface QueuedInstruction {
  id: string;
  text: string;
  attachments: PendingAttachment[];
  status: 'sending' | 'queued' | 'deferred' | 'unknown';
}

function countLines(text: string): number {
  let lines = 1;
  for (let i = 0; i < text.length; i++) if (text.charCodeAt(i) === 10) lines++;
  return lines;
}

interface ComposerProps {
  plan?: IStep;
  // 今開いているスレッド自体が、他セッションで処理中（true）。
  remoteGenerating?: boolean;
  // /locohane/threads/{id}/stop を呼び、remoteGenerating中の停止ボタンから
  // 実際の生成タスクを cancel() させる（このセッションには
  // session.current_task が無く、純正の stopTask は機能しないため）。
  onStopRemote?: () => void;
  // 今開いているスレッドは処理中ではないが、同じ所有者の別スレッドが処理中
  // （新規チャット・他スレッドからの並列送信を防ぐ。停止操作は提供しない —
  // 対象スレッドを開いてそちら側の停止ボタンを使ってもらう）。
  blockedByOtherThread?: boolean;
  // 作業ディレクトリの変更を許可するか（新規チャットで未送信の間のみtrue）。
  workDirEditable?: boolean;
  // 添付ファイルのstate。StarterPrompts（定型文ボタン）とも共有するため
  // App.tsx側で保持し、controlled propsとして受け取る。
  attachments: PendingAttachment[];
  onAttach: (files: FileList | File[] | null) => void;
  // 長文の貼り付けを textarea へ展開せず添付化する（App.tsx の handleAttachPastedText）。
  onAttachPastedText: (text: string) => void;
  onRemoveAttachment: (index: number) => void;
  onAttachmentsSent: () => void;
  // 生成中の追加指示に付けた添付を、伝わらなかったときに入力欄へ戻す（App.tsx の handleRestoreAttachments）。
  onRestoreAttachments: (attachments: PendingAttachment[]) => void;
  // 貼り付けテキスト化の閾値・最大入力文字数（config.ini [ui]、app.py の INPUT_LIMITS_PREFIX）。
  inputLimits: InputLimits;
  // 直近のターンが中断したまま（app.py の INTERRUPTED_PREFIX）。「続ける」ボタンを出す。
  interrupted?: boolean;
}

// 新規チャットの読み込みがこの時間を過ぎても終わらなければ、停止ボタンを出す（ミリ秒）。
const STARTING_STOP_AFTER_MS = 20000;

export function Composer({
  plan,
  remoteGenerating,
  onStopRemote,
  blockedByOtherThread,
  workDirEditable,
  attachments,
  onAttach,
  onAttachPastedText,
  onRemoveAttachment,
  onAttachmentsSent,
  onRestoreAttachments,
  inputLimits,
  interrupted
}: ComposerProps) {
  const { askUser, connected, loading } = useChatData();
  const { sendMessage, replyMessage, stopTask } = useChatInteract();
  const { messages } = useChatMessages();
  const currentThreadId = useRecoilValue(currentThreadIdState);
  const sessionId = useRecoilValue(sessionIdState);
  const { t } = useI18n();
  const [value, setValue] = useState('');
  const [pasteError, setPasteError] = useState<string | null>(null);
  const [queued, setQueued] = useState<QueuedInstruction[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const messagesRef = useRef(messages);
  messagesRef.current = messages;

  const isReplying = askUser?.spec.type === 'text';
  // この会話のターンが動いている間（承認ボタン等の応答待ちを含む）。送信は追加指示（割り込み）
  // として扱う。生成中に別の会話へ移って戻ってきた画面（remoteGenerating。生成しているのは
  // 別のセッション）も含める。そちらは loading が false のままなので、含めないと送った直後に
  // 「ターンが終わった」とみなして、まだ伝わっていない指示を入力欄へ戻してしまう。
  // 新規チャットの読み込み中（ユーザーの発言がまだ1件も無いのに loading が true）。ここで入力できたり
  // 停止ボタンになったりしないよう、入力欄と送信ボタンを止める（停止ボタンにはしない）。
  // ただし読み込みが STARTING_STOP_AFTER_MS を過ぎても終わらない（on_chat_start の初期化が固まった等）ときは、
  // 抜け出せるよう停止ボタンだけ出す（入力欄は止めたまま）。
  const startingRaw = loading && !messages.some((m) => m.type === 'user_message');
  const [startingTooLong, setStartingTooLong] = useState(false);
  useEffect(() => {
    if (!startingRaw) {
      setStartingTooLong(false);
      return;
    }
    const timer = window.setTimeout(() => setStartingTooLong(true), STARTING_STOP_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [startingRaw]);
  const starting = startingRaw && !startingTooLong;
  const turnRunning = (loading && !starting) || askUser?.spec.type === 'action' || Boolean(remoteGenerating);
  // useChatData().disabled は生成中（loading）も true になるが、生成中も追加指示を
  // 入力できるよう、未接続とファイル・要素の応答待ちのときだけ止める。
  // 同じ所有者の別スレッドが処理中の間は送信自体を止める（この会話が他セッションで
  // 処理中の間は、追加指示としてだけ送れる）。
  const askBlocksInput = askUser?.spec.type === 'file' || askUser?.spec.type === 'element';
  const inputBlocked = !connected || startingRaw || askBlocksInput || Boolean(blockedByOtherThread);
  const inputBlockedRef = useRef(inputBlocked);
  inputBlockedRef.current = inputBlocked;

  // 会話へ入った（チャット欄にユーザー発言として届いた）追加指示は「送信待ち」から外す。
  useEffect(() => {
    if (queued.length === 0) return;
    const applied = selectAppliedInjectionIds(messages);
    if (queued.some((q) => applied.has(q.id))) {
      setQueued((prev) => prev.filter((q) => !applied.has(q.id)));
    }
  }, [messages, queued]);

  // ターンが終わったら、受け付けられなかった指示（deferred）は通常のメッセージとして送り、
  // 受け付けたのに反映されないまま終わった指示（停止・エラーで中断した場合）は入力欄へ戻す。
  useEffect(() => {
    if (turnRunning || queued.length === 0 || queued.some((q) => q.status === 'sending')) return;
    const timer = setTimeout(() => {
      const applied = selectAppliedInjectionIds(messagesRef.current);
      const pending = queued.filter((q) => !applied.has(q.id));
      setQueued([]);
      const deferred = pending.filter((q) => q.status === 'deferred');
      const restore = pending.filter((q) => q.status !== 'deferred' || inputBlockedRef.current);
      if (deferred.length > 0 && !inputBlockedRef.current) {
        const fileReferences = deferred.flatMap((q) =>
          q.attachments.flatMap((a) => (a.fileRef ? [{ id: a.fileRef.id }] : []))
        );
        sendMessage(
          buildUserMessage(
            deferred
              .map((q) => q.text)
              .filter((s) => s)
              .join('\n\n')
          ),
          fileReferences
        );
      }
      if (restore.length > 0) {
        restoreToInput(restore);
        setPasteError(t('composer.injectNotApplied'));
      }
    }, TURN_END_SETTLE_MS);
    return () => clearTimeout(timer);
    // buildUserMessage・sendMessage・t は毎回作り直されるが、判定には使わない
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnRunning, queued]);

  const setQueuedStatus = (id: string, status: QueuedInstruction['status']) =>
    setQueued((prev) => prev.map((q) => (q.id === id ? { ...q, status } : q)));

  // 伝わらなかった指示の本文・添付を入力欄へ戻す。
  const restoreToInput = (items: QueuedInstruction[]) => {
    const texts = items.map((q) => q.text).filter((s) => s);
    if (texts.length > 0) setValue((v) => [...texts, v].filter((s) => s).join('\n'));
    const restoredAttachments = items.flatMap((q) => q.attachments);
    if (restoredAttachments.length > 0) onRestoreAttachments(restoredAttachments);
  };

  // 生成中の送信: 入力欄の本文と添付を追加指示としてバックエンドへ積む。
  const submitInstruction = () => {
    const text = value.trim();
    if (!text && attachments.length === 0) return;
    const id = uuidv4();
    const sentAttachments = attachments;
    const files = sentAttachments.flatMap((a) => (a.fileRef ? [a.fileRef.id] : []));
    setQueued((prev) => [
      ...prev,
      { id, text, attachments: sentAttachments, status: currentThreadId ? 'sending' : 'deferred' }
    ]);
    setValue('');
    setPasteError(null);
    onAttachmentsSent();
    if (fileInputRef.current) fileInputRef.current.value = '';
    if (!currentThreadId) return;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), INJECT_TIMEOUT_MS);
    fetch(`${BACKEND_URL}/locohane/threads/${currentThreadId}/inject`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, id, files, session_id: sessionId }),
      signal: controller.signal
    })
      .then((res) => {
        if (res.ok) {
          setQueuedStatus(id, 'queued');
        } else if (res.status === 409) {
          // 確実に積まれていない（受付が閉じていた）ので、ターンの終了後に通常のメッセージとして送る。
          setQueuedStatus(id, 'deferred');
        } else {
          // 長すぎる・添付が使えない等で拒否された（確実に積まれていない）。入力欄へ戻して知らせる。
          setQueued((prev) => prev.filter((q) => q.id !== id));
          restoreToInput([{ id, text, attachments: sentAttachments, status: 'deferred' }]);
          setPasteError(t('composer.injectFailed'));
        }
      })
      .catch(() => setQueuedStatus(id, 'unknown'))
      .finally(() => clearTimeout(timeout));
  };

  // 最大入力文字数の判定対象は、入力欄の本文＋貼り付けテキストの合計
  // （app.py の _check_input_length と同じ数え方）。
  const { pasteAsAttachmentThresholdChars, maxInputChars } = inputLimits;
  const pastedChars = attachments.reduce((sum, a) => sum + (a.pastedText?.length ?? 0), 0);
  const totalChars = value.length + pastedChars;
  const overLimit = maxInputChars > 0 && totalChars > maxInputChars;
  const showCharCounter = maxInputChars > 0 && totalChars >= maxInputChars * CHAR_COUNTER_SHOW_RATIO;
  // アップロード完了前に送信すると fileRef の無い添付が黙って落ちるため待たせる。
  const uploading = attachments.some((a) => a.uploading);

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    setPasteError(null);
    const items = Array.from(e.clipboardData?.items ?? []);
    const imageFiles = items
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
      .map((file, i) => {
        if (file.name && file.name !== 'image.png') return file;
        const ext = file.type.split('/')[1] ?? 'png';
        const renamed = new File([file], `clipboard-${Date.now()}-${i}.${ext}`, { type: file.type });
        return renamed;
      });
    if (imageFiles.length > 0) {
      e.preventDefault();
      onAttach(imageFiles);
      return;
    }

    // 長文はブラウザの既定動作（textareaへの挿入）に任せると描画で固まるため、
    // 既定動作を止めて「貼り付けテキスト」カードとして添付化する。
    if (pasteAsAttachmentThresholdChars <= 0) return;
    const text = e.clipboardData?.getData('text/plain') ?? '';
    if (text.length < pasteAsAttachmentThresholdChars) return;
    e.preventDefault();
    if (maxInputChars > 0 && totalChars + text.length > maxInputChars) {
      setPasteError(
        t('composer.pasteTooLong', {
          length: text.length.toLocaleString(),
          limit: maxInputChars.toLocaleString()
        })
      );
      return;
    }
    onAttachPastedText(text);
  };

  const buildUserMessage = (output: string): IStep => ({
    threadId: '',
    id: uuidv4(),
    name: 'あなた',
    type: 'user_message',
    output,
    createdAt: new Date().toISOString(),
    metadata: { reasoning_effort: getStoredReasoningEffort() }
  });

  // 入力欄の本文・添付には触れず、「続けて」だけを送る。
  const showContinue = Boolean(interrupted) && !loading && !askUser && !inputBlocked;
  const sendContinue = () => {
    if (!showContinue) return;
    sendMessage(buildUserMessage(t('messages.continuePrompt')), []);
  };

  const submit = () => {
    if (turnRunning && !isReplying) {
      if (!inputBlocked && !overLimit && !uploading) submitInstruction();
      return;
    }
    if (inputBlocked || overLimit || uploading || (!value.trim() && attachments.length === 0)) return;

    const message = buildUserMessage(value);

    if (isReplying && askUser) {
      // askUser への返信はファイル参照を送れないため、貼り付けテキストは本文へ展開する。
      const pastedTexts = attachments.flatMap((a) => (a.pastedText !== undefined ? [a.pastedText] : []));
      if (pastedTexts.length > 0) message.output = [value, ...pastedTexts].filter((t) => t).join('\n\n');
      replyMessage(message);
    } else {
      const fileReferences = attachments.filter((a) => a.fileRef).map((a) => ({ id: a.fileRef!.id }));
      sendMessage(message, fileReferences);
    }

    setValue('');
    setPasteError(null);
    onAttachmentsSent();
    if (fileInputRef.current) fileInputRef.current.value = '';
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="composer">
      {remoteGenerating ? (
        <div className="composer-remote-generating-banner">
          <span className="composer-remote-generating-dot" />
          {t('composer.busyThisThread')}
        </div>
      ) : blockedByOtherThread ? (
        <div className="composer-remote-generating-banner">
          <span className="composer-remote-generating-dot" />
          {t('composer.busyOtherThread')}
        </div>
      ) : null}
      {showContinue ? (
        <div className="composer-continue-row">
          <button
            type="button"
            className="composer-continue-button"
            onClick={sendContinue}
            title={t('messages.continueTitle')}
          >
            <Icon name="play" size={12} />
            {t('messages.continue')}
          </button>
        </div>
      ) : null}
      {attachments.some((a) => a.pastedText !== undefined) ? (
        <div className="composer-pasted-cards">
          {attachments.map((a, i) =>
            a.pastedText === undefined ? null : (
              <div key={i} className="composer-pasted-card" title={a.name}>
                <div className="composer-pasted-card-header">
                  <span className="composer-pasted-card-title">
                    {a.uploading ? <span className="attachment-chip-spinner" /> : null}
                    {t('composer.pastedText')}
                  </span>
                  <button
                    type="button"
                    className="composer-attachment-remove"
                    title={t('composer.removePastedText')}
                    onClick={() => onRemoveAttachment(i)}
                  >
                    <Icon name="x" size={10} />
                  </button>
                </div>
                <div className="composer-pasted-card-preview">{a.pastedText.slice(0, PASTED_PREVIEW_CHARS)}</div>
                <div className="composer-pasted-card-meta">
                  {t('composer.pastedTextStats', {
                    chars: a.pastedText.length.toLocaleString(),
                    lines: countLines(a.pastedText).toLocaleString()
                  })}
                </div>
              </div>
            )
          )}
        </div>
      ) : null}
      {queued.length > 0 ? (
        <div className="composer-queued">
          {queued.map((q) => (
            <div key={q.id} className="composer-queued-item">
              <span className="composer-queued-label">
                {q.status === 'deferred' ? t('composer.queuedNextTurn') : t('composer.queuedNextStep')}
              </span>
              <span className="composer-queued-text">
                {q.attachments.length > 0 ? (
                  <span className="composer-queued-files">
                    <Icon name="paperclip" size={12} />
                    {q.attachments.length}
                  </span>
                ) : null}
                {q.text}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      {pasteError ? <div className="composer-paste-error">{pasteError}</div> : null}
      {attachments.some((a) => a.pastedText === undefined) ? (
        <div className="composer-attachments">
          {attachments.map((a, i) =>
            a.pastedText !== undefined ? null : (
            <span key={i} className="composer-attachment-chip">
              {a.uploading ? <span className="attachment-chip-spinner" /> : <Icon name="paperclip" size={12} />}
              {a.name}
              <button
                type="button"
                className="composer-attachment-remove"
                title={t('composer.removeAttachment')}
                onClick={() => onRemoveAttachment(i)}
              >
                <Icon name="x" size={10} />
              </button>
            </span>
            )
          )}
        </div>
      ) : null}
      <div className="composer-box">
        <textarea
          className="composer-textarea"
          value={value}
          placeholder={
            blockedByOtherThread
              ? t('composer.placeholderOtherBusy')
              : isReplying
                ? t('composer.placeholderReply')
                : turnRunning
                  ? t('composer.placeholderInject')
                  : t('composer.placeholder')
          }
          disabled={inputBlocked}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          rows={4}
        />
        <div className="composer-toolbar">
          <div className="composer-toolbar-left">
            <button
              type="button"
              className="composer-icon-button"
              title={t('composer.attachFile')}
              onClick={() => fileInputRef.current?.click()}
            >
              <Icon name="paperclip" />
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              hidden
              onChange={(e) => onAttach(e.target.files)}
            />
            <WorkDirButton disabled={!workDirEditable} />
            {/* askUser の応答待ち（Plan承認等）も loading は false になるが、ターンの途中なので変更させない */}
            <ReasoningEffortButton disabled={loading || Boolean(askUser) || Boolean(remoteGenerating)} />
          </div>
          <div className="composer-toolbar-right">
            {showCharCounter ? (
              <span
                className={`composer-char-counter${overLimit ? ' composer-char-counter--over' : ''}`}
                title={t('composer.charCountTitle')}
              >
                {totalChars.toLocaleString()} / {maxInputChars.toLocaleString()}
              </span>
            ) : null}
            <PlanModeBadge step={plan} />
            {/* 生成中は停止ボタンと並べて出す（承認待ち等で loading=false の間は下の送信ボタンが同じ働きをする） */}
            {((loading && !starting) || remoteGenerating) && !isReplying && (value.trim() || attachments.length > 0) ? (
              <button
                type="button"
                className="composer-submit-button"
                onClick={submit}
                disabled={inputBlocked || overLimit || uploading}
                title={t('composer.injectTitle')}
              >
                {t('composer.inject')}
              </button>
            ) : null}
            {loading && !starting ? (
              <button type="button" className="composer-stop-button" onClick={stopTask}>
                {t('common.stop')}
              </button>
            ) : remoteGenerating && onStopRemote ? (
              <button type="button" className="composer-stop-button" onClick={onStopRemote}>
                {t('common.stop')}
              </button>
            ) : (
              <button
                type="button"
                className="composer-submit-button"
                onClick={submit}
                disabled={inputBlocked || overLimit || uploading}
                title={overLimit ? t('composer.overLimit') : undefined}
              >
                {t('common.send')}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
