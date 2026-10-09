"""Where loaded packs surface (067 P2): the ``polyrob pack`` CLI, a pack's own
CLI command, the skill scope, the API routers."""
import pytest
from click.testing import CliRunner

# Imported before any test registers a pack row: its VALID_TOOL_IDS is an
# import-time snapshot, and a pack id must not leak into it for later tests.
import agents.task.agent.skill_manager  # noqa: F401

from core.packs import state
from tests.unit.core.packs.conftest import write_pack
from tests.unit.core.packs.test_loader import _install, _toml


@pytest.fixture
def echo_cli(use_packs, monkeypatch, tmp_path):
    import cli.commands._bootstrap as boot
    monkeypatch.setattr(boot, "_env_loaded", True)       # never read the dev's env files
    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    loader = use_packs("echo")
    loader.register_policies()                          # what main() does at entry
    from cli.polyrob import cli
    return cli


def test_pack_list_info_doctor(echo_cli):
    r = CliRunner().invoke(echo_cli, ["pack", "list"])
    assert r.exit_code == 0 and "echo 0.1.0 (first-party): loaded" in r.output
    r = CliRunner().invoke(echo_cli, ["pack", "info", "echo"])
    assert r.exit_code == 0
    assert "tool echo: no special capabilities" in r.output and "echo_say: effect=none" in r.output
    r = CliRunner().invoke(echo_cli, ["pack", "doctor"])
    assert r.exit_code == 0, r.output
    r = CliRunner().invoke(echo_cli, ["pack", "info", "nope"])
    assert r.exit_code != 0 and "not installed" in r.output


def test_a_pack_command_runs_through_the_root_group(echo_cli):
    r = CliRunner().invoke(echo_cli, ["echo", "hello"])
    assert r.exit_code == 0, r.output
    assert "echo: hello" in r.output


def test_a_disabled_pack_command_names_why(echo_cli, monkeypatch):
    monkeypatch.setenv("POLYROB_PACKS_DISABLED", "echo")
    r = CliRunner().invoke(echo_cli, ["echo", "hello"])
    assert r.exit_code != 0 and "POLYROB_PACKS_DISABLED" in r.output


def test_enable_and_disable_write_the_flags(echo_cli, tmp_path):
    r = CliRunner().invoke(echo_cli, ["pack", "disable", "echo"])
    assert r.exit_code == 0 and "active in a new process" in r.output
    env = (tmp_path / "home" / ".env").read_text()
    assert "POLYROB_PACKS_DISABLED=echo" in env.replace('"', "").replace("'", "")


def test_disable_ignores_a_project_env(echo_cli, tmp_path, monkeypatch):
    # The CLI never loads ./.polyrob/.env (a cloned directory could supply it),
    # so a value there shadows nothing and earns no "shadowed" note.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".polyrob").mkdir()
    (tmp_path / ".polyrob/.env").write_text("POLYROB_PACKS_DISABLED=other\n")
    result = CliRunner().invoke(echo_cli, ["pack", "disable", "echo"])
    assert result.exit_code == 0, result.output
    assert "shadowed" not in result.output
    assert "disable setting saved" in result.output
    assert "pack echo disabled" not in result.output


def test_install_resolves_index_ids_and_a_first_party_pack_ships_in_polyrob(echo_cli):
    # 067 P7: only index ids resolve. 067 (one install): a first-party pack is
    # never a separate install; here (no x in this scratch install) it names that.
    r = CliRunner().invoke(echo_cli, ["pack", "install", "wallet"])
    assert r.exit_code != 0 and "not in the pack index" in r.output
    r = CliRunner().invoke(echo_cli, ["pack", "install", "x"])
    assert r.exit_code != 0 and "ships inside polyrob" in r.output
    assert "-m pip install" not in r.output
    r = CliRunner().invoke(echo_cli, ["pack", "install", "bad;id"])
    assert r.exit_code != 0 and "is not a pack id" in r.output


def test_pack_skills_are_a_trusted_scope_after_builtin(use_packs, monkeypatch, tmp_path):
    use_packs("echo").load_packs()
    assert state.record("echo").status == state.LOADED, state.record("echo").reason
    from agents.task.agent import skill_store
    from agents.task.agent.skill_manager import SkillManager
    names = [s.name for s in skill_store.resolve_scopes()]
    assert names[-1] == "pack:echo" and names.index("builtin") < names.index("pack:echo")
    scope = skill_store.pack_scopes()[0]
    assert scope.trusted and not scope.writable and skill_store.scan_exempt(scope)
    monkeypatch.setattr(skill_store, "builtin_skill_ids", lambda: ["echo-basics"])
    index = SkillManager()._load_external_skills()
    assert "echo:echo-basics" in index, "a builtin id is protected; the pack skill is namespaced"
    assert "echo-basics" not in index


def test_api_routers_mount_under_the_pack_prefix_and_failures_are_named(use_packs, monkeypatch):
    import logging
    from fastapi import APIRouter, FastAPI
    from api.pack_routes import mount_pack_routers
    loader = use_packs("echo")
    loader.load_packs()
    router = APIRouter()
    router.get("/ping")(lambda: {"ok": True})
    rec = state.record("echo")
    rec.spec = rec.spec.__class__(**{**rec.spec.__dict__, "api_routers": (router, "no.such:mod")})
    app = FastAPI()
    assert mount_pack_routers(app, logging.getLogger("t")) == 1
    from fastapi.testclient import TestClient
    assert TestClient(app).get("/api/packs/echo/ping").json() == {"ok": True}
    assert any("not mounted" in e for e in rec.errors)
    assert "not mounted" in rec.line()


def test_packs_status_section_names_refusals(scratch, tmp_path, monkeypatch):
    from core.status_packs import packs_section
    write_pack(tmp_path, monkeypatch, "old", _toml("old", core="<0.1"))
    _install(scratch, monkeypatch, ["old"])
    scratch.load_packs()
    sec = packs_section()
    assert any("refused" in line and "old" in line for line in sec.lines)
    assert [h.key for h in sec.health] == ["pack_old"]
    assert sec.data["packs"][0]["status"] == state.REFUSED


def test_packs_status_section_when_none_installed(scratch, monkeypatch):
    from core.status_packs import packs_section
    monkeypatch.setattr(scratch, "_entry_points", lambda: [])
    scratch.load_packs()
    assert packs_section().lines == ["no packs installed"]
    assert state.summary_line() == "packs: none installed"


def test_legacy_prefix_is_removed_in_1_3_0(use_packs, monkeypatch):
    """1.2.0 served /api/{polymarket,hyperliquid} deprecated for one release; 1.3.0 removed it."""
    import logging
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient
    import api.pack_routes as pr
    loader = use_packs("echo")
    loader.load_packs()
    router = APIRouter(prefix="/venue")
    router.get("/ping")(lambda: {"ok": True})
    rec = state.record("echo")
    rec.spec = rec.spec.__class__(**{**rec.spec.__dict__, "api_routers": (router,)})
    app = FastAPI()
    pr.mount_pack_routers(app, logging.getLogger("t"))
    client = TestClient(app)
    assert client.get("/api/packs/echo/venue/ping").json() == {"ok": True}
    assert client.get("/api/venue/ping").status_code == 404
    assert not hasattr(pr, "LEGACY_PREFIXES")
