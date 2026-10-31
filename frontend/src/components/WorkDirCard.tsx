import type { IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { isLoopbackHost } from '../utils/serverHost';

export function WorkDirCard({ step }: { step: IStep | undefined }) {
  const { t } = useI18n();
  if (!step || typeof step.output !== 'string') return null;
  const value = step.output.slice(step.output.indexOf(':') + 1).trim();

  return (
    <div className="work-dir-card">
      <div className="work-dir-card-label">
        {t('workdir.title')}
        {!isLoopbackHost() && (
          <span className="work-dir-card-scope" title={t('workdir.serverScopeTitle')}>
            {t('workdir.serverScope')}
          </span>
        )}
      </div>
      <div className="work-dir-card-value">{value}</div>
    </div>
  );
}
