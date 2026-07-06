"""polyrob owner allow/deny/allowlist — thin handlers over OutboundAllowlist."""
import os
import tempfile

from core.identity import resolve_identity
from core.surfaces.outbound_allowlist import OutboundAllowlist
from cli.commands.owner import _do_allow, _do_deny, _do_allowlist, _allowlist_tenant


def test_allow_and_list():
    al = OutboundAllowlist(os.path.join(tempfile.mkdtemp(), "a.db"))
    _do_allow(al, "rob", "telegram", "555", note="team")
    rows = _do_allowlist(al, "rob")
    assert any(r["target"] == "555" for r in rows)


def test_deny():
    al = OutboundAllowlist(os.path.join(tempfile.mkdtemp(), "a.db"))
    _do_allow(al, "rob", "telegram", "555")
    assert _do_deny(al, "rob", "telegram", "555") is True
    assert al.is_allowed("rob", "telegram", "555") is False


def test_deny_unknown_returns_false():
    al = OutboundAllowlist(os.path.join(tempfile.mkdtemp(), "a.db"))
    assert _do_deny(al, "rob", "telegram", "999") is False


def test_allowlist_scoped_by_tenant():
    al = OutboundAllowlist(os.path.join(tempfile.mkdtemp(), "a.db"))
    _do_allow(al, "rob", "telegram", "555")
    _do_allow(al, "other", "telegram", "777")
    rows = _do_allowlist(al, "rob")
    assert len(rows) == 1
    assert rows[0]["target"] == "555"


def test_allowlist_tenant_matches_session_identity_oracle():
    # The CLI's default tenant for allow/deny/allowlist MUST equal the tenant a local
    # REPL session reads at runtime (core.identity.resolve_identity()), NOT the
    # instance id ("rob") that _owner_tenant defaults to when no owner is bound.
    assert _allowlist_tenant(None) == resolve_identity()


def test_allowlist_tenant_round_trips_with_session_identity():
    al = OutboundAllowlist(os.path.join(tempfile.mkdtemp(), "a.db"))
    _do_allow(al, _allowlist_tenant(None), "telegram", "555")
    assert al.is_allowed(resolve_identity(), "telegram", "555") is True
