"""E3 — verify_signature must write an audit-log row when an admin wallet
authenticates (session-privilege grant), not just a logger.info line."""
import pytest

ADMIN_WALLET = "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"


class _FakeSiwe:
    async def verify_signature(self, **kwargs):
        return True


class _FakeIdentityMapper:
    async def get_or_create_user(self, wallet_address, chain):
        return "u-admin-1"


class _FakeDB:
    async def fetch_one(self, query, params):
        return {"role": "user", "tier": "free"}


class _FakeConfig:
    jwt_secret_key = "test-secret-key"


class _FakeContainer:
    config = _FakeConfig()

    def get_service(self, name):
        return {
            "siwe_authenticator": _FakeSiwe(),
            "identity_mapper": _FakeIdentityMapper(),
            "database_manager": _FakeDB(),
        }.get(name)


@pytest.mark.asyncio
async def test_admin_wallet_login_writes_audit_trail(monkeypatch):
    import api.auth_endpoints as ae
    from core.container import DependencyContainer

    monkeypatch.setenv("ADMIN_WALLETS", ADMIN_WALLET.lower())
    monkeypatch.setattr(DependencyContainer, "get_instance", staticmethod(lambda: _FakeContainer()))

    audit_calls = []

    async def _fake_log(self, **kwargs):
        audit_calls.append(kwargs)
        return 1

    monkeypatch.setattr("modules.database.audit_log.AuditLogger.log", _fake_log)

    req = ae.VerifyRequest(wallet_address=ADMIN_WALLET, message="m", signature="s", nonce="n")
    await ae.verify_signature(req)

    assert any(c.get("event_type") == "admin_wallet_auth" for c in audit_calls), audit_calls
