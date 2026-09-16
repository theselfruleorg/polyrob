"""An escalation may not name an owner action that does not exist.

2026-09-08, live: a blocked goal told the owner "One action unlocks it:
re-activate the standing Treasury goal with defi_trade granted — e.g.
`goal_update`/requeue on your side." There is no `goal_update` verb on any
surface. The owner, who runs this agent from Telegram, was handed an
instruction they could not follow for a grant that was never missing.

That is the whole class: the agent narrates a remedy from its own reasoning,
and nothing checks the remedy is real before it reaches a human. These tests
pin the checker, and the SSOTs it resolves against.
"""
import pytest

from core import owner_remedy as R


class TestChatVerbs:
    def test_a_real_verb_resolves(self):
        assert R.known_chat_verb("/approve")
        assert R.known_chat_verb("/goals")

    def test_the_invented_verb_does_not(self):
        """`goal_update` is the exact token from the live 2026-09-08 message."""
        assert not R.known_chat_verb("/goal_update")

    def test_the_verb_set_is_the_routing_ssot_not_a_copy(self):
        """A second hand-maintained list is how a verb becomes unroutable while
        still being advertised. Resolve against the dispatcher's tuple."""
        from core.surfaces.dispatcher import _COMMANDS

        assert R.chat_verbs() == frozenset(_COMMANDS)


class TestCliCommands:
    def test_a_real_command_resolves(self):
        assert R.known_cli_command("polyrob autonomy pause")
        assert R.known_cli_command("polyrob wallet")

    def test_an_invented_command_does_not(self):
        assert not R.known_cli_command("polyrob goal_update 2dee2891")

    def test_the_command_set_is_the_lazy_subcommand_ssot(self):
        from cli.polyrob import _LAZY_SUBCOMMANDS

        assert R.cli_commands() == frozenset(_LAZY_SUBCOMMANDS)


class TestTheValidator:
    def test_the_live_message_is_caught(self):
        """The verbatim escalation that shipped to the owner."""
        text = ("I can't swap or bridge myself — defi_trade is owner-locked. "
                "One action unlocks it: re-activate the standing Treasury goal "
                "with defi_trade granted — e.g. `goal_update`/requeue on your "
                "side, or restart my session with the tool granted.")
        assert "goal_update" in " ".join(R.unknown_owner_actions(text))

    def test_a_message_with_only_real_remedies_is_clean(self):
        text = ("Blocked: this needs your approval. Run /pending to see it, "
                "then /approve <id>. Or `polyrob autonomy resume` on the box.")
        assert R.unknown_owner_actions(text) == []

    def test_prose_that_merely_mentions_a_path_is_not_a_verb(self):
        """`/opt/polyrob/...` and `/var/lib/...` are paths, not chat commands.
        A checker that flags them would cry wolf on every honest message."""
        text = "Wrote the report to /var/lib/polyrob/reports/x.md on the host."
        assert R.unknown_owner_actions(text) == []

    def test_a_bare_slash_word_mid_sentence_is_checked(self):
        assert "/nonsense" in R.unknown_owner_actions("Try /nonsense to fix it.")


class TestRemedyTable:
    def test_every_blocker_names_a_surface_or_says_there_is_none(self):
        """A blocker with no chat remedy must say so HONESTLY rather than let
        the model invent one -- that invention is the bug this closes."""
        for blocker, remedy in R.BLOCKER_REMEDIES.items():
            assert remedy.telegram or remedy.cli or remedy.owner_side or remedy.none_because, (
                f"{blocker} offers the owner nothing and does not say why")

    def test_every_named_remedy_actually_exists(self):
        """The table itself must pass the checker it feeds."""
        for blocker, remedy in R.BLOCKER_REMEDIES.items():
            for text in (remedy.telegram, remedy.cli):
                if text:
                    assert R.unknown_owner_actions(text) == [], (
                        f"{blocker} names a non-existent action: {text!r}")

    def test_the_over_cap_blocker_does_not_pretend_chat_can_fix_it(self):
        """Raising a wallet ceiling from chat would let a compromised chat
        surface raise a spend limit. It is owner-side on purpose."""
        r = R.BLOCKER_REMEDIES["over_cap"]
        assert not r.telegram
        assert r.owner_side


class TestEscalationScrubbing:
    """The live path: a blocked goal's escalation carries the agent's OWN
    `last_failure_error` text straight to the owner's Telegram. That is where
    `goal_update` rode in on 2026-09-08."""

    def _goal(self, reason):
        import types
        return types.SimpleNamespace(
            title="Treasury: manage open positions and take a screened entry",
            last_failure_error=reason, consecutive_failures=1)

    def test_an_invented_remedy_is_flagged_not_silently_forwarded(self):
        from agents.task.goals.escalation import build_blocker_escalation

        out = build_blocker_escalation(self._goal(
            "defi_trade is owner-locked; re-activate the goal via "
            "`goal_update`/requeue on your side"))
        assert "goal_update" in out, "the agent's diagnosis is not hidden"
        assert "don't exist" in out or "does not exist" in out, (
            "the owner must be told the named action is not real, or they will "
            "go looking for it")

    def test_real_verbs_are_offered_when_an_invention_is_caught(self):
        from agents.task.goals.escalation import build_blocker_escalation

        out = build_blocker_escalation(self._goal("run `goal_update` please"))
        assert "/goal" in out, "flagging an invention without offering a real verb leaves the owner stuck"

    def test_an_honest_reason_is_left_completely_alone(self):
        from agents.task.goals.escalation import build_blocker_escalation

        reason = "PolicyGate: amount $96.18 exceeds catastrophic ceiling $5.00"
        out = build_blocker_escalation(self._goal(reason))
        assert reason in out
        assert "don't exist" not in out

    def test_a_server_path_in_the_reason_is_not_flagged(self):
        from agents.task.goals.escalation import build_blocker_escalation

        out = build_blocker_escalation(self._goal(
            "wrote /var/lib/polyrob/reports/x.md but could not publish"))
        assert "don't exist" not in out


class TestToolNamesAreNotInventedActions:
    """`defi_trade` is a tool, not a verb the owner types.

    The first version of the backticked-token heuristic flagged it, which would
    have put "I named `defi_trade` above — that doesn't exist" on every honest
    message about a trading run. A checker that cries wolf gets ignored, and an
    ignored checker is worse than none.
    """
    def test_a_tool_name_in_backticks_is_clean(self):
        assert R.unknown_owner_actions(
            "this run carries `defi_trade`, so it can act") == []

    def test_another_tool_name_is_clean(self):
        assert R.unknown_owner_actions("I used `web_fetch` to read it") == []

    def test_the_invented_verb_is_still_caught_beside_a_tool_name(self):
        out = R.unknown_owner_actions(
            "`defi_trade` is granted; run `goal_update` to requeue")
        assert out == ["goal_update"]
