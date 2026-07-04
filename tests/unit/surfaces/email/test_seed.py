"""WS-B auto-seed guardrails (Fusion must-fix #5).

When the agent contacts a NEW address, a correspondent binding is seeded — but:
- disabled entirely when the access model is off;
- owner-provenance + approval-required (default) -> PENDING (owner must ratify);
- owner-provenance + approval-off -> active;
- UNTRUSTED-provenance -> always pending (no self-bootstrapped trust);
- the per-tenant per-day cap refuses runaway seeding.
"""
import os
import tempfile

import pytest

from core.surfaces.correspondents import CorrespondentRegistry
from surfaces.email.seed import maybe_seed_correspondent


class _Container:
    def __init__(self, registry):
        self._registry = registry

    def get_service(self, name):
        return self._registry if name == "correspondent_registry" else None


@pytest.fixture()
def reg():
    yield CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))


def _seed(container, **over):
    base = dict(surface="email", address="john@acme.com", session_id="s1",
                user_id="u_owner", thread_id="t1", provenance="owner")
    base.update(over)
    return maybe_seed_correspondent(container, **base)


def test_disabled_when_flag_off(reg, monkeypatch):
    monkeypatch.delenv("CORRESPONDENT_ACCESS_ENABLED", raising=False)
    assert _seed(_Container(reg)) == "disabled"


def test_owner_provenance_pending_by_default(reg, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.delenv("CORRESPONDENT_REQUIRE_APPROVAL", raising=False)
    assert _seed(_Container(reg)) == "pending"


def test_owner_provenance_active_when_approval_off(reg, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    assert _seed(_Container(reg)) == "active"


def test_untrusted_provenance_always_pending(reg, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")  # even so:
    assert _seed(_Container(reg), provenance="untrusted") == "pending"


def test_per_day_cap_refuses(reg, monkeypatch):
    monkeypatch.setenv("CORRESPONDENT_ACCESS_ENABLED", "true")
    monkeypatch.setenv("CORRESPONDENT_REQUIRE_APPROVAL", "false")
    monkeypatch.setenv("CORRESPONDENT_MAX_NEW_PER_DAY", "1")
    assert _seed(_Container(reg), address="a@x.com", thread_id="t1") == "active"
    assert _seed(_Container(reg), address="b@x.com", thread_id="t2") == "refused"
