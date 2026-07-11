// Typed fetch clients for the document endpoints (Phase 42B).
// Uploads are multipart; status is polled until status === "completed".
// All requests include auth cookies; a 401 throws UnauthorizedError.

import { UnauthorizedError } from './sseClient';
import type { DocumentStatusResult, DocumentUploadResult } from './types';

/**
 * A failed document request. Carries an optional server request id (from the
 * `X-Request-ID` header) so the UI can surface a safe correlation ref without
 * ever exposing raw backend error text.
 */
export class DocumentRequestError extends Error {
  readonly requestId?: string;
  constructor(message: string, requestId?: string) {
    super(message);
    this.name = 'DocumentRequestError';
    this.requestId = requestId;
  }
}

/** POST /documents/upload — multipart upload attached to a thread (202). */
export async function uploadDocument(
  file: File,
  threadId: string,
  baseUrl = '',
): Promise<DocumentUploadResult> {
  const form = new FormData();
  form.append('file', file);
  form.append('thread_id', threadId);
  const response = await fetch(`${baseUrl}/documents/upload`, {
    method: 'POST',
    body: form,
    credentials: 'include',
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) {
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    throw new DocumentRequestError(`upload failed: HTTP ${response.status}`, requestId);
  }
  return (await response.json()) as DocumentUploadResult;
}

/** GET /documents/{id} — poll for indexing status (status === "completed"). */
export async function getDocumentStatus(
  documentId: string,
  baseUrl = '',
): Promise<DocumentStatusResult> {
  const response = await fetch(`${baseUrl}/documents/${encodeURIComponent(documentId)}`, {
    credentials: 'include',
  });
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) {
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    throw new DocumentRequestError(`document status failed: HTTP ${response.status}`, requestId);
  }
  return (await response.json()) as DocumentStatusResult;
}
