import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { PLAN_PREFIX } from '../utils/messageTree';

interface PlanStepData {
  content: string;
  activeForm: string;
  status: 'pending' | 'in_progress' | 'completed';
}

interface PlanPayload {
  steps: PlanStepData[];
  finished: boolean;
  approved: boolean;
}

const STATUS_ICON: Record<PlanStepData['status'], string> = {
  pending: '☐',
  in_progress: '◐',
  completed: '☑'
};

export function PlanCard({ step }: { step: IStep | undefined }) {
  const { t } = useI18n();
  if (!step || typeof step.output !== 'string') return null;

  let payload: PlanPayload;
  try {
    payload = JSON.parse(step.output.slice(PLAN_PREFIX.length));
  } catch {
    return null;
  }
  if (!payload.steps?.length) return null;

  return (
    <div className="plan-card">
      <div className="plan-card-title">{t('plan.title')}</div>
      <ul className="plan-card-list">
        {payload.steps.map((s, i) => (
          <li key={i} className={`plan-card-item plan-card-item-${s.status}`}>
            <span className="plan-card-icon">{STATUS_ICON[s.status]}</span>
            <span>{s.status === 'in_progress' ? s.activeForm : s.content}</span>
          </li>
        ))}
      </ul>
      {payload.finished && <div className="plan-card-done">{t('plan.done')}</div>}
    </div>
  );
}
