// IntegrationsPanel (Phase 45): a TRUTHFUL, static integrations surface.
//
// No connector HTTP API is exposed to the frontend, and real per-user OAuth for
// Gmail/GitHub is NOT implemented — so this panel makes no live calls and offers
// no connect action that could imply otherwise. It states the honest current
// status: Gmail/GitHub are not connected (planned), and the MCP runtime is
// available as infrastructure (external servers can be registered when
// configured). It never claims a live connection, account access, or send
// capability.

interface Integration {
  key: string;
  name: string;
  icon: string;
  status: 'ready' | 'soon';
  statusLabel: string;
  description: string;
}

const INTEGRATIONS: Integration[] = [
  {
    key: 'github',
    name: 'GitHub',
    icon: '⌥',
    status: 'soon',
    statusLabel: 'Coming next',
    description: 'Repository and issue tools planned. Not connected.',
  },
  {
    key: 'gmail',
    name: 'Gmail',
    icon: '✉',
    status: 'soon',
    statusLabel: 'Coming next',
    description: 'Email search and send tools planned. Not connected.',
  },
  {
    key: 'mcp',
    name: 'MCP Runtime',
    icon: '❖',
    status: 'ready',
    statusLabel: 'Available',
    description: 'Infrastructure ready — external MCP servers can be registered when configured.',
  },
];

export function IntegrationsPanel() {
  return (
    <section className="integrations" data-testid="integrations-panel" aria-label="Integrations">
      <div className="sidebar-section-label" style={{ padding: 0 }}>
        Integrations
      </div>
      <ul className="integration-list">
        {INTEGRATIONS.map((item) => (
          <li key={item.key} className="integration-item" data-testid={`integration-${item.key}`}>
            <span className="integration-icon" aria-hidden>
              {item.icon}
            </span>
            <div className="integration-body">
              <div className="integration-name">
                {item.name}
                <span className={`status-pill ${item.status}`} data-testid={`integration-status-${item.key}`}>
                  {item.statusLabel}
                </span>
              </div>
              <p className="integration-desc">{item.description}</p>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
