"""Owner-bound messages must fit the surface they are sent to.

The chat-first review (2026-08-22) traced a 20-line, multi-message goal
completion push to three independent gaps, none of which any test covered:

  G1  the deliverables block's only bound lived inside an `if written:` branch
      that a run without a ledger write-descriptor never enters;
  G2  paths were emitted as prose, so Telegram auto-linked `report.md` as a
      Moldovan domain;
  G8  no rail-level policy turned a document-sized body into gist + attachment.

Nothing here is about the transport — chunking was always correct. These are
producer-side bounds, and this file is the ratchet that keeps them.
"""
import os

import pytest

from core.identity import LocalIdentity
from core.surfaces.rendering import markdown_to_html, render_for_flavor

TELEGRAM_CAP = 4096


#: The tenant these fixtures run as. ⚠️ It was ``DEFAULT_INSTANCE_ID`` until
#: 2026-09-15: the instance id was then also the unbound owner principal, so it
#: doubled as an owner stand-in. It no longer is one — a workspace or goal keyed
#: by the instance id is a tenant nothing carries. Use the OWNER tenant.
_OWNER_TENANT = LocalIdentity.USER_ID


def _ws(monkeypatch, tmp_path, session_id="sess-b", user_id=_OWNER_TENANT):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "data_root"))
    from agents.task.path import pm
    return pm().get_workspace_dir(session_id, user_id)


def _many_artifacts(ws, n=20):
    """A realistic shared-workspace scan result: no ledger write descriptor, so
    NOTHING is attributed to this run — the exact prod shape that had no bound."""
    from pathlib import Path
    arts = []
    for i in range(n):
        name = f"status-2026-08-{i + 1:02d}.md"
        (Path(ws) / name).write_text(f"# status {i}\n" + "detail line\n" * 5)
        arts.append({"path": name, "bytes": 70, "mtime": 1})
    return arts


# ---------------------------------------------------------------------------
# G1 — the bound does not depend on ledger attribution
# ---------------------------------------------------------------------------

def test_deliverables_block_is_bounded_without_write_descriptors(monkeypatch, tmp_path):
    from agents.task.goals.deliverables import build_deliverables, deliverables_max_lines
    ws = _ws(monkeypatch, tmp_path)
    _, lines = build_deliverables(_many_artifacts(ws), "sess-b", _OWNER_TENANT)
    # cap + at most one roll-up line
    assert len(lines) <= deliverables_max_lines() + 1
    assert any(ln.startswith("- (+") and "more file(s)" in ln for ln in lines)


def test_deliverables_rollup_accounts_for_every_file(monkeypatch, tmp_path):
    """Bounded is not the same as lost: the count must add up to what was found."""
    import re
    from agents.task.goals.deliverables import build_deliverables
    ws = _ws(monkeypatch, tmp_path)
    _, lines = build_deliverables(_many_artifacts(ws, n=20), "sess-b", _OWNER_TENANT)
    rollup = [ln for ln in lines if ln.startswith("- (+")]
    assert len(rollup) == 1
    hidden = int(re.search(r"\+(\d+) more", rollup[0]).group(1))
    assert hidden + (len(lines) - 1) == 20


def test_attached_files_are_never_rolled_up(monkeypatch, tmp_path):
    """The owner HAS an attached file — naming it is the point of the block."""
    from agents.task.goals.deliverables import build_deliverables
    monkeypatch.setenv("DELIVERABLES_MAX_LINES", "2")
    ws = _ws(monkeypatch, tmp_path)
    attachments, lines = build_deliverables(
        _many_artifacts(ws, n=12), "sess-b", _OWNER_TENANT)
    attached_lines = [ln for ln in lines if "attached" in ln]
    assert len(attached_lines) == len(attachments)
    assert attachments, "expected the default attach cap to attach something"


# ---------------------------------------------------------------------------
# G2 — a filename is not a domain
# ---------------------------------------------------------------------------

def test_paths_render_as_code_not_links(monkeypatch, tmp_path):
    """`.md` is Moldova's TLD: an un-backticked filename becomes a tappable link
    to a domain that does not exist. Backticks make the renderer emit <code>."""
    from agents.task.goals.deliverables import build_deliverables
    ws = _ws(monkeypatch, tmp_path)
    _, lines = build_deliverables(_many_artifacts(ws, n=3), "sess-b", _OWNER_TENANT)
    import re
    html = markdown_to_html("\n".join(lines))
    assert "<code>status-2026-08-01.md</code>" in html
    # NO filename survives outside a code span — that is what stops the client
    # from linkifying it.
    outside = re.sub(r"<code>.*?</code>", "", html, flags=re.S)
    assert ".md" not in outside


# ---------------------------------------------------------------------------
# The composed push fits ONE message
# ---------------------------------------------------------------------------

def test_goal_completion_push_fits_one_telegram_message(monkeypatch, tmp_path):
    from agents.task.goals.board import Goal
    from agents.task.goals.deliverables import build_deliverables
    from agents.task.goals.dispatcher import GoalDispatcher

    ws = _ws(monkeypatch, tmp_path)
    _, lines = build_deliverables(_many_artifacts(ws), "sess-b", _OWNER_TENANT)

    class _Board:
        pass

    class _Agent:
        container = None

    disp = GoalDispatcher(_Board(), _Agent())
    text = disp._completion_text(
        Goal(id="g1", user_id=_OWNER_TENANT, title="nightly status sweep"),
        "Wrote the sweep. " * 300,  # a long, realistic result body
        verified="verified",
        deliverable_lines=lines,
        session_link="https://console.example.com/session/sess-b")
    chunks = render_for_flavor(text, "html", TELEGRAM_CAP)
    assert len(chunks) == 1, (
        f"completion push split into {len(chunks)} phone messages "
        f"({len(text)} chars)")


# ---------------------------------------------------------------------------
# G8 — the rail spills a document-sized body
# ---------------------------------------------------------------------------

def test_long_body_spills_to_an_attachment(tmp_path):
    from core.surfaces.spill import maybe_spill
    body = "Line one — the outcome.\n" + ("filler detail\n" * 400) + \
        "Console: https://console.example.com/session/s-1"
    spilled = maybe_spill(body, home_dir=str(tmp_path), source="goal_done")
    assert spilled is not None
    gist, entry = spilled
    assert len(gist) < len(body)
    assert gist.startswith("Line one — the outcome.")
    # the link is the actionable part — a head-truncation must not lose it
    assert "https://console.example.com/session/s-1" in gist
    assert os.path.isfile(entry["path"])
    with open(entry["path"], encoding="utf-8") as fh:
        assert fh.read() == body, "the attachment carries the FULL body"


def test_short_body_is_never_spilled(tmp_path):
    from core.surfaces.spill import maybe_spill
    assert maybe_spill("done.", home_dir=str(tmp_path), source="agent") is None


def test_spill_can_be_disabled(monkeypatch, tmp_path):
    from core.surfaces.spill import maybe_spill
    monkeypatch.setenv("USER_DELIVERY_SPILL", "false")
    assert maybe_spill("x" * 9000, home_dir=str(tmp_path), source="agent") is None


@pytest.mark.asyncio
async def test_rail_sends_gist_and_attaches_the_report(tmp_path):
    """End to end at the rail: what reaches the sink is short, and the full body
    rides as a file rather than as six chat messages."""
    from core.surfaces import user_delivery

    sent = {}

    class _Sink:
        def send_message(self, chat_id, text, media=None):
            sent["text"] = text
            sent["media"] = media
            return True

    class _Cfg:
        data_dir = str(tmp_path)

    class _Container:
        config = _Cfg()

        def get_service(self, name):
            return _Sink() if name == "telegram_sink" else None

    body = "Goal finished.\n" + ("detail\n" * 500)
    out = await user_delivery.deliver_user_message(
        _Container(), "12345", body, source="goal_done", event_log=None)
    assert out == "sent"
    assert len(sent["text"]) < len(body)
    assert sent["media"] and os.path.isfile(sent["media"][0]["path"])


@pytest.mark.asyncio
async def test_legacy_sink_without_media_gets_the_full_body(tmp_path):
    """A gist pointing at a file the owner will never receive is worse than a
    long message — so a sink that cannot carry media gets the whole body."""
    from core.surfaces import user_delivery

    sent = {}

    class _Sink:
        def send_message(self, chat_id, text):  # no media kwarg (pre-QW-1 shape)
            sent["text"] = text
            return True

    class _Cfg:
        data_dir = str(tmp_path)

    class _Container:
        config = _Cfg()

        def get_service(self, name):
            return _Sink() if name == "telegram_sink" else None

    body = "Goal finished.\n" + ("detail\n" * 500)
    out = await user_delivery.deliver_user_message(
        _Container(), "12345", body, source="goal_done", event_log=None)
    assert out == "sent"
    assert sent["text"] == body.strip()
