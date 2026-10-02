"""Credential preflight — a $0 skip for a cron whose rail needs a credential
that has an OPEN verdict (X evaluation 2026-09-26, P2-1 / build item 9).

Live: after the X OAuth 2.0 login died on 09-25, the DM CHECK cron (2×/day) and
the other X crons kept paying for full LLM turns that ended "0 touches" /
"BLOCKED" — ~$3.9 in 8 days — while ``verdicts.db`` already held the fact.
The standing verdict is readable in-process before any model call, so the tick
is a ``cron_run skipped/credential_verdict`` event instead, exactly the shape of
the ``slot_cap`` preflight.

Data-driven: :data:`NEEDS` is the ONE table ``verdict kind -> what needs it``.
A row matches a job when

* the job's DECLARED toolset (``payload.tools``, else a named narrow rig)
  includes one of ``tools`` — or, when the job declares nothing narrower than
  ``full``, its task text matches ``topic`` (every tool is loaded then, so tool
  presence says nothing about what the job is FOR);
* the task text matches ``scope`` when the row has one (the credential serves
  only part of the tool — the OAuth 2.0 login is the DM half of ``twitter``;
  the OAuth 1.0a keys still post);
* no ``alternatives`` tool whose flag is on is in the toolset (the browser rail
  needs no API credits).

A ``honor_backoff`` row skips only while the verdict is inside its re-probe
hold: once it lapses ONE tick runs, which is the probe that clears the row on
success (a topped-up X account has no other writer).

The owner is told ONCE per verdict episode (``kind`` + ``first_seen``; a
cleared and re-recorded verdict is a new episode), never per skipped run.

Fail-OPEN by construction: an unreadable verdict store, a bad payload or any
error runs the tick.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KIND_CREDENTIAL = "credential"
REASON = "credential_verdict"

_DM_RE = re.compile(r"\b(twitter_get_dms|twitter_dm|x_read_dms|dms?|direct messages?|x chat)\b",
                    re.IGNORECASE)
#: Text that NAMES a DM without the job using one. Live 2026-09-29: DEN
#: ENGAGEMENT (Telegram only) was skipped 3x as needing the X DM login because
#: its spam example read "can I get a dm", and X TARGET COLLECTION says "no DMs".
#: Quoted spans are examples; a negated DM is a prohibition, not a need.
_QUOTED_RE = re.compile(r"\"[^\"\n]*\"|“[^”\n]*”")
_NEGATED_DM_RE = re.compile(r"\b(?:no|never|not|without|nor)\s+(?:\w+\s+){0,2}?"
                            r"(?:dms?|direct messages?)\b", re.IGNORECASE)
_X_RE = re.compile(r"\b(twitter\w*|tweets?|x\.com|on x|x (?:api|account|timeline|posts?|"
                   r"mentions|engagement|outreach|dms?|replies|thread))\b", re.IGNORECASE)


def _scope_text(text: str) -> str:
    return _NEGATED_DM_RE.sub(" ", _QUOTED_RE.sub(" ", text))


@dataclass(frozen=True)
class CredentialNeed:
    kind: str                              # the credential_verdicts kind
    label: str                             # what the owner reads
    tools: Tuple[str, ...]                 # tool ids that can need the credential
    topic: Optional[re.Pattern] = None     # task text => uses those tools (full rig)
    scope: Optional[re.Pattern] = None     # task text => uses the credentialed part
    #: tool id -> the flag that must be ON for it to serve instead
    alternatives: Dict[str, str] = field(default_factory=dict)
    honor_backoff: bool = False
    #: the pack treats a token record newer than the verdict as a re-login
    store_supersedes: bool = False


NEEDS: Tuple[CredentialNeed, ...] = (
    CredentialNeed(kind="x_oauth2", label="X login (OAuth 2.0, DMs)",
                   tools=("twitter",), scope=_DM_RE, store_supersedes=True),
    CredentialNeed(kind="twitter_api", label="X API (credits)",
                   tools=("twitter",), topic=_X_RE,
                   alternatives={"x_browser": "X_BROWSER_ENABLED"}, honor_backoff=True),
)


def _declared_tools(payload: Dict[str, Any]) -> Optional[List[str]]:
    """The job's own narrow toolset, or None for "everything" (no rig / full)."""
    own = payload.get("tools")
    if own:
        return [str(t) for t in own]
    rig = str(payload.get("rig") or "").strip()
    if rig:
        from core.config_policy.rigs import rig_tools
        return rig_tools(rig)
    from core.config_policy.rigs import default_rig_name, rig_tools
    return rig_tools(default_rig_name())


def _needs(row: CredentialNeed, declared: Optional[Sequence[str]], text: str) -> bool:
    if declared is not None:
        if not set(declared) & set(row.tools):
            return False
    elif row.topic is not None and not row.topic.search(text):
        return False
    if row.scope is not None and not row.scope.search(_scope_text(text)):
        return False
    if row.alternatives:
        from core.env import bool_env
        for tool, flag in row.alternatives.items():
            if (declared is None or tool in declared) and bool_env(flag, False):
                return False
    return True


def _open_verdict(row: CredentialNeed, data_dir: str):
    from core.credential_verdicts import active
    held = active(row.kind, live_only=row.honor_backoff)
    if not held:
        return None
    v = min(held, key=lambda x: x.first_seen)
    if row.store_supersedes:
        from core.status_snapshot import x_oauth2_store_changed_since
        if x_oauth2_store_changed_since(v, data_dir):
            return None
    return v


# --- once per verdict episode ---------------------------------------------------

def _claim_notice(data_dir: str, user_id: str, kind: str, first_seen: float) -> bool:
    """True the FIRST time this (user, kind, episode) is claimed. Colocated in
    cron.db beside ``remedy_streak``. A store error answers False (no notice
    rather than one per run)."""
    try:
        from core.runtime_paths import cron_db_path
        from core.sqlite_util import execute_retry
        db = cron_db_path(data_dir)
        execute_retry(db, """CREATE TABLE IF NOT EXISTS credential_preflight_notice (
                                 user_id TEXT NOT NULL, kind TEXT NOT NULL,
                                 first_seen REAL NOT NULL, notified_at REAL NOT NULL,
                                 PRIMARY KEY (user_id, kind, first_seen))""")
        before = execute_retry(
            db, "SELECT 1 FROM credential_preflight_notice WHERE user_id=? AND kind=? "
                "AND first_seen=?", (user_id, kind, float(first_seen)), fetch="one")
        if before:
            return False
        execute_retry(db, "INSERT OR IGNORE INTO credential_preflight_notice "
                          "(user_id, kind, first_seen, notified_at) VALUES (?,?,?,?)",
                      (user_id, kind, float(first_seen), time.time()))
        return True
    except Exception:
        logger.warning("credential preflight: notice claim failed — not notifying", exc_info=True)
        return False


def _notice(job: Any, row: CredentialNeed, v: Any) -> str:
    from core.credential_verdicts import since_text
    name = " ".join(str(getattr(job, "task", "") or "").split())[:60] or str(job.id)
    remedy = v.remedy or "see /status"
    return (f"⏸ I am skipping scheduled job \"{name}\" ({job.id}) at $0, and every other "
            f"job that needs the {row.label}: it is refused since {since_text(v)} "
            f"({v.code or 'rejected'}). Fix: {remedy}. The jobs run again once it is "
            f"fixed. I will not repeat this notice for this outage.")


def credential_skip(job: Any, payload: Dict[str, Any], *, data_dir: str):
    """A ``cron.preflight.PreflightSkip`` when the job needs a credential with an
    open verdict, else None. The skip carries ``notice`` only on the FIRST skip
    of that verdict episode. Never raises."""
    from cron.preflight import PreflightSkip
    try:
        declared = _declared_tools(payload)
        text = str(getattr(job, "task", "") or "")
        for row in NEEDS:
            if not _needs(row, declared, text):
                continue
            v = _open_verdict(row, data_dir)
            if v is None:
                continue
            notice = None
            if _claim_notice(data_dir, str(getattr(job, "user_id", "") or ""), row.kind,
                             v.first_seen):
                notice = _notice(job, row, v)
            return PreflightSkip(REASON, {"preflight": KIND_CREDENTIAL, "verdict": row.kind,
                                          "verdict_code": v.code or ""}, notice=notice)
    except Exception:
        logger.warning("credential preflight error — failing open (running the tick)",
                       exc_info=True)
    return None


__all__ = ["NEEDS", "CredentialNeed", "credential_skip", "KIND_CREDENTIAL", "REASON"]
