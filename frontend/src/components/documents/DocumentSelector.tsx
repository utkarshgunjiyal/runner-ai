import { useState, type ChangeEvent } from 'react';
import type { ThreadDocument } from '../../api/types';

/**
 * Lists the active thread's documents with checkboxes (selected ids feed the run
 * scope) and an upload input. Non-completed documents show their indexing status
 * and cannot be selected until ready. Functional, not polished.
 */
export function DocumentSelector({
  documents,
  selectedIds,
  onToggle,
  onUpload,
  disabled = false,
}: {
  documents: ThreadDocument[];
  selectedIds: string[];
  onToggle: (documentId: string) => void;
  onUpload: (file: File) => Promise<void> | void;
  disabled?: boolean;
}) {
  const [uploading, setUploading] = useState(false);

  const handleFile = async (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ''; // allow re-uploading the same file
    if (!file) return;
    setUploading(true);
    try {
      await onUpload(file);
    } finally {
      setUploading(false);
    }
  };

  return (
    <div className="doc-selector" data-testid="doc-selector">
      <div className="doc-selector-head">
        <span className="doc-selector-title">Documents</span>
        <label className="doc-upload">
          <input
            type="file"
            className="doc-upload-input"
            onChange={handleFile}
            disabled={disabled || uploading}
            data-testid="doc-upload-input"
          />
          {uploading ? 'Uploading…' : 'Upload'}
        </label>
      </div>
      {documents.length === 0 ? (
        <p className="doc-empty">No documents in this conversation.</p>
      ) : (
        <div className="doc-list">
          {documents.map((doc) => {
            const ready = doc.status === 'completed';
            return (
              <label key={doc.document_id} className="doc-chip" data-testid="doc-chip">
                <input
                  type="checkbox"
                  checked={selectedIds.includes(doc.document_id)}
                  onChange={() => onToggle(doc.document_id)}
                  disabled={!ready}
                  data-testid={`doc-checkbox-${doc.document_id}`}
                />
                <span className="doc-name">{doc.filename}</span>
                {!ready ? <span className="doc-status">{doc.status}</span> : null}
              </label>
            );
          })}
        </div>
      )}
    </div>
  );
}
