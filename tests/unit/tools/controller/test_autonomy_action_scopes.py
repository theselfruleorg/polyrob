"""The agent's pause scopes ARE the pause record's scopes (043 final fix round).

⚠️ ``_SCOPE`` was a hand-copied Literal that had fallen one behind
``core.autonomy_control.SCOPES``: it was missing ``apps``, so the owner could
pause app deployment from Telegram, the REPL, the CLI and the console — and not
by asking the agent, which is the seat the owner reaches for first. A scope the
record accepts and this Literal rejects is a control that exists everywhere
except where it is asked for.

The two are pinned together in BOTH directions, because either drift is a lie:
a missing scope is a control the agent cannot use, and an extra one is a scope
the agent would offer and ``pause`` would refuse.
"""
import typing

from core.autonomy_control import SCOPES
from tools.controller.autonomy_control_action import _SCOPE


def _literal_values(literal) -> tuple:
    return typing.get_args(literal)


def test_the_agent_offers_every_scope_the_record_accepts():
    missing = sorted(set(SCOPES) - set(_literal_values(_SCOPE)))
    assert not missing, (
        "the agent cannot pause these, though every other owner seat can: "
        + ", ".join(missing))


def test_the_agent_offers_no_scope_the_record_would_refuse():
    extra = sorted(set(_literal_values(_SCOPE)) - set(SCOPES))
    assert not extra, (
        "the agent offers scopes the pause record does not know: "
        + ", ".join(extra))


def test_the_scan_is_not_vacuous():
    values = _literal_values(_SCOPE)
    assert len(values) >= 8
    assert "all" in values and "apps" in values


def test_a_pause_kind_is_not_a_scope():
    """⚠️ ``sandbox_reap`` is a KIND, not a scope: ``KIND_SCOPES`` maps it to
    ``("all",)``, so nothing but a pause of everything denies it. Adding it to
    the Literal would offer the agent a word ``pause`` refuses — the opposite of
    the fix."""
    from core.autonomy_control import KIND_SCOPES
    assert "sandbox_reap" in KIND_SCOPES
    assert "sandbox_reap" not in SCOPES
    assert "sandbox_reap" not in _literal_values(_SCOPE)
