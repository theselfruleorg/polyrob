"""WHY an X write needs the owner, in one line (prod 2026-10-10/11).

The owner-authored promo job d32f28647290 read ``reports/promo-drafts.md``
(``filesystem_read_file`` is an untrusted read) and its post waited 170 s for a
card; the agent told the owner "the rule is off but the gate is still on" and
could not name the gate. Agent-authored job 540a9bf73129 was described as
"auto-posts, no approval card" because its task text says so. The approval
gate's refusal and waiting text now carry the real cause and the owner's
remedy, so the agent can repeat it truthfully. Never raises.
"""
from typing import Any


def owner_gap_reason(tool: Any, execution_context: Any) -> str:
    if execution_context is None:
        return ""
    try:
        from polyrob.sdk import (forged_kind, is_leaf, is_owner_tenant, is_room_turn,
                                 taint_cause)
        tainted = taint_cause(execution_context=execution_context)
        if tainted:
            return (f"{tainted}. Third-party text may not choose a post without the owner "
                    "(the owner's `read_taint` security setting — SECURITY_SETTINGS in the "
                    "operator env or `polyrob config set` — governs this check)")
        if is_leaf(execution_context):
            return "a delegated sub-agent never posts as the owner"
        if forged_kind(execution_context):
            return "a self-wake / delegation re-entry is not the owner asking"
        if is_room_turn(execution_context):
            return "a room turn is not the owner asking"
        if not is_owner_tenant(execution_context):
            return "this turn is not the owner's"
        from core.autonomy_marker import (cron_job_for_session, goal_for_session,
                                          owner_job_task_for_session)
        sid = getattr(execution_context, "session_id", None)
        if owner_job_task_for_session(sid):
            return "TWITTER_REQUIRE_APPROVAL is on" if tool._require_approval() else ""
        job = cron_job_for_session(sid)
        if job:
            return (f"this job is agent-authored: {job}. An agent-authored job posts only "
                    "with a per-post owner approval, whatever its task text says; the "
                    f"owner can make it his own with `/adopt {job}`")
        goal = goal_for_session(sid)
        if goal:
            return (f"this goal is agent-authored: {goal}; the owner can make it his own "
                    f"with `/adopt {goal}`")
        if tool._require_approval():
            return "TWITTER_REQUIRE_APPROVAL is on"
    except Exception:
        tool.logger.debug("twitter owner-gap reason failed", exc_info=True)
    return ""


def with_reason(tool: Any, text: str, execution_context: Any) -> str:
    reason = owner_gap_reason(tool, execution_context)
    return f"{text} Why it needs the owner: {reason}." if reason else text
