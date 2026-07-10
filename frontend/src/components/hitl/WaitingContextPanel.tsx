import type { RunStatus } from '../../state/runTypes';

/**
 * WAITING_FOR_CONTEXT / WAITING_FOR_REPLAN: a safe, informational deferred state.
 * The backend's continuation for these is deferred (it does not fabricate
 * retrieval or planning), so the UI does not offer a fake "continue" action —
 * it surfaces the pending reason and lets the user start a new request.
 */
export function WaitingContextPanel({
  status,
  pendingReason,
}: {
  status: RunStatus;
  pendingReason: string | null;
}) {
  const isReplan = status === 'waiting_for_replan';
  return (
    <div className="hitl hitl-deferred" data-testid="deferred-panel" data-status={status}>
      <h3 className="hitl-title">{isReplan ? 'Replanning needed' : 'More context needed'}</h3>
      <p className="hitl-reason">
        {pendingReason ||
          (isReplan
            ? 'The agent determined the plan needs revision before it can continue.'
            : 'The agent needs additional context before it can continue.')}
      </p>
      <p className="hitl-note">
        This step is deferred — it will resume automatically once the required
        {isReplan ? ' plan revision' : ' context'} is available. You can start a new request in the meantime.
      </p>
    </div>
  );
}
