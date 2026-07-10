import { useState } from 'react';
import type { ResumeResolution } from '../../api/types';

/** WAITING_FOR_USER: collect a clarification and resume. */
export function ClarificationPanel({
  pendingReason,
  resuming,
  onResolve,
}: {
  pendingReason: string | null;
  resuming: boolean;
  onResolve: (resolution: ResumeResolution) => void;
}) {
  const [text, setText] = useState('');
  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || resuming) return;
    onResolve({ kind: 'clarification', value: trimmed });
  };

  return (
    <div className="hitl hitl-user" data-testid="clarification-panel">
      <h3 className="hitl-title">The agent needs a clarification</h3>
      {pendingReason ? <p className="hitl-reason">{pendingReason}</p> : null}
      <textarea
        className="hitl-input"
        rows={2}
        placeholder="Add the missing detail…"
        value={text}
        onChange={(e) => setText(e.target.value)}
        aria-label="Clarification"
        disabled={resuming}
      />
      <div className="hitl-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={submit}
          disabled={resuming || !text.trim()}
          data-testid="clarify-continue"
        >
          {resuming ? 'Continuing…' : 'Continue'}
        </button>
      </div>
    </div>
  );
}
