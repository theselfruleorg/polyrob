"""ONE screen over every source (071 W2 §3.6): RPC facts first, providers second.

``Screen{checks[{name, result, source}], not_checked[{name, reason}],
hard_fails, flags, partial}``. Each source contributes a :class:`SourceReport`;
:func:`merge` folds them. The merge rules are the owner's:

* **A check that did not run is NOT a check that passed.** A source that fails
  puts every check it WOULD have made into ``not_checked`` with its reason.
* **One source's outage never turns a check into a pass.** A name leaves
  ``not_checked`` only when ANOTHER source actually ran that check; two sources
  that both ran it are both shown, so a disagreement is visible.
* **A partial screen says PARTIAL.** ``partial`` is simply "anything is left in
  ``not_checked``".
* **No paid providers.** Every source here is keyless (GoPlus, Jupiter Tokens
  v2, RugCheck, Honeypot.is) or the chain itself.

Check names reuse GoPlus's field names where a check overlaps (``mintable``,
``freezable``, ``is_honeypot``, ``buy_tax`` …), so the same question asked of
two sources lands on one name and a GoPlus gap closes when the chain answers it.

Read side ONLY: nothing on the spend path reads this yet (the identity gate and
``defi_trade`` keep their own inputs).
"""
from __future__ import annotations

import concurrent.futures as _cf
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Wall-clock budget for every non-GoPlus source together. A source still
#: running at the deadline is NOT CHECKED ("timed out"), never awaited.
FACTS_BUDGET_SEC = 12.0

_POOL = _cf.ThreadPoolExecutor(max_workers=8, thread_name_prefix="token-screen")


@dataclass(frozen=True)
class Check:
    name: str
    result: str
    source: str


@dataclass(frozen=True)
class NotChecked:
    name: str
    reason: str


@dataclass
class SourceReport:
    source: str
    checks: List[Check] = field(default_factory=list)
    not_checked: List[NotChecked] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    hard_fails: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: Why the whole source said nothing (None = it answered).
    failed: Optional[str] = None
    #: Solana only: the per-address holder report this source built.
    holders: object = None
    #: Solana only: the pump.fun curve state when the token is ON the curve.
    curve: object = None


@dataclass
class Screen:
    checks: List[Check] = field(default_factory=list)
    not_checked: List[NotChecked] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)
    hard_fails: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    answered: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    holders: object = None
    curve: object = None

    @property
    def partial(self) -> bool:
        return bool(self.not_checked)

    @property
    def available(self) -> bool:
        return bool(self.checks)


def failed_source(source: str, reason: str, expected: Sequence[str]) -> SourceReport:
    """The report of a source that said nothing: every check it owns is NOT
    CHECKED with the reason named."""
    return SourceReport(source=source, failed=reason,
                        not_checked=[NotChecked(n, f"{source}: {reason}") for n in expected])


def merge(reports: Sequence[SourceReport]) -> Screen:
    out = Screen()
    ran = set()
    for rep in reports:
        if rep.failed is None:
            if rep.source not in out.answered:
                out.answered.append(rep.source)
        else:
            out.failed.append(f"{rep.source} ({rep.failed})")
        out.checks.extend(rep.checks)
        ran.update(c.name for c in rep.checks)
        for f in rep.flags:
            tagged = f"{f} ({rep.source})"
            if tagged not in out.flags:
                out.flags.append(tagged)
        for h in rep.hard_fails:
            tagged = f"{h} ({rep.source})"
            if tagged not in out.hard_fails:
                out.hard_fails.append(tagged)
        out.notes.extend(rep.notes)
        if rep.holders is not None and out.holders is None:
            out.holders = rep.holders
        if rep.curve is not None and out.curve is None:
            out.curve = rep.curve
    reasons: Dict[str, List[str]] = {}
    for rep in reports:
        for nc in rep.not_checked:
            if nc.name in ran:
                continue
            reasons.setdefault(nc.name, [])
            if nc.reason not in reasons[nc.name]:
                reasons[nc.name].append(nc.reason)
    out.not_checked = [NotChecked(n, "; ".join(r)) for n, r in sorted(reasons.items())]
    return out


# --------------------------------------------------------------------------
# Plain words for a reader (owner on a phone, or the model).
# --------------------------------------------------------------------------

_ERR_CLASSES = ("HTTPStatusError", "HttpStatusError", "HTTPError", "ReadTimeout",
                "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutError",
                "TimeoutException", "ConnectError", "RemoteProtocolError",
                "ReadError", "ConnectionError", "ConnectionResetError")


def plain_reason(text: Optional[str]) -> str:
    """One failure reason in plain words. An exception class name or an HTTP
    status is a fact about OUR plumbing; the reader needs to know only whether
    the source was rate-limited, busy, slow or silent."""
    raw = str(text or "").strip()
    low = raw.lower()
    if not raw:
        return "no answer"
    if "429" in low or "too many requests" in low or "rate-limit" in low or "rate limit" in low:
        return "rate-limited"
    if "timed out" in low:
        return raw if low.startswith("timed out") else "timed out"
    if "timeout" in low:
        return "timed out"
    if any(c.lower() in low for c in ("HTTPStatusError", "HTTPError")):
        if any(code in low for code in (" 500", " 502", " 503", " 504", "'50")):
            return "busy (server error)"
        return "no answer (server error)"
    if any(c.lower() in low for c in _ERR_CLASSES) or "connection" in low:
        return "no answer"
    return raw


_ERR_RE = None


def plain_errors(text: str) -> str:
    """Replace every exception class name (with its ``: detail``) inside a
    longer text by its plain word. Text without one is returned unchanged."""
    global _ERR_RE
    import re
    if _ERR_RE is None:
        names = "|".join(sorted(_ERR_CLASSES, key=len, reverse=True))
        _ERR_RE = re.compile(rf"\b(?:{names})\b(?::[^;)\n]*)?")
    out = _ERR_RE.sub(lambda m: plain_reason(m.group(0)).split(" (")[0], str(text or ""))
    return out.replace("rate-limited (429)", "rate-limited")


def plain_number(value) -> str:
    """A quantity written out (never ``8.79944e+13``), rounded to about six
    significant digits. Unknown stays the word."""
    import math
    try:
        x = float(value)
    except (TypeError, ValueError):
        return "unknown"
    if value is None or math.isnan(x) or math.isinf(x):
        return "unknown"
    if x == 0:
        return "0"
    mag = int(math.floor(math.log10(abs(x))))
    places = 5 - mag
    r = round(x, places)
    if places <= 0:
        return f"{r:,.0f}"
    text = f"{r:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def quoted(text: Optional[str], *, name: bool = False) -> Optional[str]:
    """A token symbol or name for display: ATTACKER-WRITTEN data, so it is
    bounded, put on one line, stripped of control characters and shown in
    double quotes — plainly, without Python's repr() escapes. None when empty.

    Spaces are NOT trimmed or collapsed: a symbol padded with spaces
    ("USDC  ") is a typosquat, and the quotes are what make the padding visible."""
    if not text:
        return None
    import unicodedata
    from core.wallet.tokens import MAX_NAME_CHARS, MAX_SYMBOL_CHARS
    out = []
    for ch in str(text):
        if ch.isspace():
            out.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            continue
        else:
            out.append(ch)
    t = "".join(out)[:MAX_NAME_CHARS if name else MAX_SYMBOL_CHARS].replace('"', "'")
    return f'"{t}"' if t.strip() else None


# --------------------------------------------------------------------------
# Source adapters — pure: a provider/RPC result in, a SourceReport out.
# --------------------------------------------------------------------------

def goplus_expected(family: str) -> List[str]:
    from tools.defi.providers import goplus
    if family == "svm":
        return list(goplus._SOLANA_RISKS) + list(goplus._SOLANA_ACTIVE)
    return list(goplus._BOOL_RISKS) + ["buy_tax", "sell_tax", "is_open_source"]


#: GoPlus answers that mean the token cannot be sold — a HARD fail, not a flag.
_GOPLUS_HARD = {"is_honeypot": "1", "cannot_sell_all": "1", "non_transferable": "yes",
                "default_account_state": "yes"}


def from_goplus(verdict, family: str) -> SourceReport:
    if verdict is None or not getattr(verdict, "available", False):
        return failed_source("goplus", "no answer", goplus_expected(family))
    rep = SourceReport(source="goplus")
    for k, v in sorted(verdict.checks.items()):
        rep.checks.append(Check(k, str(v), "goplus"))
        if _GOPLUS_HARD.get(k) == str(v):
            rep.hard_fails.append(k)
    rep.flags = list(verdict.flags)
    rep.not_checked = [NotChecked(m, "goplus: not reported for this token/chain")
                       for m in (verdict.missing or [])]
    return rep


SVM_MINT_CHECKS = ["token_program", "mintable", "freezable", "permanent_delegate",
                   "transfer_hook", "transfer_fee", "non_transferable",
                   "default_account_state", "closable"]


def _short(addr: Optional[str]) -> str:
    return str(addr) if addr else "?"


def from_svm_mint(facts) -> SourceReport:
    """Mint authorities + Token-2022 extensions, judged from the chain."""
    rep = SourceReport(source="rpc")
    add = lambda n, r: rep.checks.append(Check(n, r, "rpc"))  # noqa: E731
    exts = facts.extensions
    add("token_program", "Token-2022" if facts.token_2022 else "classic SPL Token")
    add("supply", plain_number(facts.supply_human))
    if facts.mint_authority:
        add("mintable", f"yes — mint authority {_short(facts.mint_authority)} can mint more")
        rep.flags.append("mintable")
    else:
        add("mintable", "no (mint authority revoked)")
    if facts.freeze_authority:
        add("freezable", f"yes — freeze authority {_short(facts.freeze_authority)} can freeze any holder")
        rep.flags.append("freezable")
    else:
        add("freezable", "no (freeze authority revoked)")
    classic = not facts.token_2022
    na = "no (classic SPL program: no extensions)"

    pd = exts.get("permanentDelegate")
    if pd is not None and pd.get("delegate"):
        add("permanent_delegate",
            f"YES — {_short(pd.get('delegate'))} can move or burn ANY holder's balance")
        rep.hard_fails.append("permanent_delegate")
    else:
        add("permanent_delegate", na if classic else "no")

    th = exts.get("transferHook")
    if th is not None:
        prog, auth = th.get("programId"), th.get("authority")
        add("transfer_hook", f"yes — hook program {prog} runs on every transfer" if prog
            else "no (extension present, no hook program set)")
        if prog:
            rep.flags.append("transfer_hook_active")
        add("transfer_hook_upgradable", f"yes — authority {auth}" if auth else "no")
        if auth:
            rep.flags.append("transfer_hook_upgradable")
    else:
        add("transfer_hook", na if classic else "no")

    tf = exts.get("transferFeeConfig")
    if tf is not None:
        bps = []
        for k in ("olderTransferFee", "newerTransferFee"):
            try:
                bps.append(int((tf.get(k) or {}).get("transferFeeBasisPoints")))
            except (TypeError, ValueError):
                pass
        top = max(bps) if bps else None
        if top is None:
            add("transfer_fee", "UNREADABLE fee config — treat as charged")
            rep.flags.append("transfer_fee_unreadable")
        else:
            add("transfer_fee", f"{top / 100:g}% per transfer ({top} bps)" if top else "0 bps")
            if top:
                rep.flags.append(f"transfer_fee_{top}bps")
        cfg = tf.get("transferFeeConfigAuthority")
        add("transfer_fee_upgradable", f"yes — authority {cfg} can raise the fee" if cfg else "no")
        if cfg:
            rep.flags.append("transfer_fee_upgradable")
        wd = tf.get("withdrawWithheldAuthority")
        if wd:
            add("transfer_fee_withdraw_authority", str(wd))
    else:
        add("transfer_fee", na if classic else "no")

    if "nonTransferable" in exts:
        add("non_transferable", "YES — holders cannot transfer (or sell) it")
        rep.hard_fails.append("non_transferable")
    else:
        add("non_transferable", na if classic else "no")

    das = exts.get("defaultAccountState")
    if das is not None:
        state = str(das.get("accountState") or "").lower()
        if state == "frozen":
            add("default_account_state", "FROZEN — a new holder account starts frozen")
            rep.hard_fails.append("default_account_state_frozen")
        elif state in ("initialized",):
            add("default_account_state", "initialized")
        else:
            rep.not_checked.append(NotChecked("default_account_state",
                                              f"rpc: unreadable state {state!r}"))
    else:
        add("default_account_state", na if classic else "no extension (initialized)")

    mca = exts.get("mintCloseAuthority")
    if mca is not None and mca.get("closeAuthority"):
        add("closable", f"yes — {mca.get('closeAuthority')} can close the mint")
        rep.flags.append("closable")
    else:
        add("closable", na if classic else "no")

    if "confidentialTransferMint" in exts:
        add("confidential_transfer", "yes — balances can be moved out of view")
        rep.flags.append("confidential_transfer")
    pc = exts.get("pausableConfig")
    if pc is not None:
        add("pausable", f"yes — authority {pc.get('authority')} can pause transfers"
            + (" (PAUSED now)" if pc.get("paused") else ""))
        rep.flags.append("pausable")
        if pc.get("paused"):
            rep.hard_fails.append("paused")
    if "scaledUiAmountConfig" in exts:
        add("scaled_ui_amount", "yes — the displayed balance is multiplied by an authority-set factor")
        rep.flags.append("scaled_ui_amount")
    if "interestBearingConfig" in exts:
        add("interest_bearing", "yes — the displayed balance accrues by a rate an authority sets")
    tm = exts.get("tokenMetadata")
    if tm is not None:
        ua = tm.get("updateAuthority")
        add("metadata_mutable", f"yes — {ua} can rewrite name/symbol" if ua else "no")
        if ua:
            rep.flags.append("metadata_mutable")
    for ext in facts.unassessed_extensions:
        rep.not_checked.append(NotChecked(f"extension:{ext}",
                                          "rpc: extension present but not assessed by this screen"))
    return rep


def holders_from_lines(lines, *, total_supply: Optional[float], reason_if_empty: str):
    """The per-address RPC rows as the SAME HolderReport the GoPlus path builds,
    so ``token_holders``/``token_info`` render both alike."""
    from tools.defi.providers.base import HolderReport, HolderRow
    if not lines:
        return HolderReport(available=False, reason=reason_if_empty)
    rows = [HolderRow(address=l.owner or l.token_account, percent=l.share,
                      is_contract=l.program_owned,
                      tag=l.label or ("program-owned (pool, curve, lock or multisig)"
                                      if l.program_owned else None))
            for l in lines]
    return HolderReport(available=True, holder_count=None, total_supply=total_supply,
                        top_holders=rows)


def from_svm_holders(lines, total_supply: Optional[float]) -> SourceReport:
    from core.wallet.spl_facts import concentration
    rep = SourceReport(source="rpc-holders")
    top1, top10, people10 = concentration(lines)
    if top10 is None:
        return failed_source("rpc-holders", "no holder shares (supply unknown or no holders)",
                             ["holder_concentration"])
    pct = lambda x: "unknown" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    rep.checks.append(Check("holder_concentration",
                            f"top-1 {pct(top1)}, top-10 {pct(top10)}, top-10 excluding "
                            f"program-owned accounts {pct(people10)}", "rpc"))
    if people10 is not None and people10 > 0.5:
        rep.flags.append("holder_concentration_over_50pct")
    n_prog = sum(1 for l in lines if l.program_owned)
    if n_prog:
        rep.notes.append(f"{n_prog} of the top {len(lines)} holder(s) are program-owned "
                         "(a pool, bonding curve, lock or multisig) — counted in top-10, "
                         "excluded from the 'excluding' figure")
    rep.holders = holders_from_lines(lines, total_supply=total_supply,
                                     reason_if_empty="no holder rows")
    return rep


def from_pump_curve(curve) -> SourceReport:
    rep = SourceReport(source="rpc")
    if curve is None:
        return failed_source("rpc", "curve address could not be derived (solders missing)",
                             ["pump_curve"])
    if curve.state == "not_pump":
        rep.checks.append(Check("pump_curve", "not a pump.fun mint (no bonding curve)", "rpc"))
    elif curve.state == "graduated":
        rep.checks.append(Check("pump_curve", "graduated — the bonding curve completed and "
                                "liquidity migrated to an AMM", "rpc"))
    else:
        sold = curve.sold_fraction
        sol = curve.sol_in_curve
        rep.checks.append(Check(
            "pump_curve",
            f"ON CURVE — ~{'unknown' if sold is None else f'{sold * 100:.1f}%'} of the sale "
            f"allocation sold, {'unknown' if sol is None else f'{sol:,.3f}'} SOL in the curve",
            "rpc"))
        rep.flags.append("on_bonding_curve")
        rep.curve = curve
    return rep


def from_jupiter(tok) -> SourceReport:
    rep = SourceReport(source="jupiter")
    if tok is None:
        return failed_source("jupiter", "token not indexed", ["jupiter_verified", "organic_score"])
    add = lambda n, r: rep.checks.append(Check(n, r, "jupiter"))  # noqa: E731
    add("jupiter_verified", "unknown" if tok.is_verified is None else
        ("yes" if tok.is_verified else "no") + (f" (tags: {', '.join(tok.tags)})" if tok.tags else ""))
    if tok.organic_score is None:
        rep.not_checked.append(NotChecked("organic_score", "jupiter: not reported"))
    else:
        add("organic_score", f"{tok.organic_score:.0f}/100"
            + (f" ({tok.organic_label})" if tok.organic_label else ""))
        if tok.organic_score < 20:
            rep.flags.append("low_organic_activity")
    if tok.mint_authority_disabled is not None:
        add("mintable", "no (audit: disabled)" if tok.mint_authority_disabled else "yes")
    if tok.freeze_authority_disabled is not None:
        add("freezable", "no (audit: disabled)" if tok.freeze_authority_disabled else "yes")
    if tok.top_holders_pct is not None:
        add("top_holders_pct", f"{tok.top_holders_pct:.1f}%")
    if tok.dev_balance_pct is not None:
        add("dev_balance_pct", f"{tok.dev_balance_pct:.2f}%")
    if tok.holder_count is not None:
        add("holder_count", f"{tok.holder_count:,}")
    if tok.launchpad:
        add("launchpad", tok.launchpad)
    return rep


def from_rugcheck(report) -> SourceReport:
    rep = SourceReport(source="rugcheck")
    rep.checks.append(Check("rugcheck_score",
                            "unknown" if report.score_normalised is None else
                            f"{report.score_normalised:g} (normalised; higher = riskier)",
                            "rugcheck"))
    if not report.risks:
        rep.checks.append(Check("rugcheck_risks", "none named", "rugcheck"))
    for r in report.risks:
        rep.checks.append(Check("rugcheck_risk",
                                f"{r.name}" + (f" [{r.level}]" if r.level else "")
                                + (f": {r.value}" if r.value else ""), "rugcheck"))
    for r in report.danger:
        rep.flags.append(f"rugcheck:{r.name}")
    if report.lp_locked_pct is not None:
        rep.checks.append(Check("lp_locked_pct", f"{report.lp_locked_pct:.1f}%", "rugcheck"))
    return rep


EVM_RPC_CHECKS = ["bytecode", "proxy", "owner"]


def from_evm_facts(facts) -> SourceReport:
    from core.wallet.evm_facts import ZERO
    rep = SourceReport(source="rpc")
    add = lambda n, r: rep.checks.append(Check(n, r, "rpc"))  # noqa: E731
    if facts.has_code is None:
        rep.not_checked.append(NotChecked("bytecode", "rpc: eth_getCode failed"))
    elif facts.has_code is False:
        add("bytecode", "NONE — no contract at this address")
        rep.hard_fails.append("no_contract_code")
        return rep
    else:
        add("bytecode", "present")
    kind = facts.proxy_kind
    if kind is None:
        rep.not_checked.append(NotChecked("proxy", "rpc: a proxy slot read failed"))
    elif kind == "none_found":
        add("proxy", "no standard proxy slot set (EIP-1967 + legacy ZeppelinOS read; "
            "a custom proxy is not ruled out)")
    else:
        impl = facts.implementation or facts.beacon
        admin = facts.admin
        who = (f"; proxy admin {admin}" if admin else "; proxy admin slot empty")
        if facts.admin_owner and facts.admin_owner != ZERO:
            who += f" (owned by {facts.admin_owner})"
        elif facts.admin_owner == ZERO:
            who += " (admin's owner renounced)"
        add("proxy", f"YES ({kind}) — implementation {impl}{who}: the code can be "
            "replaced after you buy")
        rep.flags.append("upgradeable_proxy")
    if facts.owner_state is None:
        rep.not_checked.append(NotChecked("owner", "rpc: owner() read failed"))
    elif facts.owner_state == "renounced":
        add("owner", "renounced (owner() = zero address)")
    elif facts.owner_state == "set":
        add("owner", f"set — {facts.owner}")
    else:
        add("owner", "no owner() function (ownership not readable this way)")
    return rep


HONEYPOT_CHECKS = ["is_honeypot", "buy_tax", "sell_tax"]


def from_honeypot(sim) -> SourceReport:
    if not sim.simulated:
        return failed_source("honeypot.is", f"simulation did not complete ({sim.reason})",
                             HONEYPOT_CHECKS)
    rep = SourceReport(source="honeypot.is")
    if sim.is_honeypot is None:
        rep.not_checked.append(NotChecked("is_honeypot", "honeypot.is: verdict not sent"))
    else:
        rep.checks.append(Check("is_honeypot", "1" if sim.is_honeypot else "0", "honeypot.is"))
        if sim.is_honeypot:
            rep.hard_fails.append("is_honeypot")
    for name, val in (("buy_tax", sim.buy_tax_pct), ("sell_tax", sim.sell_tax_pct)):
        if val is None:
            rep.not_checked.append(NotChecked(name, "honeypot.is: not sent"))
            continue
        rep.checks.append(Check(name, f"{val:g}% (simulated)", "honeypot.is"))
        if val > 10:
            rep.flags.append(f"{name}_{val:g}pct")
    if sim.pair:
        liq = ("unknown liquidity" if sim.pair_liquidity_usd is None
               else f"${sim.pair_liquidity_usd:,.0f} liquidity")
        rep.notes.append(f"honeypot.is simulated through pair {sim.pair} ({liq})")
    return rep


# --------------------------------------------------------------------------
# Gather — the network side. Each source is isolated; budgeted as a whole.
# --------------------------------------------------------------------------

def _why(exc: BaseException) -> str:
    """A failure, named for a reader: the provider's own reason when it gave
    one, a plain word (rate-limited / timed out / no answer) otherwise."""
    text = str(exc).strip()
    if exc.__class__.__name__ in ("SourceUnavailable", "NotAMint"):
        return plain_reason(text[:100] or exc.__class__.__name__)
    return plain_reason(exc.__class__.__name__ + (f": {text[:80]}" if text else ""))


def _svm_mint_and_holders(mint: str) -> List[SourceReport]:
    from core.wallet import spl_facts
    try:
        facts = spl_facts.read_mint(mint)
    except Exception as exc:
        return [failed_source("rpc", _why(exc), SVM_MINT_CHECKS),
                failed_source("rpc-holders", "mint not read", ["holder_concentration"])]
    out = [from_svm_mint(facts)]
    labels = {}
    curve_addr = spl_facts.pump_curve_address(mint)
    if curve_addr:
        labels[curve_addr] = "pump.fun bonding curve"
    try:
        lines = spl_facts.read_holders(mint, facts.supply_raw, labels=labels)
        out.append(from_svm_holders(lines, facts.supply_human))
    except Exception as exc:
        rep = failed_source("rpc-holders", _why(exc), ["holder_concentration"])
        out.append(rep)
    return out


def _svm_pump(mint: str) -> List[SourceReport]:
    from core.wallet import spl_facts
    try:
        return [from_pump_curve(spl_facts.read_pump_curve(mint))]
    except Exception as exc:
        return [failed_source("rpc", _why(exc), ["pump_curve"])]


def _jupiter(mint: str) -> List[SourceReport]:
    from tools.defi.providers import token_audits
    try:
        return [from_jupiter(token_audits.jupiter_token(mint))]
    except Exception as exc:
        return [failed_source("jupiter", _why(exc), ["jupiter_verified", "organic_score"])]


def _rugcheck(mint: str) -> List[SourceReport]:
    from tools.defi.providers import token_audits
    try:
        return [from_rugcheck(token_audits.rugcheck_report(mint))]
    except Exception as exc:
        return [failed_source("rugcheck", _why(exc), ["rugcheck_score"])]


def _evm_rpc(chain: str, address: str) -> List[SourceReport]:
    from core.wallet import evm_facts
    try:
        return [from_evm_facts(evm_facts.read_contract(chain, address))]
    except Exception as exc:
        return [failed_source("rpc", _why(exc), EVM_RPC_CHECKS)]


def _honeypot(chain: str, address: str) -> List[SourceReport]:
    from tools.defi.providers import token_audits
    if chain not in token_audits.HONEYPOT_CHAINS:
        return [failed_source("honeypot.is", "chain not covered", HONEYPOT_CHECKS)]
    try:
        return [from_honeypot(token_audits.honeypot_sim(chain, address))]
    except Exception as exc:
        return [failed_source("honeypot.is", _why(exc), HONEYPOT_CHECKS)]


#: (label, callable(chain, address) -> [SourceReport], checks it owns) per family.
def _jobs(family: str):
    if family == "svm":
        return [("rpc", lambda c, a: _svm_mint_and_holders(a), SVM_MINT_CHECKS + ["holder_concentration"]),
                ("rpc", lambda c, a: _svm_pump(a), ["pump_curve"]),
                ("jupiter", lambda c, a: _jupiter(a), ["jupiter_verified", "organic_score"]),
                ("rugcheck", lambda c, a: _rugcheck(a), ["rugcheck_score"])]
    return [("rpc", _evm_rpc, EVM_RPC_CHECKS),
            ("honeypot.is", _honeypot, HONEYPOT_CHECKS)]


def gather_facts(chain: str, address: str, *, budget: float = FACTS_BUDGET_SEC) -> List[SourceReport]:
    """Every non-GoPlus source for *chain*, in parallel, inside one budget."""
    from core.wallet import chains
    row = chains.get(chain)
    family = row.family if row is not None else "evm"
    jobs = _jobs(family)
    futures = [(_POOL.submit(fn, chain, address), label, owned) for label, fn, owned in jobs]
    done, _pending = _cf.wait([f for f, _l, _o in futures], timeout=budget)
    out: List[SourceReport] = []
    for fut, label, owned in futures:
        if fut in done:
            try:
                out.extend(fut.result())
                continue
            except Exception as exc:  # adapters catch their own; belt and braces
                out.append(failed_source(label, _why(exc), owned))
                continue
        fut.cancel()
        out.append(failed_source(label, f"timed out after {budget:g}s", owned))
    return out


def svm_holders(mint: str):
    """``token_holders`` on Solana: the RPC's top-20 as a HolderReport. Fails
    CLOSED with the reason — and Jupiter's aggregate figure when it has one."""
    from core.wallet import spl_facts
    from tools.defi.providers.base import HolderReport
    try:
        facts = spl_facts.read_mint(mint)
        labels = {}
        curve_addr = spl_facts.pump_curve_address(mint)
        if curve_addr:
            labels[curve_addr] = "pump.fun bonding curve"
        lines = spl_facts.read_holders(mint, facts.supply_raw, labels=labels)
        return holders_from_lines(lines, total_supply=facts.supply_human,
                                  reason_if_empty="the chain reported no non-zero holder accounts")
    except Exception as exc:
        reason = f"the Solana RPC could not list the largest holders ({_why(exc)})"
    try:
        from tools.defi.providers import token_audits
        tok = token_audits.jupiter_token(mint)
        if tok is not None and tok.top_holders_pct is not None:
            reason += (f"; Jupiter reports the top holders hold {tok.top_holders_pct:.1f}%"
                       + (f" across {tok.holder_count:,} holders" if tok.holder_count else "")
                       + " (an aggregate, no per-address rows)")
    except Exception:
        pass
    reason += ". A keyed RPC pinned in DEFI_SOLANA_RPC makes the per-address read reliable"
    return HolderReport(available=False, reason=reason)


# --------------------------------------------------------------------------
# Render — the token_info screen block.
# --------------------------------------------------------------------------

#: MATERIAL questions: the ones that decide whether a holder can sell and keep
#: the balance. A screen is PARTIAL only when one of these was asked of a source
#: that did not answer AND no other source answered it. A gap in a minor check
#: (e.g. GoPlus not sending ``cannot_sell_all`` while honeypot.is simulated the
#: sell) is still listed under "not run", but it does not make the screen PARTIAL.
MATERIAL_GROUPS = (
    ("sell check", ("is_honeypot", "cannot_sell_all", "non_transferable",
                    "default_account_state")),
    ("taxes", ("buy_tax", "sell_tax", "transfer_fee")),
    ("mint authority", ("is_mintable", "mintable")),
    ("freeze authority", ("freezable",)),
    ("permanent delegate", ("permanent_delegate", "balance_mutable_authority")),
    ("owner/proxy", ("owner", "proxy", "is_proxy", "hidden_owner",
                     "can_take_back_ownership")),
)

_MATERIAL_NAMES = {n for _label, names in MATERIAL_GROUPS for n in names}

#: Checks that report a FACT rather than pass/fail. Rendered on one facts line.
_INFO_CHECKS = ("supply", "token_program", "holder_count", "top_holders_pct",
                "dev_balance_pct", "organic_score", "jupiter_verified", "launchpad",
                "pump_curve", "holder_concentration", "rugcheck_score",
                "rugcheck_risk", "rugcheck_risks", "lp_locked_pct",
                "transfer_fee_withdraw_authority")

#: A passed check in a few plain words, most material first.
_PASS_WORDS = {
    "is_honeypot": "not a honeypot", "sell_tax": "0% sell tax", "buy_tax": "0% buy tax",
    "cannot_sell_all": "can sell all", "cannot_buy": "can buy",
    "is_mintable": "no mint function", "mintable": "mint authority revoked",
    "freezable": "freeze authority revoked", "permanent_delegate": "no permanent delegate",
    "balance_mutable_authority": "no balance authority",
    "non_transferable": "transferable", "default_account_state": "accounts not frozen",
    "transfer_fee": "no transfer fee", "transfer_hook": "no transfer hook",
    "owner": "owner renounced", "hidden_owner": "no hidden owner",
    "can_take_back_ownership": "ownership not reclaimable",
    "is_proxy": "not a proxy", "proxy": "no standard proxy",
    "is_blacklisted": "no blacklist", "transfer_pausable": "not pausable",
    "selfdestruct": "no self-destruct", "is_open_source": "source verified",
    "slippage_modifiable": "tax cannot be changed",
    "personal_slippage_modifiable": "no per-wallet tax", "trading_cooldown": "no cooldown",
    "bytecode": "contract code present", "closable": "mint not closable",
    "metadata_mutable": "metadata fixed", "rugcheck_risks": "no rugcheck risks",
}
_PASS_ORDER = list(_PASS_WORDS)

#: One fact, two names: the same risk raised by two sources is ONE line.
_FLAG_ALIASES = {
    "proxy_upgradeable": "upgradeable proxy", "upgradeable_proxy": "upgradeable proxy",
    "honeypot": "honeypot", "mintable": "mintable", "freezable": "freezable",
    "metadata_mutable": "metadata can be rewritten", "on_bonding_curve": "on the pump.fun curve",
    "low_organic_activity": "low organic activity", "blacklist": "blacklist",
    "transfer_pausable": "transfers can be paused", "pausable": "transfers can be paused",
    "hidden_owner": "hidden owner", "ownership_reclaimable": "ownership can be taken back",
    "closed_source": "source not verified", "closable": "mint can be closed",
    "transfer_hook_active": "transfer hook", "transfer_fee_upgradable": "transfer fee can be raised",
    "transfer_hook_upgradable": "transfer hook can be changed",
    "holder_concentration_over_50pct": "top-10 hold over 50%",
}
#: The check whose (longer) result explains a merged flag.
_FLAG_DETAIL = {"upgradeable proxy": "proxy", "mintable": "mintable",
                "freezable": "freezable", "metadata can be rewritten": "metadata_mutable",
                "transfer hook": "transfer_hook", "top-10 hold over 50%": "holder_concentration"}
#: The checks a merged flag already explains (not repeated as a raw row).
_FLAG_CHECKS = {
    "upgradeable proxy": {"proxy", "is_proxy"}, "mintable": {"mintable", "is_mintable"},
    "freezable": {"freezable"}, "honeypot": {"is_honeypot"}, "blacklist": {"is_blacklisted"},
    "transfers can be paused": {"transfer_pausable", "pausable"},
    "hidden owner": {"hidden_owner"}, "ownership can be taken back": {"can_take_back_ownership"},
    "source not verified": {"is_open_source"}, "metadata can be rewritten": {"metadata_mutable"},
    "on the pump.fun curve": {"pump_curve"}, "low organic activity": {"organic_score"},
    "transfer hook": {"transfer_hook"}, "transfer hook can be changed": {"transfer_hook_upgradable"},
    "transfer fee can be raised": {"transfer_fee_upgradable"}, "mint can be closed": {"closable"},
}
#: Flags that are ordinary for a canonical, issuer-run token (e.g. USDC).
_ISSUER_FLAGS = {"upgradeable proxy", "mintable", "freezable", "blacklist",
                 "transfers can be paused", "metadata can be rewritten"}


def _is_clean(name: str, result: str) -> Optional[bool]:
    """True = passed, False = raised/odd, None = a fact (no pass/fail)."""
    r = str(result or "").strip().lower()
    if name in _INFO_CHECKS:
        return None
    if name == "is_open_source":
        return r.startswith("1")
    if name == "owner":
        return True if r.startswith("renounced") else None
    if name in ("buy_tax", "sell_tax"):
        try:
            return float(r.split("%")[0].split()[0]) == 0.0
        except (ValueError, IndexError):
            return False
    return r.startswith(("0", "no", "present", "none named"))


def material_gaps(screen: Screen) -> List[str]:
    """The MATERIAL questions no source answered (see MATERIAL_GROUPS)."""
    ran = {c.name for c in screen.checks}
    missing = {n.name for n in screen.not_checked}
    return [label for label, names in MATERIAL_GROUPS
            if any(n in missing for n in names) and not any(n in ran for n in names)]


def merged_flags(screen: Screen) -> List[tuple]:
    """``[(label, [sources])]`` with synonyms folded into one fact."""
    import re
    out: Dict[str, List[str]] = {}
    for f in screen.flags:
        m = re.match(r"^(.*) \(([^()]*)\)$", f)
        raw, src = (m.group(1), m.group(2)) if m else (f, "")
        label = _FLAG_ALIASES.get(raw, raw.replace("_", " "))
        out.setdefault(label, [])
        if src and src not in out[label]:
            out[label].append(src)
    return list(out.items())


def _fact_text(name: str, results: List[Check]) -> Optional[str]:
    r = results[0].result
    if name == "token_program":
        return r
    if name == "supply":
        return f"supply {r}"
    if name == "organic_score":
        return f"organic score {r}"
    if name == "dev_balance_pct":
        return f"dev holds {r}"
    if name == "launchpad":
        return f"launchpad {r}"
    if name == "rugcheck_score":
        return f"rugcheck {r}"
    if name == "lp_locked_pct":
        return f"LP locked {r}"
    if name == "owner":
        if r.startswith("no owner()"):
            return "no owner() function"
        return f"token owner(): {r.replace('set — ', '')}"
    if name == "rugcheck_risk":
        return "rugcheck risks: " + "; ".join(c.result for c in results)
    return None


def _not_run_lines(screen: Screen, indent: str) -> List[str]:
    """Every check that did not run, grouped by reason, in ONE line."""
    if not screen.not_checked:
        return []
    groups: Dict[str, List[str]] = {}
    for n in screen.not_checked:
        for part in n.reason.split("; "):
            src, _, why = part.partition(": ")
            key = f"{src}: {plain_reason(why)}" if why else plain_reason(part)
            groups.setdefault(key, [])
            if n.name not in groups[key]:
                groups[key].append(n.name)
    bits = []
    for key, names in groups.items():
        src, _, why = key.partition(": ")
        why = why.replace("not reported for this token/chain", "not reported for this token")
        if len(names) <= 3:
            what = ", ".join(names)
            bits.append(f"{what} ({src}: {why})" if why else f"{what} ({src})")
        else:
            material = [x for x in names if x in _MATERIAL_NAMES]
            bits.append((f"{len(names)} {src} checks ({why})" if why
                         else f"{len(names)} checks ({src})")
                        + (f" incl. {', '.join(material)}" if material else ""))
    return [f"{indent}not run: " + "; ".join(bits)
            + " — a check that did not run is not a pass."]


def render(screen: Screen, *, canonical_symbol: Optional[str] = None) -> List[str]:
    """Pure. The screen in a few lines: flags (each fact once, every source that
    raised it named), ONE line of passed checks, ONE line of checks that did not
    run, grouped by reason. The full per-check table stays in the token_info
    metadata. Honesty rules kept: a check that did not run is not a pass; a
    PARTIAL screen (a material question unanswered) says PARTIAL; two sources
    that DISAGREE on one check are both shown.

    *canonical_symbol*: set for a canonical/pinned token, so an issuer power
    (proxy, mint, freeze) is labelled as normal for it rather than alarming."""
    if not screen.available:
        why = "; ".join(plain_errors(f) for f in screen.failed) if screen.failed else "no source answered"
        return [f"  screen:   unavailable — no screener returned anything ({why}). "
                "This is NOT a clean result; the token is UNSCREENED."]
    by_name: Dict[str, List[Check]] = {}
    for c in screen.checks:
        by_name.setdefault(c.name, []).append(c)
    gaps = material_gaps(screen)
    flags = merged_flags(screen)
    head = f"  screen:   sources: {', '.join(screen.answered) or 'none'}"
    if gaps:
        head += f" — PARTIAL: no source checked {', '.join(gaps)}"
    if not flags:
        head += ("; no risk flags among the checks that ran" if screen.not_checked
                 else "; no risk flags raised")
    lines = [head]
    if screen.hard_fails:
        lines.append(f"  ⛔ HARD FAIL: {', '.join(screen.hard_fails)} — a holder may be "
                     "unable to sell, or may lose the balance outright. Do not buy.")
    for label, sources in flags:
        detail = ""
        check = _FLAG_DETAIL.get(label)
        if check and check in by_name:
            long = [c.result for c in by_name[check] if "—" in c.result]
            if long:
                detail = ": " + (long[0] if label == "upgradeable proxy"
                                 else long[0].split(" — ", 1)[-1])
        if canonical_symbol and label in _ISSUER_FLAGS:
            detail += f" (normal for {canonical_symbol}; issuer-controlled)"
        lines.append(f"      FLAG {label} [{', '.join(sources) or '?'}]{detail}")
    passed, odd, facts = [], [], []
    for name in sorted(by_name, key=lambda n: (_PASS_ORDER.index(n) if n in _PASS_ORDER else 999, n)):
        results = by_name[name]
        verdicts = [_is_clean(name, c.result) for c in results]
        if all(v is True for v in verdicts):
            passed.append(name)
        elif any(v is True for v in verdicts) and any(v is False for v in verdicts):
            # Two sources that DISAGREE are shown side by side, never averaged.
            odd.append((name, f"{name} = " + " | ".join(
                f"{c.result} [{c.source}]" for c in sorted(results, key=lambda c: c.source)), True))
        elif any(v is False for v in verdicts):
            odd.append((name, f"{name} = " + " | ".join(
                f"{c.result} [{c.source}]" for c in results), False))
        else:
            fact = _fact_text(name, results)
            if fact:
                facts.append(fact)
    # A raised check already named by a FLAG line is not repeated; a check the
    # sources DISAGREE on always is.
    explained = set()
    for lbl, _s in flags:
        explained |= _FLAG_CHECKS.get(lbl, set())
    for name, text, disagree in odd:
        if disagree or name not in explained:
            lines.append(f"      {'sources disagree: ' if disagree else ''}{text}")
    if passed:
        words = []
        both_taxes = "buy_tax" in passed and "sell_tax" in passed
        for n in passed:
            w = _PASS_WORDS.get(n, n.replace("_", " "))
            if both_taxes and n in ("buy_tax", "sell_tax"):
                w = "0% buy/sell tax"
            if w not in words:
                words.append(w)
        shown = words[:5]
        more = len(words) - len(shown)
        lines.append(f"      {len(passed)} passed: {', '.join(shown)}"
                     + (f", +{more} more" if more > 0 else ""))
    if facts:
        lines.append(f"      facts: {' · '.join(facts)}")
    for note in screen.notes:
        lines.append(f"      note: {note}")
    lines += _not_run_lines(screen, "      ")
    # A source that failed but owned no listed check would otherwise vanish.
    named = {p.split(":")[0] for n in screen.not_checked for p in n.reason.split("; ")}
    silent = [f for f in screen.failed if f.split(" (")[0] not in named]
    if silent:
        lines.append(f"      did not answer: {'; '.join(plain_errors(f) for f in silent)}")
    return lines


def jupiter_verified(screen: Screen) -> Optional[str]:
    """Jupiter's own verification, for the identity line — or None."""
    for c in screen.checks:
        if c.name == "jupiter_verified" and not c.result.startswith("unknown"):
            return c.result
    return None
