"""Provider-agnostic Resource Resolution layer (Phase 46.3.1).

Sits between capability selection and argument construction:

    Capability Selection → Resource Resolver → Argument Builder → Validator → Executor

The resolver deterministically resolves a provider's resources (owner/repo/id/…)
from allowed sources only (request, prior outputs, thread state, connector
identity, cache, clarification — never the LLM); the argument builder consumes the
already-resolved resources and shapes them onto the tool's discovered schema.
GitHub is one implementation; Gmail/Slack/Jira/etc. plug into the same registries.
"""

from app.agent.resources.models import (
    ResolutionContext,
    ResolvedResources,
    Resource,
    ResourceSource,
)
from app.agent.resources.pipeline import ResourceAwareArgumentBuilder
from app.agent.resources.resolver import (
    ArgumentBuilderRegistry,
    ProviderArgumentBuilder,
    ResourceResolver,
    ResourceResolverRegistry,
    provider_of,
)

__all__ = [
    "Resource",
    "ResourceSource",
    "ResolvedResources",
    "ResolutionContext",
    "ResourceResolver",
    "ProviderArgumentBuilder",
    "ResourceResolverRegistry",
    "ArgumentBuilderRegistry",
    "ResourceAwareArgumentBuilder",
    "provider_of",
]
