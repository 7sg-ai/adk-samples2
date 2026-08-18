"""Gateway persistence, with Firestore/KMS production implementations."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Protocol

from google.cloud import firestore_v1, kms_v1

from .models import GatewaySession, OAuthTransaction, StoredAdvisorCredential


class GatewayStore(Protocol):
    async def put_oauth_transaction(self, transaction: OAuthTransaction) -> None: ...
    async def pop_oauth_transaction(self, state: str) -> OAuthTransaction | None: ...
    async def put_session(self, session: GatewaySession) -> None: ...
    async def get_session(self, session_id: str) -> GatewaySession | None: ...
    async def put_credential(self, credential: StoredAdvisorCredential) -> None: ...
    async def get_credential(
        self, advisor_key: str
    ) -> StoredAdvisorCredential | None: ...


class TokenCipher(Protocol):
    def encrypt(self, plaintext: str) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> str: ...


class InMemoryGatewayStore:
    """Local-only store; never use for multi-instance production."""

    def __init__(self) -> None:
        self.oauth: dict[str, OAuthTransaction] = {}
        self.sessions: dict[str, GatewaySession] = {}
        self.credentials: dict[str, StoredAdvisorCredential] = {}

    async def put_oauth_transaction(self, transaction: OAuthTransaction) -> None:
        self.oauth[transaction.state] = transaction

    async def pop_oauth_transaction(self, state: str) -> OAuthTransaction | None:
        return self.oauth.pop(state, None)

    async def put_session(self, session: GatewaySession) -> None:
        self.sessions[session.session_id] = session

    async def get_session(self, session_id: str) -> GatewaySession | None:
        session = self.sessions.get(session_id)
        if session and session.expires_at > datetime.now(UTC):
            return session
        return None

    async def put_credential(self, credential: StoredAdvisorCredential) -> None:
        self.credentials[credential.advisor_key] = credential

    async def get_credential(
        self, advisor_key: str
    ) -> StoredAdvisorCredential | None:
        return self.credentials.get(advisor_key)


class PlaintextDevelopmentCipher:
    """Explicitly local-only cipher used by tests and local development."""

    def encrypt(self, plaintext: str) -> bytes:
        return plaintext.encode()

    def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext.decode()


class CloudKmsTokenCipher:
    def __init__(self, key_name: str) -> None:
        self._key_name = key_name
        self._client = kms_v1.KeyManagementServiceClient()

    def encrypt(self, plaintext: str) -> bytes:
        response = self._client.encrypt(
            request={"name": self._key_name, "plaintext": plaintext.encode()}
        )
        return response.ciphertext

    def decrypt(self, ciphertext: bytes) -> str:
        response = self._client.decrypt(
            request={"name": self._key_name, "ciphertext": ciphertext}
        )
        return response.plaintext.decode()


class FirestoreGatewayStore:
    def __init__(self, *, project: str, database: str) -> None:
        self._client = firestore_v1.AsyncClient(
            project=project, database=database
        )

    async def put_oauth_transaction(self, transaction: OAuthTransaction) -> None:
        await self._client.collection("oauth_transactions").document(
            transaction.state
        ).set(
            {
                "code_verifier": transaction.code_verifier,
                "return_path": transaction.return_path,
                "expires_at": transaction.expires_at,
            }
        )

    async def pop_oauth_transaction(self, state: str) -> OAuthTransaction | None:
        ref = self._client.collection("oauth_transactions").document(state)
        snapshot = await ref.get()
        if not snapshot.exists:
            return None
        await ref.delete()
        data = snapshot.to_dict() or {}
        return OAuthTransaction(
            state=state,
            code_verifier=str(data["code_verifier"]),
            return_path=str(data["return_path"]),
            expires_at=_as_datetime(data["expires_at"]),
        )

    async def put_session(self, session: GatewaySession) -> None:
        await self._client.collection("gateway_sessions").document(
            session.session_id
        ).set(session.__dict__)

    async def get_session(self, session_id: str) -> GatewaySession | None:
        snapshot = await self._client.collection("gateway_sessions").document(
            session_id
        ).get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        session = GatewaySession(**data)
        return session if session.expires_at > datetime.now(UTC) else None

    async def put_credential(self, credential: StoredAdvisorCredential) -> None:
        await self._client.collection("advisor_credentials").document(
            credential.advisor_key
        ).set(credential.__dict__)

    async def get_credential(
        self, advisor_key: str
    ) -> StoredAdvisorCredential | None:
        snapshot = await self._client.collection("advisor_credentials").document(
            advisor_key
        ).get()
        if not snapshot.exists:
            return None
        data: Mapping[str, object] = snapshot.to_dict() or {}
        encrypted_token = data["encrypted_refresh_token"]
        if not isinstance(encrypted_token, bytes):
            raise ValueError("Expected encrypted refresh token bytes")
        return StoredAdvisorCredential(
            advisor_key=advisor_key,
            encrypted_refresh_token=encrypted_token,
            instance_url=str(data["instance_url"]),
            updated_at=_as_datetime(data["updated_at"]),
        )


def _as_datetime(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("Expected Firestore datetime")
    return value if value.tzinfo else value.replace(tzinfo=UTC)