"""Connector registry (Phase 43). Config-free; pluggable per-user connector store.

The default in-memory registry is what the shipped build uses (no real OAuth). A
production registry would back this with the connector Mongo collection + a secret
manager, implementing the same Protocol. Retrieval/eligibility read only the
registry's public metadata; credentials are resolved (later) via the opaque
``credential_reference`` at execution time, never here.
"""

from __future__ import annotations

from typing import Protocol

from app.agent.connectors.models import ConnectorRecord


class ConnectorRegistry(Protocol):
    async def list_for_user(self, user_id: str) -> list[ConnectorRecord]: ...
    async def get(self, user_id: str, provider: str) -> ConnectorRecord | None: ...


class InMemoryConnectorRegistry:
    """A simple, injectable registry — the default (and the test double)."""

    def __init__(self, records: list[ConnectorRecord] | None = None) -> None:
        self._by_user: dict[str, list[ConnectorRecord]] = {}
        for record in records or []:
            self._by_user.setdefault(record.user_id, []).append(record)

    async def list_for_user(self, user_id: str) -> list[ConnectorRecord]:
        return list(self._by_user.get(user_id, []))

    async def get(self, user_id: str, provider: str) -> ConnectorRecord | None:
        for record in self._by_user.get(user_id, []):
            if record.provider.value == provider:
                return record
        return None

    def upsert(self, record: ConnectorRecord) -> None:
        bucket = self._by_user.setdefault(record.user_id, [])
        for i, existing in enumerate(bucket):
            if existing.provider == record.provider:
                bucket[i] = record
                return
        bucket.append(record)
