"""095 W2 ratchet: one run-time refusal vocabulary (``core.tool_gates.REFUSAL_KINDS``).

(a) every slug a production module passes to ``record_refusal`` has a kind;
(b) every kind renders who can act, the lever, and whether a retry helps;
(c) a text that names /pending takes the real ``pending_id``;
(d) the hook modules hold no refusal prose of their own.
"""
import re
import string
from pathlib import Path

from core.tool_gates import REFUSAL_KINDS, refusal_text

REPO = Path(__file__).resolve().parents[3]
PROD_DIRS = ("core", "modules", "agents", "tools", "cron", "polyrob", "api", "cli",
             "surfaces", "webview", "packs")
HOOK_MODULES = (
    "core/surfaces/room_policy.py",
    "agents/task/agent/core/correspondent_gate.py",
    "core/money/authorize.py",
    "tools/controller/_helpers.py",
    "core/effects.py",
    "tools/controller/effect_hooks.py",
    "tools/controller/execution.py",
    "tools/controller/command_guard_hook.py",
    "tools/controller/approval.py",
)
PROSE = ("approve via", "owner can", "cannot move funds")


def _facts(row) -> dict:
    names = set()
    for text in (row.text, row.owner_text, row.actor.verb):
        names |= {f for _, f, _, _ in string.Formatter().parse(text) if f}
    return {n: f"<{n}>" for n in names}


def test_every_recorded_slug_has_a_kind():
    call = re.compile(r"""(?:record_refusal|_record_money_refusal)\(\s*["']([a-z_]+)["']""")
    slugs = set()
    for top in PROD_DIRS:
        for path in (REPO / top).rglob("*.py"):
            slugs |= set(call.findall(path.read_text(encoding="utf-8")))
    assert {"approval_denied", "forged_turn"} <= slugs  # the scan works
    covered = {row.slug for row in REFUSAL_KINDS.values()}
    assert not sorted(slugs - covered), f"add a REFUSAL_KINDS row for: {sorted(slugs - covered)}"


def test_every_kind_renders_actor_lever_and_retryable():
    for kind, row in REFUSAL_KINDS.items():
        text = refusal_text(kind, **_facts(row))
        assert "[who: " in text and " · lever: " in text, kind
        assert text.endswith(("retry: yes]", "retry: no]")), kind
        assert row.actor.verb.strip(), kind
        assert refusal_text(kind, for_owner=True, **_facts(row)), kind


def test_pending_needs_a_pending_id():
    for kind, row in REFUSAL_KINDS.items():
        if "/pending" in row.text or "/pending" in row.actor.verb:
            assert "{pending_id}" in row.text, kind


def test_hook_modules_hold_no_refusal_prose():
    for rel in HOOK_MODULES:
        text = (REPO / rel).read_text(encoding="utf-8").lower()
        hits = [p for p in PROSE if p in text]
        assert not hits, f"{rel}: refusal prose {hits} belongs in core.tool_gates.REFUSAL_KINDS"


def test_slug_of_every_kind_is_a_counting_reason():
    from core.security.refusals import REFUSAL_REASONS
    for kind, row in REFUSAL_KINDS.items():
        assert row.slug == "" or row.slug in REFUSAL_REASONS, kind


def test_an_unarmed_autonomous_run_is_not_called_forged(monkeypatch):
    from types import SimpleNamespace
    from core import autonomy_marker as am
    from core.money import authorize as a
    am.mark_autonomous("sess-w2-unarmed", goal_id="g-w2")
    import importlib
    regime = importlib.import_module("core.config_policy.money_regime")
    monkeypatch.setattr(regime, "unattended_money_allowed", lambda: False)
    ctx = SimpleNamespace(session_id="sess-w2-unarmed", is_sub_agent=False)
    assert a._unarmed_autonomous_run(ctx)
    assert not a._unarmed_autonomous_run(SimpleNamespace(session_id="sess-w2-unarmed",
                                                         is_sub_agent=True))
    assert "not armed" in a.UNARMED_TEXT and "operator" in a.UNARMED_TEXT
