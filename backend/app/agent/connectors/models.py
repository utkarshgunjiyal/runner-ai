"""Connector data model (Phase 43). Pydantic only — config-free.

Security: ``credential_reference`` is an OPAQUE POINTER to where a secret lives
(e.g. a secret-manager key), never the secret itself. No field here holds a raw
token. ``public_view()`` is the only shape that leaves the backend and it omits
the credential reference entirely.
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ConnectorProvider(str, Enum):
    GITHUB = "github"
    GMAIL = "gmail"
    CALENDAR = "calendar"


class ConnectorStatus(str, Enum):
    CONNECTED = "connected"      # healthy; capabilities eligible
    DISCONNECTED = "disconnected"
    EXPIRED = "expired"          # token expired — needs re-auth
    ERROR = "error"
    PENDING = "pending"          # auth flow started, not complete


class ConnectorRecord(BaseModel):
    """One user's authenticated relationship with a provider."""

    model_config = ConfigDict(frozen=True)

    connector_id: str
    user_id: str
    provider: ConnectorProvider
    status: ConnectorStatus = ConnectorStatus.DISCONNECTED
    scopes: list[str] = Field(default_factory=list)
    # Opaque reference to a secret store entry — NEVER a raw token. Excluded from
    # repr and from the public view.
    credential_reference: str | None = Field(default=None, repr=False)
    account_display_name: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_health_check: str | None = None
    error_code: str | None = None

    @property
    def is_healthy(self) -> bool:
        return self.status == ConnectorStatus.CONNECTED

    def has_scopes(self, required: list[str]) -> bool:
        return set(required or []).issubset(set(self.scopes))

    def public_view(self) -> dict:
        """Safe metadata for API/UI — never the credential reference."""
        return {
            "connector_id": self.connector_id,
            "provider": self.provider.value,
            "status": self.status.value,
            "scopes": list(self.scopes),
            "account_display_name": self.account_display_name,
            "last_health_check": self.last_health_check,
            "error_code": self.error_code,
        }
