"""Capability eligibility (Phase 43). Pure, config-free.

Decides whether a capability is *eligible* to be planned/executed for a user,
given the user's connectors. Internal (no-connector) capabilities are always
eligible. A connector-backed capability is eligible only when the connector
exists, is healthy, and grants the required scopes. The planner must never see an
ineligible capability, so this filters the retrieval candidate set.

A capability declares its requirement via a ``CapabilityRequirement`` (provider +
required scopes + risk). Requirements are derived deterministically from a
ToolSpec's tags/metadata, or supplied explicitly.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.agent.connectors.models import ConnectorProvider, ConnectorRecord

_PROVIDER_VALUES = {p.value for p in ConnectorProvider}


class CapabilityRequirement(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str | None = None          # None → internal, no connector needed
    required_scopes: list[str] = Field(default_factory=list)
    write: bool = False


class EligibilityResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    eligible: bool
    reason: str = ""


def requirement_for_tool(tool) -> CapabilityRequirement:
    """Derive a capability's connector requirement from its ToolSpec tags.

    Tag conventions (ToolSpec has no free-form metadata field):
      - a provider tag (``github`` / ``gmail`` / ``calendar``) → the connector;
      - ``scope:<name>`` tags → required scopes (e.g. ``scope:repo:write``);
      - write is inferred from side effects / requires_approval.
    Internal tools (no provider tag) need no connector.
    """
    provider = None
    scopes: list[str] = []
    for tag in getattr(tool, "tags", []) or []:
        if provider is None and tag in _PROVIDER_VALUES:
            provider = tag
        elif tag.startswith("scope:"):
            scopes.append(tag[len("scope:"):])
    side = getattr(getattr(tool, "side_effects", None), "value", None)
    write = side in {"write", "external"} or bool(getattr(tool, "requires_approval", False))
    return CapabilityRequirement(provider=provider, required_scopes=scopes, write=write)


def capability_eligibility(
    tool,
    connectors_by_provider: dict[str, ConnectorRecord],
) -> EligibilityResult:
    req = requirement_for_tool(tool)
    if not req.provider:
        return EligibilityResult(eligible=True, reason="internal")

    connector = connectors_by_provider.get(req.provider)
    if connector is None:
        return EligibilityResult(eligible=False, reason="connector_missing")
    if not connector.is_healthy:
        return EligibilityResult(eligible=False, reason=f"connector_{connector.status.value}")
    if not connector.has_scopes(req.required_scopes):
        return EligibilityResult(eligible=False, reason="insufficient_scope")
    return EligibilityResult(eligible=True, reason="connector_ok")


def filter_eligible_capabilities(tools, connectors: list[ConnectorRecord]) -> list:
    """Return only the tools eligible given the user's connectors."""
    by_provider = {c.provider.value: c for c in connectors}
    return [t for t in tools if capability_eligibility(t, by_provider).eligible]


def _connectors_from_snapshot(snapshot) -> list[ConnectorRecord]:
    """Rebuild minimal ConnectorRecords from a safe public-view snapshot."""
    records: list[ConnectorRecord] = []
    for item in snapshot or []:
        if isinstance(item, ConnectorRecord):
            records.append(item)
            continue
        try:
            records.append(
                ConnectorRecord(
                    connector_id=str(item.get("connector_id", "")),
                    user_id="",
                    provider=ConnectorProvider(item["provider"]),
                    status=item.get("status", "disconnected"),
                    scopes=list(item.get("scopes", [])),
                )
            )
        except (KeyError, ValueError):
            continue
    return records


class EligibilityCapabilityRetriever:
    """Wraps a capability retriever and drops connector-ineligible capabilities
    from RunContext-aware retrieval, so the planner never sees an unavailable
    tool. Reads the per-run connector snapshot the scope gate stored in
    ``run_context.metadata['connectors']`` (safe public views only)."""

    def __init__(self, base) -> None:
        self._base = base

    def retrieve(self, request):
        # No RunContext → no user connectors to check; delegate unfiltered.
        return self._base.retrieve(request)

    def retrieve_for_run_context(self, run_context, **kwargs):
        response = self._base.retrieve_for_run_context(run_context, **kwargs)
        snapshot = getattr(run_context, "metadata", {}).get("connectors")
        if snapshot is None:
            return response  # connectors not resolved → do not over-filter
        connectors = _connectors_from_snapshot(snapshot)
        by_provider = {c.provider.value: c for c in connectors}
        kept = [
            m for m in response.matches
            if capability_eligibility(m.tool, by_provider).eligible
        ]
        return response.model_copy(update={"matches": kept})

    def __getattr__(self, name):
        # Delegate any other attribute (e.g. `.base`) to the wrapped retriever.
        return getattr(self._base, name)
