import { useState } from 'react';
import type { ResumeResolution } from '../../api/types';

/** WAITING_FOR_APPROVAL: approve or reject, with an optional reason. */
export function ApprovalPanel({
  pendingReason,
  resuming,
  onResolve,
}: {
  pendingReason: string | null;
  resuming: boolean;
  onResolve: (resolution: ResumeResolution) => void;
}) {
  const [reason, setReason] = useState('');

  const approve = () => {
    if (resuming) return;
    onResolve({ kind: 'approval', value: true, reason: reason.trim() || undefined });
  };
  const reject = () => {
    if (resuming) return;
    onResolve({ kind: 'rejection', value: false, reason: reason.trim() || undefined });
  };

  return (
    <div className="hitl hitl-approval" data-testid="approval-panel">
      <h3 className="hitl-title">Approval required</h3>
      {pendingReason ? <p className="hitl-reason">{pendingReason}</p> : null}
      <textarea
        className="hitl-input"
        rows={2}
        placeholder="Optional note…"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        aria-label="Reason"
        disabled={resuming}
      />
      <div className="hitl-actions">
        <button type="button" className="btn btn-primary" onClick={approve} disabled={resuming} data-testid="approve-btn">
          {resuming ? 'Submitting…' : 'Approve'}
        </button>
        <button type="button" className="btn btn-danger" onClick={reject} disabled={resuming} data-testid="reject-btn">
          Reject
        </button>
      </div>
    </div>
  );
}
