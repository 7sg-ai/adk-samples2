"""PKCE, signed cookies, and identifier helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any


def random_urlsafe(size: int = 32) -> str:
    return secrets.token_urlsafe(size)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def sign_cookie(payload: dict[str, Any], key: str) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=")
    signature = hmac.new(key.encode(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).decode()}"


def verify_cookie(value: str, key: str) -> dict[str, Any] | None:
    try:
        encoded_text, signature_text = value.split(".", 1)
        encoded = encoded_text.encode()
        supplied = base64.urlsafe_b64decode(signature_text.encode())
        expected = hmac.new(key.encode(), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        padded = encoded + b"=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload if isinstance(payload, dict) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None