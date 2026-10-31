import { useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react';
import { v4 as uuidv4 } from 'uuid';
import { useChatData, useChatInteract } from '@chainlit/react-client';
import type { IFileRef, IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { WorkDirButton } from './WorkDirButton';
import { ReasoningEffortButton, getStoredReasoningEffort } from './ReasoningEffortButton';
import { PlanModeBadge } from './PlanModeBadge';
import { Icon } from './Icon';
import type { InputLimits } from '../utils/messageTree';

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
  // 貼り付けテキスト化の閾値・最大入力文字数（config.ini [ui]、app.py の INPUT_LIMITS_PREFIX）。
  inputLimits: InputLimits;
  // 直近のターンが中断したまま（app.py の INTERRUPTED_PREFIX）。「続ける」ボタンを出す。
  interrupted?: boolean;
}

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
  inputLimits,
  interrupted
}: ComposerProps) {
  const { askUser, disabled, loading } = useChatData();
  const { sendMessage, replyMessage, stopTask } = useChatInteract();
  const { t } = useI18n();
  const [value, setValue] = useState('');
  const [pasteError, setPasteError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const isReplying = askUser?.spec.type === 'text';
  // 他セッションでこのスレッドが処理中、または同じ所有者の別スレッドが
  // 処理中の間は送信自体を止める。
  const inputBlocked = disabled || Boolean(remoteGenerating) || Boolean(blockedByOtherThread);

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
            remoteGenerating
              ? t('composer.placeholderBusy')
              : blockedByOtherThread
                ? t('composer.placeholderOtherBusy')
                : isReplying
                  ? t('composer.placeholderReply')
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
            {loading ? (
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
