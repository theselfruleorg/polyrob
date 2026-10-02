"""The `agent_avatar` action — the agent reads, sends and (on an owner turn) sets
its own avatar image (the one slot in `core/avatar.py`; core generates no face).

- **read** — set (and from where) / not set / unreadable.
- **attach** — copy the image into the SESSION WORKSPACE and return the path, so
  the EXISTING `message(media_paths=[…])` rail carries it with every screen
  intact (size cap, secret filter, threat scan, workspace confinement).
- **set_from** — a workspace file, a URL or an NFT; refused on any turn that is
  not the owner's.
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


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _write_pfp(home, instance="rob"):
    from core.avatar import set_avatar
    return set_avatar(home, instance, PNG, source="file:face.png").path


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
async def test_it_reports_its_avatar_and_where_it_came_from(home):
    _write_pfp(home)
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    body = res.extracted_content
    assert "avatar: set (file:face.png)" in body
    assert "traits" not in body and "voice" not in body


@pytest.mark.asyncio
async def test_an_unreadable_record_is_not_reported_as_absent(home):
    from core.avatar import avatar_dir
    _write_pfp(home)
    (avatar_dir(home, "rob") / "avatar.json").write_text("{broken")
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    assert "unreadable" in res.extracted_content
    assert "not set" not in res.extracted_content


@pytest.mark.asyncio
async def test_an_unset_slot_reads_as_the_default(home):
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    assert "the default" in res.extracted_content.lower()
    assert "polyrob avatar set" in res.extracted_content
    assert res.error is None


@pytest.mark.asyncio
async def test_attach_with_the_default_copies_the_default_mark(home, tmp_path):
    from core.avatar import DEFAULT_AVATAR
    ws = tmp_path / "ws"
    ws.mkdir()
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    res = await fn(model(attach=True), _ctx())
    assert (ws / "avatar.png").read_bytes() == DEFAULT_AVATAR.read_bytes()
    assert "attached" in res.extracted_content


@pytest.mark.asyncio
async def test_no_avatar_is_an_honest_answer_not_an_error(home, monkeypatch, tmp_path):
    monkeypatch.setattr("core.avatar.DEFAULT_AVATAR", tmp_path / "missing.png")
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    assert "not set" in res.extracted_content.lower()
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
    assert copied.read_bytes() == PNG
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
async def test_attach_with_no_avatar_does_not_claim_success(home, tmp_path, monkeypatch):
    monkeypatch.setattr("core.avatar.DEFAULT_AVATAR", tmp_path / "missing.png")
    ws = tmp_path / "ws"
    ws.mkdir()
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    res = await fn(model(attach=True), _ctx())
    assert not (ws / "avatar.png").exists()
    assert "not set" in res.extracted_content.lower()


# --- set_from ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_from_a_workspace_file_on_an_owner_turn(home, tmp_path, monkeypatch):
    monkeypatch.setattr("core.security.owner_turn.owner_turn_refusal",
                        lambda *a, **k: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "new.png").write_bytes(PNG + b"new")
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    res = await fn(model(set_from="new.png"), _ctx())
    assert res.error is None, res.error
    from core.avatar import load_avatar
    st = load_avatar(home, "rob")
    assert st.is_set and st.source == "file:new.png"
    assert st.path.read_bytes() == PNG + b"new"


@pytest.mark.asyncio
async def test_set_from_a_path_outside_the_workspace_is_refused(home, tmp_path, monkeypatch):
    monkeypatch.setattr("core.security.owner_turn.owner_turn_refusal",
                        lambda *a, **k: None)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG)
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    for ref in (str(outside), "../outside.png"):
        res = await fn(model(set_from=ref), _ctx())
        assert res.error and "refused" in res.error
    from core.avatar import load_avatar
    assert load_avatar(home, "rob").is_default  # unchanged


@pytest.mark.asyncio
async def test_set_from_is_refused_on_a_non_owner_turn(home, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "new.png").write_bytes(PNG)
    c = _Controller(home, workspace=str(ws))
    fn, model = _action(c)
    ctx = _ctx()
    ctx.is_sub_agent = True
    res = await fn(model(set_from="new.png"), ctx)
    assert res.error and "denied" in res.error
    from core.avatar import load_avatar
    assert load_avatar(home, "rob").is_default  # unchanged


@pytest.mark.asyncio
async def test_set_from_is_refused_on_a_forged_turn(home, tmp_path):
    from core.security.forged_turns import FORGED_TURN_KINDS
    c = _Controller(home, workspace=str(tmp_path))
    fn, model = _action(c)
    ctx = _ctx()
    ctx.metadata = {"turn_kind": next(iter(FORGED_TURN_KINDS))}
    res = await fn(model(set_from="x.png"), ctx)
    assert res.error and "denied" in res.error


@pytest.mark.asyncio
async def test_set_from_is_refused_on_an_autonomous_run(home, tmp_path, monkeypatch):
    monkeypatch.setattr("core.security.owner_turn.owner_turn_refusal",
                        lambda *a, **k: None)
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session",
                        lambda sid: True)
    c = _Controller(home, workspace=str(tmp_path))
    fn, model = _action(c)
    res = await fn(model(set_from="x.png"), _ctx())
    assert res.error and "autonomous" in res.error


def test_the_action_reaches_no_push(home):
    """Pushing the image to X / Discord is an owner CLI act, never a model turn."""
    import inspect
    from tools.controller import avatar_action
    src = inspect.getsource(avatar_action)
    for forbidden in ("push_twitter", "push_discord"):
        assert forbidden not in src


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
