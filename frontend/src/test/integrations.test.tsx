import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { IntegrationsPanel } from '../components/integrations/IntegrationsPanel';

afterEach(() => vi.unstubAllGlobals());

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

const CONNECTED = {
  github: {
    provider: 'github', status: 'connected', label: 'Connected', read_only: true,
    capabilities: ['List / search GitHub repositories', 'List GitHub issues'],
  },
  gmail: { provider: 'gmail', status: 'not_configured', label: 'Coming next' },
  mcp_runtime: { provider: 'mcp', status: 'connected', label: 'Connected' },
};

const NOT_CONFIGURED = {
  github: { provider: 'github', status: 'not_configured', label: 'Not configured', read_only: true, capabilities: [] },
  gmail: { provider: 'gmail', status: 'not_configured', label: 'Coming next' },
  mcp_runtime: { provider: 'mcp', status: 'available', label: 'Available' },
};

describe('IntegrationsPanel — live GitHub status (Phase 46.2)', () => {
  it('renders the live Connected state with read-only capabilities and no token input', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(CONNECTED)));
    render(<IntegrationsPanel />);

    await waitFor(() =>
      expect(screen.getByTestId('integration-status-github')).toHaveTextContent('Connected'),
    );
    const caps = screen.getByTestId('github-capabilities');
    expect(within(caps).getByText('List GitHub issues')).toBeInTheDocument();
    // Gmail stays truthful; MCP reflects runtime.
    expect(screen.getByTestId('integration-status-gmail')).toHaveTextContent('Coming next');
    expect(screen.getByTestId('integration-status-mcp')).toHaveTextContent('Connected');
    // No token input field anywhere.
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByPlaceholderText(/token/i)).not.toBeInTheDocument();
  });

  it('shows Not configured and never a false connected state', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json(NOT_CONFIGURED)));
    render(<IntegrationsPanel />);
    await waitFor(() =>
      expect(screen.getByTestId('integration-status-github')).toHaveTextContent('Not configured'),
    );
    expect(screen.queryByTestId('github-capabilities')).not.toBeInTheDocument();
  });

  it('renders auth-failed / unavailable states truthfully', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      json({ ...NOT_CONFIGURED, github: { provider: 'github', status: 'auth_failed', label: 'Authentication failed' } }),
    ));
    render(<IntegrationsPanel />);
    await waitFor(() =>
      expect(screen.getByTestId('integration-status-github')).toHaveTextContent('Authentication failed'),
    );
  });

  it('degrades to a safe fallback (no false connection) when the API fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('boom', { status: 500 })));
    render(<IntegrationsPanel />);
    await waitFor(() =>
      expect(screen.getByTestId('integration-status-github')).toHaveTextContent('Not configured'),
    );
  });

  it('Refresh re-fetches the status', async () => {
    let call = 0;
    const fetchMock = vi.fn(async () => {
      call += 1;
      return json(call >= 2 ? CONNECTED : NOT_CONFIGURED);
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<IntegrationsPanel />);
    await waitFor(() =>
      expect(screen.getByTestId('integration-status-github')).toHaveTextContent('Not configured'),
    );
    fireEvent.click(screen.getByTestId('integrations-refresh'));
    await waitFor(() =>
      expect(screen.getByTestId('integration-status-github')).toHaveTextContent('Connected'),
    );
  });
});
