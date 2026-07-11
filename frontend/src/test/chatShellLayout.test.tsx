import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ChatShell } from '../components/chat/ChatShell';

afterEach(() => vi.unstubAllGlobals());

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function stubBackend() {
  const fetchMock = vi.fn(async (url: string) => {
    if (url.endsWith('/threads')) {
      return json({ threads: [{ thread_id: 'tA', title: 'Alpha', updated_at: '2026-07-11T11:00:00Z', message_count: 2 }] });
    }
    if (/\/threads\/[^/]+\/messages/.test(url)) return json({ messages: [] });
    if (/\/threads\/[^/]+\/documents/.test(url)) return json({ documents: [] });
    return json({});
  });
  vi.stubGlobal('fetch', fetchMock);
}

describe('ChatShell workspace layout (Phase 45)', () => {
  it('renders the three-region shell with a truthful integrations panel', async () => {
    stubBackend();
    render(<ChatShell />);
    expect(screen.getByTestId('thread-sidebar')).toBeInTheDocument();
    expect(screen.getByTestId('chat-shell')).toBeInTheDocument();
    expect(screen.getByTestId('integrations-panel')).toBeInTheDocument();
    // Runtime inspector is collapsed (not mounted) by default.
    expect(screen.queryByTestId('runtime-inspector')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('Alpha')).toBeInTheDocument());
  });

  it('opens and closes the runtime inspector from the header', async () => {
    stubBackend();
    render(<ChatShell />);
    expect(screen.queryByTestId('runtime-inspector')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('inspector-toggle'));
    expect(await screen.findByTestId('runtime-inspector')).toBeInTheDocument();
    // Empty until a run produces activity.
    expect(screen.getByTestId('inspector-empty')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('inspector-close'));
    await waitFor(() => expect(screen.queryByTestId('runtime-inspector')).not.toBeInTheDocument());
  });

  it('toggles the mobile sidebar drawer state', () => {
    stubBackend();
    render(<ChatShell />);
    const layout = screen.getByTestId('app-layout');
    expect(layout).toHaveAttribute('data-sidebar-open', 'false');
    fireEvent.click(screen.getByTestId('sidebar-toggle'));
    expect(layout).toHaveAttribute('data-sidebar-open', 'true');
    // The scrim closes the drawer.
    fireEvent.click(screen.getByTestId('scrim'));
    expect(layout).toHaveAttribute('data-sidebar-open', 'false');
  });

  it('shows the active thread title in the header after selection', async () => {
    stubBackend();
    render(<ChatShell />);
    fireEvent.click(await screen.findByText('Alpha'));
    await waitFor(() => expect(screen.getByTestId('chat-title')).toHaveTextContent('Alpha'));
  });
});
