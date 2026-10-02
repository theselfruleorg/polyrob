"""Deterministic WRITE job: one allowlisted money verb, no model turn (the collection
revealer).

The W9 read job (``cron/read_job.py``) runs a READ verb with no session and no LLM. This is
its write twin, and it is deliberately narrow:

* ``WRITE_VERBS`` holds exactly ONE verb, ``agent_nft.agent_nft_collection_reveal`` —
  ``reveal(ids)`` on a pinned collection profile with the ``reveal`` capability. It
  sends no value and moves no asset; it spends gas only, capped per transaction by
  ``AGENT_NFT_REVEAL_MAX_GAS_USD``, and its transaction goes through ``tx_guard.authorize``
  (the ``is_collection_reveal`` shape) like any other money verb — the job adds no second
  gate and grants nothing the verb does not already check. (A job still carrying the verb's
  pre-069 name is refused at validation, never run.)
* OFF unless the owner sets ``CRON_WRITE_JOBS_ENABLED=true``; a tick with the flag off is a
  $0 refusal (``cron_run skipped/write_jobs_disabled``), never a send.
* Only an OWNER-authored job runs: an owner seat, or ``cronjob_schedule`` on a genuine owner
  turn (``is_agent_authored`` false). ``cronjob_schedule``/``edit`` refuse ``write_verb`` on
  any other turn as well.
* The owner pause (``cron_run``, checked by the runner before this) and ``pause_windows`` hold
  it; the verb re-checks the pause, and ``tx_guard`` checks it a third time.

Payload — ``payload.write_verb``::

    {"verb": "agent_nft.agent_nft_collection_reveal",
     "params": {"chain": "robinhood", "max_ids": 10, "dry_run": false},
     "deliver_on": "alert"}          # "alert" (default) | "always" | "never"

⚠️ The verb's ``dry_run`` defaults to TRUE: a job that omits ``"dry_run": false`` only
simulates. That is the safe default, not a bug.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

FLAG = "CRON_WRITE_JOBS_ENABLED"

#: verb -> (module, tool class, action, params class). Writes, one row only.
WRITE_VERBS: Dict[str, Tuple[str, str, str, str]] = {
    "agent_nft.agent_nft_collection_reveal": ("tools.agent_nft.tool", "AgentNftTool",
                                              "agent_nft_collection_reveal", "RevealParams"),
}

#: What each verb spends, in words the owner reads on every run and at schedule time.
SPENDS: Dict[str, str] = {
    "agent_nft.agent_nft_collection_reveal": (
        "gas only, from the treasury key — at most AGENT_NFT_REVEAL_MAX_GAS_USD of fee per "
        "transaction, one transaction per run; no value, no asset"),
}


class WriteJobError(ValueError):
    """A write-job payload that cannot run as written."""


def write_jobs_enabled() -> bool:
    """``CRON_WRITE_JOBS_ENABLED`` (default OFF): whether a scheduled job may run an
    allowlisted WRITE verb with no model turn."""
    from core.env import bool_env
    return bool_env(FLAG, False)


def _load(verb: str):
    import importlib
    module, cls, _action, model = WRITE_VERBS[verb]
    mod = importlib.import_module(module)
    return getattr(mod, cls), getattr(mod, model)


def validate_write_verb(spec: Any) -> Dict[str, Any]:
    """The normalized spec, or WriteJobError. The params are validated against the verb's
    own model now, so a bad job is refused at schedule time, never at a tick."""
    from cron.read_job import DELIVER_ON
    if not isinstance(spec, dict):
        raise WriteJobError("write_verb must be an object {verb, params, deliver_on}")
    verb = str(spec.get("verb") or "")
    if verb not in WRITE_VERBS:
        raise WriteJobError(f"write_verb {verb!r} is not an allowlisted write verb; "
                            f"allowed: {', '.join(sorted(WRITE_VERBS))}")
    deliver_on = str(spec.get("deliver_on") or "alert")
    if deliver_on not in DELIVER_ON:
        raise WriteJobError(f"deliver_on must be one of {', '.join(DELIVER_ON)}")
    params = spec.get("params") or {}
    if not isinstance(params, dict):
        raise WriteJobError("write_verb.params must be an object")
    _cls, model = _load(verb)
    try:
        model(**params)
    except Exception as exc:
        raise WriteJobError(f"write_verb params do not validate: {exc}") from exc
    return {"verb": verb, "params": params, "deliver_on": deliver_on}


def refusal(payload: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """``(reason_slug, text)`` when this job may not run its write verb now, else None."""
    if not write_jobs_enabled():
        return ("write_jobs_disabled",
                f"write job refused: {FLAG} is off — the owner enables scheduled write jobs. "
                f"Nothing was broadcast.")
    from core.config_policy.rigs import is_agent_authored
    if is_agent_authored(payload):
        return ("write_job_not_owner",
                "write job refused: only a job the owner scheduled may run a write verb. "
                "Nothing was broadcast.")
    return None


async def call_write_verb(spec: Dict[str, Any], *, tool_factory=None) -> Tuple[bool, str]:
    """``(ok, text)``. Never raises; a verb error is ``(False, error)``. No execution
    context: this is not a model turn, so the guard treats it as a bare internal call —
    never the owner (the pause and the autonomous ceiling apply)."""
    try:
        spec = validate_write_verb(spec)
        cls, model = _load(spec["verb"])
        tool = (tool_factory or cls)()
        action = WRITE_VERBS[spec["verb"]][2]
        result = await getattr(tool, action)(model(**spec["params"]), execution_context=None)
    except Exception as exc:
        return False, f"write job error: {exc}"
    err = getattr(result, "error", None)
    if err:
        return False, str(err)
    return True, str(getattr(result, "extracted_content", "") or "")


async def run_write_job(task_agent: Any, job: Any, *, tool_factory=None) -> Tuple[bool, str]:
    """Run the job's write verb and deliver per ``deliver_on``. Returns ``(ok, reason)``;
    never raises. The caller has already applied the owner pause and the pause window."""
    from cron.read_job import should_deliver
    payload = dict(getattr(job, "payload", None) or {})
    spec = payload.get("write_verb") or {}
    blocked = refusal(payload)
    if blocked:
        return False, blocked[0]
    ok, text = await call_write_verb(spec, tool_factory=tool_factory)
    verb = str((spec or {}).get("verb") or "")
    if SPENDS.get(verb):
        text = f"{text}\n(this job spends {SPENDS[verb]})"
    deliver_on = str((spec or {}).get("deliver_on") or "alert")
    if not should_deliver(deliver_on, ok, text):
        return ok, "write_verb"
    try:
        from cron.delivery import deliver_result
        sent = await deliver_result(task_agent, job, text, target=payload.get("deliver") or "telegram",
                                    deliver_target=payload.get("deliver_target"))
    except Exception:
        logger.warning("cron job %s: write-job delivery failed", getattr(job, "id", "?"),
                       exc_info=True)
        sent = False
    return ok, "write_verb_delivered" if sent else "write_verb_undelivered"
