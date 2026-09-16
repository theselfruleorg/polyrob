"""The `agent_avatar` action — the agent can finally SEE and SEND its own face.

Before this, the Mindprint identity reached exactly one runtime output (the x402
invoice card) and the agent had no way to read or use it. `avatar/` and
`modules/pfp/` were, from the agent's side, dark.

Two halves, deliberately narrow:

- **read** — instance, kept/draft/absent, tier, traits, voice signature.
- **attach** — copy `pfp.png` into the SESSION WORKSPACE and return the path, so
  the EXISTING `message(media_paths=[…])` rail carries it with every screen
  intact (size cap, secret filter, threat scan, workspace confinement).

⚠️ Attach copies INTO the workspace on purpose. `core/surfaces/attachments.py::
validate_media_paths` requires every media path to resolve inside the session
workspace; special-casing one file would weaken that confinement rule for every
caller. One rule, no exception.

⚠️ The action can never generate, randomize, keep or push. `keep` is permanent
and irreversible, so the identity ceremony stays the owner's.
"""
import json

import pytest


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, data_dir, workspace=None):
        self.registry = _Registry()
        self.container = type("C", (), {
            "config": type("Cfg", (), {"data_dir": str(data_dir)})()})()
        self.user_id = "rob"
        self.orchestrator = type("O", (), {"workspace_dir": workspace})()


def _ctx():
    return type("Ctx", (), {"user_id": "rob", "role": "orchestrator",
                            "is_sub_agent": False, "session_id": "s",
                            "metadata": {}})()


def _write_pfp(home, instance="rob", *, locked=True):
    d = home / "identity" / instance / "pfp"
    d.mkdir(parents=True, exist_ok=True)
    d.joinpath("pfp.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 64)
    d.joinpath("pfp.json").write_text(json.dumps({
        "generator": "mindprint@v2", "seed": "POLYROB", "variant": "#a1b2",
        "instance_id": instance, "seed_hex": "0x1546", "locked": locked,
        "traits": {"tier": "rare", "eyes": "square", "mouth": "grin"},
        "voice": {"pitch": 1.29, "rate": 1.02, "timbre": 0.78},
    }))
    return d


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AVATAR_TOOL_ENABLED", "true")
    monkeypatch.setattr("core.instance.resolve_instance_id", lambda *a, **k: "rob")
    return tmp_path


def _action(c):
    from tools.controller.avatar_action import register_avatar_action
    register_avatar_action(c)
    assert "agent_avatar" in c.registry.actions, "action was not registered"
    fn, model, desc = c.registry.actions["agent_avatar"]
    return fn, model


# --- read ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_it_reports_its_own_traits_and_voice(home):
    _write_pfp(home)
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    body = res.extracted_content
    assert "rare" in body and "0x1546" in body
    assert "1.29" in body, "the voice signature must be reported"
    assert "kept" in body.lower()


@pytest.mark.asyncio
async def test_a_draft_identity_is_named_as_a_draft(home):
    _write_pfp(home, locked=False)
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    assert "draft" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_no_avatar_is_an_honest_answer_not_an_error(home):
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    assert "not set up" in res.extracted_content.lower()
    assert res.error is None


# --- attach ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_attach_copies_the_face_into_the_session_workspace(home, tmp_path):
    _write_pfp(home)
    ws = tmp_path / "ws"
    ws.mkdir()
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    res = await fn(model(attach=True), _ctx())
    copied = ws / "avatar.png"
    assert copied.is_file(), "the face was not materialised in the workspace"
    assert copied.read_bytes() == (home / "identity" / "rob" / "pfp" / "pfp.png").read_bytes()
    assert "avatar.png" in res.extracted_content


@pytest.mark.asyncio
async def test_the_attached_path_passes_the_real_media_validator(home, tmp_path):
    """⚠️ The whole point: the copy must satisfy the EXISTING confinement rule,
    not a relaxed one. If this fails, `message(media_paths=[…])` would refuse
    the very path the action just handed the agent."""
    from core.surfaces.attachments import validate_media_paths
    _write_pfp(home)
    ws = tmp_path / "ws"
    ws.mkdir()
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    await fn(model(attach=True), _ctx())
    validated, err = validate_media_paths(["avatar.png"], str(ws))
    assert err is None, err
    assert validated


@pytest.mark.asyncio
async def test_attach_without_a_workspace_says_so(home):
    _write_pfp(home)
    c = _Controller(home, workspace=None)
    fn, model = _action(c)
    res = await fn(model(attach=True), _ctx())
    assert "workspace" in res.extracted_content.lower()


@pytest.mark.asyncio
async def test_attach_with_no_avatar_does_not_claim_success(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    res = await fn(model(attach=True), _ctx())
    assert not (ws / "avatar.png").exists()
    assert "not set up" in res.extracted_content.lower()


# --- what it must NEVER do -------------------------------------------------

def test_the_action_exposes_no_mutating_verb(home):
    """`keep` is permanent. The identity ceremony is the owner's, so no
    generate/randomize/keep/push may be reachable from a model turn."""
    import inspect
    from tools.controller import avatar_action
    src = inspect.getsource(avatar_action)
    for forbidden in ("generate_pfp", "keep_pfp", "shuffle_face", "shuffle_voice",
                      "push_twitter", "push_discord", "random_config"):
        assert forbidden not in src, (
            f"{forbidden} is reachable from the agent-facing action")


def test_the_flag_off_registers_nothing(home, monkeypatch):
    monkeypatch.setenv("AVATAR_TOOL_ENABLED", "false")
    from tools.controller.avatar_action import register_avatar_action
    c = _Controller(home)
    register_avatar_action(c)
    assert "agent_avatar" not in c.registry.actions


def test_the_module_has_no_future_annotations_import():
    """⚠️ The registry introspects the closure's first-param annotation to route
    the validated param model. `from __future__ import annotations` stringizes
    it and breaks the routing (GLM live-test bug 2026-06-20)."""
    import ast
    import pathlib
    from tools.controller import avatar_action
    # Parse, don't grep: the module's own docstring WARNS about this import, so
    # a substring check trips on the warning instead of on the statement.
    tree = ast.parse(pathlib.Path(avatar_action.__file__).read_text(encoding="utf-8"))
    futures = [n for n in ast.walk(tree)
               if isinstance(n, ast.ImportFrom) and n.module == "__future__"]
    assert not futures, "from __future__ import annotations breaks registry routing"


def test_it_is_registered_from_service_not_action_registration():
    """⚠️ action_registration.py is AT its size ratchet (1971 = its exact
    ceiling, zero slack), so even a four-line delegator fails the ratchet."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4]
    service = (root / "tools" / "controller" / "service.py").read_text(encoding="utf-8")
    assert "register_avatar_action" in service
    areg = (root / "tools" / "controller" / "action_registration.py").read_text(encoding="utf-8")
    assert "register_avatar_action" not in areg
