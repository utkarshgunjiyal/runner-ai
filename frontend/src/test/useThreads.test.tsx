import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { useThreads } from '../hooks/useThreads';

afterEach(() => vi.unstubAllGlobals());

interface ThreadFixture {
  messages: Array<{ seq: number; role: string; content: string; created_at: string }>;
  documents: Array<{ document_id: string; filename: string; status: string; page_count: number; created_at: string }>;
}

/** Route fetch by URL to per-thread message/document fixtures. */
function mockThreadsBackend(fixtures: Record<string, ThreadFixture>) {
  const calls: string[] = [];
  const fn = vi.fn(async (url: string, _init?: RequestInit) => {
    calls.push(url);
    const messagesMatch = url.match(/\/threads\/([^/]+)\/messages/);
    if (messagesMatch) {
      const f = fixtures[messagesMatch[1]] ?? { messages: [], documents: [] };
      return json({ messages: f.messages });
    }
    const documentsMatch = url.match(/\/threads\/([^/]+)\/documents/);
    if (documentsMatch) {
      const f = fixtures[documentsMatch[1]] ?? { messages: [], documents: [] };
      return json({ documents: f.documents });
    }
    if (url.endsWith('/threads')) {
      return json({ thread_id: 'tNew', title: 'New', created_at: 'x', updated_at: 'x', message_count: 0 });
    }
    return json({});
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

const FIXTURES: Record<string, ThreadFixture> = {
  tA: {
    messages: [{ seq: 0, role: 'user', content: 'question A', created_at: '1' }],
    documents: [{ document_id: 'dA', filename: 'a.pdf', status: 'completed', page_count: 1, created_at: '1' }],
  },
  tB: {
    messages: [{ seq: 0, role: 'user', content: 'question B', created_at: '1' }],
    documents: [{ document_id: 'dB', filename: 'b.pdf', status: 'completed', page_count: 1, created_at: '1' }],
  },
};

describe('useThreads', () => {
  it('selecting a thread loads its messages and documents', async () => {
    mockThreadsBackend(FIXTURES);
    const { result } = renderHook(() => useThreads());

    await act(async () => {
      await result.current.selectThread('tA');
    });

    expect(result.current.activeThreadId).toBe('tA');
    expect(result.current.messages.map((m) => m.content)).toEqual(['question A']);
    expect(result.current.documents.map((d) => d.document_id)).toEqual(['dA']);
  });

  it('switching threads clears the previous thread data (no leakage)', async () => {
    mockThreadsBackend(FIXTURES);
    const { result } = renderHook(() => useThreads());

    await act(async () => {
      await result.current.selectThread('tA');
    });
    expect(result.current.messages[0].content).toBe('question A');

    await act(async () => {
      await result.current.selectThread('tB');
    });
    expect(result.current.activeThreadId).toBe('tB');
    expect(result.current.messages.map((m) => m.content)).toEqual(['question B']);
    expect(result.current.documents.map((d) => d.document_id)).toEqual(['dB']);
  });

  it('selecting null clears messages and documents', async () => {
    mockThreadsBackend(FIXTURES);
    const { result } = renderHook(() => useThreads());
    await act(async () => {
      await result.current.selectThread('tA');
    });
    await act(async () => {
      await result.current.selectThread(null);
    });
    expect(result.current.activeThreadId).toBeNull();
    expect(result.current.messages).toEqual([]);
    expect(result.current.documents).toEqual([]);
  });

  it('createThread adds the thread, activates it, and clears prior data', async () => {
    mockThreadsBackend(FIXTURES);
    const { result } = renderHook(() => useThreads());
    await act(async () => {
      await result.current.selectThread('tA');
    });
    await act(async () => {
      await result.current.createThread('New');
    });
    await waitFor(() => expect(result.current.activeThreadId).toBe('tNew'));
    expect(result.current.threads[0].thread_id).toBe('tNew');
    expect(result.current.messages).toEqual([]);
    expect(result.current.documents).toEqual([]);
  });
});
