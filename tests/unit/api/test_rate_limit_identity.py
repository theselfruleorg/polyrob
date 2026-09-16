from starlette.requests import Request

from api.middleware import RateLimitMiddleware


def request(headers=(), peer="198.51.100.10"):
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_rotating_unverified_credentials_cannot_rotate_buckets():
    middleware = RateLimitMiddleware(None)
    keys = set()
    for i in range(10):
        req = request([(b"authorization", f"Bearer forged-{i}".encode()),
                       (b"x-api-key", f"key-{i}".encode()),
                       (b"x-forwarded-for", f"192.0.2.{i}".encode())])
        keys.add(middleware._get_user_identifier(req))
    assert keys == {"ip_198.51.100.10"}


def test_verified_identity_shares_bucket_across_tokens():
    middleware = RateLimitMiddleware(None)
    a, b = request(), request(peer="198.51.100.20")
    for req in (a, b):
        req.state.authenticated = True
        req.state.user_id = "tenant-a"
    assert middleware._get_user_identifier(a) == middleware._get_user_identifier(b)
