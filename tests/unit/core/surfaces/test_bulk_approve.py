"""E5 (2026-07-13 review, slimmed per owner): bulk correspondent approval.

Per-correspondent manual approval made N-contact outreach an N-command
ceremony. `polyrob owner approve --all [--surface]` approves every pending
binding in one go (tenant-scoped when --user given).
"""
import os
import tempfile

from core.surfaces.correspondents import CorrespondentRegistry


def _reg():
    return CorrespondentRegistry(os.path.join(tempfile.mkdtemp(), "corr.db"))


def test_bulk_approve_all_pending_for_tenant():
    from cli.commands.owner import _do_approve_all
    reg = _reg()
    reg.seed(surface="email", address="a@x.com", session_id="s1", user_id="t1",
             require_approval=True)
    reg.seed(surface="email", address="b@x.com", session_id="s1", user_id="t1",
             require_approval=True)
    reg.seed(surface="x", address="123", session_id="s1", user_id="t1",
             require_approval=True)
    reg.seed(surface="email", address="other@x.com", session_id="s9", user_id="t2",
             require_approval=True)

    n = _do_approve_all(reg, user_id="t1", surface="email")
    assert n == 2
    assert reg.resolve(surface="email", address="a@x.com") is not None
    assert reg.resolve(surface="x", address="123") is None, "surface filter respected"
    assert reg.resolve(surface="email", address="other@x.com") is None, "tenant scoped"

    n2 = _do_approve_all(reg, user_id="t1")
    assert n2 == 1  # the remaining x:123
    assert reg.resolve(surface="x", address="123") is not None


def test_bulk_approve_unscoped_covers_all_tenants():
    from cli.commands.owner import _do_approve_all
    reg = _reg()
    reg.seed(surface="email", address="a@x.com", session_id="s1", user_id="t1",
             require_approval=True)
    reg.seed(surface="email", address="b@x.com", session_id="s2", user_id="t2",
             require_approval=True)
    assert _do_approve_all(reg) == 2


def test_bulk_approve_none_pending_returns_zero():
    from cli.commands.owner import _do_approve_all
    assert _do_approve_all(_reg()) == 0
