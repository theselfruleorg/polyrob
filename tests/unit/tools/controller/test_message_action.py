import asyncio, os, tempfile
from pathlib import Path

import pytest
from core.surfaces.outbound_allowlist import OutboundAllowlist
from core.surfaces.outbound_target import resolve_target_tier

# The action closure is thin; we test the decision+route contract via a small helper
# that Task 5 factors out: tools.controller.message_send.perform_message_send
from tools.controller.message_send import perform_message_send, _validate_media_paths
from tools.controller.action_registration import _is_forged_or_autonomous_turn


class _FakeExecutionContext:
    def __init__(self, is_sub_agent=False, role="orchestrator", metadata=None,
                 session_id="s1"):
        self.is_sub_agent = is_sub_agent
        self.role = role
        self.metadata = metadata or {}
        self.session_id = session_id


class _FakeControllerSelf:
    """Stand-in for the Controller (`self`) the message closure passes in."""
    _is_sub_agent = False
    session_id = "s1"


def test_forged_turn_guard_blocks_sub_agent():
    ctx = _FakeExecutionContext(is_sub_agent=True)
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is True


def test_forged_turn_guard_blocks_self_wake_reentry():
    # SK-F10 shape: role='orchestrator', is_sub_agent=False (looks like a genuine
    # owner turn) but the drained turn_kind marks it as a forged self-wake re-entry.
    ctx = _FakeExecutionContext(is_sub_agent=False, role="orchestrator",
                                 metadata={"turn_kind": "self_wake"})
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is True


def test_forged_turn_guard_blocks_delegation_result_reentry():
    ctx = _FakeExecutionContext(is_sub_agent=False, role="orchestrator",
                                 metadata={"turn_kind": "delegation_result"})
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is True


def test_forged_turn_guard_allows_genuine_owner_turn():
    ctx = _FakeExecutionContext(is_sub_agent=False, role="orchestrator", metadata={})
    assert _is_forged_or_autonomous_turn(ctx, _FakeControllerSelf()) is False

class _Router:
    def __init__(self, media_out=None):
        self.sent = []
        self.media_sent = []
        self._media_out = media_out or set()

    async def send_message(self, chat_id, text, surface_id="telegram", media=None):
        self.sent.append((surface_id, chat_id, text))
        self.media_sent.append(media)
        return True

    def capabilities(self, surface_id):
        from core.surfaces.envelopes import SurfaceCapabilities
        return SurfaceCapabilities(media_out=surface_id in self._media_out)

def _al(tmp): return OutboundAllowlist(os.path.join(tmp, "a.db"))

def test_denied_does_not_send():
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="555", text="hi", action="send"))
    assert res["success"] is False and res["tier"] == "denied" and router.sent == []

def test_owner_sends():
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send"))
    assert res["success"] is True and res["tier"] == "owner" and router.sent[0] == ("telegram","999","hi")

def test_allowlisted_sends():
    tmp = tempfile.mkdtemp(); al = _al(tmp); al.allow("rob","telegram","555")
    router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=al, owner_targets={"telegram":"999"},
        user_id="rob", surface="telegram", target="555", text="hi", action="send"))
    assert res["success"] is True and res["tier"] == "allowlisted"


def test_owner_alias_resolves_to_real_owner_target():
    """The agent has no way to learn the raw owner chat_id, so it naturally types
    target='owner' — live-observed reaching the Telegram API verbatim and failing
    'chat not found' under an open outbound policy (the tier gate doesn't catch an
    unresolved literal target in that mode). 'owner' must resolve to the real
    owner_targets[surface] address before tier resolution, same as passing the
    real id directly."""
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="owner", text="hi", action="send"))
    assert res["success"] is True and res["tier"] == "owner"
    assert router.sent[0] == ("telegram", "999", "hi")


def test_owner_alias_case_and_whitespace_insensitive():
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target=" Owner ", text="hi", action="send"))
    assert res["success"] is True and res["tier"] == "owner"


def test_owner_alias_falls_back_to_denied_when_no_owner_target_for_surface():
    """No owner address configured for this surface -> the alias can't resolve, so
    the literal string is passed through unchanged (same as before this fix) and
    denied normally — never silently sent somewhere wrong."""
    tmp = tempfile.mkdtemp(); router = _Router()
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={},
        user_id="rob", surface="telegram", target="owner", text="hi", action="send"))
    assert res["success"] is False and res["tier"] == "denied" and router.sent == []


# --- media_paths (Task 7): workspace-confined validation + capability-gated delivery ---

def test_media_path_outside_workspace_is_rejected_and_router_not_called(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"x")
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=[str(outside)], session_id="sess1"))
    assert res["success"] is False
    assert "media" in res["error"].lower()
    assert router.sent == []


def test_media_path_traversal_component_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=["../../etc/passwd"], session_id="sess1"))
    assert res["success"] is False
    assert router.sent == []


def test_media_path_inside_workspace_is_sent_to_router(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    from agents.task.path import pm
    ws = pm().get_workspace_dir("sess1", "rob")
    inside = Path(ws) / "card.png"
    inside.write_bytes(b"x")
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=[str(inside)], session_id="sess1"))
    assert res["success"] is True
    assert router.sent[0] == ("telegram", "999", "hi")
    assert router.media_sent[0] == [
        {"kind": "image", "path": str(inside.resolve()), "caption": None}
    ]


def test_media_on_nonmedia_surface_carries_honest_note_and_still_sends_text(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    from agents.task.path import pm
    ws = pm().get_workspace_dir("sess1", "rob")
    inside = Path(ws) / "card.png"
    inside.write_bytes(b"x")
    tmp = tempfile.mkdtemp(); router = _Router(media_out=set())  # no surface supports media
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=[str(inside)], session_id="sess1"))
    assert res["success"] is True
    assert res["tier"] == "owner"
    assert router.sent[0] == ("telegram", "999", "hi")
    assert router.media_sent[0] is None
    assert "does not support media" in res["note"]


def test_no_media_paths_keeps_legacy_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send"))
    assert res["success"] is True
    assert "note" not in res
    assert router.media_sent[0] is None


# --- _validate_media_paths: symlink-escape + null-byte hardening (security follow-up) ---

def test_validate_media_paths_rejects_symlink_escape_via_realpath(tmp_path):
    """Regression guard: a symlink INSIDE the workspace pointing to a file OUTSIDE
    it must be rejected. This only holds because _validate_media_paths resolves
    os.path.realpath() on the candidate and checks the RESOLVED path against the
    workspace prefix -- a naive string-prefix check on the raw path would pass this
    (the symlink's own path is inside the workspace). If the realpath-resolution +
    post-resolution prefix check were ever removed/weakened, this test fails."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("outside-the-workspace")
    escape_link = workspace / "escape.png"
    os.symlink(secret, escape_link)

    validated, err = _validate_media_paths([str(escape_link)], str(workspace))

    assert validated is None
    assert err is not None
    assert "outside" in err.lower() or "workspace" in err.lower()


def test_validate_media_paths_accepts_real_file_inside_workspace(tmp_path):
    """Positive control paired with the symlink-escape test above: a plain regular
    file that genuinely lives inside the workspace must still be accepted."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_file = workspace / "card.png"
    real_file.write_bytes(b"x")

    validated, err = _validate_media_paths([str(real_file)], str(workspace))

    assert err is None
    assert validated == [os.path.realpath(str(real_file))]


def test_validate_media_paths_rejects_null_byte_path_gracefully(tmp_path):
    """FINDING 2: an embedded-null-byte path (no literal '..', so it clears the
    traversal check) makes os.path.realpath() raise ValueError. That must be
    caught and turned into the same graceful (None, error) rejection tuple every
    other bad path gets -- never an unhandled exception bubbling out of the
    Controller action."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    validated, err = _validate_media_paths(["card.png\x00.txt"], str(workspace))

    assert validated is None
    assert err is not None


def test_forged_and_nonallowlisted_refusals_hold_with_media_paths(monkeypatch, tmp_path):
    """Media doesn't bypass the owner-allowlist gate: a denied target is still denied,
    and the forged/autonomous-turn refusal (tested above via _is_forged_or_autonomous_turn)
    is enforced upstream of perform_message_send in the action closure — media_paths
    changes nothing about that gate."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="555", text="hi", action="send",
        media_paths=["card.png"], session_id="sess1"))
    assert res["success"] is False and res["tier"] == "denied" and router.sent == []


# --- attach screening (QW-1, 2026-07-19): size cap + secret filter + threat scan ---

def test_media_oversize_file_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    monkeypatch.setenv("MESSAGE_MEDIA_MAX_MB", "0.001")  # 1 KB cap (message-tool cap)
    from agents.task.path import pm
    ws = pm().get_workspace_dir("sess1", "rob")
    big = Path(ws) / "big.bin"
    big.write_bytes(b"x" * 4096)
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=[str(big)], session_id="sess1"))
    assert res["success"] is False
    assert "cap" in res["error"].lower()
    assert router.sent == []


def test_media_secret_shaped_file_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    from agents.task.path import pm
    ws = pm().get_workspace_dir("sess1", "rob")
    env_file = Path(ws) / "polyrob.env"
    env_file.write_text("OPENAI_API_KEY=sk-secret")
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=[str(env_file)], session_id="sess1"))
    assert res["success"] is False
    assert "secret" in res["error"].lower()
    assert router.sent == []


def test_media_send_result_acknowledges_attachment(monkeypatch, tmp_path):
    """Overnight 2026-07-19 live finding: the result string was attachment-blind
    ('message[owner] -> telegram:owner OK' with or without media), so the agent
    couldn't tell the file went, retried ~12x and declared BLOCKED. The result
    must NAME the attached files."""
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    from agents.task.path import pm
    ws = pm().get_workspace_dir("sess1", "rob")
    inside = Path(ws) / "note.md"
    inside.write_text("hello")
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        media_paths=[str(inside)], session_id="sess1"))
    assert res["success"] is True
    assert res.get("media_attached") == ["note.md"]


def test_no_media_send_has_no_media_attached_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    tmp = tempfile.mkdtemp(); router = _Router(media_out={"telegram"})
    res = asyncio.run(perform_message_send(
        router=router, allowlist=_al(tmp), owner_targets={"telegram": "999"},
        user_id="rob", surface="telegram", target="999", text="hi", action="send",
        session_id="sess1"))
    assert res["success"] is True
    assert "media_attached" not in res
