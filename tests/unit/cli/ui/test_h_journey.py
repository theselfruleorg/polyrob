"""/journey — pure renderer groups core.recap.build_recap's entries.

Data-gathering (episodes/events/authored-skills/ledger) was extracted to
``core/recap.py`` (owner-UX P4 T1, see ``tests/unit/core/test_recap.py`` for
the source-level tests, including the loop-safety regression for the ledger
seam that used to live here). This module only tests the rendering layer:
``build_recap`` is the seam h_journey now depends on, so these tests
monkeypatch it directly instead of the old per-source private functions.
"""
from types import SimpleNamespace

from cli.ui.commands import h_journey
from core.recap import RecapEntry


def test_render_journey_sections(monkeypatch):
    monkeypatch.setattr(h_journey.recap, "build_recap", lambda *a, **k: [
        RecapEntry(ts=100.0, kind="episode", text='goal:done $0.20 "ship X"', amount=0.2),
        RecapEntry(ts=90.0, kind="self_modification", text="skill s1"),
        RecapEntry(ts=80.0, kind="skill", text="s1 [agent] (used 3x)"),
        RecapEntry(ts=70.0, kind="ledger",
                   text="Income: $1.50 (1 settled) · spend $0.20 · net $1.30 · runtime $0.05",
                   amount=1.3),
    ])
    out = h_journey.render_journey(user_id="u1", since_label="7d")
    assert "ship X" in out
    assert "Income: $1.50" in out and "net $1.30" in out
    assert "s1" in out and "used 3x" in out
    assert "── Changed" in out and "skill s1" in out


def test_render_journey_all_empty_is_stable(monkeypatch):
    # build_recap fails open to [] — the renderer still produces all sections.
    monkeypatch.setattr(h_journey.recap, "build_recap", lambda *a, **k: [])
    out = h_journey.render_journey(user_id="u1", since_label="24h")
    assert "── Did" in out and "no episodes" in out
    # H14b: an absent/empty ledger must NOT be rendered as a fabricated "$0.00 ·
    # $0.00 · $0.00" (a broken/absent data layer masquerading as a real balance
    # sheet) — the honest empty-state line replaces it. Terminology is
    # income/spend — "earned" is retired, including this fallback line.
    assert "Income: no money activity recorded yet" in out
    assert "Earned" not in out            # word-specific: "learned" also matches a bare "earned" substring
    assert "$0.00" not in out
    assert "no authored skills" in out
    assert "no self-modifications" in out


def test_render_journey_invalid_window_is_friendly(monkeypatch):
    # core.recap.build_recap raises ValueError on a malformed window — the REPL
    # renderer must turn that into a short message, never a raw traceback.
    def _raise(*a, **k):
        raise ValueError("invalid recap window 'junk' (expected e.g. '30m' / '24h' / '7d')")

    monkeypatch.setattr(h_journey.recap, "build_recap", _raise)
    out = h_journey.render_journey(user_id="u1", since_label="junk")
    assert "invalid window" in out


def test_window_seconds_parse():
    assert h_journey._window_seconds("24h") == 24 * 3600
    assert h_journey._window_seconds("7d") == 7 * 86400
    assert h_journey._window_seconds("") is None
    assert h_journey._window_seconds("junk") is None


def test_h_journey_handler_emits(monkeypatch):
    monkeypatch.setattr(h_journey, "render_journey",
                        lambda **kw: f"RENDERED:{kw['user_id']}:{kw['since_label']}")
    emitted = {}
    ctx = SimpleNamespace(
        args=["24h"], user_id="u1", container=None,
        emit=lambda text, title=None: emitted.update({"text": text, "title": title}))
    h_journey.h_journey(ctx)
    assert emitted["text"] == "RENDERED:u1:24h"
    assert emitted["title"] == "journey"


def test_recap_alias_resolves_to_journey():
    """2026-07-12 UI-surface review: the same core.recap ships as '/journey' on
    the CLI and '/recap' on Telegram — the REPL now answers to both."""
    from cli.ui.commands.handlers import build_default_registry
    cmd = build_default_registry().lookup("recap")
    assert cmd is not None and cmd.name == "journey"
