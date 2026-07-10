import { useAgentRun } from '../../hooks/useAgentRun';
import type { ResumeResolution } from '../../api/types';
import { canSubmit, isStreamActive } from '../../state/runTypes';
import { RuntimeOutcomeBadge } from '../runtime/RuntimeOutcomeBadge';
import { RuntimeTimeline } from '../runtime/RuntimeTimeline';
import { ToolExecutionCard } from '../runtime/ToolExecutionCard';
import { ApprovalPanel } from '../hitl/ApprovalPanel';
import { ClarificationPanel } from '../hitl/ClarificationPanel';
import { WaitingContextPanel } from '../hitl/WaitingContextPanel';
import { FailedRunPanel } from '../hitl/FailedRunPanel';
import { MessageList } from './MessageList';
import { Composer } from './Composer';

/** Top-level chat experience: streaming answer, runtime timeline, HITL panels. */
export function ChatShell({ baseUrl = '' }: { baseUrl?: string }) {
  const { state, submit, cancel, resume, reset } = useAgentRun(baseUrl);
  const onResolve = (resolution: ResumeResolution) => resume(resolution);

  return (
    <div className="chat-shell" data-testid="chat-shell" data-status={state.status}>
      <header className="chat-header">
        <div className="brand">
          <span className="brand-mark">◇</span> Runner.ai
        </div>
        <RuntimeOutcomeBadge status={state.status} />
      </header>

      <main className="chat-main">
        <MessageList state={state} />

        <ToolExecutionCard items={state.timeline} />
        <RuntimeTimeline items={state.timeline} />

        {state.status === 'waiting_for_user' ? (
          <ClarificationPanel pendingReason={state.pendingReason} resuming={state.resuming} onResolve={onResolve} />
        ) : null}
        {state.status === 'waiting_for_approval' ? (
          <ApprovalPanel pendingReason={state.pendingReason} resuming={state.resuming} onResolve={onResolve} />
        ) : null}
        {state.status === 'waiting_for_context' || state.status === 'waiting_for_replan' ? (
          <WaitingContextPanel status={state.status} pendingReason={state.pendingReason} />
        ) : null}
        {state.status === 'failed' ? (
          <FailedRunPanel
            error={state.error}
            canRetry={state.error?.retryable === true && state.request != null}
            onRetry={() => {
              if (state.request) submit(state.request);
            }}
          />
        ) : null}

        {state.resuming ? <p className="resuming-note" data-testid="resuming-note">Continuing the run…</p> : null}
      </main>

      <footer className="chat-footer">
        <Composer
          canSend={canSubmit(state.status)}
          isActive={isStreamActive(state.status)}
          onSend={submit}
          onCancel={cancel}
        />
        {state.status === 'completed' || state.status === 'cancelled' ? (
          <button type="button" className="btn btn-ghost btn-new" onClick={reset} data-testid="new-run-btn">
            New request
          </button>
        ) : null}
      </footer>
    </div>
  );
}
