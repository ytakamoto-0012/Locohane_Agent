import { useState } from 'react';
import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { StepList } from './StepList';
import { TokenUsageCard } from './TokenUsageCard';
import { WorkDirCard } from './WorkDirCard';
import { PlanCard } from './PlanCard';

export function SidePanel({
  sideSteps,
  tokenUsage,
  workDir,
  plan,
  mobile,
  open,
  onClose
}: {
  sideSteps: IStep[];
  tokenUsage: IStep | undefined;
  workDir: IStep | undefined;
  plan: IStep | undefined;
  /** スマホ幅では折りたたみボタンの代わりに引き出し（open/onClose は App が持つ）で表示する */
  mobile: boolean;
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const [collapsed, setCollapsed] = useState(false);

  if (mobile && !open) return null;

  if (!mobile && collapsed) {
    return (
      <button type="button" className="side-panel-collapsed" onClick={() => setCollapsed(false)}>
        {t('side.detailsCollapsed')}
      </button>
    );
  }

  return (
    <aside className={mobile ? 'side-panel side-panel--drawer' : 'side-panel'}>
      <div className="side-panel-fixed">
        <WorkDirCard step={workDir} />
        <div className="side-panel-header">
          <span>{t('side.details')}</span>
          <button
            type="button"
            className="side-panel-toggle"
            title={mobile ? t('side.closeDetails') : undefined}
            onClick={() => (mobile ? onClose() : setCollapsed(true))}
          >
            {mobile ? '✕' : '▸'}
          </button>
        </div>
        <TokenUsageCard step={tokenUsage} />
        <PlanCard step={plan} />
      </div>
      <StepList steps={sideSteps} />
    </aside>
  );
}
