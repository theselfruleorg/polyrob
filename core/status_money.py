"""Status slots ``wallet``, ``money``, ``creations``, ``collectibles`` — 067 P5a.

The four sections that read the money rail, moved out of
``core/status_snapshot.py`` unchanged (the snapshot re-exports every name, so
``from core.status_snapshot import _money_section`` and the monkeypatch
targets keep working) and registered into the slots the snapshot names
(``core.status_sections``). ``moves_section`` rides along: it is the generic
sibling of creations the console renders. P5b moves this module into the
wallet pack; without it each slot renders one ``not installed`` line.

Rules kept from the snapshot: cheap by default (no network read — the wallet
reads the balance CACHE, collectibles never gets an enumerator here), an
unreadable store is ``unavailable`` or a counted line, never an empty list.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from core import status_snapshot as _ss
from core.status_sections import register_status_section
from core.status_snapshot import (SEVERITY_WARN, STATE_DEGRADED, STATE_OK,
                                  STATE_UNAVAILABLE, HealthItem, Section)


# The snapshot's readers, called THROUGH the module so a test that patches
# ``core.status_snapshot._rows`` (or its siblings) patches these builders too.
def _rows(*args, **kwargs):
    return _ss._rows(*args, **kwargs)


def _telemetry_db_path(data_dir: str) -> str:
    return _ss._telemetry_db_path(data_dir)


def _hhmm(ts: Optional[float]) -> str:
    return _ss._hhmm(ts)


def _wallet_section(data_dir: Optional[str]) -> Section:
    """What the agent is holding, from the cache — NEVER a network read (039 D).

    Deliberately separate from `money`. That section is CASH FLOW: income minus
    spend, which cannot see a held bag and is not a balance. This one is the bag.
    Summing or conflating them is how a treasury report describes a position it
    does not have.

    Cache-only is what keeps `include_balances=False` honest and what lets the
    per-turn `<live-health>` note carry a wallet line without a turn ever waiting
    on four JSON-RPC round trips.
    """
    from core.env import bool_env
    from core.wallet import balance_cache
    if not bool_env("AGENT_WALLET_ENABLED", False):
        # A deployment with no wallet has nothing to report, and reporting that as
        # `unavailable` would mark every status PARTIAL forever over a feature
        # nobody turned on. A status must degrade for real conditions only.
        return Section(name="wallet", state=STATE_OK,
                       lines=["wallet: not enabled (AGENT_WALLET_ENABLED)"],
                       data={"enabled": False})
    snap = balance_cache.read(data_dir)
    if snap is None:
        # The wallet IS on and no snapshot exists, so the reader has not run. That
        # is a real gap: the agent would answer a balance question from context.
        return Section(name="wallet", state=STATE_UNAVAILABLE,
                       reason="no balance snapshot yet")
    lines = balance_cache.render_lines(snap)
    totals = balance_cache.total_native_by_symbol(snap)
    unknown = [c.chain for c in snap.chains if c.native is None]
    sec = Section(name="wallet",
                  state=STATE_DEGRADED if (unknown or snap.stale) else STATE_OK,
                  lines=lines,
                  data={"address": snap.address, "age_sec": snap.age_sec,
                        "stale": snap.stale, "totals": totals,
                        "unknown_chains": unknown})
    if snap.stale:
        sec.reason = f"snapshot is {int(snap.age_sec // 60)}m old"
        sec.health.append(HealthItem(
            key="wallet_stale",
            text=f"wallet balances are {int(snap.age_sec // 60)}m old — do not "
                 f"quote them as current",
            remedy="the balance reader runs on the autonomy runtime; check it is up"))
    if unknown:
        # A chain that could not be read is NOT a chain with nothing on it, and the
        # difference is the whole point of carrying `None` through.
        sec.health.append(HealthItem(
            key="wallet_unreadable",
            text=f"balance unreadable on {', '.join(unknown)} — treat as UNKNOWN, "
                 f"never as zero",
            remedy="check the pinned RPC for those chains"))
    return sec


def _positions_line(data_dir: Optional[str]) -> str:
    """One line naming how many positions the LEDGER records as open.

    The treasury figure is cash flow; it cannot see a held bag. Saying only
    "open positions NOT included" leaves the owner unable to tell an empty book
    from two unsellable positions — on 2026-08-28 it was two, and on 2026-08-25
    that same blind spot was published to X as "book flat". This is the cheap
    half of the fix (a file read, no network); verifying the count against chain
    stays with the `reconcile` verb, which is what the remedy points at.

    UNKNOWN is never zero: an unreadable/absent ledger says so.
    """
    from core.position_ledger import open_positions, read_open_positions
    parsed, err = read_open_positions(data_dir)
    if err:
        return f"open positions: UNKNOWN — {err} (not the same as none)"
    # A row left in the table after a full exit (`Size` = `0 — FULL EXIT …`) is
    # a record, not a holding — counting it told the owner they held one more
    # position than they did.
    rows = open_positions(parsed)
    if not rows:
        return "open positions: none recorded in the ledger"
    syms = ", ".join(r.symbol for r in rows[:5]) + ("…" if len(rows) > 5 else "")
    return (f"open positions: {len(rows)} recorded in the ledger ({syms}) — "
            f"ledger-recorded, NOT verified against chain; run `reconcile`")


def _money_section(user_id: str, ledger: Any, data_dir: Optional[str] = None) -> Section:
    """``ledger`` is the ``build_ledger`` dict, or the exception it raised."""
    if isinstance(ledger, BaseException):
        raise ledger
    if not isinstance(ledger, dict) or not ledger:
        raise RuntimeError("ledger returned no data")
    sec = Section(name="money", data={"ledger": ledger})
    r = ledger.get("runtime") or {}
    t = ledger.get("treasury") or {}
    # 2026-09-21: a leg that did NOT read renders `unavailable`, never `$0.00`
    # — the availability note below already marks the section degraded, but
    # the figure line above it read as a confident zero on a fresh home.
    if r.get("available") is False:
        line = "runtime cost (owner's compute bill): unavailable (usage records not readable)"
    else:
        spend = float(r.get("spend_window_usd") or 0.0)
        total = float(r.get("spend_total_usd") or 0.0)
        line = f"runtime cost (owner's compute bill): ${spend:.2f} last 24h · ${total:.2f} total"
        if r.get("provider_balance_usd") is not None:
            line += f" · provider balance ${float(r['provider_balance_usd']):.2f}"
    sec.lines.append(line)
    if t.get("available") is False:
        line = ("treasury cash flow (income − spend; open positions NOT included): "
                "unavailable (invoice or wallet ledger not readable)")
    else:
        net = float(t.get("net_usd") or 0.0)
        line = (f"treasury cash flow (income − spend; open positions NOT included): "
                f"net ${net:+.2f}")
    if t.get("balance_usd") is not None:
        line += f" · USDC balance ${float(t['balance_usd']):.2f}"
    if int(t.get("pending_count") or 0):
        line += f" · {int(t['pending_count'])} pending invoice(s) ${float(t.get('pending_usd') or 0):.2f}"
    sec.lines.append(line)
    # A settled machine payment whose work then failed downstream
    # (`modules/x402/x402_integration.py::mark_payment_refund_due`). ⚠️ Its OWN
    # line and its own health item: it is money the agent TOOK and owes back,
    # so folding it into the net figure would net a debt against income and
    # show a healthier treasury the more the agent owes. The obligation only
    # grows while nobody acts on it, and `refund_due` is a status no invoice
    # listing showed until 2026-09-21, so no seat could name it at all.
    _refund_n = int(t.get("refund_due_count") or 0)
    if _refund_n:
        _refund_usd = float(t.get("refund_due_usd") or 0.0)
        sec.lines.append(
            f"⚠ refund owed: ${_refund_usd:.2f} across {_refund_n} settled "
            f"payment(s) we did not deliver on (NOT netted above)")
        sec.health.append(HealthItem(
            key="payment_refund_due", severity=SEVERITY_WARN,
            text=(f"{_refund_n} settled payment(s) worth ${_refund_usd:.2f} are "
                  f"owed back — the work failed after the money arrived"),
            remedy="`/invoices refund_due` to see them, then refund each payer"))
    # Positions are read separately and must never take the money section down:
    # a missing ledger is a fact about the ledger, not about the treasury.
    try:
        sec.lines.append(_positions_line(data_dir))
    except Exception as e:
        sec.lines.append(f"open positions: UNKNOWN ({type(e).__name__}: {e})")
    from core.activity_evidence import ledger_note
    note = ledger_note(ledger)
    if note:
        sec.state = STATE_DEGRADED
        sec.reason = note
        sec.lines.append(f"⚠ {note}")
    return sec


#: The verbs that CREATE something the owner will later be asked about. Derived
#: from the spend ledger rather than a second store: every one of them already
#: calls `gate.record(counterparty=<the address it made>, result_ref=<the tx>)`,
#: so the record exists and nothing new has to be written to read it back.
CREATION_VERBS = ("deploy_token", "deploy_contract", "solana_deploy_token",
                  "launchpad_launch")


def _creations_section(user_id: str, data_dir: str) -> Section:
    """What this agent has CREATED on-chain (042b).

    The gap this closes is the one the 2026-08-25 ledger incident is the famous
    instance of: the agent did something durable and no surface could show it
    back. A token it deployed last week existed only in a transaction hash in a
    chat message.

    Read-only over `telemetry_events`, tenant-scoped, newest first. An absent or
    unreadable store renders its reason — never an empty list, because "I have
    created nothing" and "I cannot see what I created" are different facts and
    only one of them is reassuring.
    """
    from core.wallet.chains import explorer_url

    sec = Section(name="creations")
    db = _telemetry_db_path(data_dir)
    rows = _rows(
        db,
        "SELECT ts, attrs FROM telemetry_events WHERE kind='wallet_spend' "
        "AND user_id=? ORDER BY ts DESC LIMIT 400",
        (user_id,))

    made = []
    unreadable = 0
    for row in rows:
        try:
            attrs = json.loads(row.get("attrs") or "{}")
        except Exception:
            # NOT silent: a row we cannot parse might be a creation, so it is
            # counted and surfaced. A status view that quietly drops rows is how
            # "I have created nothing" comes to mean "I could not tell".
            unreadable += 1
            continue
        action = str(attrs.get("action") or "")
        if action not in CREATION_VERBS:
            continue
        chain = attrs.get("chain")
        address = attrs.get("counterparty")
        url = None
        if chain and address:
            kind = "token" if action in (
                "deploy_token", "solana_deploy_token", "launchpad_launch") else "address"
            url = explorer_url(chain, kind, str(address))
        made.append({
            "action": action,
            "address": address,
            "chain": chain,
            "tx": attrs.get("result_ref"),
            "usd": attrs.get("amount_usd"),
            "ts": row.get("ts"),
            "url": url,
        })

    sec.data["creations"] = made
    sec.data["unreadable_rows"] = unreadable
    if unreadable:
        sec.lines.append(f"⚠ {unreadable} spend row(s) could not be read — this "
                         f"list may be incomplete")
        sec.health.append(HealthItem(
            key="creations_unreadable", severity=SEVERITY_WARN,
            text=(f"{unreadable} wallet_spend row(s) did not parse, so what I "
                  f"have created cannot be listed in full"),
            remedy="check telemetry_events for malformed attrs"))
    if not made:
        sec.lines.append("nothing deployed or launched"
                         + (" (that could be read)" if unreadable else ""))
        return sec

    by = {}
    for item in made:
        by[item["action"]] = by.get(item["action"], 0) + 1
    sec.lines.append(", ".join(f"{n} {a}" for a, n in sorted(by.items())))
    for item in made[:5]:
        action = item["action"]
        addr = item.get("address") or "address unknown"
        chain = item.get("chain")
        ts_part = f" ({_hhmm(item['ts'])})" if item.get("ts") else ""
        url = item.get("url")
        url_part = f" — {url}" if url else ""
        if chain:
            sec.lines.append(f"{action}: {addr} on {chain}{ts_part}{url_part}")
        else:
            sec.lines.append(f"{action}: {addr}{ts_part}{url_part}")
    if len(made) > 5:
        sec.lines.append(f"…and {len(made) - 5} more")
    return sec


def moves_section(user_id: str, data_dir: str, *, limit: int = 20) -> Section:
    """Every MOVE the wallet made — the generic sibling of ``_creations_section``.

    PUBLIC because the console renders it directly (E24 lists ``moves`` among
    the rows no seat had). Same source and same discipline as creations:
    read-only over ``wallet_spend`` telemetry, tenant-scoped, newest first,
    never CREATING the store.

    ⚠️ Derived from the SPEND ledger, so it answers "what did I send" and never
    "what do I hold" — a row exists only for an outflow the guard authorized.
    An unparseable row is COUNTED and surfaced rather than dropped: "nothing
    moved" must never come to mean "I could not tell".

    Each move carries ``url`` — a block-explorer link, exactly as
    ``_creations_section`` does — when the row recorded BOTH a chain and a
    transaction reference, and ``None`` otherwise. The console renders it as
    the row's one action, so building it here keeps the link and the row that
    justifies it in one place instead of two readers deriving it apart.
    """
    from core.wallet.chains import explorer_url
    sec = Section(name="moves")
    rows = _rows(
        _telemetry_db_path(data_dir),
        "SELECT ts, attrs FROM telemetry_events WHERE kind='wallet_spend' "
        "AND user_id=? ORDER BY ts DESC LIMIT 400",
        (user_id,))
    moves: List[Dict[str, Any]] = []
    unreadable = 0
    total_usd = 0.0
    priced = 0
    for row in rows:
        try:
            attrs = json.loads(row.get("attrs") or "{}")
        except Exception:
            unreadable += 1
            continue
        usd = attrs.get("amount_usd")
        try:
            if usd is not None:
                total_usd += float(usd)
                priced += 1
        except (TypeError, ValueError):
            usd = None
        chain = attrs.get("chain")
        tx = attrs.get("result_ref")
        moves.append({
            "action": str(attrs.get("action") or ""),
            "asset": attrs.get("asset"),
            "to": attrs.get("counterparty"),
            "chain": chain,
            "tx": tx,
            "url": explorer_url(chain, "tx", str(tx)) if (chain and tx) else None,
            "usd": usd,
            "ts": row.get("ts"),
        })
    sec.data["moves"] = moves[:limit]
    sec.data["total"] = len(moves)
    sec.data["unreadable_rows"] = unreadable
    #: ``None``, never 0.0, when NO row carried a price — an unpriced move is
    #: not a free one.
    sec.data["total_usd"] = round(total_usd, 4) if priced else None
    if unreadable:
        sec.lines.append(f"⚠ {unreadable} spend row(s) could not be read — this "
                         f"list may be incomplete")
        sec.health.append(HealthItem(
            key="moves_unreadable", severity=SEVERITY_WARN,
            text=(f"{unreadable} wallet_spend row(s) did not parse, so what the "
                  f"wallet moved cannot be listed in full"),
            remedy="check telemetry_events for malformed attrs"))
    if not moves:
        sec.lines.append("nothing moved"
                         + (" (that could be read)" if unreadable else ""))
        return sec
    priced_note = (f", ${sec.data['total_usd']:,.2f} total" if priced
                   else ", none priced")
    sec.lines.append(f"{len(moves)} move(s){priced_note}")
    for item in moves[:5]:
        amount = (f"${item['usd']:,.2f}" if item.get("usd") is not None
                  else "amount unrecorded")
        sec.lines.append(
            f"{item['action'] or 'move'}: {amount} "
            f"{item.get('asset') or ''} -> {item.get('to') or 'unrecorded'}"
            f"{(' on ' + str(item['chain'])) if item.get('chain') else ''} "
            f"({_hhmm(item.get('ts'))})".replace("  ", " "))
    if len(moves) > 5:
        sec.lines.append(f"…and {len(moves) - 5} more")
    return sec


#: NFT move verbs, by their `gate.record(action=...)` name. Derived from the
#: spend ledger for the same reason CREATION_VERBS is: the record already
#: exists, so nothing new has to be written to read it back.
NFT_MOVE_VERBS = ("nft_transfer",)


def _collectibles_section(user_id: str, data_dir: str,
                          enumerate_fn=None) -> Section:
    """Non-fungibles: what the chain says is HELD, and what the guard MOVED.

    ⚠️ These are two different questions and the section never merges them. A
    `wallet_spend` row exists only for a SPEND, so an AIRDROPPED token has no
    row at all — telemetry can say what left, never what arrived unasked. Only
    a chain read answers "held".

    ⚠️ `held is None` means NOT READ (no provider, a failed read, or simply not
    asked for), and renders as such. `held == []` means a working read returned
    nothing. Collapsing the two would turn "I could not look" into "you own
    nothing", which is the exact confident-zero class the status SSOT exists to
    prevent.

    ⚠️ No network read unless `enumerate_fn` is supplied — status is cheap by
    default, the same contract `include_balances` carries for the ledger.
    """
    sec = Section(name="collectibles")

    # --- held (a chain read, opt-in) ---------------------------------------
    sec.data["held"] = None
    if enumerate_fn is not None:
        try:
            sec.data["held"] = list(enumerate_fn(user_id=user_id))
        except Exception as e:
            sec.lines.append(f"held: could not be read ({type(e).__name__}: "
                             f"{str(e)[:120]})")
    else:
        sec.lines.append("held: not read (a chain read is opt-in here; ask for "
                         "it explicitly, or run the nft_holdings verb)")

    held = sec.data["held"]
    if held is not None:
        if not held:
            sec.lines.append("held: none — this IS an answer from a working "
                             "read, not a failed one")
        else:
            sec.lines.append(f"held: {len(held)} NFT(s)")
            for item in held[:5]:
                name = item.get("name") or "(unnamed)"
                sec.lines.append(f"  {name} — {item.get('contract')} "
                                 f"#{item.get('token_id')}")
            if len(held) > 5:
                sec.lines.append(f"  …and {len(held) - 5} more")

    # --- moved (derived from the spend ledger) -----------------------------
    rows = _rows(
        _telemetry_db_path(data_dir),
        "SELECT ts, attrs FROM telemetry_events WHERE kind='wallet_spend' "
        "AND user_id=? ORDER BY ts DESC LIMIT 400",
        (user_id,))
    moved = []
    unreadable = 0
    for row in rows:
        try:
            attrs = json.loads(row.get("attrs") or "{}")
        except Exception:
            # NOT silent: an unparseable row might BE a move, so it is counted
            # and surfaced rather than quietly dropped.
            unreadable += 1
            continue
        if str(attrs.get("action") or "") not in NFT_MOVE_VERBS:
            continue
        moved.append({"asset": attrs.get("asset"), "to": attrs.get("counterparty"),
                      "chain": attrs.get("chain"), "tx": attrs.get("result_ref"),
                      "ts": row.get("ts")})
    sec.data["moved"] = moved
    sec.data["unreadable_rows"] = unreadable
    if unreadable:
        sec.lines.append(f"⚠ {unreadable} spend row(s) could not be read — the "
                         f"move list may be incomplete")
    if moved:
        sec.lines.append(f"moved out: {len(moved)}")
        for item in moved[:5]:
            sec.lines.append(f"  {item.get('asset') or 'asset unrecorded'} -> "
                             f"{item.get('to')} ({_hhmm(item.get('ts'))})")
    else:
        sec.lines.append("moved out: none recorded")
    return sec


# --- the slots ---------------------------------------------------------------

def _money_slot(ctx) -> Section:
    """``money`` exactly as ``build_status_snapshot`` built it before 067 P5a:
    the caller's ledger, else one strict rollup read (a raise is carried as the
    ledger and renders unavailable), else "no tenant". The ledger read here is
    handed on (``ctx.ledger``) to the economics runway line."""
    if not ctx.include_money:
        return Section(name="money", state=STATE_UNAVAILABLE,
                       reason="not requested on this path")
    ledger = ctx.ledger
    if ledger is None and ctx.uid:
        try:
            from core.activity_evidence import ledger_rollup_strict
            ledger = ledger_rollup_strict(ctx.uid, max(1, ctx.window_sec // 86400),
                                          include_balances=ctx.include_balances)
        except Exception as e:
            ledger = e
    elif ledger is None:
        ledger = ValueError("no tenant (empty user_id)")
    ctx.ledger = ledger
    return _ss._guarded("money", _ss._money_section, ctx.uid, ledger, ctx.data_dir)


# Late-bound through the snapshot module: a test that patches
# ``core.status_snapshot._creations_section`` patches the slot too.
register_status_section(
    "wallet", lambda ctx: _ss._guarded("wallet", _ss._wallet_section, ctx.data_dir),
    tenant=False)
register_status_section("money", _money_slot, tenant=False)
register_status_section(
    "creations", lambda ctx: _ss._guarded("creations", _ss._creations_section,
                                          ctx.uid, ctx.data_dir))
register_status_section(
    "collectibles", lambda ctx: _ss._guarded("collectibles", _ss._collectibles_section,
                                             ctx.uid, ctx.data_dir))
