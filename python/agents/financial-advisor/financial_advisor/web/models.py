"""Gateway domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class OAuthTransaction:
    state: str
    code_verifier: str
    return_path: str
    expires_at: datetime


@dataclass(frozen=True)
class AdvisorIdentity:
    advisor_key: str
    org_id: str
    user_id: str


@dataclass(frozen=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    instance_url: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class StoredAdvisorCredential:
    advisor_key: str
    encrypted_refresh_token: bytes
    instance_url: str
    updated_at: datetime


@dataclass(frozen=True)
class GatewaySession:
    session_id: str
    adk_session_id: str
    adk_user_id: str
    access_mode: str
    advisor_key: str | None
    created_at: datetime
    expires_at: datetime