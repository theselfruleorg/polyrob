"""`/identity` — this instance's own ERC-8004 registration (E8).

``register_agent`` and ``set_agent_uri`` shipped as AGENT actions with no human
seat, so the one irreversible identity act this agent can perform was reachable
only by asking the model to perform it.

⚠️ ``register()`` is NOT idempotent: a second call mints a SECOND token and
leaves two agentIds with no authority between them. The verb underneath reads
the CHAIN for an existing token (never a local flag — a fresh data dir would
lose one and re-register) and a FAILED read refuses rather than reading as "not
registered". This seat adds one more guard in front of it: when a confirmed
registration record already exists it REFUSES before touching the rail, and
names `set-uri` as the thing the owner probably meant.

⚠️ REACH, not policy. ``tx_guard`` still adjudicates. A registration is the
sixth intent shape (``is_registration``): it transfers no value, so the receipt
assertion IS the shape — a ``Transfer(from=0x0, to=us)`` from the PINNED
registry, one and only one. Its cost is the worst-case FEE, never $0.00.

Shared: the REPL and the CLI import :func:`identity_reply`.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

#: Chain used when the owner names none — matches the tool default.
_DEFAULT_CHAIN = "base"

USAGE = (
    "Usage: /identity register [on <chain>] [go]\n"
    "/identity set-uri [on <chain>] [id <agentId>] [go]\n\n"
    "`register` mints THIS instance's identity token on the ERC-8004 Identity "
    "Registry from my own wallet. The registry is PINNED per chain — it cannot "
    "be aimed anywhere else.\n"
    "⚠️ It is permanent and NOT repeatable: a second registration would mint a "
    "second token and leave two identities with no authority. I refuse if I "
    "already have one.\n"
    "`set-uri` republishes where my registration document lives, without "
    "minting anything. Writes simulate unless you add `go`."
)


def _existing_registration(data_dir: str) -> tuple:
    """``(agent_id, error)`` for a CONFIRMED on-chain registration record.

    ``agent_id`` is None when there is no record at all. ``error`` is set when
    a record FILE exists and does not parse into a usable registration — which
    is neither "registered" nor "not registered", and must never be flattened
    into either, because one of those two answers mints a second identity.
    """
    try:
        from core.instance import (erc8004_record_path, load_erc8004_record,
                                   resolve_instance_id)
    except Exception as exc:                       # pragma: no cover - import guard
        return None, f"{type(exc).__name__}: {str(exc)[:100]}"
    try:
        instance_id = resolve_instance_id()
        record = load_erc8004_record(data_dir, instance_id)
        present = erc8004_record_path(data_dir, instance_id).is_file()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:100]}"
    if isinstance(record, dict) and record.get("agent_id"):
        return record["agent_id"], None
    if present:
        return None, ("an ERC-8004 record file exists but does not parse into "
                      "a usable registration (no agentId, or no transaction "
                      "hash), so I cannot tell whether I am registered")
    return None, None


def _render(result: Any) -> str:
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The verb returned neither an error nor a report. That is a "
                "bug — do NOT retry until it is understood: a half-written "
                "identity on-chain is permanent.")
    return str(body)


async def identity_reply(user_id: Optional[str], data_dir: str,
                         args: List[str]) -> str:
    """``/identity register|set-uri [go]`` — one chat-ready string. Never raises."""
    if not user_id:
        return "Only the owner can register my identity."
    tokens = [str(a) for a in (args or []) if str(a).strip()]
    if not tokens:
        return USAGE

    verb = tokens.pop(0).lower().replace("_", "-")
    if verb in ("uri", "seturi"):
        verb = "set-uri"
    if verb not in ("register", "set-uri"):
        return f"Unknown /identity verb {verb!r}.\n{USAGE}"

    execute = False
    if tokens and tokens[-1].lower() in ("go", "execute", "confirm"):
        execute = True
        tokens.pop()

    chain = _DEFAULT_CHAIN
    agent_id_raw = None
    for i, word in enumerate(list(tokens)):
        low = word.lower()
        if low == "on" and i + 1 < len(tokens):
            chain = tokens[i + 1]
        elif low in ("id", "agent", "agentid") and i + 1 < len(tokens):
            agent_id_raw = tokens[i + 1]

    from surfaces.telegram.token_ops import _owner_ctx
    ctx = _owner_ctx(user_id)

    try:
        from tools.defi.trade_tool import (DefiTradeTool, RegisterAgentParams,
                                           SetAgentUriParams)
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The identity rail is unavailable: {exc}"

    existing, read_error = _existing_registration(data_dir)

    if verb == "register":
        if read_error:
            return (f"❌ I will not register: {read_error}. An unreadable "
                    f"record is not evidence that I am unregistered, and a "
                    f"second registration is permanent.")
        if existing is not None:
            return (f"❌ I am already registered as agentId `{existing}`. "
                    f"Registering again would mint a SECOND token and leave "
                    f"two identities with no authority between them.\n"
                    f"To republish where my document lives: /identity set-uri.")
        try:
            result = await DefiTradeTool().register_agent(
                RegisterAgentParams(chain=chain, dry_run=not execute), ctx)
        except Exception as exc:
            logger.warning("register_agent failed", exc_info=True)
            return f"The registration did not run: {exc}"
        body = _render(result)
        if not execute and not body.startswith("❌"):
            body += ("\n\nAdd `go` to mint it: /identity register "
                     f"on {chain} go\n⚠️ Permanent, and I can only do it once.")
        return body

    agent_id = agent_id_raw if agent_id_raw is not None else existing
    if agent_id is None:
        if read_error:
            return (f"❌ I could not read my registration record "
                    f"({read_error}), so I do not know which agentId to "
                    f"update. Name it: /identity set-uri id <agentId>.")
        return ("❌ I have no registration on record, so there is no document "
                "to republish. Register first: /identity register.")
    try:
        agent_id_int = int(agent_id)
    except (TypeError, ValueError):
        return f"agentId must be a whole number, got {agent_id!r}."
    try:
        result = await DefiTradeTool().set_agent_uri(
            SetAgentUriParams(chain=chain, agent_id=agent_id_int,
                              dry_run=not execute), ctx)
    except Exception as exc:
        logger.warning("set_agent_uri failed", exc_info=True)
        return f"The update did not run: {exc}"
    body = _render(result)
    if not execute and not body.startswith("❌"):
        body += ("\n\nAdd `go` to publish it: /identity set-uri "
                 f"on {chain} id {agent_id_int} go")
    return body


__all__ = ["USAGE", "identity_reply"]
