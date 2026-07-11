import { afterEach, describe, expect, it, vi } from 'vitest';
import { createThread, listThreads, loadMessages, loadThreadDocuments } from '../api/threadsClient';
import { UnauthorizedError } from '../api/sseClient';

afterEach(() => vi.unstubAllGlobals());

function mockJson(status: number, body: unknown) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const fn = vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init: init ?? {} });
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fn);
  return { fn, calls };
}

describe('threadsClient', () => {
  it('listThreads parses the threads array and sends credentials', async () => {
    const { calls } = mockJson(200, {
      threads: [
        { thread_id: 't1', title: 'First', updated_at: '2026-01-01', message_count: 3 },
        { thread_id: 't2', title: 'Second', updated_at: '2026-01-02', message_count: 0 },
      ],
    });
    const threads = await listThreads();
    expect(threads).toHaveLength(2);
    expect(threads[0].thread_id).toBe('t1');
    expect(calls[0].url).toContain('/threads');
    expect(calls[0].init.credentials).toBe('include');
  });

  it('createThread posts the title and parses the created thread', async () => {
    const { calls } = mockJson(200, {
      thread_id: 't9',
      title: 'New chat',
      created_at: '2026-07-11',
      updated_at: '2026-07-11',
      message_count: 0,
    });
    const thread = await createThread('New chat');
    expect(thread.thread_id).toBe('t9');
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.credentials).toBe('include');
    expect(JSON.parse(calls[0].init.body as string)).toEqual({ title: 'New chat' });
  });

  it('loadMessages includes a limit query and parses messages', async () => {
    const { calls } = mockJson(200, {
      messages: [{ seq: 0, role: 'user', content: 'hi', created_at: '2026-01-01' }],
    });
    const messages = await loadMessages('t1', 50);
    expect(messages[0].content).toBe('hi');
    expect(calls[0].url).toContain('/threads/t1/messages?limit=50');
  });

  it('loadThreadDocuments parses documents', async () => {
    mockJson(200, {
      documents: [
        { document_id: 'd1', filename: 'a.pdf', status: 'completed', page_count: 2, created_at: '2026-01-01' },
      ],
    });
    const docs = await loadThreadDocuments('t1');
    expect(docs[0].document_id).toBe('d1');
  });

  it('throws UnauthorizedError on a 401', async () => {
    mockJson(401, {});
    await expect(listThreads()).rejects.toBeInstanceOf(UnauthorizedError);
  });
});
