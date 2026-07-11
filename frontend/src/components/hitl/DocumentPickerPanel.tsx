import { useState } from 'react';
import type { DocumentCandidate } from '../../api/types';

/**
 * Rendered when a run pauses to disambiguate which document(s) to use
 * (pendingAction === "select_document"). Presents the candidates as selectable
 * options and resumes with the chosen document ids as a clarification. Keyboard
 * accessible (native checkboxes + button); disabled while resuming.
 */
export function DocumentPickerPanel({
  candidates,
  resuming,
  onConfirm,
}: {
  candidates: DocumentCandidate[];
  resuming: boolean;
  onConfirm: (documentIds: string[]) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]);

  const toggle = (id: string) => {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const confirm = () => {
    if (resuming || selected.length === 0) return;
    onConfirm(selected);
  };

  return (
    <div className="hitl doc-picker" data-testid="document-picker-panel">
      <h3 className="hitl-title">Which document should the agent use?</h3>
      <p className="hitl-reason">
        This request could match more than one document. Choose one or more to continue.
      </p>
      <ul className="doc-picker-list">
        {candidates.map((candidate) => (
          <li key={candidate.document_id}>
            <label className="doc-picker-option">
              <input
                type="checkbox"
                checked={selected.includes(candidate.document_id)}
                onChange={() => toggle(candidate.document_id)}
                disabled={resuming}
                aria-label={candidate.filename}
                data-testid={`doc-picker-option-${candidate.document_id}`}
              />
              <span className="doc-name">{candidate.filename}</span>
            </label>
          </li>
        ))}
      </ul>
      <div className="hitl-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={confirm}
          disabled={resuming || selected.length === 0}
          data-testid="doc-picker-confirm"
        >
          {resuming ? 'Continuing…' : 'Confirm'}
        </button>
        <span className="hitl-selected-count" data-testid="doc-picker-count">
          {selected.length} selected
        </span>
      </div>
    </div>
  );
}
