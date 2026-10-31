import { useContext, useState } from 'react';
import { useRecoilValue } from 'recoil';
import { ChainlitContext, sessionIdState, type IAction, type IStep } from '@chainlit/react-client';
import { useI18n } from '../i18n';
import { PLAN_PREFIX } from '../utils/messageTree';

interface PlanPayload {
  approved: boolean;
}

/**
 * 送信ボタン付近に Plan Mode / Edit Automatically の状態を常時表示するバッジ。
 * クリックで手動切り替え可能（app.py の @cl.action_callback("toggle_plan_mode") を
 * 呼ぶ。ロック解除方向を許可するかは config.ini の [plan].allow_badge_unlock で
 * サーバー側が判断する。計画が無い場合はクリック不可）。
 */
export function PlanModeBadge({ step }: { step: IStep | undefined }) {
  const client = useContext(ChainlitContext);
  const sessionId = useRecoilValue(sessionIdState);
  const { t } = useI18n();
  const [pending, setPending] = useState(false);

  let approved = false;
  let hasPlan = false;
  if (step && typeof step.output === 'string') {
    try {
      const payload: PlanPayload = JSON.parse(step.output.slice(PLAN_PREFIX.length));
      approved = payload.approved;
      hasPlan = true;
    } catch {
      // 計画未作成・パース失敗時は既定の Plan Mode 扱い（クリック不可）
    }
  }

  const handleClick = async () => {
    if (pending || !hasPlan) return;
    setPending(true);
    const action: IAction = {
      id: '',
      name: 'toggle_plan_mode',
      payload: {},
      label: '',
      tooltip: '',
      forId: '',
      onClick: () => {}
    };
    try {
      await client.callAction(action, sessionId);
    } finally {
      setPending(false);
    }
  };

  return (
    <button
      type="button"
      className={`plan-mode-badge ${approved ? 'plan-mode-badge-auto' : 'plan-mode-badge-locked'}`}
      onClick={handleClick}
      disabled={pending || !hasPlan}
      title={
        !hasPlan
          ? t('plan.noPlan')
          : approved
          ? t('plan.toPlanMode')
          : t('plan.toEditAutomatically')
      }
    >
      {approved ? 'Edit Automatically' : 'Plan Mode'}
    </button>
  );
}
