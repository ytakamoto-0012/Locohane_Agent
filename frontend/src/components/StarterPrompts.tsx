import { v4 as uuidv4 } from 'uuid';
import { useChatInteract } from '@chainlit/react-client';
import type { IStep } from '@chainlit/react-client';
import type { PendingAttachment } from './Composer';
import { getStoredReasoningEffort } from './ReasoningEffortButton';

/**
 * チャット開始時のみ表示する定型文ボタン列。クリックすると即座にそのテキストを
 * ユーザー発言として送信する（Composer.tsx の submit() と同じ IStep 組み立て方式）。
 * 添付ファイルはApp.tsx側で保持するstateをComposerと共有し、クリック時に
 * アップロード済みの分だけ一緒に送信する。
 */
export function StarterPrompts({
  prompts,
  attachments,
  onAttachmentsSent
}: {
  prompts: string[];
  attachments: PendingAttachment[];
  onAttachmentsSent: () => void;
}) {
  const { sendMessage } = useChatInteract();

  if (prompts.length === 0) return null;

  const handleClick = (text: string) => {
    const message: IStep = {
      threadId: '',
      id: uuidv4(),
      name: 'あなた',
      type: 'user_message',
      output: text,
      createdAt: new Date().toISOString(),
      metadata: { reasoning_effort: getStoredReasoningEffort() }
    };
    const fileReferences = attachments.filter((a) => a.fileRef).map((a) => ({ id: a.fileRef!.id }));
    sendMessage(message, fileReferences);
    onAttachmentsSent();
  };

  return (
    <div className="starter-prompts">
      {prompts.map((text, i) => (
        <button
          key={i}
          type="button"
          className="starter-prompt-button"
          onClick={() => handleClick(text)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
