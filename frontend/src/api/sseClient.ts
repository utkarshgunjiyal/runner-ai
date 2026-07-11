// POST SSE client (Phase 41B).
//
// The browser EventSource API cannot POST a body, so we POST via fetch, read the
// ReadableStream, and parse SSE frames manually. Handles partial network chunks,
// multiple frames per chunk, event ordering, malformed JSON (skipped safely),
// abort/cancel, and 401. Does NOT buffer the whole answer before delivering it —
// each frame is dispatched as soon as it is parsed.

import type { ExplicitContextMode, RuntimeEvent } from './types';

export class UnauthorizedError extends Error {
  constructor() {
    super('unauthorized');
    this.name = 'UnauthorizedError';
  }
}

export interface StreamCallbacks {
  onEvent: (event: RuntimeEvent) => void;
  onError?: (error: unknown) => void;
  onDone?: () => void;
}

export interface StreamRequestBody {
  user_request: string;
  thread_id?: string | null;
  metadata?: Record<string, unknown> | null;
  selected_document_ids?: string[];
  selected_page_numbers?: number[];
  explicit_context_mode?: ExplicitContextMode;
}

/** Parse a single SSE frame block ("event: x\ndata: {...}") into a RuntimeEvent. */
export function parseSseFrame(block: string): RuntimeEvent | null {
  let eventType: string | null = null;
  const dataLines: string[] = [];
  for (const rawLine of block.split('\n')) {
    const line = rawLine.replace(/\r$/, '');
    if (line.startsWith('event:')) {
      eventType = line.slice('event:'.length).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).replace(/^ /, ''));
    }
    // ':' comment lines and blank lines are ignored.
  }
  if (!eventType || dataLines.length === 0) return null;
  try {
    const parsed = JSON.parse(dataLines.join('\n')) as RuntimeEvent;
    if (!parsed || typeof parsed.type !== 'string') return null;
    return parsed;
  } catch {
    // Malformed JSON — skip this frame safely rather than crashing the stream.
    return null;
  }
}

/**
 * A stateful SSE frame parser. Feed it raw text chunks; it emits complete
 * RuntimeEvents in order, correctly buffering frames split across chunks and
 * splitting multiple frames arriving in one chunk.
 */
export class SseParser {
  private buffer = '';

  push(chunk: string): RuntimeEvent[] {
    this.buffer += chunk;
    const events: RuntimeEvent[] = [];
    let sepIndex: number;
    // Frames are separated by a blank line (\n\n). Emit each complete block.
    while ((sepIndex = this.buffer.indexOf('\n\n')) !== -1) {
      const block = this.buffer.slice(0, sepIndex);
      this.buffer = this.buffer.slice(sepIndex + 2);
      const event = parseSseFrame(block);
      if (event) events.push(event);
    }
    return events;
  }

  /** Flush any trailing frame not terminated by a blank line (stream end). */
  flush(): RuntimeEvent[] {
    const remaining = this.buffer.trim();
    this.buffer = '';
    if (!remaining) return [];
    const event = parseSseFrame(remaining);
    return event ? [event] : [];
  }
}

/**
 * Stream POST /agent/run/stream. Returns when the stream ends (or is aborted).
 * The caller supplies an AbortSignal to cancel (unmount / new request).
 */
export async function streamAgentRun(
  body: StreamRequestBody,
  callbacks: StreamCallbacks,
  signal: AbortSignal,
  baseUrl = '',
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl}/agent/run/stream`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
      body: JSON.stringify(body),
      credentials: 'include', // auth cookies
      signal,
    });
  } catch (error) {
    if (isAbort(error)) return;
    callbacks.onError?.(error);
    return;
  }

  if (response.status === 401) {
    callbacks.onError?.(new UnauthorizedError());
    return;
  }
  if (!response.ok || !response.body) {
    callbacks.onError?.(new Error(`stream failed: HTTP ${response.status}`));
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const parser = new SseParser();

  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = decoder.decode(value, { stream: true });
      for (const event of parser.push(chunk)) callbacks.onEvent(event);
    }
    for (const event of parser.flush()) callbacks.onEvent(event);
    callbacks.onDone?.();
  } catch (error) {
    if (isAbort(error)) return;
    callbacks.onError?.(error);
  } finally {
    reader.releaseLock?.();
  }
}

function isAbort(error: unknown): boolean {
  return (
    !!error &&
    typeof error === 'object' &&
    'name' in error &&
    (error as { name: string }).name === 'AbortError'
  );
}
