"""An owner action must be one the owner can actually take (2026-09-15).

`core.owner_remedy` has known since 2026-09-08 which owner actions are real.
It guarded exactly ONE producer — the goal escalation — so the agent's own
`message` tool and every framework notice went out unchecked, and a phone owner
kept being handed `polyrob …` commands they have no shell to run.
"""
import pytest


# --- the checker itself -------------------------------------------------------

def test_an_invented_verb_is_named():
    from core.owner_remedy import correction_line

    line = correction_line("requeue it with `/goal_update` on your side")
    assert "/goal_update" in line and "don't exist" in line


def test_a_real_verb_passes_clean():
    from core.owner_remedy import correction_line

    assert correction_line("tap /approve, or see /pending and /status") == ""


def test_an_absolute_path_is_not_an_invented_verb():
    """A checker that cries wolf is one that gets ignored. "the tree lives under
    /opt" and "logs in /var/lib/polyrob" are shapes an honest message uses."""
    from core.owner_remedy import correction_line, unknown_owner_actions

    text = "the tree lives under /opt; logs in /var/lib/polyrob and /etc"
    assert unknown_owner_actions(text) == []
    assert correction_line(text) == ""


def test_a_path_segment_is_not_a_cli_invocation():
    """`/opt/polyrob and the rest` used to parse as `polyrob and`."""
    from core.owner_remedy import cli_calls_named

    assert cli_calls_named("code is in /opt/polyrob and data in /var/lib/polyrob") == []


# --- the shell-free rewrite ---------------------------------------------------

def test_a_cli_instruction_is_rewritten_for_a_reader_with_no_shell():
    """This is the exact line the live pending notice shipped."""
    from core.owner_remedy import shell_free_correction

    line = shell_free_correction(
        "or run `polyrob owner pending` to review + `owner promote/reject`.")
    assert "no shell" in line
    assert "/pending" in line


def test_a_cli_command_with_no_chat_twin_says_so_rather_than_inventing_one():
    """Some things genuinely need a shell. Inventing a chat verb for them would
    be the same defect one layer down."""
    from core.owner_remedy import chat_equivalent, shell_free_correction

    assert chat_equivalent("polyrob datagen build") == ""
    assert "needs a shell on the box" in shell_free_correction("run polyrob datagen build")


def test_a_message_with_no_cli_command_is_untouched():
    from core.owner_remedy import shell_free_correction

    assert shell_free_correction("tap /pending when you have a moment") == ""


# --- the two owner rails ------------------------------------------------------

def test_the_framework_delivery_rail_corrects_before_it_dedups():
    """`deliver_user_message` carries every framework notice to the owner.

    The correction must land BEFORE the content hash, or a corrected body and
    its uncorrected twin dedup as two different messages and the owner gets
    both.
    """
    import inspect

    from core.surfaces import user_delivery

    src = inspect.getsource(user_delivery.deliver_user_message)
    assert "shell_free_correction" in src and "correction_line" in src
    assert src.index("shell_free_correction") < src.index("_content_hash")


def test_the_message_tool_corrects_an_owner_bound_message():
    """`perform_message_send` is the agent's commonest route to a phone."""
    import inspect

    from tools.controller import message_send

    src = inspect.getsource(message_send.perform_message_send)
    assert "shell_free_correction" in src and "correction_line" in src
    # Owner tier only: a third party is not promised a chat verb catalogue.
    assert 'tier == "owner"' in src


# --- the prompt ---------------------------------------------------------------

def test_the_prompt_teaches_the_tap_shape_in_a_private_chat():
    from agents.task.agent.prompts import SystemPrompt

    p = SystemPrompt.__new__(SystemPrompt)
    p.surface = {"surface_id": "telegram", "max_message_bytes": 4096,
                 "media_out": True, "chat_type": "dm"}
    block = p._get_surface_content()

    assert "ONE tappable token" in block
    assert "/approve_p_" in block          # the shape, shown not described
    assert "polyrob" in block              # named as the thing NOT to write
    assert "/pending" in block             # the real vocabulary is listed


def test_a_room_does_not_get_the_owner_verb_catalogue():
    """Room members are not the owner; the owner's control verbs are not room
    verbs and listing them there would only invite an attempt."""
    from agents.task.agent.prompts import SystemPrompt

    p = SystemPrompt.__new__(SystemPrompt)
    p.surface = {"surface_id": "telegram", "max_message_bytes": 4096,
                 "media_out": True, "chat_type": "group", "chat_id": "-100"}
    block = p._get_surface_content()

    assert "ONE tappable token" not in block


def test_the_verb_list_comes_from_the_router_not_a_second_copy():
    """A verb that stops being routable must stop being advertised in the same
    commit — so the prompt reads the dispatcher's own tuple."""
    from agents.task.agent.prompts import SystemPrompt
    from core.owner_remedy import chat_verbs

    p = SystemPrompt.__new__(SystemPrompt)
    p.surface = {"surface_id": "telegram", "chat_type": "dm"}
    block = p._get_surface_content()

    for verb in ("/pending", "/approve", "/status"):
        assert verb in chat_verbs() and verb in block


# --- the checker must not flag the framework's own remedies -------------------

def test_a_tappable_token_is_not_an_invented_verb():
    """`/approve_p_1245c6` is `/approve` with its argument folded in. Without
    this the checker flagged the framework's OWN tappable remedies and appended
    a correction to every message that offered one."""
    from core.owner_remedy import known_chat_verb, unknown_owner_actions

    assert known_chat_verb("/approve_p_1245c6")
    assert known_chat_verb("/approve_all")
    assert unknown_owner_actions("tap /approve_p_1245c6 or /reject_all") == []
    # And the guard still holds for a genuine invention.
    assert unknown_owner_actions("run /goal_update") == ["/goal_update"]


def test_the_pending_notice_passes_its_own_checker():
    """Both owner rails now append a correction. A notice that trips the check
    would ship its own disclaimer."""
    from core.owner_remedy import correction_line, shell_free_correction
    from core.self_evolution import build_pending_notification

    text = build_pending_notification(
        [{"kind": "skill", "id": "dm-conversation", "preview": "How Rob handles DMs"}])
    assert correction_line(text) == ""
    assert shell_free_correction(text) == ""


def test_the_status_remedy_passes_its_own_checker():
    import inspect

    from core import status_snapshot
    from core.owner_remedy import unknown_owner_actions

    src = inspect.getsource(status_snapshot)
    assert 'remedy="/pending · /approve_all · /reject_all"' in src
    assert unknown_owner_actions("/pending · /approve_all · /reject_all") == []


def test_one_grammar_not_two():
    """The harness parses a tapped token and the checker recognises one. Two
    tiers, one file."""
    import inspect

    from core.surfaces import tappable
    from surfaces.telegram import harness

    assert "parse_tappable" in inspect.getsource(harness.normalize_tappable_command)
    assert harness.normalize_tappable_command("/approve_all") == \
        tappable.parse_tappable("/approve_all")
