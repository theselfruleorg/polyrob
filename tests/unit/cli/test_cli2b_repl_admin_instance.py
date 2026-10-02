"""CLI2 on the instance axis (2026-10-03 audit follow-up).

``polyrob owner pending`` reads the review queue under ``admin_instance_id`` —
the instance the DEPLOYED service runs (``rob``), adopted from the deployed env
file when the shell is silent. The REPL ``/pending``, ``/approve`` and ``/reject``
read ``resolve_instance_id`` (the ``polyrob`` default in an owner's SSH shell),
so they listed and decided a different instance's queue.
"""
import types

import pytest

import core.admin_data_home as adh


@pytest.fixture
def deployed_instance(tmp_path, monkeypatch):
    env = tmp_path / "polyrob.env"
    env.write_text("POLYROB_INSTANCE_ID=rob\n")
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(env))
    for key in ("POLYROB_INSTANCE_ID", "BOT_INSTANCE_ID", "POLYROB_PROFILE",
                "POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_LOCAL_OWNER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    seen = []

    def _all_pending(*, user_id, home_dir, instance_id):
        seen.append(instance_id)
        return types.SimpleNamespace(items=[], unavailable=[],
                                     degraded_line=lambda: "")
    from tools.controller import approval_queue
    monkeypatch.setattr(approval_queue, "all_pending", _all_pending)
    return seen


def _ctx(*args):
    out = []
    ctx = types.SimpleNamespace(user_id="local", args=list(args))
    ctx.emit = lambda *a, **k: out.append(a)
    return ctx


def test_seam_adopts_the_deployed_instance(deployed_instance):
    from cli._admin_home import admin_instance
    assert admin_instance() == "rob"


def test_repl_pending_reads_the_deployed_instance(deployed_instance):
    from cli.ui.commands.h_pending import _h_pending as h_pending
    h_pending(_ctx())
    assert deployed_instance == ["rob"]


def test_repl_approve_reads_the_deployed_instance(deployed_instance):
    from cli.ui.commands import h_gates
    h_gates._list_pending(_ctx())
    assert deployed_instance == ["rob"]


def test_repl_reject_reads_the_deployed_instance(deployed_instance):
    from cli.ui.commands.h_reject import h_reject
    h_reject(_ctx())
    assert deployed_instance == ["rob"]
