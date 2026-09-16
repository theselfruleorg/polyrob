"""Ratchets for the agent communication contract.

These pin the WIRING, not the behaviour — the behaviour tests live next door.
Both defects this contract closes were wiring defects: a rule that existed but
was reachable from only one seat (the recap suppressor in `cli/ui`, the console
deep link in `goals/`). A future emit verb that forgets the wiring reintroduces
exactly that shape, silently.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ACTION_REG = ROOT / "tools" / "controller" / "action_registration.py"


def _source() -> str:
    return ACTION_REG.read_text()


def test_both_emit_verbs_pass_the_publish_context():
    """send_message and done must both hand the mirror its session context, or a
    workspace path in their body reaches the owner as an unopenable server path."""
    src = _source()
    for builder in ("build_discrete_publish", "build_completion_publish"):
        call = re.search(builder + r"\((.*?)\n\t*\)", src, re.S)
        assert call, f"{builder} call not found — did the emit path move?"
        # `_publish_context(self…)` — 044 T20 added a second argument
        # (`reply_to=params.reply_to`) at the send_message call site; what this
        # pins is that the context is PASSED, not its exact argument list.
        assert "_publish_context(self" in call.group(1), (
            f"{builder} no longer receives _publish_context; a named workspace "
            f"file would be pasted as /var/lib/... instead of attached or linked")


def test_done_uses_the_completion_mirror_not_the_discrete_one():
    """done must go through the latched mirror. Using build_discrete_publish here
    restores the unconditional double-publish this contract removed."""
    src = _source()
    done_at = src.index("async def done(params: DoneAction")
    # Code only: both builder names are named in explanatory comments here.
    done_code = [ln for ln in src[done_at:].splitlines()
                 if not ln.lstrip().startswith("#")]
    assert any("build_completion_publish" in ln for ln in done_code)
    assert not any("build_discrete_publish" in ln for ln in done_code)


def test_action_registration_never_gains_future_annotations():
    """Landmine: `from __future__ import annotations` stringizes the registry
    closures' first-param annotations, which Registry introspects to route the
    validated param model. Re-pinned here because this contract edits the file."""
    # The IMPORT, not the many comments warning against it.
    assert not any(ln.strip() == "from __future__ import annotations"
                   for ln in _source().splitlines())


def test_the_recap_belt_lives_in_core_not_in_a_renderer():
    """F3: the suppressor sat in cli/ui, so every non-CLI seat had nothing."""
    assert (ROOT / "core" / "surfaces" / "recap.py").is_file()
    dialog = (ROOT / "cli" / "ui" / "dialog.py").read_text()
    assert "from core.surfaces.recap import" in dialog, (
        "cli/ui/dialog.py must re-export the shared belt, never re-implement it")
