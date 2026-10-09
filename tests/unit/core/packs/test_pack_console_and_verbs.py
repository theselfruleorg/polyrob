"""The console seam (``PackSpec.console_routers`` + ``[console] public_paths``) and
the owner-verb hook (``owner.verbs`` -> ``core.verbs.register_verbs``)."""
import pytest

from core.packs import state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _install, _toml

ROUTER_MOD = '''from fastapi import APIRouter
router = APIRouter()
@router.get("/cb")
def cb():
    return {"public": True}
@router.get("/private")
def private():
    return {"private": True}
@router.post("/form")
def form():
    return {"posted": True}
'''


def _console_pack(tmp_path, monkeypatch, pid="cw", public=("/cb",), tier="first-party",
                  routers=True, caps=("tools", "console_routes")):
    toml = _toml(pid, tier=tier).replace('capabilities = ["tools"]',
                                         f"capabilities = {list(caps)!r}".replace("'", '"'))
    if public:
        toml += "\n[console]\npublic_paths = [" + ", ".join(f'"{p}"' for p in public) + "]\n"
    extra = f", console_routers=('polyrob_{pid}.routes:router',)" if routers else ""
    init = ("from core.packs.spec import PackSpec, ToolContribution\n"
            f"def pack():\n    return PackSpec(id={pid!r}, tools=(ToolContribution("
            f"id={pid + 't'!r}, registrar=lambda: True),){extra})\n")
    pkg = write_pack(tmp_path, monkeypatch, pid, toml, init)
    (pkg / "routes.py").write_text(ROUTER_MOD)
    return pkg


def test_console_routers_and_public_paths_are_read_from_loaded_packs(scratch, tmp_path,
                                                                     monkeypatch):
    _console_pack(tmp_path, monkeypatch)
    _install(scratch, monkeypatch, ["cw"])
    scratch.load_packs()
    assert state.record("cw").status == state.LOADED, state.record("cw").reason
    assert scratch.console_routers() == [("cw", "polyrob_cw.routes:router")]
    assert scratch.console_public_paths() == ["/api/packs/cw/cb"]


def test_a_disabled_pack_opens_no_public_path(scratch, tmp_path, monkeypatch):
    _console_pack(tmp_path, monkeypatch)
    _install(scratch, monkeypatch, ["cw"])
    monkeypatch.setenv("POLYROB_PACKS_DISABLED", "cw")
    scratch.load_packs()
    assert scratch.console_routers() == [] and scratch.console_public_paths() == []


@pytest.mark.parametrize("kw, match", [
    ({"routers": False}, "console_routes differs"),
    ({"public": (), "caps": ("tools",)}, "console_routes differs"),
])
def test_the_capability_must_match_the_code(scratch, tmp_path, monkeypatch, kw, match):
    _console_pack(tmp_path, monkeypatch, **kw)
    _install(scratch, monkeypatch, ["cw"])
    scratch.load_packs()
    rec = state.record("cw")
    assert rec.status == state.REFUSED and match in rec.reason


def test_a_third_party_pack_cannot_declare_a_public_path(scratch, tmp_path, monkeypatch):
    _console_pack(tmp_path, monkeypatch, tier="third-party")
    _install(scratch, monkeypatch, ["cw"])
    scratch.register_policies()
    rec = state.record("cw")
    assert rec.status == state.REFUSED and "public console paths" in rec.reason


# --- the console mount (webview/pack_console.py) ------------------------------

@pytest.fixture
def console(scratch, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import webview.pack_console as pc
    _console_pack(tmp_path, monkeypatch, public=("/cb", "/nothing-serves-this"))
    _install(scratch, monkeypatch, ["cw"])
    pc.reset_for_tests()
    app = FastAPI()
    @app.middleware("http")
    async def fixture_owner(request, call_next):
        if request.headers.get("x-test-owner") == "yes":
            from api.auth_state import set_auth_state
            set_auth_state(request.state, user_id="local", tier="admin", role="owner",
                           authenticated=True, payment_method=None)
        return await call_next(request)
    assert pc.mount_pack_console_routers(app) == 1
    yield pc, TestClient(app)
    pc.reset_for_tests()


def test_owner_posture_and_public_carve_out(console, monkeypatch):
    pc, client = console
    # A declared path that no route serves opens nothing.
    assert pc.public_paths() == frozenset({"/api/packs/cw/cb"})
    assert pc.is_public("/api/packs/cw/cb", "GET") and pc.is_public("/api/packs/cw/cb", "HEAD")
    assert not pc.is_public("/api/packs/cw/cb", "POST")
    assert not pc.is_public("/api/packs/cw/cb/", "GET")
    assert not pc.is_public("/api/packs/cw/private", "GET")
    monkeypatch.setenv("POLYROB_POSTURE", "own_ops")
    # No authenticated owner on request.state: only the public GET answers.
    assert client.get("/api/packs/cw/cb").json() == {"public": True}
    assert client.get("/api/packs/cw/private").status_code == 403
    assert client.post("/api/packs/cw/form").status_code == 403
    # Local posture also needs an authenticated owner.
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    assert client.get("/api/packs/cw/private").status_code == 403
    client.headers["x-test-owner"] = "yes"
    assert client.get("/api/packs/cw/private").json() == {"private": True}


def test_a_read_only_console_refuses_a_pack_mutation(console, monkeypatch):
    _pc, client = console
    monkeypatch.setenv("POLYROB_POSTURE", "local")
    monkeypatch.setenv("WEBVIEW_READ_ONLY", "true")
    client.headers["x-test-owner"] = "yes"
    assert client.post("/api/packs/cw/form").status_code == 403
    assert client.get("/api/packs/cw/private").status_code == 200


def test_a_broken_console_router_is_named_on_its_pack(scratch, tmp_path, monkeypatch):
    from fastapi import FastAPI
    import webview.pack_console as pc
    pkg = _console_pack(tmp_path, monkeypatch)
    (pkg / "routes.py").write_text("raise RuntimeError('router broke')\n")
    _install(scratch, monkeypatch, ["cw"])
    pc.reset_for_tests()
    failures = []
    assert pc.mount_pack_console_routers(FastAPI(), lambda n, e: failures.append(n)) == 0
    assert failures == ["pack cw console"]
    assert any("not mounted" in e for e in state.record("cw").errors)
    assert pc.public_paths() == frozenset()
    pc.reset_for_tests()


# --- the owner.verbs hook --------------------------------------------------------

@pytest.fixture
def verbs_scratch(monkeypatch):
    from core import verbs as V
    monkeypatch.setattr(V, "_REGISTERED", list(V._REGISTERED))
    monkeypatch.setattr(V, "_HANDLERS", dict(V._HANDLERS))
    monkeypatch.setattr(V, "_ROOM_OK", set(V._ROOM_OK))
    yield V
    V._rebuild()


def _verb_pack(tmp_path, monkeypatch, pid, hooks, caps=("tools", "owner_verbs")):
    toml = _toml(pid).replace('capabilities = ["tools"]',
                              f"capabilities = {list(caps)!r}".replace("'", '"'))
    toml = toml.replace("requires_packs = []", "requires_packs = []\ndelivery_channels = []")
    init = ("from core.packs.spec import PackSpec, ToolContribution\n"
            "from core.verbs import Verb\n"
            f"ROWS = {{'rows': (Verb('/{pid}v', 'set up', 'a pack verb'),),\n"
            f"        'handlers': {{'telegram': {{'/{pid}v': 'polyrob_{pid}:h'}}}}}}\n"
            "async def h(**kw):\n    return 'ok'\n"
            f"def pack():\n    return PackSpec(id={pid!r}, tools=(ToolContribution("
            f"id={pid + 't'!r}, registrar=lambda: True),), hooks={hooks})\n")
    write_pack(tmp_path, monkeypatch, pid, toml, init)


def test_owner_verbs_register_through_the_one_table(scratch, verbs_scratch, tmp_path,
                                                    monkeypatch):
    V = verbs_scratch
    _verb_pack(tmp_path, monkeypatch, "vp", "{'owner.verbs': ROWS}")
    _install(scratch, monkeypatch, ["vp"])
    scratch.load_packs()
    assert state.record("vp").status == state.LOADED, state.record("vp").reason
    assert V.verb_for("/vpv").group == "set up"
    assert V.handler_ref("telegram", "/vpv") == "polyrob_vp:h"
    assert "/vpv" in V.routed_names() and "/vpv" in V.pack_verb_names()
    assert V.room_refused("/vpv")
    assert [r.name for r in V.registered_verbs("pack:vp")] == ["/vpv"]


def test_owner_verbs_need_the_capability(scratch, verbs_scratch, tmp_path, monkeypatch):
    _verb_pack(tmp_path, monkeypatch, "vq", "{'owner.verbs': ROWS}", caps=("tools",))
    _install(scratch, monkeypatch, ["vq"])
    scratch.load_packs()
    rec = state.record("vq")
    assert rec.status == state.REFUSED and "owner_verbs differs" in rec.reason
    assert verbs_scratch.verb_for("/vqv") is None


def test_a_later_failing_hook_rolls_the_verbs_back(scratch, verbs_scratch, tmp_path,
                                                   monkeypatch):
    V = verbs_scratch
    _verb_pack(tmp_path, monkeypatch, "vr",
               "{'owner.verbs': ROWS, 'cron.delivery_channel': {'undeclared': print}}")
    _install(scratch, monkeypatch, ["vr"])
    scratch.load_packs()
    rec = state.record("vr")
    assert rec.status == state.REFUSED and "not declared" in rec.reason
    assert V.verb_for("/vrv") is None and V.handler_ref("telegram", "/vrv") is None


def test_unregister_verbs_is_exact(verbs_scratch):
    V = verbs_scratch
    V.register_verbs([V.Verb("/zzv", "look", "one")], {"repl": {"/zzv": "m:a"}},
                     source="pack:zz")
    V.unregister_verbs("pack:nobody")
    assert V.verb_for("/zzv") is not None
    V.unregister_verbs("pack:zz")
    assert V.verb_for("/zzv") is None and V.handler_ref("repl", "/zzv") is None
