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
        self.user_id = "alice"
        self.orchestrator = type("O", (), {"workspace_dir": workspace})()


def _ctx():
    return type("Ctx", (), {"user_id": "alice", "role": "orchestrator",
                            "is_sub_agent": False, "session_id": "s",
                            "metadata": {}})()


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _write_pfp(home, instance="alice"):
    from core.avatar import set_avatar
    return set_avatar(home, instance, PNG, source="file:face.png").path


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setattr("core.instance.resolve_instance_id", lambda *a, **k: "alice")
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
    (avatar_dir(home, "alice") / "avatar.json").write_text("{broken")
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
    st = load_avatar(home, "alice")
    assert st.is_set and st.source == "file:new.png"
    assert st.path.read_bytes() == PNG + b"new"
    # set_from writes the slot only; it must not claim the profile photos changed
    assert "every surface" not in res.extracted_content
    # 120: the reply names the agent's own push, never a command it cannot run
    assert "agent_avatar push=[]" in res.extracted_content
    assert "polyrob avatar push" not in res.extracted_content


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
    assert load_avatar(home, "alice").is_default  # unchanged


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
    assert load_avatar(home, "alice").is_default  # unchanged


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


def test_the_action_holds_no_push_procedure_of_its_own(home):
    """120 W1 (owner decision D1): the agent may push, through the ONE procedure
    (`modules.avatar.push.push_avatar`) the CLI and `/avatar push` run. It never
    calls a platform itself, and it names no command the agent cannot run."""
    import inspect
    from tools.controller import avatar_action
    src = inspect.getsource(avatar_action)
    assert "push_avatar" in src
    for forbidden in ("push_twitter", "push_discord", "push_telegram", "pushed.json",
                      "polyrob avatar push", "bool_env"):
        assert forbidden not in src


# --- push / clear (120 W1) --------------------------------------------------

@pytest.fixture
def owner_turn(monkeypatch):
    monkeypatch.setattr("core.security.owner_turn.owner_turn_refusal", lambda *a, **k: None)


@pytest.fixture
def pushes(monkeypatch):
    """The platform calls, recorded."""
    calls = []
    monkeypatch.setattr("modules.avatar.push.push_twitter", lambda p: calls.append("twitter"))
    monkeypatch.setattr("modules.avatar.push.push_discord", lambda p: calls.append("discord"))
    monkeypatch.setattr("modules.avatar.push.push_telegram_bot", lambda p: calls.append("telegram"))
    monkeypatch.setattr("modules.avatar.push.push_telegram_chat",
                        lambda p, chat: calls.append(f"telegram:{chat}"))
    return calls


@pytest.mark.asyncio
async def test_push_updates_every_account_and_is_idempotent(home, owner_turn, pushes):
    _write_pfp(home)
    fn, model = _action(_Controller(home))
    res = await fn(model(push=[]), _ctx())
    assert res.error is None, res.error
    assert pushes == ["twitter", "discord", "telegram"]
    assert "twitter: profile image updated" in res.extracted_content
    again = await fn(model(push=["x"]), _ctx())
    assert "twitter: unchanged, skipped" in again.extracted_content
    assert pushes == ["twitter", "discord", "telegram"]        # no second call


@pytest.mark.asyncio
async def test_push_skips_an_account_that_is_not_connected(home, owner_turn, pushes, monkeypatch):
    from modules.avatar import push as push_mod

    def no_creds(p):
        raise push_mod.TwitterCredsMissing("missing Twitter OAuth1 creds: TWITTER_API_KEY")
    monkeypatch.setattr("modules.avatar.push.push_twitter", no_creds)
    _write_pfp(home)
    fn, model = _action(_Controller(home))
    res = await fn(model(push=[]), _ctx())
    assert "twitter: not connected, skipped" in res.extracted_content
    assert "discord: bot avatar updated" in res.extracted_content      # the others still run
    assert pushes == ["discord", "telegram"]


@pytest.mark.asyncio
async def test_set_and_push_in_one_call_sets_first(home, tmp_path, owner_turn, pushes):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "new.png").write_bytes(PNG + b"new")
    fn, model = _action(_Controller(home, workspace=str(ws)))
    res = await fn(model(set_from="new.png", push=["telegram"]), _ctx())
    assert res.error is None, res.error
    assert "set (file:new.png)" in res.extracted_content
    assert "telegram: bot photo updated" in res.extracted_content
    assert "keep the old image" not in res.extracted_content


@pytest.mark.asyncio
async def test_a_failed_set_pushes_nothing(home, tmp_path, owner_turn, pushes):
    fn, model = _action(_Controller(home, workspace=str(tmp_path)))
    res = await fn(model(set_from="missing.png", push=[]), _ctx())
    assert res.error and "refused" in res.error and pushes == []


@pytest.mark.asyncio
async def test_push_of_an_svg_is_refused_with_the_reason(home, owner_turn, pushes):
    from core.avatar import set_avatar
    set_avatar(home, "alice", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', source="x")
    fn, model = _action(_Controller(home))
    res = await fn(model(push=[]), _ctx())
    assert "SVG" in res.extracted_content and pushes == []


@pytest.mark.asyncio
async def test_a_room_photo_needs_a_room_the_owner_put_me_in(home, owner_turn, pushes,
                                                             monkeypatch):
    from tools.controller import avatar_action
    _write_pfp(home)
    monkeypatch.setattr(avatar_action, "_allowed_telegram_rooms", lambda c, h: {"-1001"})
    fn, model = _action(_Controller(home))
    res = await fn(model(push=["telegram:-1009"]), _ctx())
    assert "not a Telegram room the owner put me in" in res.extracted_content
    assert pushes == []
    ok = await fn(model(push=["telegram:-1001"]), _ctx())
    assert "chat -1001 photo updated" in ok.extracted_content
    assert pushes == ["telegram:-1001"]            # the room only: the bot photo was not named

    def _boom(c, h):
        raise OSError("locked")
    monkeypatch.setattr(avatar_action, "_allowed_telegram_rooms", _boom)
    bad = await fn(model(push=["telegram:-1001"]), _ctx())
    assert "could not be read" in bad.extracted_content       # unreadable, never "not a room"


@pytest.mark.asyncio
async def test_clear_returns_the_default_mark(home, owner_turn):
    _write_pfp(home)
    fn, model = _action(_Controller(home))
    res = await fn(model(clear=True), _ctx())
    assert "avatar: cleared" in res.extracted_content and "default" in res.extracted_content
    from core.avatar import load_avatar
    assert load_avatar(home, "alice").is_default
    both = await fn(model(clear=True, set_from="x.png"), _ctx())
    assert both.error and "opposites" in both.error


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"push": []}, {"clear": True}])
async def test_push_and_clear_follow_the_set_rule(home, pushes, params, monkeypatch):
    """A leaf, a forged turn and an autonomous run never change the face."""
    _write_pfp(home)
    fn, model = _action(_Controller(home))
    leaf = _ctx()
    leaf.is_sub_agent = True
    res = await fn(model(**params), leaf)
    assert res.error and "denied" in res.error
    monkeypatch.setattr("core.security.owner_turn.owner_turn_refusal", lambda *a, **k: None)
    monkeypatch.setattr("agents.task.session_class.is_autonomous_session", lambda sid: True)
    # A standing job is no exception: a goal the agent made itself is one too.
    monkeypatch.setattr("core.security.owner_turn.is_owner_standing_run", lambda ctx: True)
    res = await fn(model(**params), _ctx())
    assert res.error and "autonomous" in res.error
    from core.avatar import load_avatar
    assert not load_avatar(home, "alice").is_default and pushes == []


def test_the_registration_note_never_says_now_for_a_snapshot(monkeypatch):
    """120 T6: a `data:` URI registration is a snapshot."""
    from tools.controller import avatar_action
    monkeypatch.setenv("A2A_BASE_URL", "http://localhost:9000")
    note = avatar_action._registration_note()
    assert "snapshot" in note and "set_agent_uri" in note and "shows it now" not in note
    monkeypatch.setenv("A2A_BASE_URL", "https://agent.example")
    monkeypatch.delenv("EIP8004_AGENT_URI_MODE", raising=False)
    assert "shows it now" in avatar_action._registration_note()


def test_the_action_has_no_switch(home, monkeypatch):
    monkeypatch.setenv("AVATAR_TOOL_ENABLED", "false")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    from tools.controller.avatar_action import register_avatar_action
    c = _Controller(home)
    register_avatar_action(c)
    assert "agent_avatar" in c.registry.actions


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
