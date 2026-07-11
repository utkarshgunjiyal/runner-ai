import { useCallback, useEffect, useState } from 'react';
import {
  fetchIntegrations,
  refreshIntegrations,
  FALLBACK_INTEGRATIONS,
  type IntegrationsView,
} from '../../api/integrationsClient';

// IntegrationsPanel (Phase 46.2): shows the REAL, live integration status.
//
// GitHub reflects the deployment's actual MCP connector state (Not configured /
// Connecting / Connected / Degraded / Authentication failed / Unavailable) with
// its enabled read-only capabilities and a safe Refresh. Gmail stays truthfully
// "Coming next" (not implemented). The MCP runtime reflects runtime availability.
//
// No token input is ever shown — this phase configures GitHub at the deployment
// level only. It never claims per-user OAuth or a false connection; on any fetch
// failure it degrades to a safe fallback view.

const ICONS: Record<string, string> = { github: '⌥', gmail: '✉', mcp: '❖' };

// Map a status string to a pill tone (ready / warn / neutral).
function tone(status: string): 'ready' | 'soon' | 'error' | 'neutral' {
  switch (status) {
    case 'connected':
      return 'ready';
    case 'connecting':
    case 'not_configured':
      return 'soon';
    case 'auth_failed':
    case 'unavailable':
    case 'degraded':
      return 'error';
    default:
      return 'neutral';
  }
}

const GITHUB_DESC: Record<string, string> = {
  connected: 'Read-only repositories, issues, and pull requests.',
  not_configured: 'Repository and issue tools planned. Not connected.',
  connecting: 'Connecting to the GitHub MCP server…',
  degraded: 'Connected with reduced availability.',
  auth_failed: 'GitHub authentication failed — check the deployment token.',
  unavailable: 'The GitHub MCP server is currently unavailable.',
};

export function IntegrationsPanel({ baseUrl = '' }: { baseUrl?: string }) {
  const [view, setView] = useState<IntegrationsView>(FALLBACK_INTEGRATIONS);
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (refresh = false) => {
      setBusy(true);
      try {
        setView(refresh ? await refreshIntegrations(baseUrl) : await fetchIntegrations(baseUrl));
      } catch {
        setView(FALLBACK_INTEGRATIONS); // safe: never a false "connected"
      } finally {
        setBusy(false);
      }
    },
    [baseUrl],
  );

  useEffect(() => {
    void load(false);
  }, [load]);

  const gh = view.github;
  const githubDesc = GITHUB_DESC[gh.status] ?? GITHUB_DESC.not_configured;
  const caps = gh.capabilities ?? [];

  return (
    <section className="integrations" data-testid="integrations-panel" aria-label="Integrations">
      <div className="integrations-head">
        <span className="sidebar-section-label" style={{ padding: 0 }}>
          Integrations
        </span>
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={() => void load(true)}
          disabled={busy}
          data-testid="integrations-refresh"
          aria-label="Refresh integration status"
        >
          {busy ? '…' : 'Refresh'}
        </button>
      </div>

      <ul className="integration-list">
        <li className="integration-item" data-testid="integration-github">
          <span className="integration-icon" aria-hidden>
            {ICONS.github}
          </span>
          <div className="integration-body">
            <div className="integration-name">
              GitHub
              <span className={`status-pill ${tone(gh.status)}`} data-testid="integration-status-github">
                {gh.label}
              </span>
            </div>
            <p className="integration-desc">{githubDesc}</p>
            {gh.status === 'connected' && caps.length > 0 ? (
              <ul className="integration-caps" data-testid="github-capabilities">
                {caps.map((cap) => (
                  <li key={cap} className="integration-cap">
                    {cap}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        </li>

        <li className="integration-item" data-testid="integration-gmail">
          <span className="integration-icon" aria-hidden>
            {ICONS.gmail}
          </span>
          <div className="integration-body">
            <div className="integration-name">
              Gmail
              <span className="status-pill soon" data-testid="integration-status-gmail">
                Coming next
              </span>
            </div>
            <p className="integration-desc">Email search and send tools planned. Not connected.</p>
          </div>
        </li>

        <li className="integration-item" data-testid="integration-mcp">
          <span className="integration-icon" aria-hidden>
            {ICONS.mcp}
          </span>
          <div className="integration-body">
            <div className="integration-name">
              MCP Runtime
              <span
                className={`status-pill ${view.mcp_runtime.status === 'connected' ? 'ready' : 'neutral'}`}
                data-testid="integration-status-mcp"
              >
                {view.mcp_runtime.label}
              </span>
            </div>
            <p className="integration-desc">
              External MCP servers can be registered when configured.
            </p>
          </div>
        </li>
      </ul>
    </section>
  );
}
