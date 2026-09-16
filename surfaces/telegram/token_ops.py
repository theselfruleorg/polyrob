"""`/launch` and `/deploy` — the owner's chat seat for 042.

Kept out of ``owner_ops.py``, which already carries a note that it is god-file
sized and should not grow another handler.

Why these exist at all: the owner's standing directive is that he can do
anything from the chat, and he is usually on a phone. A rail that ships
CLI-only means the one person allowed to run it has to SSH to the box — which
is what happened to the bridge, and is why `/bridge` exists.

Both verbs QUOTE by default and need an explicit `go` to execute. Typing `go`
is a deliberate second act, not the approval: the durable owner queue still
applies above the autonomous ceiling, and every guard applies underneath.
"""
from __future__ import annotations

import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


def _run(coro):
    """Run *coro*, whether or not a loop is already turning under us."""
    import asyncio
    try:
        return asyncio.run(coro)
    except RuntimeError:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()


def _render(result, *, retry_hint: Optional[str]) -> str:
    if getattr(result, "error", None):
        return f"❌ {result.error}"
    body = getattr(result, "extracted_content", None)
    if not body:
        return ("The verb returned neither an error nor a report. That is a bug "
                "— do NOT retry until it is understood; assume nothing about "
                "what happened.")
    if retry_hint:
        body += f"\n\nAdd `go` to execute: {retry_hint}"
    return body


def _owner_ctx(user_id: str):
    """A genuine owner chat turn — the same seat the CLI is, from a phone."""
    from types import SimpleNamespace
    return SimpleNamespace(user_id=user_id, role="owner", is_sub_agent=False,
                           metadata={})


def _positive(raw: str) -> Optional[float]:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# /launch — a token on the launchpad
# ---------------------------------------------------------------------------

def launch_reply(user_id: Optional[str], args: List[str]) -> str:
    """`/launch <SYMBOL> <name…> [buy <amount>] [go]`.

    The launchpad quotes in the chain's NATIVE asset, so `buy` is an amount of
    it. Without `buy` the token launches with no opening position — which also
    means anyone else can take the first one.
    """
    if not user_id:
        return "Only the owner can launch a token."
    if not args:
        return ("Usage: /launch <SYMBOL> <name…> [logo <url>] [desc <text>] "
                "[x <url>] [site <url>] [buy <amount>] [go]\n"
                "e.g. /launch ROB Rob Coin                 — quote only\n"
                "     /launch ROB Rob Coin logo https://i/r.png buy 0.05 go\n\n"
                "⚠️ A launch with no logo and no description looks abandoned on "
                "the launchpad. Set them.\n\n"
                "Launches on Pons V2 (Robinhood Chain). Fixed 1B supply into a "
                "bonding curve that graduates to a locked Uniswap pool.\n"
                "⚠️ Everything on a launchpad is a memecoin. The caps bound what "
                "this can spend; they do not make it a good idea.")

    tokens = list(args)
    execute = False
    if tokens and tokens[-1].lower() in ("go", "execute", "confirm"):
        execute = True
        tokens.pop()

    # ⚠️ Pulled BEFORE the name is joined: a launchpad listing with no logo and
    # no description is invisible next to the ones that have them, and the Pons
    # ABI has carried these fields all along — they were simply unreachable from
    # any seat until now.
    extras = {}
    for field in ("logo", "desc", "x", "site"):
        for i, word in enumerate(tokens):
            if word.lower() == field and i + 1 < len(tokens):
                extras[field] = tokens[i + 1]
                tokens = tokens[:i] + tokens[i + 2:]
                break

    buy_amount = 0.0
    for i, word in enumerate(tokens):
        if word.lower() == "buy" and i + 1 < len(tokens):
            parsed = _positive(tokens[i + 1])
            if parsed is None:
                return f"The buy amount must be a positive number, got {tokens[i + 1]!r}."
            buy_amount = parsed
            tokens = tokens[:i] + tokens[i + 2:]
            break

    if not tokens:
        return "Give me a symbol: /launch <SYMBOL> <name…>"
    symbol = tokens[0]
    name = " ".join(tokens[1:]) or symbol

    try:
        from tools.launchpad.tool import LaunchParams, LaunchpadTool
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The launchpad rail is unavailable: {exc}"

    # The ceiling the owner is asked to stand behind. The launch fee is 0.0005
    # native; the opening buy is whatever he typed. Valued generously so a
    # normal launch is not refused by its own declaration, and still bounded —
    # tx_guard prices the REAL outflow and holds it to this.
    max_spend_usd = max(25.0, (buy_amount + 0.01) * 6000.0)
    params = LaunchParams(name=name, symbol=symbol, buy_amount=buy_amount,
                          logo=extras.get("logo", ""),
                          description=extras.get("desc", ""),
                          twitter=extras.get("x", ""),
                          website=extras.get("site", ""),
                          max_spend_usd=max_spend_usd, dry_run=not execute)
    tool = LaunchpadTool()
    try:
        result = _run(tool.launchpad_launch(params, _owner_ctx(user_id)))
    except Exception as exc:
        logger.warning("launch verb failed", exc_info=True)
        return f"The launch did not run: {exc}"

    hint = None if execute else (
        f"/launch {symbol} {name}"
        + "".join(f" {k} {v}" for k, v in extras.items())
        + (f" buy {buy_amount:g}" if buy_amount else "") + " go")
    # The absent-logo nudge is NOT repeated here: it lives in the launch
    # report itself (`tools.launchpad.tool._metadata_block`), so the CLI and the
    # agent's own action get it too. This seat only adds the syntax for it.
    body = _render(result, retry_hint=hint)
    if not extras.get("logo") and not execute:
        body += "\nAdd it with `logo <url> desc \"...\"` (also `x`, `site`)."
    return body


# ---------------------------------------------------------------------------
# /deploy — a plain fixed-supply token
# ---------------------------------------------------------------------------

def deploy_reply(user_id: Optional[str], args: List[str]) -> str:
    """`/deploy <SYMBOL> <supply> <name…> [on <chain>] [go]`.

    An ordinary ERC-20 with no launchpad attached: fixed supply, minted to the
    wallet, no mint function and no owner. Deploying a token does NOT make it
    tradable — that is what `/launch` is for.
    """
    if not user_id:
        return "Only the owner can deploy a token."
    if len(args) < 2:
        return ("Usage: /deploy <SYMBOL> <supply> <name…> [on <chain>] "
                "[vanity <hex>] [go]\n"
                "e.g. /deploy ROB 1000000000 Rob Coin              — quote only\n"
                "     /deploy ROB 1e9 Rob Coin on solana uri https://x/y.json\n"
                "     /deploy ROB 1e9 Rob Coin vanity b0b go     — mine 0xb0b…\n\n"
                "On SOLANA the name and symbol are written ON-CHAIN and `uri` "
                "points at the JSON that carries the LOGO. On an EVM chain "
                "`vanity` mines an address and gives the token the SAME address "
                "on every chain.\n\n"
                "A fixed-supply ERC-20: the whole supply is minted to my wallet "
                "and there is no mint function, no owner and no transfer fee. "
                "The bytecode is an audited template and the guard checks the "
                "deployed code against it byte for byte before signing.\n"
                "This does NOT make the token tradable — use /launch for that.")

    tokens = list(args)
    execute = False
    if tokens and tokens[-1].lower() in ("go", "execute", "confirm"):
        execute = True
        tokens.pop()

    chain = "base"
    for i, word in enumerate(tokens):
        if word.lower() == "on" and i + 1 < len(tokens):
            chain = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break

    uri = ""
    for i, word in enumerate(tokens):
        if word.lower() == "uri" and i + 1 < len(tokens):
            uri = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break

    vanity = ""
    for i, word in enumerate(tokens):
        if word.lower() == "vanity" and i + 1 < len(tokens):
            vanity = tokens[i + 1]
            tokens = tokens[:i] + tokens[i + 2:]
            break

    if len(tokens) < 2:
        return "Give me a symbol and a supply: /deploy <SYMBOL> <supply> <name…>"
    symbol = tokens[0]
    supply = _positive(tokens[1])
    if supply is None:
        return f"The supply must be a positive number, got {tokens[1]!r}."
    name = " ".join(tokens[2:]) or symbol

    try:
        from tools.defi.deploy_verb import perform_deploy_token
        from tools.defi.trade_tool import DefiTradeTool, DeployTokenParams
    except Exception as exc:                       # pragma: no cover - import guard
        return f"The deploy rail is unavailable: {exc}"

    if chain.strip().lower() == "solana":
        if vanity:
            return ("A Solana mint address is a keypair, not a hash of its "
                    "code — there is nothing to mine against. Drop `vanity`.")
        try:
            from tools.defi.spl_deploy_verb import perform_solana_deploy_token
            from tools.defi.trade_tool import SolanaDeployTokenParams
        except Exception as exc:                   # pragma: no cover - import guard
            return f"The Solana deploy rail is unavailable: {exc}"
        sol = SolanaDeployTokenParams(name=name, symbol=symbol, supply=supply,
                                      decimals=9, uri=uri, max_spend_usd=25.0,
                                      dry_run=not execute)
        try:
            result = _run(perform_solana_deploy_token(
                DefiTradeTool(), sol, _owner_ctx(user_id)))
        except Exception as exc:
            logger.warning("solana deploy verb failed", exc_info=True)
            return f"The deploy did not run: {exc}"
        hint = None if execute else (
            f"/deploy {symbol} {supply:g} {name} on solana"
            + (f" uri {uri}" if uri else "") + " go")
        return _render(result, retry_hint=hint)

    params = DeployTokenParams(chain=chain, name=name, symbol=symbol,
                               supply=supply, max_spend_usd=25.0,
                               vanity=vanity, dry_run=not execute)
    try:
        result = _run(perform_deploy_token(DefiTradeTool(), params,
                                           _owner_ctx(user_id)))
    except Exception as exc:
        logger.warning("deploy verb failed", exc_info=True)
        return f"The deploy did not run: {exc}"

    hint = None if execute else (
        f"/deploy {symbol} {supply:g} {name}"
        + (f" on {chain}" if chain != "base" else "")
        + (f" vanity {vanity}" if vanity else "") + " go")
    return _render(result, retry_hint=hint)
