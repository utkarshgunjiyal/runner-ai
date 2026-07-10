// Test helpers: build a mock fetch Response whose body is a ReadableStream that
// emits caller-controlled text chunks — so tests exercise the real SSE parser
// with partial frames, multiple frames per chunk, and mid-stream cancellation.
// No live backend.

import { vi } from 'vitest';
import type { RuntimeEvent } from '../api/types';

const encoder = new TextEncoder();

/** Serialize a RuntimeEvent to an SSE frame block. */
export function frame(event: Partial<RuntimeEvent> & { type: string; sequence: number }): string {
  const payload = { run_id: null, data: {}, ...event };
  return `event: ${event.type}\ndata: ${JSON.stringify(payload)}\n\n`;
}

/** A Response backed by a ReadableStream that yields the given text chunks. */
export function streamResponse(chunks: string[], init?: { status?: number }): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, {
    status: init?.status ?? 200,
    headers: { 'content-type': 'text/event-stream' },
  });
}

function abortError(): Error {
  const err = new Error('aborted');
  err.name = 'AbortError';
  return err;
}

/** Install a fetch mock that returns the given response and records the request.
 *  Honors an already-aborted signal by throwing AbortError (like real fetch). */
export function mockFetchStream(response: Response) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fn = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    if (init.signal?.aborted) throw abortError();
    return response;
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

/** Install a fetch mock that returns a JSON response (for /agent/resume). */
export function mockFetchJson(status: number, body: unknown) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fn = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}
