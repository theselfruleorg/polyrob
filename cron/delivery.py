"""Cron result delivery (W3, Reference-parity §_deliver_result).

After a cron job runs the agent loop (W3 run-loop fix), its final result can be
delivered out-of-band to an external sink — email, twitter, or telegram — so a
scheduled task can actually *report back* instead of dying silently in a session.

Design constraints (Fusion-validated):

- **Allowlisted targets** ``{telegram, email, twitter}`` — never an arbitrary sink.
- **`[SILENT]`** anywhere in the result (case-insensitive) suppresses delivery,
  matching the Reference convention (the agent opts a run out of notifying).
- **Tenant-scoped** — the recipient is resolved from the *job's owner*, not from a
  free-form payload address, except where the agent is delivering to its own
  configured channel. A cron job must not be able to exfiltrate to a stranger.
- **Fail-open** — a sink that errors (or is unconfigured) logs and returns False;
  it NEVER fails the cron job. Delivery is best-effort reporting, not core work.
- **Inside the budget** — callers invoke this from within the scheduler's
  ``asyncio.wait_for(runner(job), timeout=max_duration_seconds)``, so delivery I/O
  shares the per-run hard cap rather than running unbounded after a hard-cancel.

Gated by ``CRON_DELIVERY_ENABLED`` (default OFF) at the call site (``cron/runner.py``).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

ALLOWED_TARGETS = ("telegram", "email", "twitter")
SILENT_MARKER = "[SILENT]"


def effective_digest_channel(user_id: Optional[str], home_dir: Optional[str],
                             *, default: str = "telegram") -> str:
    """Owner's preferred digest delivery channel: pref (override, spec
    ``digest.channel``) over ``default`` (no env backing this knob — it is a
    pure preference). No pref file present => ``default`` unchanged (owner-UX
    P1 T4), preserving ``cron/digest.py``'s legacy ``payload.get("deliver") or
    "telegram"`` fallback."""
    from core import prefs
    return prefs.resolve("digest.channel", user_id, home_dir,
                         env_value=None, default=default)


def _allow_explicit_target() -> bool:
    """Whether an agent-supplied ``deliver_target`` (arbitrary address/chat) is honored.

    Default FALSE → delivery always goes to the JOB OWNER's own channel, closing the
    exfiltration vector where a (possibly prompt-injected) cron job sets
    ``deliver_target="attacker@evil.com"``. Operators of trusted single-user installs
    can opt in with ``CRON_DELIVERY_ALLOW_EXPLICIT_TARGET=true``.
    """
    import os
    return os.getenv("CRON_DELIVERY_ALLOW_EXPLICIT_TARGET", "").strip().lower() in (
        "1", "true", "yes", "on")


def is_silent(final: Optional[str]) -> bool:
    """True when the agent opted this run out of delivery via ``[SILENT]``."""
    return bool(final) and SILENT_MARKER in final.upper()


def delivery_outcome(final: Optional[str], ok: bool) -> str:
    """Classify a cron delivery for the observability log: ``suppressed`` (agent chose
    ``[SILENT]``), ``sent`` (delivered), or ``failed`` (real send failure).

    The runner previously logged both a ``[SILENT]`` opt-out and a genuine send error
    as ``ok=False``, so harmless status-digest opt-outs were indistinguishable from
    broken deliveries in the journal. ``is_silent`` takes precedence because a silent
    run never attempts a send (``deliver_result`` returns False for it).
    """
    if is_silent(final):
        return "suppressed"
    return "sent" if ok else "failed"


async def deliver_result(
    task_agent: Any,
    job: Any,
    final: Optional[str],
    *,
    target: Optional[str],
    deliver_target: Optional[str] = None,
    session_id: Optional[str] = None,
) -> bool:
    """Deliver a cron job's final result to an allowlisted external sink.

    Returns True on a successful send, False otherwise (unknown/blank target,
    empty/`[SILENT]` result, sink unconfigured, or any sink exception). Never
    raises — delivery must not fail the job.

    ``session_id`` (Task 7, optional): when provided, a SUCCESSFUL send marks the
    episodic row for this session as ``surfaced`` (the digest builder then omits
    it via ``exclude_surfaced=True``, avoiding double-reporting what was already
    delivered out-of-band). Callers must pass ``session_id`` AFTER the episode
    row has been written (``finalize_episode``) — the mark is a plain ``UPDATE``
    that no-ops if the row doesn't exist yet (see ``cron/runner.py`` ordering).
    """
    if not target:
        return False
    target = target.strip().lower()
    if target not in ALLOWED_TARGETS:
        logger.warning("cron delivery: target %r not in allowlist %s", target, ALLOWED_TARGETS)
        return False
    if not final or not final.strip():
        return False
    if is_silent(final):
        logger.info("cron delivery: job %s opted out via [SILENT]", getattr(job, "id", "?"))
        return False

    # Security: ignore an agent-supplied explicit recipient unless an operator opted in.
    # By default, deliver only to the job owner's own channel (no exfiltration).
    if deliver_target and not _allow_explicit_target():
        logger.info("cron delivery: ignoring explicit deliver_target (owner-only by default)")
        deliver_target = None

    ok = False
    try:
        if target == "email":
            ok = await _deliver_email(task_agent, job, final, deliver_target)
        elif target == "twitter":
            ok = await _deliver_twitter(task_agent, job, final)
        elif target == "telegram":
            ok = await _deliver_telegram(task_agent, job, final, deliver_target)
    except Exception as e:  # fail-open: a delivery error never fails the job
        logger.error("cron delivery to %s failed for job %s: %s",
                     target, getattr(job, "id", "?"), e, exc_info=True)
        return False
    if ok and session_id:
        _mark_surfaced(session_id, getattr(job, "user_id", None))
    return ok


def _mark_surfaced(session_id: str, user_id: Optional[str] = None) -> None:
    """Fail-open: mark the delivered session's episode as surfaced (Task 7).

    Scoped to the job's own ``user_id`` (episodes are keyed on the composite
    ``(user_id, session_id)``) so a session_id collision across tenants can't
    flip another tenant's row.
    """
    try:
        from modules.memory.registry import get_memory_registry
        prov = get_memory_registry().active()
        if prov is not None and hasattr(prov, "mark_episode_surfaced"):
            prov.mark_episode_surfaced(session_id=session_id, user_id=user_id)
    except Exception:
        logger.debug("cron delivery: mark_episode_surfaced skipped", exc_info=True)


# --- sinks -------------------------------------------------------------------
# Each sink resolves its tool lazily from the task_agent's container/config and is
# wrapped by deliver_result's try/except. Kept as module functions so tests can
# monkeypatch them without standing up SMTP/Twitter/Telegram.

def _config_and_container(task_agent: Any):
    config = getattr(task_agent, "config", None)
    container = getattr(task_agent, "container", None)
    return config, container


async def _deliver_email(task_agent: Any, job: Any, final: str, deliver_target: Optional[str]) -> bool:
    # Tenant scope: only the job owner's own address. ``deliver_target`` is honored
    # only as an explicit user-supplied override (the agent scheduling for itself);
    # callers that don't trust it should leave it None and rely on owner lookup.
    to_email = deliver_target or _owner_email(task_agent, job)
    if not to_email:
        logger.info("cron delivery: no email recipient for job %s", getattr(job, "id", "?"))
        return False
    config, container = _config_and_container(task_agent)
    from tools.email_tool import EmailTool
    tool = EmailTool("email", config, container)
    await tool.ensure_initialized()
    subject = f"[POLYROB cron] {getattr(job, 'task', 'scheduled task')[:60]}"
    return bool(await tool.send_email(to_email=to_email, subject=subject, body=final))


async def _deliver_twitter(task_agent: Any, job: Any, final: str) -> bool:
    config, container = _config_and_container(task_agent)
    from tools.twitter_tool import TwitterTool
    tool = TwitterTool("twitter", config, container)
    text = final.strip()
    if len(text) > 280:
        text = text[:277] + "..."
    return bool(await tool.post(text=text))


async def _deliver_telegram(task_agent: Any, job: Any, final: str, deliver_target: Optional[str]) -> bool:
    # Recipient + sink resolution both live on the user-delivery rail (T6): the rail
    # does its own sink lookup and records a durable owner_notice fallback when no
    # live sink exists — strictly better than the old silent early-False. We resolve
    # the chat id up front only because the proactive-send gate needs it.
    _, container = _config_and_container(task_agent)
    chat_id = deliver_target or _owner_telegram(task_agent, job)
    if not chat_id:
        logger.info("cron delivery: no telegram recipient for job %s", getattr(job, "id", "?"))
        return False

    # Gate proactive send by surface send-policy (WhatsApp 24h window etc.).
    # For Telegram (no window), resolve_proactive_send returns ("send", None) — no-op.
    from core.surfaces.proactive import resolve_proactive_send
    _action, _extra = await resolve_proactive_send(container, "telegram", chat_id, final)
    if _action == "suppress":
        logger.info("cron delivery: suppressed (send policy) for job %s", getattr(job, "id", "?"))
        return False
    if _action == "template":
        # TODO(4.x): send approved template instead of suppress
        logger.info(
            "cron delivery: send window closed; template required (job %s) — suppressing free-text",
            getattr(job, "id", "?"),
        )
        return False

    # §3.2: the actual send rides the ONE user-delivery rail — content-hash
    # dedup + per-tenant caps (proposal 006's duplicate-spam class) and the
    # durable owner_notice fallback live there, shared with agent sends and
    # framework notices. The cron layer keeps its own gates above.
    from core.surfaces.user_delivery import deliver_user_message
    outcome = await deliver_user_message(
        container, str(getattr(job, "user_id", "") or ""), final,
        source="cron", recipient_override=chat_id)
    if outcome != "sent":
        logger.info("cron delivery: rail outcome=%s for job %s",
                    outcome, getattr(job, "id", "?"))
    return outcome == "sent"


def _owner_email(task_agent: Any, job: Any) -> Optional[str]:
    """Resolve the job owner's email (tenant scope).

    1. A registered ``user_directory`` service (real multi-user store) — the hook
       point for a proper user store.
    2. E3 fallback: the single configured ``POLYROB_OWNER_EMAIL`` for single-owner
       headless deploys where nothing registers ``user_directory`` (mirrors the
       telegram ``_configured_owner_telegram_id`` fallback). Without this, the
       user_directory service is never registered, so ``deliver="email"`` was
       always unreachable.
    """
    container = getattr(task_agent, "container", None)
    if container is not None and hasattr(container, "get_service"):
        users = container.get_service("user_directory")
        if users is not None and hasattr(users, "get_email"):
            try:
                addr = users.get_email(getattr(job, "user_id", None))
                if addr:
                    return addr
            except Exception:
                pass
    # 2. single-owner env fallback (SSOT in core.instance)
    try:
        from core.instance import resolve_owner_email
        return resolve_owner_email()
    except Exception:
        return None


def _configured_owner_telegram_id() -> Optional[str]:
    """The single configured owner telegram id, for headless single-owner deploys.

    A goal/cron run started by the autonomy loops carries a non-numeric tenant id
    (e.g. ``"rob"`` — now also the aliased tenant of the owner's OWN chat), so it
    cannot self-resolve a telegram chat. When the deploy is owner-locked to exactly
    ONE telegram id via ``ALLOWED_TELEGRAM_USER_IDS`` (or an explicit
    ``POLYROB_OWNER_TELEGRAM_ID``), that id IS the owner's chat.

    SSOT: delegates to ``core.instance.resolve_owner_telegram_id`` — the SAME resolver
    the inbound owner alias uses, so the id an owner-tg sender is aliased FROM and the
    chat an out-of-band delivery is routed TO stay in lockstep.
    """
    from core.instance import resolve_owner_telegram_id
    return resolve_owner_telegram_id()


def _owner_telegram(task_agent: Any, job: Any) -> Optional[str]:
    """Resolve the job owner's telegram chat id (tenant scope).

    Delegates to the ONE canonical resolver on the user-delivery rail
    (user_directory → digit-uid-IS-chat-id → owner principal) so cron and the
    rail can never disagree about who "the owner" is (audit T6, 2026-07-16 —
    previously a byte-similar copy of ``user_delivery._resolve_recipient``).
    """
    import core.surfaces.user_delivery as _ud
    container = getattr(task_agent, "container", None)
    return _ud.resolve_telegram_recipient(
        container, str(getattr(job, "user_id", "") or ""))
