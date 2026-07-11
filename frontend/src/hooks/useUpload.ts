// useUpload (Phase 42C — Defect 8): a small upload state machine that keeps the
// selected file visible while uploading, surfaces a SAFE inline error with a
// retry path on failure, clears the file only after a successful upload, and
// then polls the document's indexing status on a bounded interval (refreshing
// the thread's documents on each poll). Polling is cancelled on thread switch
// and on unmount, and poll failures degrade to a stopped state — never throw.

import { useCallback, useEffect, useRef, useState } from 'react';
import { DocumentRequestError } from '../api/documentsClient';
import type { DocumentStatusResult, DocumentUploadResult } from '../api/types';

export type UploadPhase = 'idle' | 'uploading' | 'error';

/** The only user-facing failure text — never raw backend detail. */
export const UPLOAD_ERROR_MESSAGE = 'Upload failed. Please try again.';

const DEFAULT_POLL_INTERVAL_MS = 2000;
const DEFAULT_POLL_MAX_MS = 60000;

export interface UseUploadParams {
  /** Performs the actual upload; returns the 202 result (with document_id) or void. */
  onUpload: (file: File) => Promise<DocumentUploadResult | void> | void;
  /** Refreshes the active thread's documents (called on each poll + terminal). */
  onRefreshDocuments?: () => void | Promise<void>;
  /** Fetches a document's indexing status. When omitted, polling is skipped. */
  pollStatus?: (documentId: string) => Promise<DocumentStatusResult>;
  /** Active thread id — a change cancels any in-flight polling. */
  activeThreadId?: string | null;
  pollIntervalMs?: number;
  pollMaxMs?: number;
}

export interface UseUploadState {
  phase: UploadPhase;
  /** The selected filename, kept visible while uploading and while showing an error. */
  filename: string | null;
  /** Safe, display-ready error message (or null). */
  error: string | null;
  /** True while an upload request is in flight (disable the control). */
  busy: boolean;
  selectFile: (file: File) => void;
  retry: () => void;
  reset: () => void;
}

export function useUpload({
  onUpload,
  onRefreshDocuments,
  pollStatus,
  activeThreadId,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  pollMaxMs = DEFAULT_POLL_MAX_MS,
}: UseUploadParams): UseUploadState {
  const [phase, setPhase] = useState<UploadPhase>('idle');
  const [filename, setFilename] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // The file kept selected so a failed upload can be retried.
  const pendingFileRef = useRef<File | null>(null);
  // Polling bookkeeping.
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollActiveRef = useRef(false);
  // Synchronous guard against duplicate in-flight uploads (survives batching).
  const uploadingRef = useRef(false);

  const stopPolling = useCallback(() => {
    pollActiveRef.current = false;
    if (pollTimerRef.current != null) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      await onRefreshDocuments?.();
    } catch {
      /* a refresh failure is non-fatal — keep the current document list */
    }
  }, [onRefreshDocuments]);

  const startPolling = useCallback(
    (documentId: string) => {
      if (!pollStatus) {
        // No poller wired up: refresh once so a just-uploaded doc shows up.
        void refresh();
        return;
      }
      stopPolling();
      pollActiveRef.current = true;
      const startedAt = Date.now();

      const tick = async () => {
        if (!pollActiveRef.current) return;
        let result: DocumentStatusResult;
        try {
          result = await pollStatus(documentId);
        } catch {
          // Degrade to a stopped state; never throw an unhandled rejection.
          stopPolling();
          await refresh();
          return;
        }
        if (!pollActiveRef.current) return;
        await refresh();
        if (!pollActiveRef.current) return;
        const terminal = result.status === 'completed' || result.status === 'failed';
        if (terminal || Date.now() - startedAt >= pollMaxMs) {
          stopPolling();
          return;
        }
        pollTimerRef.current = setTimeout(() => void tick(), pollIntervalMs);
      };

      pollTimerRef.current = setTimeout(() => void tick(), pollIntervalMs);
    },
    [pollStatus, refresh, stopPolling, pollIntervalMs, pollMaxMs],
  );

  const runUpload = useCallback(
    async (file: File) => {
      if (uploadingRef.current) return; // prevent duplicate submits
      uploadingRef.current = true;
      stopPolling();
      pendingFileRef.current = file;
      setFilename(file.name);
      setError(null);
      setPhase('uploading');
      try {
        const result = await onUpload(file);
        // Success: clear the selected file, then poll for indexing status.
        pendingFileRef.current = null;
        setFilename(null);
        setError(null);
        setPhase('idle');
        const documentId = result ? result.document_id : undefined;
        if (documentId) {
          startPolling(documentId);
        } else {
          void refresh();
        }
      } catch (err) {
        // Keep the file selected + show a safe, retryable error (optionally a ref id).
        const requestId = err instanceof DocumentRequestError ? err.requestId : undefined;
        setError(requestId ? `${UPLOAD_ERROR_MESSAGE} (ref: ${requestId})` : UPLOAD_ERROR_MESSAGE);
        setPhase('error');
      } finally {
        uploadingRef.current = false;
      }
    },
    [onUpload, startPolling, stopPolling, refresh],
  );

  const selectFile = useCallback(
    (file: File) => {
      void runUpload(file);
    },
    [runUpload],
  );

  const retry = useCallback(() => {
    const file = pendingFileRef.current;
    if (file) void runUpload(file);
  }, [runUpload]);

  const reset = useCallback(() => {
    stopPolling();
    pendingFileRef.current = null;
    setFilename(null);
    setError(null);
    setPhase('idle');
  }, [stopPolling]);

  // Cancel polling on thread switch and on unmount.
  useEffect(() => {
    return () => stopPolling();
  }, [activeThreadId, stopPolling]);

  return {
    phase,
    filename,
    error,
    busy: phase === 'uploading',
    selectFile,
    retry,
    reset,
  };
}
