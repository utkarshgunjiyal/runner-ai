import type { SafeError } from '../../state/runTypes';

/** Safe failure presentation + retry (retry re-submits the original request). */
export function FailedRunPanel({
  error,
  canRetry,
  onRetry,
}: {
  error: SafeError | null;
  canRetry: boolean;
  onRetry: () => void;
}) {
  const sessionExpired = error?.sessionExpired === true;
  return (
    <div className="hitl hitl-failed" data-testid="failed-panel">
      <h3 className="hitl-title">{sessionExpired ? 'Session expired' : 'The run did not complete'}</h3>
      <p className="hitl-reason">{error?.message ?? 'Something went wrong. Please try again.'}</p>
      {error?.code ? <p className="hitl-code">Reference: {error.code}</p> : null}
      {sessionExpired ? (
        <p className="hitl-note">Please sign in again to continue.</p>
      ) : canRetry ? (
        <div className="hitl-actions">
          <button type="button" className="btn btn-primary" onClick={onRetry} data-testid="retry-btn">
            Retry
          </button>
        </div>
      ) : null}
    </div>
  );
}
