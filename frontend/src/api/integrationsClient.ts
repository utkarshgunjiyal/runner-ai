// Typed client for the integration status API (Phase 46.2). Same-origin; sends
// auth cookies. Degrades gracefully — a failure yields a safe "unknown" view so
// the panel never crashes and never claims a false connection.

export interface IntegrationStatus {
  provider: string;
  status: string;
  label: string;
  read_only?: boolean;
  configured?: boolean;
  capabilities?: string[];
  allowed_tool_count?: number;
  error_code?: string | null;
}

export interface McpRuntimeStatus {
  provider: string;
  status: string;
  label: string;
}

export interface IntegrationsView {
  github: IntegrationStatus;
  gmail: IntegrationStatus;
  mcp_runtime: McpRuntimeStatus;
}

export const FALLBACK_INTEGRATIONS: IntegrationsView = {
  github: {
    provider: 'github',
    status: 'not_configured',
    label: 'Not configured',
    read_only: true,
    capabilities: [],
  },
  gmail: { provider: 'gmail', status: 'not_configured', label: 'Coming next', read_only: true, capabilities: [] },
  mcp_runtime: { provider: 'mcp', status: 'available', label: 'Available' },
};

function coerce(body: unknown): IntegrationsView {
  const b = (body ?? {}) as Partial<IntegrationsView>;
  return {
    github: { ...FALLBACK_INTEGRATIONS.github, ...(b.github ?? {}) },
    gmail: { ...FALLBACK_INTEGRATIONS.gmail, ...(b.gmail ?? {}) },
    mcp_runtime: { ...FALLBACK_INTEGRATIONS.mcp_runtime, ...(b.mcp_runtime ?? {}) },
  };
}

async function request(url: string, method: string): Promise<IntegrationsView> {
  const response = await fetch(url, { method, credentials: 'include' });
  if (!response.ok) throw new Error(`integrations ${response.status}`);
  return coerce(await response.json());
}

export async function fetchIntegrations(baseUrl = ''): Promise<IntegrationsView> {
  return request(`${baseUrl}/integrations`, 'GET');
}

export async function refreshIntegrations(baseUrl = ''): Promise<IntegrationsView> {
  return request(`${baseUrl}/integrations/refresh`, 'POST');
}
