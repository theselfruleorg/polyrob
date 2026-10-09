"""Wallet intelligence verbs of ``defi_data`` (proposal 071 §3.7, W4).

``wallet_activity`` — any wallet's recent transactions as typed rows, plus a
per-asset NET FLOW the verb computes (never the model). ``token_origin`` —
who deployed a token, what else they deployed, whether they still hold it, and
(Solana) who bought in the first slots and whether a shared address funded
them.

The reads live in ``core.wallet.activity`` / ``core.wallet.token_origin``;
this module only renders them. Two output rules matter most:

* every symbol and explorer label is ATTACKER-WRITTEN and is rendered in
  double quotes, cleaned of control characters (``token_screen.quoted``);
  a canonical token is named by its pinned symbol instead. Addresses are
  printed in full (base58 is case-sensitive, and a shortened address is a
  different, guessable address); a TRANSACTION id is shortened on the row
  (it is a reference, not a destination) and kept in full in the metadata;
* a bound that ran out, a list that was not read, or a figure that is unknown
  is SAID — "PARTIAL — not read: …" — never rendered as nothing.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field

#: The deployer's other contracts listed on the row; the rest are counted.
_ORIGIN_ROWS_SHOWN = 5

CLUSTER_NOTE = ("addresses, not people; a shared funder is a signal, not proof "
                "(an exchange funds many unrelated users)")


class WalletActivityParams(BaseModel):
    address: str = Field(..., description=(
        "The WALLET whose recent activity to read — any owner. Solana is base58; "
        "EVM is 0x… (40 hex). For a token's history use token_origin."))
    chain: Optional[str] = Field(None, description=(
        "Omit for a base58 address (read on solana). REQUIRED for a 0x address — "
        "the same 0x wallet has different activity on each EVM chain."))
    limit: int = Field(20, ge=1, le=25, description="How many recent transactions to show (max 25).")


class TokenOriginParams(BaseModel):
    address: str = Field(..., description=(
        "The TOKEN: an EVM contract (0x…) or a Solana mint (base58). Not a wallet."))
    chain: Optional[str] = Field(None, description=(
        "Omit for a base58 mint (read on solana). REQUIRED for a 0x token."))


# -- formatting -----------------------------------------------------------------

def fmt_amount(raw: int, decimals: Optional[int]) -> str:
    """Exact human amount from raw units; ``decimals=None`` says so."""
    from core.wallet.tokens import bounded_decimals
    decimals = bounded_decimals(decimals)
    if decimals is None:
        return f"{raw:,} raw units (decimals unknown)"
    sign = "-" if raw < 0 else ""
    text = format(Decimal(abs(raw)).scaleb(-int(decimals)), "f")
    whole, _, frac = text.partition(".")
    frac = frac.rstrip("0")
    return f"{sign}{int(whole):,}" + (f".{frac}" if frac else "")


def _signed(raw: int, decimals: Optional[int]) -> str:
    text = fmt_amount(raw, decimals)
    return text if text.startswith("-") else "+" + text


def _when(ts: Optional[int]) -> str:
    if not ts:
        return "time unknown    "
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def _asset_label(asset: str, symbol: Optional[str], native_symbol: str,
                 chain: Optional[str] = None) -> str:
    if asset == "native":
        return native_symbol or "native"
    if chain:
        try:
            from core.wallet.tokens import canonical_token
            pinned = canonical_token(chain, asset)
        except Exception:
            pinned = None
        if pinned and pinned.get("symbol"):
            return str(pinned["symbol"])
    from tools.defi.token_screen import quoted
    label = quoted(symbol)
    return f"{label} {asset}" if label else asset


def _short_ref(ref: Optional[str]) -> str:
    text = str(ref or "")
    return text if len(text) <= 12 else text[:8] + "…"


# -- wallet_activity ------------------------------------------------------------

def render_activity(rep, *, own: bool = False):
    from core.wallet.activity import fees_paid, net_flows
    whose = "this agent's own wallet" if own else "wallet"
    lines = [f"wallet_activity — {whose} {rep.owner} on {rep.chain}",
             f"source: {rep.source}"]
    if not rep.available:
        lines.append(f"history: NOT AVAILABLE — {rep.reason}")
        lines.append("This is not an empty history; it was not read.")
        return "\n".join(lines), None
    shown = len(rep.rows)
    head = f"read: {shown} transaction(s) shown, newest first"
    if rep.more_exist:
        head += "; older activity exists and was NOT read"
    lines.append(head)
    if rep.partial:
        from tools.defi.token_screen import plain_errors
        lines.append("PARTIAL — not read: " + plain_errors("; ".join(rep.not_read)))
    if not rep.rows:
        lines.append("No transactions in the window read.")
    for row in rep.rows:
        parts = [f"{_signed(d.raw, d.decimals)} "
                 f"{_asset_label(d.asset, d.symbol, rep.native_symbol, rep.chain)}"
                 + (" (NFT)" if d.nft else "") for d in row.deltas]
        line = (f"  {_when(row.time)}  tx {_short_ref(row.ref)}  {row.kind:<7}  "
                + ("; ".join(parts) or "no balance change for this wallet"))
        if row.counterparty:
            line += f"  ↔ {row.counterparty}"
        if row.fee_raw:
            line += f"  fee {fmt_amount(row.fee_raw, rep.native_decimals)} {rep.native_symbol}"
        if row.flags:
            line += "  [" + ", ".join(row.flags) + "]"
        lines.append(line)
    flows = net_flows([r for r in rep.rows if r.kind != "failed"])
    if flows:
        lines.append("")
        lines.append(f"Net flow over the {shown} rows read — NOT a cost-basis PnL "
                     f"(no prices, no earlier history):")
        for f in flows:
            label = _asset_label(f["asset"], f["symbol"], rep.native_symbol, rep.chain)
            dec = f["decimals"]
            lines.append(f"  {label}: in {fmt_amount(f['in_raw'], dec)}, out "
                         f"{fmt_amount(f['out_raw'], dec)}, net {_signed(f['net_raw'], dec)}")
    fees = fees_paid(rep.rows)
    if fees is None:
        lines.append("fees paid by this wallet: UNKNOWN for at least one row")
    elif fees:
        lines.append(f"fees paid by this wallet over these rows: "
                     f"{fmt_amount(fees, rep.native_decimals)} {rep.native_symbol}")
    lines.append("Quoted symbols are written by the token's creator — not identity. "
                 "Full tx ids are in the metadata.")
    meta = {"verb": "wallet_activity", "report": rep.to_dict(), "net_flows": flows,
            "fees_paid_raw": fees}
    return "\n".join(lines), meta


def render_own_payments(address: str, rows) -> List[str]:
    """Our OWN recorded payments to *address* — the answer to "did we pay X?"."""
    head = "Our own recorded transfers to this address (wallet audit ledger):"
    if rows is None:
        return ["", head, "  the audit ledger could not be read — UNKNOWN, not 'none'."]
    if not rows:
        return ["", head, "  no transfer to this address is recorded in the audit ledger."]
    lines = ["", head]
    for r in rows:
        amt = r.get("amount_usd")
        amt_txt = f"${amt:,.2f}" if isinstance(amt, (int, float)) else "amount unknown"
        lines.append(f"  {_when(r.get('ts'))}  {amt_txt}  {r.get('asset') or ''}"
                     f" on {r.get('chain') or '?'}  tx {r.get('result_ref') or '?'}"
                     f"  ({r.get('lane') or 'lane unknown'})")
    return lines


def wallet_activity_sync(tool, params: WalletActivityParams, execution_context=None):
    from core.wallet import activity
    address, chain, own, err = tool._wallet_target(params.address, params.chain,
                                                   execution_context)
    if err:
        return tool._ar(error=err)
    rep = activity.activity(address, chain, params.limit)
    text, meta = render_activity(rep, own=own)
    if meta is None:
        meta = {"verb": "wallet_activity", "report": rep.to_dict()}
    from tools.defi.data_tool import _operator_read_refusal
    if not own and _operator_read_refusal(execution_context) is None:
        from core.wallet.trade_index import own_transfers_to
        paid = own_transfers_to(address)
        text += "\n" + "\n".join(render_own_payments(address, paid))
        meta["own_transfers_to"] = paid
    return tool._ar(content=text, metadata=meta)


# -- token_origin -----------------------------------------------------------------

def _render_forensics(fx, rep) -> List[str]:
    dec = rep.decimals
    lines = ["", f"Launch forensics — first {fx.slots + 1} slot(s) from slot {fx.first_slot}, "
                 f"{fx.launch_tx_read} launch transaction(s) read. {CLUSTER_NOTE}."]
    if not fx.buyers:
        lines.append("  No signer received the token in the launch transactions read.")
    for b in fx.buyers:
        if b.funder:
            fund = f"funded by {b.funder} (tx {b.funder_ref})"
        elif b.funder_read:
            fund = "no SOL inflow found in its last transactions before the buy"
        else:
            fund = "funder NOT traced"
        tag = "  (the deployer)" if rep.deployer and b.address == rep.deployer else ""
        lines.append(f"  buyer {b.address}{tag}  {_signed(b.raw, dec)}  — {fund}")
    if fx.shared_funders:
        for funder, buyers in fx.shared_funders.items():
            lines.append(f"  ⚠ SHARED FUNDER {funder} funded {len(buyers)} launch buyers: "
                         + ", ".join(buyers))
    elif any(b.funder for b in fx.buyers):
        lines.append("  No shared funder among the buyers traced (one hop back only).")
    if fx.deployer_funded:
        lines.append("  ⚠ The DEPLOYER funded launch buyer(s): " + ", ".join(fx.deployer_funded))
    if fx.not_read:
        lines.append("  PARTIAL — not read: " + "; ".join(fx.not_read))
    return lines


def render_origin(rep):
    from core.wallet import chains
    row = chains.get(rep.chain)
    svm = row is not None and row.family == "svm"
    lines = [f"token_origin — {rep.token} on {rep.chain}", f"source: {rep.source}"]
    from tools.defi.token_screen import plain_errors, quoted
    if not rep.available:
        reason = plain_errors(str(rep.reason or ""))
        if "timed out" in reason.lower():
            reason = "explorer timed out — try again"
        lines.append(f"origin: NOT AVAILABLE — {reason}")
        return "\n".join(lines)
    if rep.symbol or rep.name:
        bits = [b for b in (f"symbol {quoted(rep.symbol)}" if quoted(rep.symbol) else None,
                            f"name {quoted(rep.name, name=True)}"
                            if quoted(rep.name, name=True) else None) if b]
        lines.append(f"label: {', '.join(bits)} (written by the token's creator — not identity)")
    for flag in rep.explorer_flags:
        lines.append(f"⚠ {flag}")
    if rep.deployer:
        what = ("fee payer of the mint's earliest transaction" if svm
                else "sent the creation transaction")
        lines.append(f"deployer: {rep.deployer} ({what})")
    if rep.creator_contract:
        lines.append(f"created by contract {rep.creator_contract} (a factory or launchpad); "
                     f"the deployer above sent that transaction")
    if rep.launchpad:
        lines.append(f"launchpad: {rep.launchpad} (its program is in the creation transaction)")
    elif rep.deployer and svm and rep.token.endswith("pump"):
        lines.append("launchpad: the mint ends in 'pump' (pump.fun's vanity suffix), but the "
                     "pump.fun program was NOT in the earliest transaction read")
    if rep.creation_ref:
        slot = f"slot {rep.creation_slot}, " if rep.creation_slot is not None else ""
        lines.append(f"created: {slot}{_when(rep.creation_time).strip()} — tx {rep.creation_ref}")
    if rep.other_creations_read is not None:
        more = "; more exist and were NOT read" if rep.other_creations_more else ""
        lines.append(f"deployer's other contract creations (its earliest "
                     f"{rep.other_creations_read} transactions read{more}): "
                     f"{len(rep.other_creations)}")
        for c in rep.other_creations[:_ORIGIN_ROWS_SHOWN]:
            q = quoted(c.name, name=True)
            name = f" {q}" if q else ""
            lines.append(f"  {c.address}{name}  {_when(c.time).strip()}")
        if len(rep.other_creations) > _ORIGIN_ROWS_SHOWN:
            lines.append(f"  +{len(rep.other_creations) - _ORIGIN_ROWS_SHOWN} more "
                         f"(listed in the metadata)")
        lines.append("  (contracts made THROUGH a factory are internal calls and are not "
                     "listed; whether earlier tokens died is NOT CHECKED here)")
    if rep.deployer:
        if rep.deployer_balance_raw is None:
            lines.append("deployer holds now: UNKNOWN (could not be read)")
        else:
            lines.append(f"deployer holds now: {fmt_amount(rep.deployer_balance_raw, rep.decimals)}")
    if rep.deployer_transfers_read is not None:
        more = ", more exist and were NOT read" if rep.deployer_transfers_more else ""
        lines.append(f"deployer's transfers of this token (newest {rep.deployer_transfers_read} "
                     f"read{more}): sent {fmt_amount(rep.deployer_sent_raw or 0, rep.decimals)}, "
                     f"received {fmt_amount(rep.deployer_received_raw or 0, rep.decimals)} — "
                     f"a send is not necessarily a sale")
    if rep.not_read:
        not_read = plain_errors("; ".join(rep.not_read))
        lines.append("PARTIAL — not read: " + not_read.replace(
            "(timed out)", "(explorer timed out — try again)"))
    if rep.forensics is not None:
        lines += _render_forensics(rep.forensics, rep)
    elif svm:
        why = ("the creation transaction could not be read" if rep.creation_ref
               else "the creation was not found")
        lines.append(f"launch forensics: NOT RUN ({why})")
    else:
        lines.append("launch forensics: NOT CHECKED on EVM chains (Solana only)")
    return "\n".join(lines)


def token_origin_sync(tool, params: TokenOriginParams, execution_context=None):
    from core.wallet import token_origin
    address, chain, err = tool._address_on_chain(params.address, params.chain)
    if err:
        return tool._ar(error=err)
    wrong = tool._not_a_token(chain, address)
    if wrong:
        return tool._ar(error=wrong)
    rep = token_origin.origin(address, chain)
    return tool._ar(content=render_origin(rep),
                    metadata={"verb": "token_origin", "report": rep.to_dict()})
