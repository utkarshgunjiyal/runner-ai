import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ChatShell } from '../components/chat/ChatShell';

afterEach(() => vi.unstubAllGlobals());

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('ChatShell — new conversation (Defect 9)', () => {
  it('creates a thread immediately, activates it, and clears selected documents', async () => {
    const calls: Array<{ url: string; method?: string }> = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, method: init?.method });
      if (url.endsWith('/threads') && init?.method === 'POST') {
        return json({ thread_id: 'tNew', title: 'New', created_at: 'x', updated_at: 'x', message_count: 0 });
      }
      if (url.endsWith('/threads')) {
        return json({ threads: [{ thread_id: 'tA', title: 'Alpha', updated_at: '1', message_count: 1 }] });
      }
      if (/\/threads\/[^/]+\/messages/.test(url)) return json({ messages: [] });
      const docMatch = url.match(/\/threads\/([^/]+)\/documents/);
      if (docMatch) {
        return docMatch[1] === 'tA'
          ? json({
              documents: [
                { document_id: 'dA', filename: 'a.pdf', status: 'completed', page_count: 1, created_at: '1' },
              ],
            })
          : json({ documents: [] });
      }
      return json({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ChatShell />);

    // Select the existing thread so a completed document is present + selectable.
    fireEvent.click(await screen.findByText('Alpha'));
    fireEvent.click(await screen.findByTestId('doc-checkbox-dA'));
    expect(screen.getByTestId('doc-checkbox-dA')).toBeChecked();

    // New conversation → POST /threads.
    fireEvent.click(screen.getByTestId('new-thread-btn'));

    await waitFor(() =>
      expect(calls.some((c) => c.url.endsWith('/threads') && c.method === 'POST')).toBe(true),
    );
    // The created thread is active and the previously-selected doc is cleared.
    await waitFor(() => expect(screen.queryByTestId('doc-checkbox-dA')).not.toBeInTheDocument());
    await waitFor(() => {
      const active = screen
        .getAllByTestId('thread-item')
        .find((item) => item.getAttribute('aria-current') === 'true');
      expect(active).toHaveTextContent('New');
    });
  });

  it('surfaces a safe error when thread creation fails (no silent reset)', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/threads') && init?.method === 'POST') return new Response('boom', { status: 500 });
      if (url.endsWith('/threads')) return json({ threads: [] });
      return json({});
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<ChatShell />);
    fireEvent.click(screen.getByTestId('new-thread-btn'));

    expect(await screen.findByTestId('thread-error')).toBeInTheDocument();
  });
});
