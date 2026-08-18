"""Security and mode-isolation tests for the FastAPI gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from financial_advisor.client_profile import ClientProfile
from financial_advisor.web.app import create_app
from financial_advisor.web.settings import Settings
from financial_advisor.web.storage import (
    InMemoryGatewayStore,
    PlaintextDevelopmentCipher,
)

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_TEMPORARY_REDIRECT = 307


class ReversingCipher:
    def encrypt(self, plaintext: str) -> bytes:
        return plaintext[::-1].encode()

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode()[::-1]


class FakeProfileService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_profile(
        self, *, account_id: str, access_token: str
    ) -> ClientProfile:
        self.calls.append((account_id, access_token))
        return ClientProfile("Moderate", "Long-term retirement growth")


class FakeAdkGateway:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict[str, Any]]] = []

    async def create_session(self, *, user_id: str, state: dict[str, Any]) -> str:
        self.created.append((user_id, state))
        return f"adk-{len(self.created)}"

    async def chat(
        self, *, user_id: str, session_id: str, message: str
    ) -> AsyncIterator[str]:
        yield "ok"


def settings() -> Settings:
    return Settings(
        public_base_url="http://testserver",
        session_signing_key="test-signing-key-with-at-least-32-bytes",
        salesforce_client_id="client-id",
        salesforce_client_secret=None,
        salesforce_login_url="https://login.salesforce.com",
        salesforce_mcp_url="https://api.salesforce.com/mcp",
        firestore_project=None,
        firestore_database="(default)",
        kms_key_name=None,
        storage_backend="memory",
        cookie_secure=False,
    )


@pytest.mark.asyncio
async def test_anonymous_session_has_no_salesforce_state() -> None:
    adk = FakeAdkGateway()
    app = create_app(
        settings=settings(),
        store=InMemoryGatewayStore(),
        cipher=PlaintextDevelopmentCipher(),
        adk=adk,  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/anonymous/sessions",
            json={"sfdc_account_id": "001000000000001AAA"},
        )

    assert response.status_code == HTTP_OK
    assert adk.created[0][1] == {"access_mode": "anonymous"}
    assert "sfdc_account_id" not in adk.created[0][1]
    assert not app.state.gateway.store.credentials


@pytest.mark.asyncio
async def test_cross_origin_session_creation_is_rejected() -> None:
    adk = FakeAdkGateway()
    app = create_app(
        settings=settings(),
        store=InMemoryGatewayStore(),
        cipher=PlaintextDevelopmentCipher(),
        adk=adk,  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/anonymous/sessions",
            headers={"Origin": "https://attacker.example"},
        )

    assert response.status_code == HTTP_FORBIDDEN
    assert not adk.created


@pytest.mark.asyncio
async def test_salesforce_session_requires_authenticated_cookie() -> None:
    app = create_app(
        settings=settings(),
        store=InMemoryGatewayStore(),
        cipher=PlaintextDevelopmentCipher(),
        adk=FakeAdkGateway(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/salesforce/sessions",
            json={"account_id": "001000000000001AAA"},
        )

    assert response.status_code == HTTP_UNAUTHORIZED


@pytest.mark.asyncio
async def test_oauth_login_uses_authorization_code_pkce_and_required_scopes() -> None:
    store = InMemoryGatewayStore()
    app = create_app(
        settings=settings(),
        store=store,
        cipher=PlaintextDevelopmentCipher(),
        adk=FakeAdkGateway(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/auth/salesforce/login?account_id=001000000000001AAA"
        )

    assert response.status_code == HTTP_TEMPORARY_REDIRECT
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["mcp_api refresh_token"]
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    assert len(store.oauth) == 1


@pytest.mark.asyncio
async def test_oauth_callback_rejects_missing_state_cookie() -> None:
    app = create_app(
        settings=settings(),
        store=InMemoryGatewayStore(),
        cipher=PlaintextDevelopmentCipher(),
        adk=FakeAdkGateway(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/auth/salesforce/callback?code=fake&state=fake"
        )

    assert response.status_code == HTTP_BAD_REQUEST


@pytest.mark.asyncio
async def test_oauth_callback_and_account_session_keep_tokens_out_of_adk_state() -> None:
    store = InMemoryGatewayStore()
    adk = FakeAdkGateway()
    profiles = FakeProfileService()

    def salesforce(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/services/oauth2/token"):
            return httpx.Response(
                HTTP_OK,
                json={
                    "access_token": "advisor-access-token",
                    "refresh_token": "advisor-refresh-token",
                    "instance_url": "https://example.my.salesforce.com",
                    "id": "https://login.salesforce.com/id/org-123/user-456",
                },
            )
        if request.url.path == "/id/org-123/user-456":
            return httpx.Response(
                HTTP_OK,
                json={"organization_id": "org-123", "user_id": "user-456"},
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    sf_http = httpx.AsyncClient(transport=httpx.MockTransport(salesforce))
    app = create_app(
        settings=settings(),
        store=store,
        cipher=ReversingCipher(),
        http_client=sf_http,
        adk=adk,  # type: ignore[arg-type]
        profiles=profiles,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        login = await client.get(
            "/auth/salesforce/login?account_id=001000000000001AAA"
        )
        state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
        callback = await client.get(
            f"/auth/salesforce/callback?code=auth-code&state={state}"
        )
        account_session = await client.post(
            "/api/salesforce/sessions",
            json={"account_id": "001000000000001AAA"},
        )

    assert callback.status_code == HTTP_TEMPORARY_REDIRECT
    assert account_session.status_code == HTTP_OK
    credential = store.credentials["org-123:user-456"]
    assert credential.encrypted_refresh_token == b"nekot-hserfer-rosivda"
    assert b"advisor-refresh-token" not in credential.encrypted_refresh_token
    assert profiles.calls == [
        ("001000000000001AAA", "advisor-access-token")
    ]
    account_state = adk.created[-1][1]
    assert account_state == {
        "access_mode": "salesforce",
        "client_risk_profile": "Moderate",
        "client_investment_goals": "Long-term retirement growth",
        "client_profile_source": "salesforce",
    }
    assert "advisor-access-token" not in str(adk.created)
    assert "advisor-refresh-token" not in str(adk.created)
    assert "001000000000001AAA" not in str(adk.created)
    await sf_http.aclose()