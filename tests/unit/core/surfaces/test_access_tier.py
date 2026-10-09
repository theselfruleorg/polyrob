"""WS-A access-tier resolver — OWNER / CORRESPONDENT / DENIED.

Resolved ONCE at the routing boundary. Invariants (Fusion-validated):
- tier = authenticated sender (never thread membership);
- owner-or-paired -> OWNER; a non-owner with an ACTIVE correspondent binding ->
  CORRESPONDENT; everyone else -> DENIED;
- anonymous (empty user_id) -> DENIED;
- group / multi-party -> DENIED in v1 (single-principal envelope);
- fail-closed: a fault never UPGRADES a sender to OWNER (downgrade is safe).
"""
import os
import tempfile

import pytest

from core.surfaces.access import AccessTier, resolve_access_tier
from core.surfaces.correspondents import CorrespondentRegistry
from core.surfaces.envelopes import Identity, SessionSource


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir, registry=None):
        self.config = _Cfg(data_dir)
        self._services = {"correspondent_registry": registry} if registry else {}

    def get_service(self, name):
        return self._services.get(name)


def _identity(user_id, *, raw=None, surface="email", chat_type="dm", authenticated=True):
    # `authenticated` models the receiving MX's Authentication-Results having
    # proven the From address (CHAT-6); the tier rules below assume it.
    return Identity(
        user_id=user_id,
        source=SessionSource(surface_id=surface, chat_id="c1", chat_type=chat_type),
        raw_user_id=raw if raw is not None else user_id,
        sender_authenticated=authenticated,
    )


@pytest.fixture()
def workdir():
    yield tempfile.mkdtemp()


def test_anonymous_is_denied(workdir):
    c = _Container(workdir)
    assert resolve_access_tier(c, _identity(""), env={}) == AccessTier.DENIED


def test_owner_principal_match_is_owner(workdir):
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_owner"), env=env) == AccessTier.OWNER


def test_non_owner_without_binding_is_denied(workdir):
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_stranger"), env=env) == AccessTier.DENIED


def test_non_owner_with_active_binding_is_correspondent(workdir):
    reg = CorrespondentRegistry(os.path.join(workdir, "corr.db"))
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner", require_approval=False)
    c = _Container(workdir, registry=reg)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    ident = _identity("u_john", raw="john@acme.com")
    assert resolve_access_tier(c, ident, thread_id="t1", env=env) == AccessTier.CORRESPONDENT


def test_pending_binding_is_not_correspondent(workdir):
    reg = CorrespondentRegistry(os.path.join(workdir, "corr.db"))
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner", require_approval=True)
    c = _Container(workdir, registry=reg)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    ident = _identity("u_john", raw="john@acme.com")
    assert resolve_access_tier(c, ident, thread_id="t1", env=env) == AccessTier.DENIED


def test_group_is_denied_in_v1(workdir):
    reg = CorrespondentRegistry(os.path.join(workdir, "corr.db"))
    c = _Container(workdir, registry=reg)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    # even the owner in a group is denied in v1 (no per-author tiering yet)
    ident = _identity("u_owner", chat_type="group")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.DENIED


def test_paired_user_is_owner_tier(workdir):
    from core.pairing import PairingStore
    store = PairingStore(os.path.join(workdir, "pairing.db"))
    code = store.request("u_paired")
    assert store.approve(code) == "u_paired"
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    # A pairing row is keyed on the sender address, so it is only evidence on a
    # surface whose address is authenticated (telegram here).
    assert resolve_access_tier(c, _identity("u_paired", surface="telegram"),
                               env=env) == AccessTier.OWNER


def test_paired_user_is_never_owner_on_a_forgeable_surface(workdir):
    """D1: a ``From:`` header is spoofable, so a pairing row keyed on an email
    address proves nothing. The pairing branch used to run for every surface,
    which made a paired address an OWNER command-turn on email."""
    from core.pairing import PairingStore
    store = PairingStore(os.path.join(workdir, "pairing.db"))
    code = store.request("u_paired")
    assert store.approve(code) == "u_paired"
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_paired", surface="email"),
                               env=env) == AccessTier.DENIED


def test_local_mode_owns_local_surface_only_not_network(workdir):
    # local single-user mode owns the CLI/local surface...
    c = _Container(workdir)
    env = {"POLYROB_LOCAL": "true"}
    cli_ident = _identity("u_local", surface="cli")
    assert resolve_access_tier(c, cli_ident, env=env) == AccessTier.OWNER
    # ...but NOT a forgeable network sender (email) — that would be an open command channel
    assert resolve_access_tier(c, _identity("u_anyone"), env=env) == AccessTier.DENIED


def test_aliased_telegram_owner_is_owner_tier(workdir):
    """Seam 7 (owner-instance identity model): the Telegram owner, aliased at inbound
    to the OWNER principal (`rob`), must resolve to OWNER tier on the *network* telegram
    surface — WITHOUT the local-owner bypass (telegram ∉ {cli,local,repl}). So the
    owner can steer + use gated tools once POLYROB_OWNER_USER_ID=rob is bound.
    """
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "rob"}
    ident = _identity("rob", raw="28436760", surface="telegram")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.OWNER


def test_non_owner_telegram_sender_isolated_not_owner(workdir):
    """A different authenticated telegram sender keeps its own hashed tenant and is NOT
    the owner (no correspondent binding -> DENIED). Tenant isolation is intact.
    """
    c = _Container(workdir)
    env = {"POLYROB_OWNER_USER_ID": "rob"}
    ident = _identity("u_66170da11e7a74ac54e2bfaa", raw="99887766", surface="telegram")
    assert resolve_access_tier(c, ident, env=env) == AccessTier.DENIED


def test_registry_fault_fails_closed_to_denied_not_owner(workdir):
    class _Boom:
        def resolve(self, **kw):
            raise RuntimeError("db down")

    c = _Container(workdir, registry=_Boom())
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    ident = _identity("u_stranger", raw="john@acme.com")
    assert resolve_access_tier(c, ident, thread_id="t1", env=env) == AccessTier.DENIED


def test_a_pack_surface_registered_after_import_is_forgeable(workdir, monkeypatch):
    """AC6: FORGEABLE_NETWORK_SURFACES was frozen at import, so a pack surface
    with ``forgeable=true`` loaded later still honoured a PAIRING row keyed on
    its forgeable sender address. The resolver must read the catalog live."""
    import core.surfaces.access as access
    import core.surfaces.catalog as catalog
    real = catalog.forgeable_ids
    monkeypatch.setattr(catalog, "forgeable_ids",
                        lambda: frozenset(real()) | {"packmail"})
    assert "packmail" not in access.FORGEABLE_NETWORK_SURFACES  # the frozen copy
    assert access.is_forgeable_surface("packmail")
    seen = {}

    def spy(container, uid, env, *, allow_local, allow_pairing=True):
        seen["allow_pairing"] = allow_pairing
        return False

    monkeypatch.setattr(access, "_is_owner_or_paired", spy)
    resolve_access_tier(_Container(workdir), _identity("u_x", surface="packmail"),
                        env={})
    assert seen["allow_pairing"] is False


@pytest.mark.parametrize("auth", [None, False])
def test_unauthenticated_email_sender_is_denied_even_as_owner_or_correspondent(workdir, auth):
    """CHAT-6: a From: the MX did not authenticate is the lowest tier."""
    reg = CorrespondentRegistry(os.path.join(workdir, "corr.db"))
    reg.seed(surface="email", address="john@acme.com", session_id="s1",
             user_id="u_owner", thread_id="t1", provenance="owner", require_approval=False)
    c = _Container(workdir, registry=reg)
    env = {"POLYROB_OWNER_USER_ID": "u_owner"}
    assert resolve_access_tier(c, _identity("u_john", raw="john@acme.com"),
                               thread_id="t1", env=env) == AccessTier.CORRESPONDENT
    assert resolve_access_tier(c, _identity("u_owner", authenticated=auth),
                               env=env) == AccessTier.DENIED
    assert resolve_access_tier(c, _identity("u_john", raw="john@acme.com", authenticated=auth),
                               thread_id="t1", env=env) == AccessTier.DENIED
