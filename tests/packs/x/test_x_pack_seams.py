"""The x pack's contributions through the core seams: the `/x` owner verb
(`owner.verbs`), the console router with its one public path, and the
`x login` status section."""
import pytest

pytest.importorskip("polyrob_x")

import polyrob_x
from core.packs.spec import StatusSection


def test_the_pack_contributes_the_verb_console_router_and_status_section():
    spec = polyrob_x.pack()
    assert spec.console_routers == ("polyrob_x.console_routes:router",)
    assert spec.hooks["owner.verbs"] == "polyrob_x.owner_verbs:OWNER_VERBS"
    (section,) = spec.status_sections
    assert isinstance(section, StatusSection) and section.name == "x login"


def test_the_status_section_is_lazy_and_secret_free(monkeypatch):
    import polyrob_x.status as st
    monkeypatch.setattr(st, "build", lambda: ["X login (OAuth 2.0, DMs): stored"])
    (section,) = polyrob_x.pack().status_sections
    assert section.build() == ["X login (OAuth 2.0, DMs): stored"]


def test_the_manifest_declares_the_one_public_console_path():
    from core.packs.manifest import read
    man = read("polyrob_x")
    assert man.console_public_paths == ("/oauth/callback",)
    assert {"console_routes", "owner_verbs"} <= man.capabilities


def test_the_loaded_pack_registers_x_on_the_owner_seats():
    from core.packs.loader import console_public_paths, load_packs
    from core.verbs import handler_ref, pack_verb_names, room_refused, verb_for
    load_packs()
    assert verb_for("/x").group == "set up"
    assert handler_ref("telegram", "/x") == "polyrob_x.owner_verbs:telegram_x"
    assert handler_ref("repl", "/x") == "polyrob_x.owner_verbs:repl_x"
    assert "/x" in pack_verb_names() and room_refused("/x")
    assert "/api/packs/x/oauth/callback" in console_public_paths()


def test_the_router_serves_exactly_the_declared_public_path():
    from polyrob_x.console_routes import router
    paths = {r.path for r in router.routes}
    assert "/oauth/callback" in paths and "/oauth/status" in paths
