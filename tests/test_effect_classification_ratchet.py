"""033 ratchet — every external writer routes through the ONE effect recorder.

`social_write` is not a class of event: it is one hand-written `record()` call
inside one tool (tools/twitter_tool.py), reachable from two of that tool's
twenty-two actions. In fourteen days of production it wrote 25 rows while eight
public Telegram broadcast posts wrote none. Twenty-two modules call
`get_event_log().record(...)`, each choosing its own kind, source string and
attrs shape, so every reader is a closed allowlist that a new writer walks past.

Proposal 033 replaces that with one effect classification recorded at three
seams (the Controller post-hook, the two MessageRouter methods, and direct
`core.effects.record_external_write` calls for the writers that have neither).

This ratchet pins the debt while that lands. Two directions, mirroring
tests/test_layering_ratchet.py:

1. ``UNCOVERED_WRITERS`` is the frozen list of modules that perform an external
   write WITHOUT reaching the recorder. It is SHRINK-ONLY.
2. A row whose module now reaches the recorder must be DELETED in the same
   commit, so the list tightens instead of going stale — the direction
   tests/test_autonomy_control_ratchet.py lacks.

Seeded 2026-09-08 from the telemetry effect-classification review §10.
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The recorder every external writer must eventually reach, directly or through
#: a seam (the Controller hook / the MessageRouter methods) that calls it.
RECORDER = "record_external_write"

#: The seven effect classes proposal 033 defines. Kept as a literal here on
#: purpose: this test must fail loudly if core/effects.py renames one, rather
#: than silently agreeing with whatever the module happens to say.
EFFECT_CLASSES = frozenset({
    "social", "comms", "public", "money", "code", "self", "network",
})

#: module -> the effect class it writes. SHRINK-ONLY: delete a row the day its
#: module reaches the recorder. Every entry is evidenced in the review's §10 map.
UNCOVERED_WRITERS = {
    # --- social / public broadcast -------------------------------------------
    "tools/twitter_tool.py": "social",
    "tools/x_browser/tool.py": "social",
    "tools/publish/tool.py": "public",
    "tools/git/tool.py": "public",
    "tools/github/tool.py": "public",
    # --- outbound comms -------------------------------------------------------
    "tools/email_tool.py": "comms",
    "tools/controller/message_send.py": "comms",
    "cron/delivery.py": "comms",
    "core/surfaces/message_router.py": "comms",
    "core/surfaces/outbound_dispatcher.py": "comms",
    "surfaces/telegram/surface.py": "comms",
    "surfaces/email/surface.py": "comms",
    "surfaces/discord/surface.py": "comms",
    "surfaces/slack/surface.py": "comms",
    "surfaces/signal/surface.py": "comms",
    "surfaces/whatsapp/surface.py": "comms",
    "surfaces/x/surface.py": "comms",
    # --- money ----------------------------------------------------------------
    # hyperliquid/polymarket cancel, cancel_all and approve_agent touch the venue
    # with no policy.record at all (review F-10 neighbours).
    "tools/hyperliquid/service.py": "money",
    "tools/polymarket/service.py": "money",
    # --- arbitrary exec / egress ----------------------------------------------
    "tools/mcp/mcp_tool.py": "network",
    "tools/anysite/tool.py": "network",
    "tools/browser/browser.py": "network",
    "tools/shell/tool.py": "code",
    "tools/shell/process_tool.py": "code",
    "tools/code_exec/tool.py": "code",
    # --- self-modification ----------------------------------------------------
    # load_tool is an unlogged capability expansion: an orchestrator turn can
    # self-load twitter/email/x_browser/publish/shell/mcp mid-session (F-15).
    "tools/tool_disclosure.py": "self",
}


def _reaches_recorder(path: Path) -> bool:
    """True when the module calls, or imports, ``record_external_write``.

    AST rather than a substring so a mention in a docstring or a comment does
    not count as coverage.
    """
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except (SyntaxError, OSError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (getattr(fn, "attr", None) or getattr(fn, "id", None)) == RECORDER:
                return True
        if isinstance(node, ast.ImportFrom) and node.module == "core.effects":
            if any(a.name == RECORDER for a in node.names):
                return True
    return False


def test_uncovered_writer_rows_still_exist():
    """A listed module that was deleted or renamed must be removed from the list,
    or the ratchet is measuring a file nobody ships."""
    missing = sorted(m for m in UNCOVERED_WRITERS if not (REPO / m).exists())
    assert missing == [], f"stale UNCOVERED_WRITERS rows (module gone): {missing}"


def test_uncovered_writers_only_shrink():
    """A row whose module NOW reaches the recorder must be deleted in the same
    commit, so the debt list tightens rather than accumulating stale entries."""
    fixed = sorted(m for m in UNCOVERED_WRITERS if _reaches_recorder(REPO / m))
    assert fixed == [], (
        "these modules now reach record_external_write — delete their "
        f"UNCOVERED_WRITERS rows in the same commit: {fixed}")


def test_every_declared_effect_is_a_known_class():
    """Guards a typo from hiding a row behind an effect nobody queries."""
    bad = {m: e for m, e in UNCOVERED_WRITERS.items() if e not in EFFECT_CLASSES}
    assert bad == {}, f"unknown effect classes in UNCOVERED_WRITERS: {bad}"


def test_the_debt_is_not_silently_growing():
    """The seed count. Raising this number is a deliberate act that shows up in
    review; it must never happen without a new writer being justified."""
    assert len(UNCOVERED_WRITERS) <= 26, (
        f"UNCOVERED_WRITERS grew to {len(UNCOVERED_WRITERS)} — a NEW external "
        "writer was added without routing through core.effects.record_external_write")
