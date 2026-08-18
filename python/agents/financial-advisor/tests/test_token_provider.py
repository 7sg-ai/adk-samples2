"""Tests for encrypted per-advisor token handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from financial_advisor.web.models import OAuthTokens
from financial_advisor.web.storage import InMemoryGatewayStore
from financial_advisor.web.token_provider import AdvisorTokenProvider


class TrackingCipher:
    def encrypt(self, plaintext: str) -> bytes:
        return f"encrypted:{plaintext}".encode()

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode().removeprefix("encrypted:")


class RefreshingOAuthClient:
    def __init__(self) -> None:
        self.refresh_tokens: list[str] = []

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        self.refresh_tokens.append(refresh_token)
        now = datetime.now(UTC)
        return OAuthTokens(
            access_token="new-access",
            refresh_token="rotated-refresh",
            instance_url="https://example.my.salesforce.com",
            issued_at=now,
            expires_at=now + timedelta(minutes=15),
        )


@pytest.mark.asyncio
async def test_refresh_token_is_encrypted_and_rotated() -> None:
    store = InMemoryGatewayStore()
    oauth = RefreshingOAuthClient()
    provider = AdvisorTokenProvider(
        store=store,
        cipher=TrackingCipher(),
        oauth_client=oauth,  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    await provider.save_authorization(
        "org:user",
        OAuthTokens(
            access_token="expired-access",
            refresh_token="original-refresh",
            instance_url="https://example.my.salesforce.com",
            issued_at=now - timedelta(hours=1),
            expires_at=now - timedelta(minutes=1),
        ),
    )

    access_token = await provider.get_valid_access_token("org:user")

    assert access_token == "new-access"
    assert oauth.refresh_tokens == ["original-refresh"]
    assert (
        store.credentials["org:user"].encrypted_refresh_token
        == b"encrypted:rotated-refresh"
    )