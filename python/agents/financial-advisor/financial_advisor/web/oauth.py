"""Salesforce Authorization Code with PKCE client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx

from .models import AdvisorIdentity, OAuthTokens
from .security import pkce_challenge
from .settings import Settings

HTTP_ERROR_STATUS = 400


class SalesforceOAuthError(RuntimeError):
    """Salesforce authorization or token exchange failed."""


class SalesforceOAuthClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._http = http_client

    def authorization_url(self, *, state: str, code_verifier: str) -> str:
        client_id = self._require_client_id()
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self._settings.salesforce_callback_url,
            "scope": "mcp_api refresh_token",
            "state": state,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return (
            f"{self._settings.salesforce_login_url}/services/oauth2/authorize?"
            f"{urlencode(params)}"
        )

    async def exchange_code(
        self, *, code: str, code_verifier: str
    ) -> tuple[OAuthTokens, AdvisorIdentity]:
        payload = {
            "grant_type": "authorization_code",
            "client_id": self._require_client_id(),
            "redirect_uri": self._settings.salesforce_callback_url,
            "code": code,
            "code_verifier": code_verifier,
        }
        if self._settings.salesforce_client_secret:
            payload["client_secret"] = self._settings.salesforce_client_secret
        token_data = await self._token_request(payload)
        tokens = self._parse_tokens(token_data)
        identity = await self._fetch_identity(token_data, tokens.access_token)
        return tokens, identity

    async def refresh(self, refresh_token: str) -> OAuthTokens:
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._require_client_id(),
            "refresh_token": refresh_token,
        }
        if self._settings.salesforce_client_secret:
            payload["client_secret"] = self._settings.salesforce_client_secret
        return self._parse_tokens(await self._token_request(payload))

    async def _token_request(self, payload: dict[str, str]) -> dict[str, object]:
        response = await self._http.post(
            f"{self._settings.salesforce_login_url}/services/oauth2/token",
            data=payload,
            headers={"Accept": "application/json"},
        )
        if response.status_code >= HTTP_ERROR_STATUS:
            raise SalesforceOAuthError("Salesforce OAuth token request failed")
        data = response.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SalesforceOAuthError("Salesforce returned an invalid token response")
        return data

    async def _fetch_identity(
        self, token_data: dict[str, object], access_token: str
    ) -> AdvisorIdentity:
        identity_url = token_data.get("id")
        identity_host = (
            urlparse(identity_url).hostname
            if isinstance(identity_url, str)
            else None
        )
        if (
            not isinstance(identity_url, str)
            or not identity_url.startswith("https://")
            or not identity_host
            or not (
                identity_host == "salesforce.com"
                or identity_host.endswith(".salesforce.com")
            )
        ):
            raise SalesforceOAuthError("Salesforce identity URL is missing")
        response = await self._http.get(
            identity_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code >= HTTP_ERROR_STATUS:
            raise SalesforceOAuthError("Salesforce identity lookup failed")
        data = response.json()
        org_id, user_id = data.get("organization_id"), data.get("user_id")
        if not isinstance(org_id, str) or not isinstance(user_id, str):
            raise SalesforceOAuthError("Salesforce identity response is invalid")
        return AdvisorIdentity(
            advisor_key=f"{org_id}:{user_id}", org_id=org_id, user_id=user_id
        )

    def _parse_tokens(self, data: dict[str, object]) -> OAuthTokens:
        access_token = data.get("access_token")
        instance_url = data.get("instance_url")
        refresh_token = data.get("refresh_token")
        if not isinstance(access_token, str) or not isinstance(instance_url, str):
            raise SalesforceOAuthError("Salesforce token response is incomplete")
        now = datetime.now(UTC)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            instance_url=instance_url,
            issued_at=now,
            expires_at=now + timedelta(minutes=15),
        )

    def _require_client_id(self) -> str:
        if not self._settings.salesforce_client_id:
            raise SalesforceOAuthError("Salesforce OAuth is not configured")
        return self._settings.salesforce_client_id