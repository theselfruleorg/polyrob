"""Regression: the single-user webgate must NOT require JWT_SECRET_KEY.

Caught live during doc-03 P1/P2 verify — `startup_event` hard-raised
"JWT_SECRET_KEY not configured" even when WEBGATE_MULTITENANT is OFF (the default
single-user primitive, which has no auth at all), blocking the loopback webgate.

These tests drive the JWT-checking startup handler with the heavy collaborators
(container / config / core init) mocked, isolating ONLY the JWT-requirement branch.

NOTE: `server.py` registers TWO `@on_event("startup")` handlers named
`startup_event` (the module attribute resolves to the LAST one). The JWT check
lives in the FIRST-registered handler, so we reach it via the app's on_startup
list rather than the shadowed module attribute.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

import webview.server as server


def _jwt_startup_handler():
    """The first-registered startup handler — the one carrying the JWT check."""
    handlers = list(server._fastapi.router.on_startup)
    assert handlers, "no startup handlers registered"
    return handlers[0]


@pytest.fixture
def _stub_core(monkeypatch):
    """No-op the heavy startup collaborators so we test only the JWT branch."""
    import core.container
    import core.config
    import core.initialization

    monkeypatch.setattr(core.container.DependencyContainer, "get_instance",
                        classmethod(lambda cls, *a, **k: MagicMock()), raising=False)
    monkeypatch.setattr(core.config, "BotConfig", lambda *a, **k: MagicMock())

    async def _noop_init(*a, **k):
        return None

    monkeypatch.setattr(core.initialization, "initialize_core", _noop_init)


def test_single_user_startup_does_not_require_jwt(monkeypatch, _stub_core):
    """WEBGATE_MULTITENANT OFF + no JWT → startup must not raise on JWT."""
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)  # default = single-user
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    # Must complete without the "JWT_SECRET_KEY not configured" RuntimeError.
    asyncio.run(_jwt_startup_handler()())


def test_multitenant_startup_still_requires_jwt(monkeypatch, _stub_core):
    """WEBGATE_MULTITENANT ON + no JWT → startup still raises (layer-on-top intact).

    The writable-console preconditions are satisfied here so the branch under
    test is the JWT one: since 043 W7/W8 a writable NON-local console also
    refuses to boot with no bound owner or an unshared session registry, and
    that refusal runs first (see test_owner_bound_at_boot.py and
    test_writable_requires_sqlite_registry.py)."""
    monkeypatch.setenv("WEBGATE_MULTITENANT", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u-owner")
    monkeypatch.setenv("SESSION_REGISTRY_BACKEND", "sqlite")
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        asyncio.run(_jwt_startup_handler()())
