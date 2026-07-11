// Typed fetch clients for the thread endpoints (Phase 42B).
// All requests include auth cookies; a 401 throws UnauthorizedError.

import { UnauthorizedError } from './sseClient';
import type { ThreadDocument, ThreadMessage, ThreadSummary } from './types';

async function readJson<T>(response: Response, what: string): Promise<T> {
  if (response.status === 401) throw new UnauthorizedError();
  if (!response.ok) throw new Error(`${what} failed: HTTP ${response.status}`);
  return (await response.json()) as T;
}

/** GET /threads — the caller's conversation list. */
export async function listThreads(baseUrl = ''): Promise<ThreadSummary[]> {
  const response = await fetch(`${baseUrl}/threads`, { credentials: 'include' });
  const data = await readJson<{ threads: ThreadSummary[] }>(response, 'list threads');
  return data.threads ?? [];
}

/** POST /threads — create a new conversation. */
export async function createThread(title?: string, baseUrl = ''): Promise<ThreadSummary> {
  const response = await fetch(`${baseUrl}/threads`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(title != null ? { title } : {}),
    credentials: 'include',
  });
  return readJson<ThreadSummary>(response, 'create thread');
}

/** GET /threads/{id}/messages — the persisted history for a thread. */
export async function loadMessages(
  threadId: string,
  limit?: number,
  baseUrl = '',
): Promise<ThreadMessage[]> {
  const query = limit != null ? `?limit=${encodeURIComponent(limit)}` : '';
  const response = await fetch(
    `${baseUrl}/threads/${encodeURIComponent(threadId)}/messages${query}`,
    { credentials: 'include' },
  );
  const data = await readJson<{ messages: ThreadMessage[] }>(response, 'load messages');
  return data.messages ?? [];
}

/** GET /threads/{id}/documents — the documents attached to a thread. */
export async function loadThreadDocuments(
  threadId: string,
  baseUrl = '',
): Promise<ThreadDocument[]> {
  const response = await fetch(
    `${baseUrl}/threads/${encodeURIComponent(threadId)}/documents`,
    { credentials: 'include' },
  );
  const data = await readJson<{ documents: ThreadDocument[] }>(response, 'load documents');
  return data.documents ?? [];
}
