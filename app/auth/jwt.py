"""Minimal JWT (JWS, HS256) implementation on top of the standard library.

Why hand-rolled instead of PyJWT: the dependency policy for this project
allows only flask + gunicorn. A JWT is not much more than
"base64url(header).base64url(payload).base64url(hmac_sha256(...))", so the
whole format fits in ~80 lines of stdlib code (hmac, hashlib, base64, json)
and stays fully auditable.

Supported algorithm: HS256 only. Anything else is rejected on decode, which
also blocks the classic "alg": "none" downgrade attack.
"""

import base64
import hashlib
import hmac
import json
import time

ALGORITHM = "HS256"


class JWTError(Exception):
    """Raised when a token is malformed, has a bad signature, or is expired."""


# --- base64url helpers (JWT uses base64url *without* padding) ---------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    # Re-add the padding that the JWT spec strips off.
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise JWTError("token segment is not valid base64url") from exc


def _sign(signing_input: bytes, secret: str) -> str:
    signature = hmac.new(
        secret.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    return _b64url_encode(signature)


# --- public API -------------------------------------------------------------


def encode(payload: dict, secret: str, ttl_seconds: int | None = None) -> str:
    """Serialize and sign a payload.

    Adds the standard "iat" (issued at) claim and, if ttl_seconds is given,
    an "exp" (expires at) claim. Both are UNIX timestamps in seconds.
    """
    claims = dict(payload)
    issued_at = int(time.time())
    claims.setdefault("iat", issued_at)
    if ttl_seconds is not None:
        claims.setdefault("exp", issued_at + int(ttl_seconds))

    header = {"alg": ALGORITHM, "typ": "JWT"}

    # separators=(",", ":") -> compact JSON, no cosmetic whitespace.
    header_segment = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    payload_segment = _b64url_encode(
        json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )

    signing_input = f"{header_segment}.{payload_segment}".encode("ascii")
    signature_segment = _sign(signing_input, secret)
    return f"{header_segment}.{payload_segment}.{signature_segment}"


def decode(token: str, secret: str, verify_exp: bool = True) -> dict:
    """Verify signature + expiry and return the claims. Raises JWTError."""
    if not token or token.count(".") != 2:
        raise JWTError("token must have exactly three dot-separated segments")

    header_segment, payload_segment, signature_segment = token.split(".")

    try:
        header = json.loads(_b64url_decode(header_segment))
        claims = json.loads(_b64url_decode(payload_segment))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JWTError("token header/payload is not valid JSON") from exc

    if not isinstance(claims, dict) or not isinstance(header, dict):
        raise JWTError("token header/payload must be JSON objects")

    # Reject unexpected algorithms *before* comparing signatures.
    if header.get("alg") != ALGORITHM:
        raise JWTError(f"unsupported algorithm: {header.get('alg')!r}")

    expected = _sign(f"{header_segment}.{payload_segment}".encode("ascii"), secret)
    # compare_digest avoids leaking information through timing differences.
    if not hmac.compare_digest(expected, signature_segment):
        raise JWTError("signature mismatch")

    if verify_exp and "exp" in claims:
        try:
            expires_at = int(claims["exp"])
        except (TypeError, ValueError) as exc:
            raise JWTError("exp claim is not an integer") from exc
        if time.time() >= expires_at:
            raise JWTError("token expired")

    return claims


def is_valid(token: str, secret: str) -> bool:
    """Convenience wrapper: True if the token verifies, False otherwise."""
    try:
        decode(token, secret)
        return True
    except JWTError:
        return False
