"""Phase 43 — connector model + capability eligibility. Config-free."""

import asyncio

from app.agent.connectors import (
    ConnectorProvider,
    ConnectorRecord,
    ConnectorStatus,
    EligibilityCapabilityRetriever,
    InMemoryConnectorRegistry,
    capability_eligibility,
    filter_eligible_capabilities,
)
from app.agent.connectors.eligibility import requirement_for_tool
from app.agent.capabilities.models import CapabilityMatch, CapabilityRetrievalResponse
from app.agent.models.tool_spec import RiskLevel, SideEffectType, ToolKind, ToolSpec


def _tool(tool_id, *, tags=None, kind=ToolKind.MCP, side=SideEffectType.READ,
          requires_approval=False):
    return ToolSpec(
        id=tool_id, name=tool_id, kind=kind, description=tool_id,
        input_schema={}, output_schema={}, tags=tags or [],
        risk_level=RiskLevel.MEDIUM, side_effects=side,
        requires_approval=requires_approval,
    )


GH_READ = _tool("github.list_issues", tags=["github", "scope:repo:read"])
GH_WRITE = _tool("github.merge_pr", tags=["github", "scope:repo:write"],
                 side=SideEffectType.WRITE, requires_approval=True)
INTERNAL = _tool("search_documents", kind=ToolKind.INTERNAL, tags=["documents"])


def _connectors(*records):
    return list(records)


def connected(provider, scopes):
    return ConnectorRecord(
        connector_id=f"c-{provider}", user_id="u", provider=ConnectorProvider(provider),
        status=ConnectorStatus.CONNECTED, scopes=scopes,
    )


def test_internal_capability_always_eligible():
    assert capability_eligibility(INTERNAL, {}).eligible is True


def test_missing_connector_makes_capability_ineligible():
    res = capability_eligibility(GH_READ, {})
    assert res.eligible is False
    assert res.reason == "connector_missing"


def test_disconnected_connector_filters_capability():
    disconnected = ConnectorRecord(
        connector_id="c", user_id="u", provider=ConnectorProvider.GITHUB,
        status=ConnectorStatus.DISCONNECTED, scopes=["repo:read"],
    )
    kept = filter_eligible_capabilities([GH_READ], [disconnected])
    assert kept == []


def test_healthy_connector_with_scope_enables_capability():
    kept = filter_eligible_capabilities([GH_READ], [connected("github", ["repo:read"])])
    assert kept == [GH_READ]


def test_insufficient_scope_filters_write_but_keeps_read():
    conns = [connected("github", ["repo:read"])]  # read scope only
    kept = filter_eligible_capabilities([GH_READ, GH_WRITE], conns)
    assert GH_READ in kept
    assert GH_WRITE not in kept  # needs repo:write


def test_write_capability_is_classified_as_write():
    req = requirement_for_tool(GH_WRITE)
    assert req.write is True
    assert req.provider == "github"


def test_credential_reference_never_serialized_in_public_view():
    rec = ConnectorRecord(
        connector_id="c", user_id="u", provider=ConnectorProvider.GMAIL,
        status=ConnectorStatus.CONNECTED, scopes=["mail:read"],
        credential_reference="secret-manager://gmail/u/token",
    )
    view = rec.public_view()
    assert "credential_reference" not in view
    assert "secret-manager" not in str(view)


def test_registry_lists_and_gets_per_user():
    reg = InMemoryConnectorRegistry([connected("github", ["repo:read"])])
    got = asyncio.run(reg.list_for_user("u"))
    assert len(got) == 1
    assert asyncio.run(reg.get("u", "github")).provider == ConnectorProvider.GITHUB
    assert asyncio.run(reg.get("u", "gmail")) is None


class _FakeBase:
    def __init__(self, tools):
        self._tools = tools

    def retrieve_for_run_context(self, run_context, **kwargs):
        return CapabilityRetrievalResponse(
            query="q", matches=[CapabilityMatch(tool=t, score=1.0) for t in self._tools]
        )

    def retrieve(self, request):
        return CapabilityRetrievalResponse(query="q", matches=[])


class _Ctx:
    def __init__(self, connectors):
        self.metadata = {"connectors": connectors}


def test_eligibility_retriever_drops_ineligible_matches():
    wrapper = EligibilityCapabilityRetriever(_FakeBase([INTERNAL, GH_READ, GH_WRITE]))
    # github connected with read scope only
    ctx = _Ctx([{"connector_id": "c", "provider": "github", "status": "connected", "scopes": ["repo:read"]}])
    matches = wrapper.retrieve_for_run_context(ctx).matches
    ids = {m.tool.id for m in matches}
    assert "search_documents" in ids       # internal always
    assert "github.list_issues" in ids      # read scope present
    assert "github.merge_pr" not in ids     # write scope missing


def test_eligibility_retriever_no_snapshot_does_not_overfilter():
    wrapper = EligibilityCapabilityRetriever(_FakeBase([GH_READ]))

    class _NoConn:
        metadata: dict = {}

    matches = wrapper.retrieve_for_run_context(_NoConn()).matches
    assert len(matches) == 1  # no snapshot → do not filter
