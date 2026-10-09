"""The shell command guard on the controller path (073 W2).

Two pieces, both over ``core.security.command_guard.classify``:

- :func:`make_command_guard_hook` — a fail-closed PRE hook, registered BEFORE the
  approval hooks, so a refused command never files an owner ask. It refuses the
  floor always, a command on the owner's deny list (``SHELL_DENY`` / ``shell.deny``)
  always, and the dangerous class on an UNATTENDED turn (a goal / cron / planner
  run) whenever `shell_run` is approval-gated: nobody is there to decide, so the
  answer is no, not a 300 s wait (the usual unattended ``deny`` mode). In ``smart`` mode it
  also asks the aux model to triage a dangerous command (D3, below).
- :func:`make_shell_command_exemption` — the approval hook's ``exempt_fn``: a command
  the guard calls ``ok``, or one on the owner's allow list (``SHELL_ALLOW`` /
  ``shell.allow``; never a floor command), skips the owner wait. So a gated
  `shell_run` asks the owner only for the dangerous class (a per-command
  prompt) instead of for every ``ls``.

``SHELL_APPROVAL_MODE``:
  ``per_command`` (default) — the above;
  ``every``  — every gated `shell_run` waits (the pre-073 behaviour; lists still apply);
  ``smart``  — ``per_command`` + aux-model triage of the dangerous class (a
               ``smart`` mode). The OWNER CEILING (D3): the model may APPROVE only a command
               that runs inside a sandbox — never on the host — and only on an attended
               turn; it may DENY anywhere; anything else, an error or a timeout escalates
               to the owner as usual.

No second approval queue: the dangerous class rides the SAME provider/owner_queue
the gated set already uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable, Dict, Optional, Sequence

logger = logging.getLogger(__name__)

SHELL_ACTIONS = frozenset({"shell_run", "coding_run_tests", "code_execution_run_code"})
#: Text typed into a running background job's stdin. The job may be a shell
#: (``script``, ``ssh``, an owner-approved ``bash``), so the floor and the owner's
#: deny list apply to the typed text too. The dangerous class does not: input to
#: a REPL or an installer is not a shell line, and starting the job was judged.
STDIN_ACTIONS = frozenset({"process_write", "process_submit"})
APPROVAL_MODES = ("per_command", "every", "smart")
#: context.metadata key the smart pre-hook stamps for the exemption to read.
SMART_APPROVED_KEY = "shell_smart_approved"
_SMART_TIMEOUT_SEC = 20.0


def shell_approval_mode() -> str:
    """``SHELL_APPROVAL_MODE``: ``per_command`` (default) | ``every`` | ``smart``.
    Garbage -> ``every`` (the stricter answer). Read once per Controller."""
    raw = (os.getenv("SHELL_APPROVAL_MODE") or "per_command").strip().lower()
    return raw if raw in APPROVAL_MODES else "every"


def shell_lists(user_id: Optional[str] = None, home_dir: Any = None):
    """``(allow_globs, deny_globs)``: env (``SHELL_ALLOW``/``SHELL_DENY``) UNION the
    owner's ``shell.allow``/``shell.deny`` prefs. Read once per Controller (the
    prefs apply next session, like ``approvals.*``). Fail-closed on the deny side:
    an unreadable pref keeps the env list."""
    from core.security.command_guard import parse_globs
    allow_env = parse_globs(os.getenv("SHELL_ALLOW"))
    deny_env = parse_globs(os.getenv("SHELL_DENY"))
    allow, deny = allow_env, deny_env
    if user_id and home_dir is not None:
        try:
            from core import prefs
            allow = parse_globs(prefs.resolve("shell.allow", user_id, home_dir,
                                              env_value=list(allow_env), default=[]))
            deny = parse_globs(prefs.resolve("shell.deny", user_id, home_dir,
                                             env_value=list(deny_env), default=[]))
        except Exception:
            logger.warning("shell allow/deny prefs unreadable; env lists only", exc_info=True)
    return allow, deny


#: ``run_code`` languages every backend runs through a shell (``bash -c``).
SHELL_LANGUAGES = frozenset({"bash", "sh", "shell", "zsh", "dash", "ksh"})


def _command(params: Optional[Dict[str, Any]], action_name: str = "shell_run") -> str:
    """The shell text an action runs; ``""`` for ``run_code`` in a non-shell
    language (a program, not a shell line — its own approval gate decides)."""
    params = params or {}
    if action_name == "code_execution_run_code":
        language = str(params.get("language") or "python").strip().lower()
        if language in SHELL_LANGUAGES:
            return str(params.get("code") or "")
        return ""
    if action_name == "coding_run_tests":
        # run_tests runs `pytest -q` when no command is given (tools/coding/tool.py);
        # judge that line, or the default test run waits on the owner every time.
        return str(params.get("command") or "pytest -q")
    return str(params.get("command") or "")


def make_shell_command_exemption(mode: Optional[str] = None, *,
                                 allow_globs: Sequence[str] = (),
                                 inherited: Sequence[str] = ()) -> Callable:
    """``inherited``: the execution actions (``coding_run_tests`` /
    ``code_execution_run_code``) gated ONLY because ``shell_run`` is. They follow
    the shell rule — the owner decides the dangerous class — so a routine test
    run or a Python program does not wait on the owner every time."""
    mode = mode or shell_approval_mode()
    allow = tuple(allow_globs or ())
    inherited_set = frozenset(inherited or ())

    def _exempt(action_name: str, params: Dict[str, Any], context: Any = None) -> Optional[str]:
        if action_name != "shell_run" and action_name not in inherited_set:
            return None
        command = _command(params, action_name).strip()
        if not command:
            if action_name == "code_execution_run_code":
                return "not a shell command; the shell rule gates only shell lines"
            return None  # nothing to classify is never "safe"
        from core.security.command_guard import OK, classify, matches_any
        verdict = classify(command)
        if verdict.is_floor:
            return None  # the pre-hook refuses it; never exempt
        hit = matches_any(command, allow, for_allow=True)
        if hit:
            return f"on the owner's shell allow list ({hit})"
        if mode == "every":
            return None
        if verdict.level == OK:
            return "the shell guard classes this command as safe"
        if mode == "smart":
            meta = getattr(context, "metadata", None)
            if isinstance(meta, dict) and meta.get(SMART_APPROVED_KEY) == command:
                return "approved by the smart triage (sandbox only)"
        return None

    _exempt.takes_context = True  # make_approval_hook passes the execution context
    return _exempt


def shell_command_exemption(action_name: str, params: Dict[str, Any]) -> Optional[str]:
    """Module-level form of :func:`make_shell_command_exemption` (env read per call)."""
    return make_shell_command_exemption()(action_name, params)


def _unattended(context: Any) -> bool:
    try:
        from agents.task.session_class import is_autonomous_session
        return is_autonomous_session(getattr(context, "session_id", None))
    except Exception:
        return True


def _runs_on_host(context: Any) -> bool:
    """True when THIS turn's shell would run on the host — or when that cannot be
    decided (fail toward the stricter answer: no model approval)."""
    try:
        from tools.shell.backend_pool import host_selected
        return bool(host_selected(context))
    except Exception:
        return True


_TRIAGE_PROMPT = (
    "You review ONE shell command an AI agent wants to run inside a disposable sandbox "
    "container on behalf of its owner. A pattern guard flagged it as risky: {reason}.\n"
    "Answer with exactly one word:\n"
    "APPROVE  - clearly routine and low risk for a development sandbox;\n"
    "DENY     - clearly destructive, exfiltrating, or hiding what it does;\n"
    "ESCALATE - anything else (the owner decides).\n\n"
    "Command:\n{command}"
)


async def smart_triage(command: str, reason: str, llm_factory: Callable) -> str:
    """``approve`` | ``deny`` | ``escalate``. Fail-safe: any fault escalates."""
    try:
        llm = await llm_factory()
        if llm is None:
            return "escalate"
        from modules.llm.messages import HumanMessage
        reply = await asyncio.wait_for(
            llm.ainvoke([HumanMessage(content=_TRIAGE_PROMPT.format(reason=reason, command=command))]),
            timeout=_SMART_TIMEOUT_SEC)
        text = str(getattr(reply, "content", reply) or "").strip().upper()
        word = text.split()[0].strip(".:!*`") if text.split() else ""
        return {"APPROVE": "approve", "DENY": "deny"}.get(word, "escalate")
    except Exception:
        logger.debug("smart shell triage failed -> escalate", exc_info=True)
        return "escalate"


def orchestrator_llm_factory(orchestrator) -> Callable:
    """The aux 'judge' model of the session's agent, else its main model."""
    async def _factory():
        agents = list((getattr(orchestrator, "agents", None) or {}).values())
        for agent in agents:
            prov = getattr(agent, "_provision_aux_llm_async", None)
            if prov is not None:
                try:
                    llm = await prov("judge")
                    if llm is not None:
                        return llm
                except Exception:
                    pass
            if getattr(agent, "llm", None) is not None:
                return agent.llm
        return None
    return _factory


def make_command_guard_hook(*, shell_gated: bool, deny_globs: Sequence[str] = (),
                            mode: Optional[str] = None,
                            llm_factory: Optional[Callable] = None) -> Callable:
    """Pre hook: floor / deny list -> refuse; dangerous + unattended + gated -> refuse;
    smart mode: triage the dangerous class (approve in a sandbox only, or deny)."""
    deny = tuple(deny_globs or ())
    mode = mode or shell_approval_mode()

    async def _hook(action_name, params, context):
        if action_name in STDIN_ACTIONS:
            from core.security.command_guard import classify, matches_any
            typed = str((params or {}).get("data") or "")
            if not typed.strip():
                return None
            verdict = classify(typed)
            if verdict.is_floor:
                return (f"refused: this input {verdict.reason}. It is on the shell guard's "
                        "floor and no approval can allow it, typed into a job or not.")
            hit = matches_any(typed.strip(), deny)
            if hit:
                return (f"refused: this input matches the owner's shell deny list ({hit}). "
                        "Do not retry it in another form.")
            return None
        if action_name not in SHELL_ACTIONS:
            return None
        from core.security.command_guard import classify, matches_any
        command = _command(params, action_name)
        if not command.strip():
            return None  # not a shell line (a run_code program): nothing to judge
        verdict = classify(command)
        if verdict.is_floor:
            return (f"refused: this command {verdict.reason}. It is on the shell guard's "
                    "floor and no approval can allow it.")
        hit = matches_any(command, deny)
        if hit:
            return (f"refused: this command matches the owner's shell deny list ({hit}). "
                    "Do not retry it in another form.")
        if not verdict.is_dangerous or not shell_gated:
            return None
        if _unattended(context):
            from core.security.refusals import record_refusal
            record_refusal("approval_denied", tool=action_name,
                           user_id=getattr(context, "user_id", None) or "",
                           session_id=getattr(context, "session_id", None) or "",
                           detail=f"unattended dangerous shell command: {verdict.reason}")
            return (f"refused: this command {verdict.reason}, and an unattended run has no "
                    "owner to approve it. Do it another way, or leave it for an owner turn.")
        if mode == "smart" and llm_factory is not None:
            decision = await smart_triage(command, verdict.reason, llm_factory)
            if decision == "deny":
                return (f"refused by the smart shell triage: this command {verdict.reason}. "
                        "Ask the owner if it is really needed.")
            if decision == "approve" and not _runs_on_host(context):
                meta = getattr(context, "metadata", None)
                if isinstance(meta, dict):
                    meta[SMART_APPROVED_KEY] = command.strip()
        return None

    return _hook


__all__ = ["make_command_guard_hook", "make_shell_command_exemption",
           "shell_command_exemption", "shell_approval_mode", "shell_lists",
           "smart_triage", "orchestrator_llm_factory", "SHELL_ACTIONS", "STDIN_ACTIONS",
           "SMART_APPROVED_KEY"]
