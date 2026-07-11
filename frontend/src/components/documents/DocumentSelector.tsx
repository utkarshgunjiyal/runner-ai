import { useEffect, useRef, type ChangeEvent } from 'react';
import { getDocumentStatus } from '../../api/documentsClient';
import { useUpload } from '../../hooks/useUpload';
import type { DocumentStatusResult, DocumentUploadResult, ThreadDocument } from '../../api/types';

/**
 * Lists the active thread's documents with checkboxes (selected ids feed the run
 * scope) and an upload input. The upload flow is a small state machine (see
 * useUpload): the filename stays visible while uploading, failures show a safe
 * inline retry message, the file clears only on success, and indexing status is
 * polled in the background. Functional, not polished.
 */
export function DocumentSelector({
  documents,
  selectedIds,
  onToggle,
  onUpload,
  disabled = false,
  activeThreadId = null,
  onRefreshDocuments,
  pollStatus,
  baseUrl = '',
}: {
  documents: ThreadDocument[];
  selectedIds: string[];
  onToggle: (documentId: string) => void;
  onUpload: (file: File) => Promise<DocumentUploadResult | void> | void;
  disabled?: boolean;
  activeThreadId?: string | null;
  onRefreshDocuments?: () => void | Promise<void>;
  pollStatus?: (documentId: string) => Promise<DocumentStatusResult>;
  baseUrl?: string;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const upload = useUpload({
    onUpload,
    onRefreshDocuments,
    pollStatus: pollStatus ?? ((id: string) => getDocumentStatus(id, baseUrl)),
    activeThreadId,
  });

  // Clear the native <input> only after the selection is cleared (success/reset),
  // never immediately — so the chosen file stays visible during the upload.
  useEffect(() => {
    if (upload.filename === null && inputRef.current) inputRef.current.value = '';
  }, [upload.filename]);

  const handleFile = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    upload.selectFile(file);
  };

  return (
    <div className="doc-selector" data-testid="doc-selector">
      <div className="doc-selector-head">
        <span className="doc-selector-title">Documents</span>
        <label className="doc-upload">
          <input
            ref={inputRef}
            type="file"
            className="doc-upload-input"
            onChange={handleFile}
            disabled={disabled || upload.busy}
            data-testid="doc-upload-input"
          />
          {upload.busy ? 'Uploading…' : 'Upload'}
        </label>
      </div>
      {upload.filename ? (
        <p className="doc-upload-status" data-testid="doc-upload-filename">
          {upload.filename}
        </p>
      ) : null}
      {upload.error ? (
        <div className="doc-upload-error" data-testid="doc-upload-error">
          <span>{upload.error}</span>
          <button
            type="button"
            className="btn btn-ghost doc-upload-retry"
            onClick={upload.retry}
            data-testid="doc-upload-retry"
          >
            Retry
          </button>
        </div>
      ) : null}
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
