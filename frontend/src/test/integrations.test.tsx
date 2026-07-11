import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { IntegrationsPanel } from '../components/integrations/IntegrationsPanel';

describe('IntegrationsPanel — truthful, no fake live connectors', () => {
  it('shows GitHub and Gmail as not connected / coming next', () => {
    render(<IntegrationsPanel />);
    const github = screen.getByTestId('integration-github');
    const gmail = screen.getByTestId('integration-gmail');
    expect(within(github).getByTestId('integration-status-github')).toHaveTextContent('Coming next');
    expect(within(github).getByText(/not connected/i)).toBeInTheDocument();
    expect(within(gmail).getByTestId('integration-status-gmail')).toHaveTextContent('Coming next');
    expect(within(gmail).getByText(/not connected/i)).toBeInTheDocument();
  });

  it('shows the MCP runtime as available infrastructure', () => {
    render(<IntegrationsPanel />);
    const mcp = screen.getByTestId('integration-mcp');
    expect(within(mcp).getByTestId('integration-status-mcp')).toHaveTextContent('Available');
  });

  it('offers no connect action that could imply a live connection', () => {
    render(<IntegrationsPanel />);
    const panel = screen.getByTestId('integrations-panel');
    // No buttons/links at all — the panel is purely informational.
    expect(within(panel).queryAllByRole('button')).toHaveLength(0);
    expect(within(panel).queryAllByRole('link')).toHaveLength(0);
    expect(within(panel).queryByText(/^connect$/i)).not.toBeInTheDocument();
    // No status pill ever reads a positive "Connected" state.
    for (const key of ['github', 'gmail', 'mcp']) {
      expect(screen.getByTestId(`integration-status-${key}`)).not.toHaveTextContent(/^connected$/i);
    }
  });
});
