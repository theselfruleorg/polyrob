"""The Telegram seat's owner-admin verb table.

W9 (2026-10-10 structural review): this replaces the ~42-branch ``if cmd ==``
chain that was ``surfaces/telegram/harness.py::_handle_owner_admin``. The
harness keeps the ORDER that matters and nothing else:

  1. :func:`pre_gate_reply` — the room verbs a ROOM ADMIN may run too
     (``/groups mode|tail|role``, ``/mute``, ``/unmute``, ``/ban``,
     ``/unban``, ``/paid``). ``group_ops`` does its own role check;
  2. the owner gate (``harness._is_admin_owner``);
  3. a verb a pack contributed WITH a Telegram handler
     (``core.verbs.handler_ref``);
  4. :data:`ADMIN_VERBS` — the built-in handlers below, one per verb;
  5. ``/help`` for anything else.

Every handler takes ONE :class:`AdminCall` and returns the reply text (or a
``core.surfaces.command_reply`` envelope). The ``*_ops`` modules are imported
LAZILY inside each handler, exactly as the chain did, so a test that patches
``surfaces.telegram.job_ops.jobs_reply`` (and friends) still reaches the run.

⚠️ This is a SEAT table, not a second verb table: the words, groups and seats
of every verb stay in ``core.verbs`` (``tests/unit/core/test_verbs.py``), and
the owner-gated set stays ``harness._OWNER_ADMIN_COMMANDS``. A core row cannot
carry a seat handler in ``core.verbs`` (``register_verbs`` refuses a core name,
and the money rows deliberately carry none until 067 P5b), so the built-in
handlers live here, beside the seat that runs them.
"""
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass
class AdminCall:
    """One owner verb, after the owner gate. ``args`` is the line's words after
    the verb (or the tapped id), ``board`` the ONE ``GoalBoard`` this message
    opened."""
    task_agent: Any
    result: Any
    cmd: str
    user_id: str
    data_dir: str
    instance_id: str
    args: list
    board: Any


def _words(result: Any) -> list:
    return result.inbound.text.strip().split()[1:]


async def pre_gate_reply(task_agent: Any, result: Any, cmd: str) -> Optional[str]:
    """The verbs a ROOM ADMIN may run, dispatched BEFORE the owner-only gate.
    ``None`` = not one of them; the caller continues to the owner gate.

    044 T18: `/groups mode|tail` and `/mute` are reachable by a ROOM ADMIN too.
    `group_ops` does its own role check (owner OR that room's admin), so a plain
    member is still refused; every other `/groups` verb (allow/deny/list/set/
    role/service/admins) stays behind the owner-only gate.
    """
    if cmd == "/groups":
        _groups_args = _words(result)
        _groups_verb = _groups_args[0].lower() if _groups_args else "list"
        # 044 T18 + D14: `mode`/`tail` are owner-OR-admin, and so is exactly one
        # shape of `role` — an admin granting `blocked`. Forwarding only the
        # first two made that carve-out UNREACHABLE: an admin's
        # `/groups role here <id> blocked` hit the owner-only gate below and
        # was refused before `group_ops._gate` ever saw it, so the documented
        # "an admin may block a disruptive member" did not exist on Telegram.
        # `group_ops` re-checks the role itself and refuses every other grant,
        # so forwarding grants ROUTING, never authority.
        if _groups_verb in ("mode", "tail", "role"):
            from surfaces.telegram import group_ops
            return await group_ops.groups_reply(task_agent, result, _groups_args)
        return None
    if cmd == "/mute":
        from surfaces.telegram import group_ops
        return await group_ops.mute_reply(task_agent, result, _words(result))
    if cmd in ("/unmute", "/ban", "/unban"):
        # 046 phase 2: the rest of the catalog, same two meanings as `/mute` —
        # in REPLY it names a person (free for the owner and room admins, priced
        # for a member); with no reply it has no room meaning and says so.
        from surfaces.telegram import group_ops
        handler = {"/unmute": group_ops.unmute_reply,
                   "/ban": group_ops.ban_reply,
                   "/unban": group_ops.unban_reply}[cmd]
        return await handler(task_agent, result, _words(result))
    if cmd == "/paid":
        # 046: owner OR that room's admin, checked inside `group_ops` like
        # `/groups mode|tail` — dispatched BEFORE the owner-only gate below.
        from surfaces.telegram import group_ops
        return await group_ops.paid_reply(task_agent, result, _words(result))
    return None


# --- look / control --------------------------------------------------------

async def _status(c: AdminCall) -> str:
    from surfaces.telegram.status_ops import _status_reply
    return await _status_reply(c.task_agent, c.user_id, c.result.decision.session_id,
                               c.data_dir, board=c.board)


async def _mode(c: AdminCall) -> str:
    # 030 WS-E4: the posture card + how to change it — health first
    from surfaces.telegram.status_ops import _health_header, _mode_reply
    header = await _health_header(c.user_id, c.data_dir, c.task_agent)
    return "\n".join(header) + "\n" + _mode_reply()


async def _recap(c: AdminCall) -> str:
    # one recap vocabulary across surfaces (/recap = /journey)
    from surfaces.telegram.status_ops import _health_header, _recap_reply
    header = await _health_header(c.user_id, c.data_dir, c.task_agent, limit=3)
    return "\n".join(header) + "\n" + _recap_reply(c.user_id, c.data_dir, c.args)


async def _avatar(c: AdminCall) -> Any:
    # show / set <url|nft:…> / clear. The image rides the SAME media rail the
    # invoice card uses (MessageRouter), best-effort: if no router is
    # reachable the text still answers rather than failing the verb.
    # `/avatar set` fetches over the network, so it runs off the poll loop
    # (`harness._runs_in_background`).
    from surfaces.telegram import owner_ops as _owner_ops
    if c.args and c.args[0].lower() in ("set", "clear"):
        text, img = await _owner_ops.avatar_change(c.data_dir, c.args)
    else:
        text, img = _owner_ops.avatar_reply(c.data_dir, c.args)
    from core.surfaces.room_keys import is_group_session_key
    # TG7: in a room the text answer goes to the owner's DM; the photo
    # would go to the ROOM's chat id, so a room read sends none.
    if img and not is_group_session_key(c.result.decision.session_key):
        # Looked up on the harness at call time: it is the harness's seam.
        from surfaces.telegram import harness as _h
        await _h._send_photo_best_effort(c.task_agent, c.result, img)
    return text


async def _missed(c: AdminCall) -> str:
    from surfaces.telegram.status_ops import _missed_reply
    return _missed_reply(c.user_id, c.data_dir, c.args)


async def _run(c: AdminCall) -> str:
    # 070: one background run — the core.run_control SSOT
    from agents.task.path import pm
    from core.run_control import run_reply
    return await run_reply(c.user_id, c.data_dir, str(pm().data_root), c.args,
                           exclude=(c.result.decision.session_id,))


async def _goals(c: AdminCall) -> str:
    from surfaces.telegram.status_ops import _goals_reply, _pause_line
    return _pause_line(c.data_dir) + "\n" + _goals_reply(c.user_id, c.data_dir,
                                                         board=c.board)


async def _apps(c: AdminCall) -> str:
    from surfaces.telegram.apps_ops import apps_reply  # 032 durable app service
    from surfaces.telegram.status_ops import _pause_line
    return _pause_line(c.data_dir) + "\n" + apps_reply(c.user_id, c.data_dir, c.args)


async def _prefs(c: AdminCall) -> str:
    from surfaces.telegram.prefs_ops import _prefs_reply
    return _prefs_reply(c.user_id, c.data_dir, c.instance_id,
                        full=bool(c.args) and c.args[0].lower() in ("all", "full"))


async def _config(c: AdminCall) -> str:
    from surfaces.telegram.prefs_ops import _config_reply
    return _config_reply(c.user_id, c.data_dir, c.instance_id, c.args)


async def _model(c: AdminCall) -> str:
    from surfaces.telegram.prefs_ops import model_reply  # 080 D8
    return model_reply(c.user_id, c.args)


async def _usage(c: AdminCall) -> str:
    from surfaces.telegram.usage_ops import usage_reply  # 2026-10-14 D6
    return usage_reply(c.user_id, c.data_dir, c.args)


async def _skills(c: AdminCall) -> str:
    from surfaces.telegram.skills_ops import skills_reply  # 080 D8
    return skills_reply(c.user_id, c.args)


async def _kb(c: AdminCall) -> str:
    # CLI7: the ONE grammar (core.kb_grammar), shared with the REPL.
    from surfaces.telegram.kb_ops import _kb_reply
    return await _kb_reply(c.user_id, c.args)


async def _files(c: AdminCall) -> str:
    from surfaces.telegram.kb_ops import _files_reply
    return await _files_reply(c.user_id, c.args)


async def _dev(c: AdminCall) -> str:
    # Owner ↔ on-host dev-loop rail (proposal 027 WS-1). Raw text, not
    # `args` — the loop should see exactly what the owner typed.
    from surfaces.telegram.dev_rail import perform_dev_command, strip_dev_prefix
    return await perform_dev_command(strip_dev_prefix(c.result.inbound.text))


async def _pause(c: AdminCall) -> str:
    from surfaces.telegram.status_ops import _pause_reply, _seat_via
    return _pause_reply(c.data_dir, c.cmd, c.args, via=_seat_via(c.result))


async def _cwd(c: AdminCall) -> str:
    from surfaces.telegram.status_ops import _cwd_reply
    return _cwd_reply()


# --- G13 / 046 E6-E9: owner write + money verbs ----------------------------
# Each is a thin `*_reply` helper in its own module (the `token_ops` pattern)
# so the REPL and the CLI import the SAME parse and the SAME sentences. REACH,
# not policy: every gate under them is untouched.

async def _claim(c: AdminCall) -> str:
    from surfaces.telegram.claim_ops import claim_reply
    return await claim_reply(c.user_id, c.args)


async def _pay(c: AdminCall) -> str:
    from surfaces.telegram.pay_ops import pay_reply
    # 068 B9: the update_id names this command — stable across a
    # re-delivery, new for every new /pay message.
    _key = getattr(c.result.inbound, "idempotency_key", None)
    return await pay_reply(c.user_id, c.args,
                           request_id=(f"tg:{_key}" if _key else None))


async def _nft(c: AdminCall) -> str:
    from surfaces.telegram.nft_ops import nft_reply
    return await nft_reply(c.user_id, c.args)


async def _dapp(c: AdminCall) -> str:
    from surfaces.telegram.dapp_ops import dapp_reply
    return dapp_reply(c.user_id, c.args)


async def _identity(c: AdminCall) -> str:
    from surfaces.telegram.identity_ops import identity_reply
    return await identity_reply(c.user_id, c.data_dir, c.args)


async def _why(c: AdminCall) -> str:
    from surfaces.telegram.why_ops import why_reply
    return why_reply(c.user_id, c.data_dir, c.args)


async def _contacts(c: AdminCall) -> str:
    from surfaces.telegram.contacts_ops import contacts_reply
    return contacts_reply(c.user_id, c.data_dir, c.args,
                          container=getattr(c.task_agent, "container", None))


async def _thread(c: AdminCall) -> str:
    # 061: the ONE owner transcript (every rail, every seat).
    from surfaces.telegram.history_ops import history_reply
    return history_reply(c.user_id, c.data_dir, c.args,
                         container=getattr(c.task_agent, "container", None))


# D12: the money-quote helpers are `async def` and AWAITED. They used to
# bridge a coroutine onto another loop and BLOCK — stalling the whole polling
# loop for the length of a quote, and handing a loop-affine object to the
# wrong loop (the 2026-09-15 "Future attached to a different loop" bug).

async def _lp(c: AdminCall) -> str:
    from surfaces.telegram.lp_ops import lp_reply
    return await lp_reply(c.user_id, c.args)


async def _launch(c: AdminCall) -> str:
    # 042: token creation shipped CLI-only, which means the one person allowed
    # to run it has to SSH to the box. Same lesson as /bridge.
    from surfaces.telegram import token_ops
    return (await token_ops.launch_reply(c.user_id, c.args) if c.cmd == "/launch"
            else await token_ops.deploy_reply(c.user_id, c.args))


async def _mcp(c: AdminCall) -> str:
    # Giving the agent a new MCP server used to be a box-side file edit, which
    # the owner (on a phone) could not do.
    from surfaces.telegram import owner_ops
    return await owner_ops.mcp_reply(c.user_id, c.args)


async def _bridge(c: AdminCall) -> str:
    # 037: the bridge shipped CLI-only, so the one person allowed to run it had
    # to SSH to the box. He is usually on a phone.
    from surfaces.telegram import owner_ops
    return await owner_ops.bridge_reply(c.user_id, c.data_dir, c.args)


async def _trade(c: AdminCall) -> Any:
    # 086 W5: the trading mandate — start (one card), status, pause, resume, stop, release.
    from surfaces.telegram import trade_ops
    return await asyncio.to_thread(trade_ops.trade_reply, c.user_id, c.data_dir, c.args,
                                   via="telegram")


async def _job(c: AdminCall) -> str:
    # 080 WS-2: one handler for `/jobs` (the list) and `/job <sub> …`.
    from surfaces.telegram import job_ops
    args = ["list"] if c.cmd == "/jobs" else c.args
    return job_ops.jobs_reply(c.user_id, c.data_dir, args, via="telegram")


async def _goal(c: AdminCall) -> str:
    from surfaces.telegram import owner_ops
    return owner_ops.goal_reply(c.user_id, c.data_dir, c.args, board=c.board)


async def _wallet(c: AdminCall) -> str:
    # TG3: `/wallet balances` makes blocking RPC reads (urllib, up to ~20 s);
    # on the poll loop they stalled every chat and every turn.
    from surfaces.telegram import owner_ops
    return await asyncio.to_thread(owner_ops.wallet_reply, c.args,
                                   user_id=c.user_id, data_dir=c.data_dir)


async def _invoices(c: AdminCall) -> str:
    from surfaces.telegram import owner_ops
    return await owner_ops.invoices_reply(c.user_id, c.args)


async def _settle(c: AdminCall) -> str:
    from surfaces.telegram import owner_ops
    return await owner_ops.settle_reply(c.user_id, c.args)


async def _inbox(c: AdminCall) -> str:
    # 043 D1: the ONE list of what is waiting on you — self-evolution
    # proposals, spend approvals, correspondents, blocked goals and apps,
    # composed once and rendered the same way on every seat. /pending stays
    # as the narrower, older view of the first three.
    from surfaces.telegram import owner_ops
    return owner_ops.inbox_reply(c.user_id, c.data_dir)


async def _book(c: AdminCall) -> str:
    if c.args and str(c.args[0]).lower() == "retire":
        # 086 W0: close a gone/dust row on a fresh chain measurement.
        from tools.defi.retire import book_retire_reply
        return await book_retire_reply(c.user_id, c.data_dir, list(c.args[1:]))
    from surfaces.telegram import owner_ops
    return await owner_ops.book_reply(c.user_id, c.data_dir)


async def _groups(c: AdminCall) -> str:
    # Reached here only for the owner-only verbs (mode/tail/role already
    # returned in `pre_gate_reply`, before the owner gate). The sender IS the
    # owner at this point, so `group_ops` grants every verb.
    from surfaces.telegram import group_ops
    return await group_ops.groups_reply(c.task_agent, c.result, c.args)


def _approval(name: str) -> Callable[[AdminCall], Awaitable[Any]]:
    async def _call(c: AdminCall) -> Any:
        from surfaces.telegram import approval_ops
        return await getattr(approval_ops, name)(c)
    _call.__name__ = name
    return _call


async def _restart(c: AdminCall) -> str:
    from surfaces.telegram.lifecycle_ops import origin_of, restart_reply
    return restart_reply(c.user_id, c.data_dir, c.args, origin_of(c.result, c.user_id))


async def _update(c: AdminCall) -> str:
    from surfaces.telegram.lifecycle_ops import origin_of, update_reply
    return await update_reply(c.user_id, c.data_dir, c.args, origin_of(c.result, c.user_id))


#: verb -> handler. ONE row per verb the chain used to branch on.
ADMIN_VERBS: dict[str, Callable[[AdminCall], Awaitable[Any]]] = {
    "/status": _status,
    "/mode": _mode,
    "/recap": _recap,
    "/journey": _recap,
    "/avatar": _avatar,
    "/missed": _missed,
    "/run": _run,
    "/goals": _goals,
    "/apps": _apps,
    "/prefs": _prefs,
    "/config": _config,
    "/model": _model,
    "/usage": _usage,
    "/skills": _skills,
    "/kb": _kb,
    "/files": _files,
    "/dev": _dev,
    "/halt": _pause,
    "/resume": _pause,
    "/pause": _pause,
    "/claim": _claim,
    "/pay": _pay,
    "/nft": _nft,
    "/dapp": _dapp,
    "/identity": _identity,
    "/why": _why,
    "/contacts": _contacts,
    "/thread": _thread,
    "/cwd": _cwd,
    "/lp": _lp,
    "/launch": _launch,
    "/deploy": _launch,
    "/mcp": _mcp,
    "/bridge": _bridge,
    "/trade": _trade,
    "/jobs": _job,
    "/job": _job,
    "/goal": _goal,
    "/wallet": _wallet,
    "/invoices": _invoices,
    "/settle": _settle,
    "/inbox": _inbox,
    "/book": _book,
    "/pending": _approval("pending_verb"),
    "/approve": _approval("decide_verb"),
    "/reject": _approval("decide_verb"),
    "/asks": _approval("asks_verb"),
    "/fulfill": _approval("fulfill_verb"),
    "/allow": _approval("allow_verb"),
    "/deny": _approval("deny_verb"),
    "/allowlist": _approval("allowlist_verb"),
    "/groups": _groups,
    "/restart": _restart,
    "/update": _update,
}


def handler_for(cmd: str) -> Optional[Callable[[AdminCall], Awaitable[Any]]]:
    """The built-in Telegram handler for ``cmd``, or None (the caller renders
    ``/help``)."""
    return ADMIN_VERBS.get(cmd)
