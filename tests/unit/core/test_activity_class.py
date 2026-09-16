"""Contract tests for ``core/activity_class.py`` (043 Work › Log, Task G1).

The classifier is the ONE seam that groups the flat durable/feed ``kind`` space
into the owner-facing Log classes. The invariants pinned here:

- the six anchor mappings the spec names;
- totality: every durable kind (``event_kinds.KNOWN_KINDS``) AND every feed kind
  ``webview/activity.py::summarize`` handles classifies into ``CLIENT_CLASSES``
  without crashing;
- non-string / empty input degrades to ``"system"``, never raises;
- ``is_diagnostic`` names exactly the default-hidden engine kinds.

The feed-kind list is a LITERAL here (not an import of ``webview.activity``) so
this stays a core-only unit test — a webview import would break the module's
core-tier isolation and pull heavy deps into a core test. Keep it in sync with
``webview/activity.py::summarize``; a new feed kind added there should be added
here (all any feed kind needs is to classify without crashing).
"""

import pytest

from core import activity_class as ac
from core import event_kinds as ek


# Every non-``goal_``-prefixed feed kind ``webview/activity.py::summarize``
# branches on, plus the suppressed ``streaming_output`` kind and the A16
# ``tool_result`` kind. Durable kinds are covered by KNOWN_KINDS below.
_FEED_KINDS = (
    "tool_result",
    "tool_execution",
    "tool_started",
    "llm_started",
    "awaiting_approval",
    "approval_resolved",
    "compaction_started",
    "compaction_finished",
    "retry_wait",
    "subagent_started",
    "subagent_finished",
    "delegation_dispatched",
    "delegation_completed",
    "provider_failure",
    "provider_fallback_success",
    "llm_request",
    "step",
    "session_start",
    "session_completion",
    "task_complete",
    "agent_end",
    "agent_registration",
    "available_actions",
    "status",
    "error",
    "skill_install",
    "streaming_output",
    # goal-board prefix kinds (summarize matches on the ``goal_`` prefix)
    "goal_created",
    "goal_claimed",
    "goal_succeeded",
    "goal_failed",
    "goal_blocked",
    "goal_gave_up",
)


def test_anchor_mappings():
    """The six mappings the spec names by hand."""
    assert ac.classify(ek.GOAL_RUN) == "goal"
    assert ac.classify(ek.CRON_RUN) == "cron"
    assert ac.classify(ek.WALLET_SPEND) == "money"
    assert ac.classify(ek.OWNER_NOTICE) == "message"
    assert ac.classify("tool_result") == "tool"
    assert ac.classify("a_kind_that_will_never_exist") == "system"


def test_client_classes_shape():
    assert ac.CLIENT_CLASSES == ("goal", "cron", "money", "message", "system", "tool")
    # exactly six, unique, all lowercase identifiers
    assert len(ac.CLIENT_CLASSES) == len(set(ac.CLIENT_CLASSES)) == 6


@pytest.mark.parametrize("kind", sorted(ek.KNOWN_KINDS))
def test_every_durable_kind_classifies(kind):
    """No durable SSOT kind may crash or fall outside CLIENT_CLASSES."""
    cls = ac.classify(kind)
    assert cls in ac.CLIENT_CLASSES, (kind, cls)


@pytest.mark.parametrize("kind", _FEED_KINDS)
def test_every_feed_kind_classifies(kind):
    """No feed kind summarize() handles may crash or fall outside CLIENT_CLASSES."""
    cls = ac.classify(kind)
    assert cls in ac.CLIENT_CLASSES, (kind, cls)


@pytest.mark.parametrize("bad", [None, "", 123, 4.5, b"cron_run", [], {}, object()])
def test_non_string_and_empty_are_system(bad):
    """Total on garbage input — the Log must never 500 on a malformed kind."""
    assert ac.classify(bad) == "system"


# Representative per-class mappings. These document intent and guard against a
# silent re-classification; only the six anchors above are spec-mandated.
_EXPECTED = {
    "goal": [ek.GOAL_RUN, ek.GOAL_COMPLETION, "goal_created", "goal_succeeded"],
    "cron": [ek.CRON_RUN],
    "money": [
        ek.WALLET_SPEND,
        ek.PAYMENT_REQUESTED,
        ek.PAYMENT_SETTLED,
        ek.PAYMENT_AUTO_APPROVED,
        ek.SUBSCRIPTION_RENEWED,
        ek.ROOM_ACTION_SETTLED,
        ek.TX_BROADCAST,
        ek.TX_SETTLED,
    ],
    "message": [
        ek.OWNER_NOTICE,
        ek.USER_DELIVERY,
        ek.OUTBOUND_OPEN_SEND,
        ek.SOCIAL_WRITE,
        ek.CORRESPONDENT_PENDING,
        ek.INBOUND_ROUTED,
    ],
    "tool": [
        "tool_result",
        "tool_execution",
        "tool_started",
        ek.TOOL_DENIED,
        ek.TOOL_TIMEOUT,
        ek.TOOL_AUTO_APPROVED,
        "available_actions",
    ],
    "system": [
        ek.AUTONOMY_TICK,
        ek.SELF_WAKE,
        ek.SELF_MODIFICATION,
        ek.APP_LIVE,
        ek.MCP_INSTALL,
        "skill_install",
        "error",
        "session_start",
        "delegation_dispatched",
        ek.ACCESS_DENIED,
        ek.INJECTION_FLAGGED,
    ],
}


@pytest.mark.parametrize(
    "expected_class,kind",
    [(cls, kind) for cls, kinds in _EXPECTED.items() for kind in kinds],
)
def test_representative_class_mappings(expected_class, kind):
    assert ac.classify(kind) == expected_class, kind


def test_is_diagnostic_set():
    for kind in ("llm_started", "llm_request", "retry_wait", "step",
                 "tool_started", "compaction_started", "compaction_finished"):
        assert ac.is_diagnostic(kind) is True, kind
    for kind in ("tool_result", ek.GOAL_RUN, ek.WALLET_SPEND, ek.OWNER_NOTICE,
                 "tool_execution", "error", ""):
        assert ac.is_diagnostic(kind) is False, kind


def test_is_diagnostic_total_on_garbage():
    for bad in (None, 123, [], {}, object()):
        assert ac.is_diagnostic(bad) is False


def test_diagnostic_kinds_still_classify():
    """A diagnostic kind is a visibility flag, not a class — it still classifies."""
    for kind in ("llm_started", "step", "compaction_started", "tool_started", "retry_wait"):
        assert ac.classify(kind) in ac.CLIENT_CLASSES
