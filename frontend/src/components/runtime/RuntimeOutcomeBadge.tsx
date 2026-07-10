import type { RunStatus } from '../../state/runTypes';

const LABEL: Record<RunStatus, string> = {
  idle: 'Ready',
  connecting: 'Connecting…',
  running: 'Running',
  streaming_answer: 'Answering',
  waiting_for_user: 'Needs clarification',
  waiting_for_approval: 'Needs approval',
  waiting_for_context: 'Waiting for context',
  waiting_for_replan: 'Waiting for replan',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
};

const TONE: Record<RunStatus, string> = {
  idle: 'neutral',
  connecting: 'active',
  running: 'active',
  streaming_answer: 'active',
  waiting_for_user: 'wait',
  waiting_for_approval: 'wait',
  waiting_for_context: 'wait',
  waiting_for_replan: 'wait',
  completed: 'ok',
  failed: 'error',
  cancelled: 'neutral',
};

export function RuntimeOutcomeBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`badge badge-${TONE[status]}`} data-testid="outcome-badge" data-status={status}>
      {LABEL[status]}
    </span>
  );
}
