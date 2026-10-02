"""F7 — old base64 images are retired in BATCHES, not re-anchored every step.

The previous rule anchored on the newest image-bearing turn and stripped every
turn before it. A browser run screenshots every step, so each new screenshot
demoted the previous one: a message the provider had already cached was
rewritten on EVERY step and everything behind it re-billed. The rule is now a
step function (``MEDIA_KEEP_MAX`` / ``MEDIA_RETIRE_BATCH`` / ``MEDIA_KEEP_FLOOR``),
so the rewrite is paid once per batch and consecutive assemblies share a prefix
in between.
"""

from __future__ import annotations

from agents.task.agent.messages.filters import (
    IMAGE_STRIPPED_MARKER,
    strip_historical_media,
)
from modules.llm.messages import HumanMessage

from tests.support.prefix_cache import common_prefix_len, serialize_messages


def _image_turn(step: int) -> HumanMessage:
    return HumanMessage(content=[
        {"type": "text", "text": f"screenshot {step}"},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + ("A" * 32) + str(step)}},
    ])


def _has_b64(message) -> bool:
    content = message.content
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") == "image_url"
        and "base64" in str(block.get("image_url", {}).get("url", ""))
        for block in content
    )


def _image_count(messages) -> int:
    return sum(1 for m in messages if _has_b64(m))


# ---------------------------------------------------------------------------
# The step function
# ---------------------------------------------------------------------------


def test_nothing_is_retired_below_the_ceiling():
    msgs = [_image_turn(i) for i in range(6)]
    out = strip_historical_media(msgs)
    assert _image_count(out) == 6
    assert serialize_messages(out) == serialize_messages(msgs)


def test_a_batch_is_retired_once_the_ceiling_is_passed():
    msgs = [_image_turn(i) for i in range(7)]
    out = strip_historical_media(msgs)
    # default batch 4, floor 2 -> the four OLDEST turns go
    assert _image_count(out) == 3
    assert not _has_b64(out[0]) and not _has_b64(out[3])
    assert _has_b64(out[4]) and _has_b64(out[6])
    assert IMAGE_STRIPPED_MARKER in str(out[0].content)


def test_the_floor_of_newest_turns_is_never_retired():
    msgs = [_image_turn(i) for i in range(40)]
    out = strip_historical_media(msgs)
    assert _image_count(out) >= 2
    assert _has_b64(out[-1]) and _has_b64(out[-2])


def test_the_batch_lands_at_or_under_the_ceiling():
    # 40 images with a batch of 4 would need ten passes; one call must suffice.
    out = strip_historical_media([_image_turn(i) for i in range(40)])
    assert _image_count(out) <= 6


def test_env_overrides_drive_the_step_function(monkeypatch):
    monkeypatch.setenv("MEDIA_KEEP_MAX", "2")
    monkeypatch.setenv("MEDIA_RETIRE_BATCH", "1")
    monkeypatch.setenv("MEDIA_KEEP_FLOOR", "1")
    out = strip_historical_media([_image_turn(i) for i in range(3)])
    assert _image_count(out) == 2
    assert not _has_b64(out[0])


def test_non_image_messages_pass_through_untouched():
    msgs = [HumanMessage(content="plain"), _image_turn(0), HumanMessage(content="also plain")]
    out = strip_historical_media(msgs)
    assert serialize_messages(out) == serialize_messages(msgs)
    assert out[0] is msgs[0]


# ---------------------------------------------------------------------------
# Idempotence + prefix stability
# ---------------------------------------------------------------------------


def test_running_on_an_already_stripped_list_is_identity():
    for count in (3, 6, 7, 12, 40):
        once = strip_historical_media([_image_turn(i) for i in range(count)])
        twice = strip_historical_media(once)
        assert serialize_messages(twice) == serialize_messages(once), count


def test_consecutive_assemblies_share_the_prefix_between_batches():
    """Six successive screenshots must not rewrite a single earlier turn."""
    previous = None
    breaks = 0
    for count in range(1, 7):
        current = serialize_messages(
            strip_historical_media([_image_turn(i) for i in range(count)]))
        if previous is not None:
            shared = common_prefix_len(previous, current)
            if shared != len(previous):
                breaks += 1
        previous = current
    assert breaks == 0, f"{breaks} of 5 assemblies rewrote an earlier image turn"


def test_one_cold_step_per_batch_then_warm_again():
    """Across a 14-screenshot run, the rewrite is paid per batch, not per step.

    Models the real loop: a retired turn STAYS retired in history, so the next
    step filters the already-stripped list, exactly as ``get_messages_for_llm``
    does over the conversation deque.
    """
    history: list = []
    previous = None
    cold = 0
    for step in range(14):
        history.append(_image_turn(step))
        assembled = strip_historical_media(history)
        history = list(assembled)  # the retired turns persist, as in the deque
        current = serialize_messages(assembled)
        if previous is not None and common_prefix_len(previous, current) != len(previous):
            cold += 1
        previous = current
    # ceiling 6 / batch 4 => the ceiling is crossed twice in 14 steps; the old
    # anchor rule rewrote an earlier turn on 12 of the 13.
    assert cold <= 3, f"{cold} of 13 steps were cold"
