"""CLI2 (2026-10-03 audit): the REPL owner verbs act on the DEPLOYED owner tenant.

On the box, an owner's SSH shell carries none of the service's environment, so
the REPL session resolves the tenant ``local`` while the running daemon writes
under the deployment's ``POLYROB_OWNER_USER_ID``. ``polyrob owner …`` adopted
that tenant (``admin_owner_principal``); the REPL owner verbs read the shell
tenant, so ``/pending``, ``/goals``, ``/inbox`` and ``/cards`` came back empty and
``/allow`` wrote rows the service never reads.
"""
import types

import pytest

import core.admin_data_home as adh


@pytest.fixture
def deployed_owner(tmp_path, monkeypatch):
    env = tmp_path / "polyrob.env"
    env.write_text("POLYROB_OWNER_USER_ID=tg_owner\n")
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(env))
    for key in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_LOCAL_OWNER"):
        monkeypatch.delenv(key, raising=False)
    return "tg_owner"


def _ctx(user_id="local"):
    return types.SimpleNamespace(user_id=user_id, args=[])


def test_seam_adopts_the_deployed_owner(deployed_owner):
    from cli._admin_home import admin_owner_tenant
    assert admin_owner_tenant("local") == deployed_owner
    assert admin_owner_tenant(None) == deployed_owner


def test_seam_keeps_an_explicit_other_tenant(deployed_owner):
    from cli._admin_home import admin_owner_tenant
    assert admin_owner_tenant("alice") == "alice"


def test_seam_is_silent_on_a_local_box(tmp_path, monkeypatch):
    monkeypatch.setattr(adh, "DEPLOYED_ENV_FILE", str(tmp_path / "absent.env"))
    for key in ("POLYROB_OWNER_USER_ID", "BOT_OWNER_USER_ID",
                "SURFACE_SUPER_ADMIN_USER_IDS", "POLYROB_LOCAL_OWNER"):
        monkeypatch.delenv(key, raising=False)
    from cli._admin_home import admin_owner_tenant
    assert admin_owner_tenant("local") == "local"


@pytest.mark.parametrize("mod", ["cli.ui.commands.h_owner", "cli.ui.commands.h_cards",
                                 "cli.ui.commands.h_cron"])
def test_repl_owner_tenants_use_the_seam(deployed_owner, mod):
    import importlib
    assert importlib.import_module(mod)._tenant(_ctx()) == deployed_owner


def test_repl_inbox_reads_the_deployed_owner(deployed_owner, monkeypatch):
    from cli.ui.commands import h_inbox
    seen = {}
    monkeypatch.setattr(h_inbox, "build", lambda uid, home: seen.setdefault("uid", uid) and {})
    monkeypatch.setattr(h_inbox, "_home", lambda ctx: "/tmp/x")
    ctx = _ctx()
    ctx.emit = lambda *a, **k: None
    h_inbox.h_inbox(ctx)
    assert seen["uid"] == deployed_owner


def test_repl_goals_reads_the_deployed_owner(deployed_owner, monkeypatch):
    from cli.ui.commands import h_goals_view, handlers
    seen = {}
    monkeypatch.setattr(h_goals_view, "goals_view",
                        lambda uid: seen.setdefault("uid", uid) or "")
    ctx = _ctx()
    ctx.emit = lambda *a, **k: None
    handlers._h_goals(ctx)
    assert seen["uid"] == deployed_owner
