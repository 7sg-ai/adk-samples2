"""Gateway configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Environment-backed gateway settings."""

    public_base_url: str
    session_signing_key: str
    salesforce_client_id: str | None
    salesforce_client_secret: str | None
    salesforce_login_url: str
    salesforce_mcp_url: str
    firestore_project: str | None
    firestore_database: str
    kms_key_name: str | None
    storage_backend: str
    cookie_secure: bool

    @property
    def salesforce_callback_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/salesforce/callback"

    @classmethod
    def from_environment(cls) -> Settings:
        base_url = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080")
        signing_key = os.getenv("APP_SESSION_SIGNING_KEY", "")
        if not signing_key:
            if os.getenv("APP_ENV", "development") == "production":
                raise RuntimeError("APP_SESSION_SIGNING_KEY is required")
            signing_key = "development-only-change-me"
        return cls(
            public_base_url=base_url,
            session_signing_key=signing_key,
            salesforce_client_id=os.getenv("SFDC_OAUTH_CLIENT_ID"),
            salesforce_client_secret=os.getenv("SFDC_OAUTH_CLIENT_SECRET"),
            salesforce_login_url=os.getenv(
                "SFDC_LOGIN_URL", "https://login.salesforce.com"
            ).rstrip("/"),
            salesforce_mcp_url=os.getenv(
                "SFDC_MCP_URL",
                "https://api.salesforce.com/platform/mcp/v1/platform/sobject-reads",
            ),
            firestore_project=os.getenv("FIRESTORE_PROJECT")
            or os.getenv("GOOGLE_CLOUD_PROJECT"),
            firestore_database=os.getenv("FIRESTORE_DATABASE", "(default)"),
            kms_key_name=os.getenv("TOKEN_KMS_KEY_NAME"),
            storage_backend=os.getenv("GATEWAY_STORAGE_BACKEND", "memory"),
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() == "true",
        )