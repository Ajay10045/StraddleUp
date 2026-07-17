import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any


def hash_passcode(passcode: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, 210_000)
    return base64.b64encode(salt + digest).decode()


def verify_passcode(passcode: str, encoded: str) -> bool:
    raw = base64.b64decode(encoded.encode())
    salt, expected = raw[:16], raw[16:]
    actual = hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, 210_000)
    return hmac.compare_digest(actual, expected)


_INSECURE_SECRETS = {"local-development-secret-change-me", "change-me-before-sharing", "replace-with-a-long-random-secret", ""}


def _secret() -> bytes:
    secret = os.getenv("APP_SECRET", "")
    if secret.strip() in _INSECURE_SECRETS:
        raise RuntimeError(
            "APP_SECRET is unset or using a known placeholder value. Set APP_SECRET to a long "
            "random secret before starting the server; it signs host and player tokens."
        )
    return secret.encode()


def issue_token(payload: dict[str, Any], expires_in: int = 43_200) -> str:
    body = {**payload, "exp": int(time.time()) + expires_in, "nonce": secrets.token_urlsafe(8)}
    encoded = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def read_token(token: str) -> dict[str, Any] | None:
    try:
        encoded, signature = token.rsplit(".", 1)
        expected = hmac.new(_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        return payload if payload.get("exp", 0) >= time.time() else None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None

