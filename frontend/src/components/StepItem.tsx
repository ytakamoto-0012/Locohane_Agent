import { useState } from 'react';
import type { IStep } from '@chainlit/react-client';
import { useI18n, type MessageKey } from '../i18n';

const TYPE_LABELS: Record<string, MessageKey> = {
  tool: 'step.tool',
  llm: 'step.llm',
  retrieval: 'step.retrieval',
  system_message: 'step.system'
};

function formatContent(value: unknown): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

const STATUS_LABELS = {
  running: 'step.running',
  done: 'step.done',
  error: 'step.error',
  stopped: 'step.stopped',
  background: 'step.background'
} as const satisfies Record<string, MessageKey>;

export function StepItem({ step }: { step: IStep }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const hasContent = Boolean(step.input || step.output);
  // dispatch_agent（サブエージェント実行）配下のツール呼び出しは、実際には
  // 別のRunnableとして委譲元Stepの下に正しくネストされる（app.pyの
  // _resolve_parent_id 参照）。ここで子Stepを折りたたみ内に再帰表示することで、
  // 「サブエージェント実行中の間、その内部で何が起きているか」が見える。
  const children = step.steps ?? [];
  const hasChildren = children.length > 0;
  const expandable = hasContent || hasChildren;
  const running = step.start && !step.end && !step.isError;
  // ループ検知等、アプリ側の判断で打ち切られたStepは正常完了と区別する
  // （app.py が thinking.metadata.stopped_reason を設定する）。
  const stopped = Boolean(step.metadata?.stopped_reason) && !step.isError;
  // dispatch_agent_batch が安全上限で先に返った後も裏で動き続けるグループ
  // （app.py の _mark_background_group_steps が metadata.background を設定する）。
  const background = Boolean(step.metadata?.background) && !step.isError;
  const status: keyof typeof STATUS_LABELS = step.isError
    ? 'error'
    : stopped
      ? 'stopped'
      : background
        ? 'background'
        : running
          ? 'running'
          : 'done';
  // サブエージェントの思考Stepは思考を input、回答本文を output に持つ
  // （app.py の _get_or_open_thinking が metadata.subagent_output を設定する）。
  const subagentOutput = Boolean(step.metadata?.subagent_output);
  // メインエージェントの思考Stepは思考を output に持つため、見出しを「思考」にする
  // （「出力」と表示すると回答本文がバッジへ入ったように見える）。
  const inputTitle = subagentOutput ? t('step.thinking') : t('step.input');
  const outputTitle = step.type === 'llm' && !subagentOutput ? t('step.thinking') : t('step.output');
  const typeKey = TYPE_LABELS[step.type];
  const typeLabel = typeKey ? t(typeKey) : step.type;
  // dispatch_agent（サブエージェント実行）のStepは、app.py側で name を
  // "SUB: <agent_type>" 形式にして送ってくる。これは「ツール: サブエージェ...」
  // という中身の分からない表示（狭い幅で agent_type 部分が見切れる）を避け、
  // どのサブエージェント種別かひと目で分かるようにするためのもので、
  // 通常のツールと違い typeLabel（"ツール"）を前置しない。
  // 思考Stepの name はバックエンドが常に「思考中」で送るため、表示言語の文言に置き換える。
  const displayLabel =
    step.name && step.name.startsWith('SUB: ')
      ? step.name
      : step.type !== 'llm' && step.name && step.name !== typeLabel
        ? `${typeLabel}: ${step.name}`
        : typeLabel;

  return (
    <div className={`step-item ${step.isError ? 'step-item--error' : ''}`}>
      <button
        type="button"
        className="step-item-header"
        onClick={() => expandable && setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={`step-item-status ${running ? 'step-item-status--running' : ''}`} />
        <span className="step-item-label">{displayLabel}</span>
        {hasChildren ? <span className="step-item-child-count">{children.length}</span> : null}
        <span className={`step-item-status-badge step-item-status-badge--${status}`}>
          {t(STATUS_LABELS[status])}
        </span>
        {expandable ? <span className="step-item-caret">{open ? '▾' : '▸'}</span> : null}
      </button>
      {open && hasContent ? (
        <div className="step-item-body">
          {step.input ? (
            <div>
              <div className="step-item-body-title">{inputTitle}</div>
              <pre>{formatContent(step.input)}</pre>
            </div>
          ) : null}
          {step.output ? (
            <div>
              <div className="step-item-body-title">{outputTitle}</div>
              <pre>{formatContent(step.output)}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
      {open && hasChildren ? (
        <div className="step-item-children">
          {children.map((child) => (
            <StepItem key={child.id} step={child} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
