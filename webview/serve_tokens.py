"""030 S1 companion: scoped, expiring serve-tokens for the preview iframe.

The workspace preview sandbox dropped `allow-same-origin` (agent HTML must
never run with the console origin/cookie). An opaque-origin iframe no longer
sends the SameSite auth cookie, so the authed page fetches this token and
appends it as ?st= — a credential scoped to ONE session's /serve/ tree,
valid for minutes, useless for any other API.
"""
import hashlib
import hmac
import os
import time
from typing import Optional



SERVE_TOKEN_TTL_SEC = 600


def mint_serve_token(session_id: str) -> Optional[str]:
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret:
        return None  # no-auth posture (local): /serve/ needs no credential
    exp = int(time.time()) + SERVE_TOKEN_TTL_SEC
    sig = hmac.new(secret.encode(), f"serve:{session_id}:{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_serve_token(session_id: str, token) -> bool:
    secret = os.environ.get("JWT_SECRET_KEY")
    if not secret or not token or not isinstance(token, str) or "." not in token:
        return False
    exp_s, _, sig = token.partition(".")
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    want = hmac.new(secret.encode(), f"serve:{session_id}:{exp}".encode(),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


