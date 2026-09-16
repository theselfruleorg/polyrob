import time

import jwt
import pytest

from core.security.session_tokens import decode_session_token

SECRET = "session-token-security-test-secret-32"


@pytest.mark.parametrize("change", [
    {"exp": None}, {"jti": None}, {"jti": ""}, {"jti": " "},
    {"jti": "x" * 129}, {"exp": float("nan")}, {"exp": float("inf")},
    {"exp": True}, {"exp": "9999999999"}, {"exp": 1}, {"exp": 10 ** 400},
])
def test_invalid_or_unbounded_session_claims_are_refused(change):
    claims = {"user_id": "owner", "exp": time.time() + 60, "jti": "session"}
    claims.update(change)
    claims = {key: value for key, value in claims.items() if value is not None}
    token = jwt.encode(claims, SECRET)
    with pytest.raises(jwt.InvalidTokenError):
        decode_session_token(token, SECRET)


def test_valid_session_and_logout_decode_of_expired_token():
    token = jwt.encode({"exp": 1, "jti": "expired-session"}, SECRET)
    assert decode_session_token(token, SECRET, verify_exp=False)["jti"] == "expired-session"
