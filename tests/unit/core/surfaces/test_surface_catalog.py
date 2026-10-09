"""The surface catalog (064 S2b F1): one row per surface, every list derived.

Pins three things:

1. every current surface has exactly one ``SurfaceSpec``;
2. a RATCHET: none of the touch-point modules carries a literal collection of
   two or more surface ids — each list is derived from the catalog, so a new
   surface is one row, not thirteen edits;
3. the derived lists agree with the catalog (owner admin lists every owner
   seat — it listed three of seven before; the forgeable set is exactly the
   forgeable rows; the owner alias set is exactly the alias rows).
"""
import ast
from pathlib import Path

import pytest

from core.surfaces import catalog

REPO = Path(__file__).resolve().parents[4]

CURRENT = ("telegram", "email", "slack", "discord", "signal", "whatsapp")
#: 067 P3b: "x" is the x pack's row (tests/packs/x/test_x_pack.py).

#: The modules that used to hand-carry a surface-id list.
TOUCH_POINTS = (
    "core/surfaces/config.py",
    "core/surfaces/owner_address.py",
    "core/surfaces/owner_admin.py",
    "core/surfaces/access.py",
    "core/instance.py",
    "core/db_manifest.py",
    "core/owner_remedy.py",
    "agents/task/agent/session.py",
    "tools/controller/message_send.py",
    "cron/delivery.py",
    "cli/polyrob.py",
    "cli/commands/gateway.py",
    "cli/surfaces_config.py",
    "cli/update/process_guard.py",
)


def test_every_current_surface_has_exactly_one_row():
    ids = [s.id for s in catalog.SURFACES]
    assert len(ids) == len(set(ids)), f"duplicate catalog rows: {ids}"
    for sid in CURRENT:
        assert ids.count(sid) == 1, f"{sid} has {ids.count(sid)} catalog rows"


def test_rows_are_well_formed():
    for s in catalog.SURFACES:
        assert s.module == f"surfaces.{s.id}", s
        assert s.enabled_flag == f"{s.id.upper()}_SURFACE_ENABLED", s
        assert s.max_message_chars > 0, s
        assert s.transport in ("ws", "webhook", "longpoll", "poll", "sse"), s
        if s.alias_owner:
            assert not s.forgeable, f"{s.id}: a forgeable surface may never alias the owner"
        if s.owner_seat:
            assert s.owner_env, f"{s.id}: an owner seat needs an owner address env"


def _string_ids(node, ids):
    """Surface-id string constants among ``node``'s direct elements (and the
    first element of a nested tuple — the ``(("telegram", …), …)`` table shape)."""
    if isinstance(node, ast.Dict):
        elems = [k for k in node.keys if k is not None]
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        elems = list(node.elts)
    else:
        return []
    found = []
    for e in elems:
        if isinstance(e, (ast.Tuple, ast.List)) and e.elts:
            e = e.elts[0]
        if isinstance(e, ast.Constant) and isinstance(e.value, str) and e.value in ids:
            found.append(e.value)
    return found


def _offenders(rel: str) -> list:
    ids = set(catalog.surface_ids())
    tree = ast.parse((REPO / rel).read_text(), filename=rel)
    return [f"{rel}:{node.lineno} {sorted(set(hits))}"
            for node in ast.walk(tree)
            if len(set(hits := _string_ids(node, ids))) >= 2]


@pytest.mark.parametrize("rel", TOUCH_POINTS)
def test_no_literal_surface_id_list_in_touch_points(rel):
    offenders = _offenders(rel)
    assert not offenders, (
        "a literal list of surface ids — derive it from core/surfaces/catalog.py "
        "instead, so a new surface is one catalog row:\n  " + "\n  ".join(offenders))


#: Runtime tiers the repo-wide scan covers (tests and scripts may name surfaces).
_RUNTIME_TIERS = ("core", "modules", "agents", "tools", "api", "cli", "surfaces",
                  "webview", "cron")


def test_no_literal_surface_id_list_anywhere_in_the_runtime():
    """The touch-point list above is the history; this is the guarantee — no
    runtime module may hand-keep a list of two or more surface ids."""
    offenders = []
    for tier in _RUNTIME_TIERS:
        for py in sorted((REPO / tier).rglob("*.py")):
            offenders.extend(_offenders(py.relative_to(REPO).as_posix()))
    assert not offenders, (
        "a literal list of surface ids — derive it from core/surfaces/catalog.py:"
        "\n  " + "\n  ".join(offenders))


def test_owner_admin_summary_lists_every_owner_seat(monkeypatch):
    from core.surfaces.owner_admin import owner_access_summary as access_summary
    monkeypatch.setattr("core.surfaces.owner_admin._deployed_owner_principal",
                        lambda: None, raising=False)
    summary = access_summary()
    for sid in catalog.owner_seat_ids():
        assert sid in summary["surfaces"], f"{sid} missing from the owner admin summary"


def test_forgeable_set_is_exactly_the_forgeable_rows():
    from core.surfaces.access import FORGEABLE_NETWORK_SURFACES
    assert set(FORGEABLE_NETWORK_SURFACES) == {s.id for s in catalog.SURFACES if s.forgeable}


def test_owner_alias_set_is_unchanged_telegram_only():
    """F1 step 6 decision: data only, behaviour identical. Widening who is OWNER
    is a separate, reviewed change."""
    from core.instance import _OWNER_ALIAS_SURFACES
    assert set(_OWNER_ALIAS_SURFACES) == set(catalog.alias_owner_ids()) == {"telegram"}


def test_owner_chat_surfaces_derive_from_the_catalog():
    from agents.task.agent.session import _OWNER_CHAT_SURFACES
    assert set(_OWNER_CHAT_SURFACES) == set(catalog.owner_chat_ids()) | {"webview"}
    assert "email" not in _OWNER_CHAT_SURFACES


def test_cron_targets_derive_from_the_catalog():
    from cron import delivery
    from core.delivery_channels import channel_names
    assert set(delivery.allowed_targets()) == set(catalog.cron_target_ids()) | set(channel_names())
    pack_rows = {s.id for s in catalog.surfaces() if s.pack and s.cron_target}
    assert set(delivery.router_targets()) == {"slack", "discord", "signal", "whatsapp"} | pack_rows


def test_db_manifest_carries_every_surface_state_db():
    from core.db_manifest import SIDECAR_DB_NAMES
    for name in catalog.state_dbs():
        assert name in SIDECAR_DB_NAMES, name


# --- Accept (064 F1 step 7): one catalog row + one package, zero other edits --

_FAKE_LAUNCH = '''
from surfaces._launch import Launched

RAN = []


async def launch(ctx):
    async def _run():
        RAN.append(ctx.data_dir)
    return Launched(run=_run)
'''

_FAKE_CMD = '''
import click


@click.command()
def fakesurf():
    """Run the fake surface standalone."""
'''


@pytest.fixture
def fake_surface(tmp_path, monkeypatch):
    """A new surface: one package in a temp dir + one catalog row. Nothing else."""
    import importlib
    import sys

    import surfaces
    pkg = tmp_path / "fakesurf"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "launch.py").write_text(_FAKE_LAUNCH)
    (pkg / "cmd.py").write_text(_FAKE_CMD)
    monkeypatch.setattr(surfaces, "__path__", [*surfaces.__path__, str(tmp_path)])
    row = catalog.SurfaceSpec(
        id="fakesurf", label="Fake", module="surfaces.fakesurf",
        enabled_flag="FAKESURF_SURFACE_ENABLED", owner_env="OWNER_FAKESURF_ID",
        owner_seat=True, forgeable=False, transport="ws", extra=None,
        cron_target=True, region="global", max_message_chars=1000,
        cli_command="surfaces.fakesurf.cmd:fakesurf", state_dbs=("fakesurf_dedup.db",))
    monkeypatch.setattr(catalog, "SURFACES", (*catalog.SURFACES, row))
    yield row
    for name in [m for m in sys.modules if m.startswith("surfaces.fakesurf")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


def test_a_new_surface_is_one_row_and_one_package(fake_surface, monkeypatch):
    import os

    from click.testing import CliRunner

    # gateway: launched through its own launch.py with the container data dir
    from tests.unit.cli.test_gateway_command import _patch_gateway_bootstrap
    _patch_gateway_bootstrap(monkeypatch, data_dir="/tmp/fake-home")
    monkeypatch.setenv("FAKESURF_SURFACE_ENABLED", "true")
    from cli.commands import gateway as gw_mod
    res = CliRunner().invoke(gw_mod.gateway, [])
    assert "fakesurf" in res.output, res.output
    import surfaces.fakesurf.launch as fake_launch
    assert fake_launch.RAN == ["/tmp/fake-home"], res.output

    # CLI: `polyrob fakesurf` exists and sits in the Surfaces help group
    from cli.polyrob import cli
    assert "fakesurf" in cli.list_commands(None)
    assert cli.get_command(None, "fakesurf").name == "fakesurf"
    from cli.surfaces_config import gateway_surfaces
    assert "fakesurf" in gateway_surfaces({"FAKESURF_SURFACE_ENABLED": "true"})
    from cli.update.process_guard import _server_subcommands
    assert "fakesurf" in _server_subcommands()

    # cron: a delivery target, through the outbound router
    from cron import delivery
    assert "fakesurf" in delivery.allowed_targets()
    assert "fakesurf" in delivery.router_targets()

    # owner fan-out: `message(target="owner")` reaches it at OWNER_FAKESURF_ID
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "u1")
    monkeypatch.setenv("OWNER_FAKESURF_ID", "fake-owner-1")
    from tools.controller.message_send import build_owner_targets, resolve_message_defaults
    targets = build_owner_targets(None, "u1")
    assert targets.get("fakesurf") == "fake-owner-1"
    assert resolve_message_defaults(None, None, {"fakesurf": "fake-owner-1"})[0] == "fakesurf"

    # owner admin: listed with its flag state
    from core.surfaces.owner_admin import owner_access_summary
    assert owner_access_summary()["surfaces"]["fakesurf"] is True

    # the enabled flag reads through the one seam
    from core.surfaces.config import SurfaceConfig
    assert SurfaceConfig.surface_enabled("fakesurf") is True
    assert os.environ.get("FAKESURF_SURFACE_ENABLED") == "true"


# --- every row's package honours the package contract ----------------------------

def _surface_class(spec):
    import importlib
    import inspect

    from core.surfaces.surface import Surface
    mod = importlib.import_module(f"{spec.module}.surface")
    classes = [c for _n, c in inspect.getmembers(mod, inspect.isclass)
               if issubclass(c, Surface) and c is not Surface and c.__module__ == mod.__name__]
    assert len(classes) == 1, f"{spec.id}: expected one Surface class, got {classes}"
    return classes[0]


@pytest.mark.parametrize("spec", catalog.SURFACES, ids=lambda s: s.id)
def test_every_row_ships_launch_and_probe(spec):
    import importlib
    import inspect
    launch = importlib.import_module(f"{spec.module}.launch").launch
    probe = importlib.import_module(f"{spec.module}.probe").probe
    assert inspect.iscoroutinefunction(launch) and inspect.iscoroutinefunction(probe)


@pytest.mark.parametrize("spec", catalog.SURFACES, ids=lambda s: s.id)
def test_the_row_agrees_with_its_surface_class(spec):
    """The catalog and the Surface must not disagree about the one limit both
    carry, nor about which surface this is."""
    surface = _surface_class(spec)(object())
    assert surface.surface_id == spec.id
    assert surface.capabilities.max_message_bytes == spec.max_message_chars, spec.id


@pytest.mark.parametrize("sid", ["cli", "local", "repl"])
def test_a_pack_row_may_not_take_a_local_owner_id(sid):
    """CHAT-26: `cli`/`local`/`repl` carry the POLYROB_LOCAL owner bypass in
    `core.surfaces.access`; a pack surface with that id would hand its remote
    sender the local owner."""
    row = catalog.SurfaceSpec(
        id=sid, label="X", module="mypack.surf",
        enabled_flag="X_SURFACE_ENABLED", owner_env=None,
        owner_seat=False, forgeable=False, transport="ws", extra=None,
        cron_target=False, region="global", max_message_chars=1000)
    with pytest.raises(ValueError, match="reserved"):
        catalog.validate_surface(row, pack_id="mypack", package="mypack")


def test_reserved_ids_match_the_access_local_owner_set():
    from core.surfaces import access
    assert catalog.RESERVED_LOCAL_IDS == frozenset(access._LOCAL_OWNER_SURFACES)
