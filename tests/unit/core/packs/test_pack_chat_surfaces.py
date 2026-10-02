"""067 P3b: a first-party pack may contribute chat-surface catalog rows and cron
delivery channels. Rows are DATA (phase 1, no pack import); a third-party pack
that declares a surface is refused; a disabled pack's surface and channel are
absent with a named reason."""
import asyncio

import pytest

from core.packs import state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _install

SURF = '''id = "{id}"
version = "0.1.0"
pack_api = 1
tier = "{tier}"
capabilities = [{caps}]
delivery_channels = ["{id}post"]

[cli]
commands = ["{id}"]

[surfaces.{id}]
label = "Zed"
module = "{module}"
enabled_flag = "{up}_SURFACE_ENABLED"
owner_env = "OWNER_{up}_ID"
owner_seat = true
forgeable = false
transport = "poll"
cron_target = true
region = "global"
max_message_chars = 1000
alias_owner = {alias}
credentials = ["{up}_TOKEN"]
alt_required = [["{up}_OTHER"]]
cli_command = "polyrob_{id}.cmd:{id}"
state_dbs = ["{id}_dedup.db"]
'''

INIT = '''import click
from core.packs.spec import PackSpec


@click.command(name={id!r})
def cmd():
    """Run it."""


async def post(task_agent, job, final):
    SENT.append(final)
    return True


SENT = []


def pack():
    return PackSpec(id={id!r}, cli=(cmd,),
                    hooks={{"cron.delivery_channel": {{{id!r} + "post": post}}}})
'''


def _pack(tmp_path, monkeypatch, pid, *, tier="first-party", module=None, alias="false"):
    toml = SURF.format(id=pid, up=pid.upper(), tier=tier, caps='"surface"',
                       module=module or f"polyrob_{pid}.surface", alias=alias)
    write_pack(tmp_path, monkeypatch, pid, toml, init=INIT.format(id=pid))


def test_phase_one_registers_the_row_without_importing_the_pack(scratch, tmp_path,
                                                                monkeypatch):
    import sys
    from core.surfaces import catalog
    _pack(tmp_path, monkeypatch, "zed")
    _install(scratch, monkeypatch, ["zed"])
    scratch.register_policies()
    assert state.record("zed").status == state.INSTALLED, state.record("zed").reason
    row = catalog.get("zed")
    assert row is not None and row.pack == "zed"
    assert row.credentials == ("ZED_TOKEN",) and row.alt_required == (("ZED_OTHER",),)
    assert "zed" in catalog.owner_seat_ids() and "zed" in catalog.cron_target_ids()
    assert "zed_dedup.db" in catalog.state_dbs()
    assert "zed" in catalog.cli_commands() and "zed" not in catalog.cli_commands(core_only=True)
    assert "polyrob_zed" not in sys.modules


def test_a_third_party_pack_may_not_contribute_a_surface(scratch, tmp_path, monkeypatch):
    from core.surfaces import catalog
    _pack(tmp_path, monkeypatch, "zed", tier="third-party")
    _install(scratch, monkeypatch, ["zed"])
    scratch.register_policies()
    rec = state.record("zed")
    assert rec.status == state.REFUSED and "may not contribute a chat surface" in rec.reason
    assert catalog.get("zed") is None


@pytest.mark.parametrize("kw,needle", [
    ({"alias": "true"}, "may not alias the owner"),
    ({"module": "surfaces.telegram"}, "is not in the pack's package"),
])
def test_security_columns_and_foreign_modules_are_refused(scratch, tmp_path, monkeypatch,
                                                          kw, needle):
    from core.surfaces import catalog
    _pack(tmp_path, monkeypatch, "zed", **kw)
    _install(scratch, monkeypatch, ["zed"])
    scratch.register_policies()
    rec = state.record("zed")
    assert rec.status == state.REFUSED and needle in rec.reason, rec.reason
    assert catalog.get("zed") is None


def test_a_core_surface_id_cannot_be_taken(scratch, tmp_path, monkeypatch):
    _pack(tmp_path, monkeypatch, "telegram")
    _install(scratch, monkeypatch, ["telegram"])
    scratch.register_policies()
    assert "already in the catalog" in state.record("telegram").reason


def test_loaded_pack_serves_surface_and_channel(scratch, tmp_path, monkeypatch):
    from core import delivery_channels as dc
    from core.surfaces import catalog
    from cron import delivery
    _pack(tmp_path, monkeypatch, "zed")
    _install(scratch, monkeypatch, ["zed"])
    scratch.load_packs()
    assert state.record("zed").status == state.LOADED, state.record("zed").reason
    assert catalog.withheld_reason("zed") is None and catalog.get("zed") is not None
    assert "zedpost" in delivery.allowed_targets() and dc.unavailable_reason("zedpost") is None

    class _Job:
        id, user_id, session_id = "j1", "u1", ""
    out = asyncio.run(delivery.deliver_result_ex(object(), _Job(), "report", target="zedpost"))
    assert out == "sent"
    import polyrob_zed
    assert polyrob_zed.SENT == ["report"]


def test_disabled_pack_surface_and_channel_are_absent_with_a_reason(scratch, tmp_path,
                                                                    monkeypatch, caplog):
    from core import delivery_channels as dc
    from core.surfaces import catalog
    from cron import delivery
    monkeypatch.setenv("POLYROB_PACKS_DISABLED", "zed")
    _pack(tmp_path, monkeypatch, "zed")
    _install(scratch, monkeypatch, ["zed"])
    scratch.load_packs()
    assert state.record("zed").status == state.DISABLED
    assert catalog.get("zed") is None and "zed" not in catalog.surface_ids()
    assert "zed" not in catalog.owner_seat_ids()
    why = catalog.withheld_reason("zed")
    assert why and "pack 'zed'" in why and "POLYROB_PACKS_DISABLED" in why
    assert [s.id for s, _ in catalog.withheld_surfaces()] == ["zed"]
    # the backup set and the server-process guard still see it
    assert "zed_dedup.db" in catalog.state_dbs() and "zed" in catalog.cli_commands()
    # the channel stays a known target and reports why it cannot deliver
    assert "zedpost" in delivery.allowed_targets()
    assert "pack 'zed'" in dc.unavailable_reason("zedpost")

    class _Job:
        id, user_id, session_id = "j1", "u1", ""
    with caplog.at_level("WARNING"):
        out = asyncio.run(delivery.deliver_result_ex(object(), _Job(), "report",
                                                     target="zedpost"))
    assert out == "unavailable"
    assert "POLYROB_PACKS_DISABLED" in caplog.text


def test_an_undeclared_channel_refuses_the_pack(scratch, tmp_path, monkeypatch):
    _pack(tmp_path, monkeypatch, "zed")
    toml = (tmp_path / "polyrob_zed" / "pack.toml")
    toml.write_text(toml.read_text().replace('delivery_channels = ["zedpost"]',
                                             'delivery_channels = []'))
    _install(scratch, monkeypatch, ["zed"])
    scratch.load_packs()
    rec = state.record("zed")
    assert rec.status == state.REFUSED and "not declared" in rec.reason


def test_no_pack_declares_the_channel():
    from core import delivery_channels as dc
    assert "no installed pack provides" in dc.unavailable_reason("nosuchchannel")
