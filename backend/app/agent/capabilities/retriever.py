"""CapabilityRetriever interface.

Implementations rank the registry's tools for a query and return the top
matches for the future planner. Phase 2 ships one keyword implementation; the
interface is stable so embedding/hybrid/reranker variants slot in later without
changing callers (docs/architecture/v2.md §4).
"""

from abc import ABC, abstractmethod

from app.agent.capabilities.models import (
    CapabilityRetrievalRequest,
    CapabilityRetrievalResponse,
)


class CapabilityRetriever(ABC):
    @abstractmethod
    def retrieve(
        self, request: CapabilityRetrievalRequest
    ) -> CapabilityRetrievalResponse:
        ...
