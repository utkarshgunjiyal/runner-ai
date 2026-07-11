"""Per-user connectors (Phase 43).

A *connector* is distinct from an *MCP server*:
- an MCP server exposes tool definitions and executes protocol calls;
- a connector represents ONE user's authenticated relationship with a provider
  (GitHub, Gmail, Calendar, …): its status, granted scopes, and a *reference* to
  where its credentials live — never the raw credentials themselves.

This module implements the metadata/status/eligibility BOUNDARY only. Real OAuth,
token acquisition/refresh, and secret storage are explicitly DEFERRED (see
docs/CONNECTORS.md). Nothing here stores or serializes raw tokens; capability
eligibility is decided from connector status + scopes so the planner never sees
an unavailable tool.
"""

from app.agent.connectors.eligibility import (
    CapabilityRequirement,
    EligibilityCapabilityRetriever,
    EligibilityResult,
    capability_eligibility,
    filter_eligible_capabilities,
)
from app.agent.connectors.models import (
    ConnectorProvider,
    ConnectorRecord,
    ConnectorStatus,
)
from app.agent.connectors.registry import ConnectorRegistry, InMemoryConnectorRegistry

__all__ = [
    "CapabilityRequirement",
    "EligibilityCapabilityRetriever",
    "EligibilityResult",
    "capability_eligibility",
    "filter_eligible_capabilities",
    "ConnectorProvider",
    "ConnectorRecord",
    "ConnectorStatus",
    "ConnectorRegistry",
    "InMemoryConnectorRegistry",
]
