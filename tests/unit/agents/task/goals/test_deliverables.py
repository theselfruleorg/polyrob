"""QW-1/QW-3 (2026-07-19, proposal 021): a completed goal's owner push carries
its deliverables — attached within caps, listed server-only otherwise, plus a
webview deep link. Producer: agents/task/goals/deliverables.py; consumer:
GoalDispatcher._notify_owner_done/_completion_text.
"""
import asyncio

import pytest

from agents.task.goals.board import Goal
from agents.task.goals.dispatcher import GoalDispatcher


def _ws(monkeypatch, tmp_path, session_id="sess-d", user_id="rob"):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    from agents.task.path import pm
    return pm().get_workspace_dir(session_id, user_id)


# ---------------------------------------------------------------------------
# build_deliverables
# ---------------------------------------------------------------------------

def test_build_deliverables_attaches_written_files(monkeypatch, tmp_path):
    from agents.task.goals.deliverables import build_deliverables
    ws = _ws(monkeypatch, tmp_path)
    from pathlib import Path
    (Path(ws) / "x402-recon.md").write_text("# recon\nplain findings")
    artifacts = [
        {"kind": "filesystem_write_file",
         "detail": '{\n  "success": true,\n  "filepath": "x402-recon.md"'},
        {"path": "x402-recon.md", "bytes": 22, "mtime": 1},
    ]
    attachments, lines = build_deliverables(artifacts, "sess-d", "rob")
    assert len(attachments) == 1
    assert attachments[0]["kind"] == "document"
    assert attachments[0]["path"].endswith("x402-recon.md")
    assert any("x402-recon.md" in ln and "attached" in ln for ln in lines)


def test_build_deliverables_ledger_attribution_filters_other_runs(monkeypatch, tmp_path):
    """Shared project-root workspaces pollute the scan with OTHER goals' files
    (assessment §3.9) — when write descriptors exist, only THEIR files ATTACH.
    Review follow-up (Important #2): unattributed files are still LISTED
    (server-only) — never silently vanished; the 021 contract says every
    artifact is accounted for."""
    from agents.task.goals.deliverables import build_deliverables
    ws = _ws(monkeypatch, tmp_path)
    from pathlib import Path
    (Path(ws) / "mine.md").write_text("mine")
    (Path(ws) / "other.md").write_text("someone else's")
    artifacts = [
        {"kind": "filesystem_write_file", "detail": '"filepath": "mine.md"'},
        {"path": "mine.md", "bytes": 4, "mtime": 1},
        {"path": "other.md", "bytes": 14, "mtime": 1},
    ]
    attachments, lines = build_deliverables(artifacts, "sess-d", "rob")
    assert [a["path"].endswith("mine.md") for a in attachments] == [True]
    other = [ln for ln in lines if "other.md" in ln]
    assert other and "server-only" in other[0] and "unattributed" in other[0]


def test_build_deliverables_attributes_all_write_kinds(monkeypatch, tmp_path):
    """fs_write / coding-tool descriptors count as write attribution too —
    a run that produced chart.png via a non-filesystem write must still attach it."""
    from agents.task.goals.deliverables import build_deliverables
    ws = _ws(monkeypatch, tmp_path)
    from pathlib import Path
    (Path(ws) / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    artifacts = [
        {"kind": "fs_write", "detail": '"file_path": "chart.png"'},
        {"path": "chart.png", "bytes": 16, "mtime": 1},
    ]
    attachments, lines = build_deliverables(artifacts, "sess-d", "rob")
    assert len(attachments) == 1 and attachments[0]["path"].endswith("chart.png")


def test_attached_line_carries_absolute_server_path(monkeypatch, tmp_path):
    """Review Important #3: the text may outlive the attachment (quiet-hold /
    capped / fallback re-delivery is text-only), so the attached line itself
    must carry a reachable server path."""
    from agents.task.goals.deliverables import build_deliverables
    ws = _ws(monkeypatch, tmp_path)
    from pathlib import Path
    (Path(ws) / "r.md").write_text("r")
    attachments, lines = build_deliverables(
        [{"path": "r.md", "bytes": 1, "mtime": 1}], "sess-d", "rob")
    assert len(attachments) == 1
    assert any("attached" in ln and str(Path(ws).resolve()) in ln for ln in lines)


def test_build_deliverables_oversize_listed_server_only(monkeypatch, tmp_path):
    from agents.task.goals.deliverables import build_deliverables
    monkeypatch.setenv("DELIVERABLES_ATTACH_MAX_MB", "0.001")  # 1 KB
    ws = _ws(monkeypatch, tmp_path)
    from pathlib import Path
    (Path(ws) / "big.bin").write_bytes(b"x" * 4096)
    artifacts = [{"path": "big.bin", "bytes": 4096, "mtime": 1}]
    attachments, lines = build_deliverables(artifacts, "sess-d", "rob")
    assert attachments == []
    assert any("big.bin" in ln and "server-only" in ln for ln in lines)


def test_build_deliverables_respects_max_files(monkeypatch, tmp_path):
    from agents.task.goals.deliverables import build_deliverables
    monkeypatch.setenv("DELIVERABLES_ATTACH_MAX_FILES", "1")
    ws = _ws(monkeypatch, tmp_path)
    from pathlib import Path
    (Path(ws) / "a.md").write_text("a")
    (Path(ws) / "b.md").write_text("b")
    artifacts = [{"path": "a.md", "bytes": 1, "mtime": 1},
                 {"path": "b.md", "bytes": 1, "mtime": 1}]
    attachments, lines = build_deliverables(artifacts, "sess-d", "rob")
    assert len(attachments) == 1
    assert sum("server-only" in ln for ln in lines) == 1


def test_build_deliverables_empty_artifacts(monkeypatch, tmp_path):
    from agents.task.goals.deliverables import build_deliverables
    _ws(monkeypatch, tmp_path)
    assert build_deliverables([], "sess-d", "rob") == ([], [])


# ---------------------------------------------------------------------------
# webview deep link
# ---------------------------------------------------------------------------

def test_webview_session_link(monkeypatch):
    from core.surfaces.deep_link import webview_session_link
    monkeypatch.setenv("WEBVIEW_PUBLIC_URL", "https://app.example.com/")
    assert webview_session_link("s-1") == "https://app.example.com/session/s-1"
    monkeypatch.delenv("WEBVIEW_PUBLIC_URL")
    assert webview_session_link("s-1") is None


# ---------------------------------------------------------------------------
# completion text + notify wiring
# ---------------------------------------------------------------------------

def _dispatcher():
    class _Board:  # not exercised by these tests
        pass
    class _Agent:
        container = None
    return GoalDispatcher(_Board(), _Agent())


def test_completion_text_carries_deliverables_and_link():
    disp = _dispatcher()
    goal = Goal(id="g1", user_id="u1", title="recon")
    text = disp._completion_text(
        goal, "did the thing", verified="verified",
        deliverable_lines=["- x402-recon.md (3.6 KB) — attached"],
        session_link="https://app.example.com/session/s-1")
    assert "Deliverables:" in text
    assert "x402-recon.md" in text
    assert "https://app.example.com/session/s-1" in text


def test_completion_text_legacy_shape_unchanged():
    disp = _dispatcher()
    goal = Goal(id="g1", user_id="u1", title="recon")
    text = disp._completion_text(goal, "did the thing")
    assert text == "✅ Background goal 'recon' completed.\nResult:\ndid the thing"


def test_notify_owner_done_threads_attachments(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path, session_id="sess-n")
    from pathlib import Path
    (Path(ws) / "report.md").write_text("# report")
    calls = []
    import core.self_evolution as se

    async def _fake_push(container, text, attachments=None):
        calls.append((text, attachments))
        return True

    monkeypatch.setattr(se, "push_owner_message", _fake_push)
    monkeypatch.setenv("DELIVERABLES_ATTACH_ENABLED", "true")
    monkeypatch.setenv("WEBVIEW_PUBLIC_URL", "https://app.example.com")
    disp = _dispatcher()
    goal = Goal(id="g1", user_id="rob", title="recon")
    told = asyncio.run(disp._notify_owner_done(
        goal, "sess-n", "wrote report.md", verified="verified",
        artifacts=[{"path": "report.md", "bytes": 8, "mtime": 1}]))
    assert told is True
    text, attachments = calls[0]
    assert "Deliverables:" in text
    assert "https://app.example.com/session/sess-n" in text
    assert attachments and attachments[0]["path"].endswith("report.md")


def test_notify_owner_done_flag_off_lists_but_never_attaches(monkeypatch, tmp_path):
    ws = _ws(monkeypatch, tmp_path, session_id="sess-n2")
    from pathlib import Path
    (Path(ws) / "report.md").write_text("# report")
    calls = []
    import core.self_evolution as se

    async def _fake_push(container, text, attachments=None):
        calls.append((text, attachments))
        return True

    monkeypatch.setattr(se, "push_owner_message", _fake_push)
    monkeypatch.setenv("DELIVERABLES_ATTACH_ENABLED", "false")
    monkeypatch.delenv("WEBVIEW_PUBLIC_URL", raising=False)
    disp = _dispatcher()
    goal = Goal(id="g1", user_id="rob", title="recon")
    told = asyncio.run(disp._notify_owner_done(
        goal, "sess-n2", "wrote report.md", verified="verified",
        artifacts=[{"path": "report.md", "bytes": 8, "mtime": 1}]))
    assert told is True
    text, attachments = calls[0]
    assert attachments is None
    assert "report.md" in text  # still listed for the owner


def test_notify_owner_done_strips_attachments_on_tenant_mismatch(monkeypatch, tmp_path):
    """Security review F4: attachments deliver to the INSTANCE OWNER principal;
    if the goal's tenant is a DIFFERENT (known) principal, media must not ride."""
    ws = _ws(monkeypatch, tmp_path, session_id="sess-mt", user_id="tenant-b")
    from pathlib import Path
    (Path(ws) / "b.md").write_text("tenant b file")
    calls = []
    import core.self_evolution as se

    async def _fake_push(container, text, attachments=None):
        calls.append((text, attachments))
        return True

    monkeypatch.setattr(se, "push_owner_message", _fake_push)
    monkeypatch.setenv("DELIVERABLES_ATTACH_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner-a")
    import asyncio
    from agents.task.goals.board import Goal
    disp = _dispatcher()
    told = asyncio.run(disp._notify_owner_done(
        Goal(id="g1", user_id="tenant-b", title="t"), "sess-mt", "made b.md",
        verified="verified", artifacts=[{"path": "b.md", "bytes": 13, "mtime": 1}]))
    assert told is True
    text, attachments = calls[0]
    assert attachments is None  # stripped — never cross-tenant media
    assert "b.md" in text  # still listed honestly
