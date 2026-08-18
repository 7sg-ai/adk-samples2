"""Per-advisor Salesforce access-token lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from .models import OAuthTokens, StoredAdvisorCredential
from .oauth import SalesforceOAuthClient, SalesforceOAuthError
from .storage import GatewayStore, TokenCipher


class ReauthorizationRequired(RuntimeError):
    """The named Salesforce advisor must authorize the app again."""


class AdvisorTokenProvider:
    def __init__(
        self,
        *,
        store: GatewayStore,
        cipher: TokenCipher,
        oauth_client: SalesforceOAuthClient,
    ) -> None:
        self._store = store
        self._cipher = cipher
        self._oauth = oauth_client
        self._cache: dict[str, OAuthTokens] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def save_authorization(
        self, advisor_key: str, tokens: OAuthTokens
    ) -> None:
        if not tokens.refresh_token:
            raise ReauthorizationRequired("Salesforce did not issue a refresh token")
        await self._store.put_credential(
            StoredAdvisorCredential(
                advisor_key=advisor_key,
                encrypted_refresh_token=self._cipher.encrypt(tokens.refresh_token),
                instance_url=tokens.instance_url,
                updated_at=datetime.now(UTC),
            )
        )
        self._cache[advisor_key] = tokens

    async def get_valid_access_token(self, advisor_key: str) -> str:
        cached = self._cache.get(advisor_key)
        if cached and cached.expires_at > datetime.now(UTC) + timedelta(seconds=30):
            return cached.access_token

        lock = self._locks.setdefault(advisor_key, asyncio.Lock())
        async with lock:
            cached = self._cache.get(advisor_key)
            if cached and cached.expires_at > datetime.now(UTC) + timedelta(
                seconds=30
            ):
                return cached.access_token
            credential = await self._store.get_credential(advisor_key)
            if not credential:
                raise ReauthorizationRequired("No Salesforce authorization found")
            refresh_token = self._cipher.decrypt(
                credential.encrypted_refresh_token
            )
            try:
                tokens = await self._oauth.refresh(refresh_token)
            except SalesforceOAuthError as exc:
                raise ReauthorizationRequired(
                    "Salesforce authorization must be renewed"
                ) from exc
            rotated_refresh = tokens.refresh_token or refresh_token
            if rotated_refresh != refresh_token:
                await self._store.put_credential(
                    StoredAdvisorCredential(
                        advisor_key=advisor_key,
                        encrypted_refresh_token=self._cipher.encrypt(
                            rotated_refresh
                        ),
                        instance_url=tokens.instance_url,
                        updated_at=datetime.now(UTC),
                    )
                )
            self._cache[advisor_key] = tokens
            return tokens.access_token