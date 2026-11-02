import type { IStep } from '@chainlit/react-client';
import type { MessageKey, Translate } from '../i18n';

const TYPE_LABELS: Record<string, MessageKey> = {
  tool: 'step.tool',
  llm: 'step.llm',
  retrieval: 'step.retrieval',
  system_message: 'step.system'
};

/** Stepの見出しに出す表示名（サイドパネルの StepItem）。チャット欄の応答待ち表示
 *  （MessageThread の TypingIndicator）も同じ表記を使う。 */
export function stepDisplayLabel(step: IStep, t: Translate): string {
  const typeKey = TYPE_LABELS[step.type];
  const typeLabel = typeKey ? t(typeKey) : step.type;
  // dispatch_agent（サブエージェント実行）のStepは、app.py側で name を
  // "SUB: <agent_type>" 形式にして送ってくる。これは「ツール: サブエージェ...」
  // という中身の分からない表示（狭い幅で agent_type 部分が見切れる）を避け、
  // どのサブエージェント種別かひと目で分かるようにするためのもので、
  // 通常のツールと違い typeLabel（"ツール"）を前置しない。
  // 思考Stepの name はバックエンドが常に「思考中」で送るため、表示言語の文言に置き換える。
  if (step.name && step.name.startsWith('SUB: ')) return step.name;
  if (step.type !== 'llm' && step.name && step.name !== typeLabel) return `${typeLabel}: ${step.name}`;
  return typeLabel;
}
