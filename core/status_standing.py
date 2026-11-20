"""Status slots ``income_rule`` and ``mandates`` — the owner's standing money rules.

Two owner-signed stores the wallet acts on with no card per act (122/124):

* ``income_rule`` — the income allocation rule and its debt
  (``core.wallet.income_rule``, ``<data_home>/wallet/income_rule.json``, the
  ``/wallet income`` verb). The lines are that module's own ``status_lines``.
* ``mandates`` — the owner's standing grants
  (``core.wallet.standing_grant``, ``<data_home>/wallet/mandates.json``, the
  ``/nft mandate`` verb). Each row renders through ``Mandate.line``.

Both read through the module's own reader (``income_rule.load``,
``standing_grant.mandates``), which RAISES on an unreadable store: the slot is
then ``unavailable(<reason>)`` (``status_snapshot._guarded``), never "none".
No file is "none", and says so. A stored rule or a live mandate whose signature
does not verify grants nothing — the section says that too, with a health item,
so "live" on a screen always means "the gate would honour it".

Cheap: no network read. The signature check reads the wallet's local signer,
and only when a rule or a live mandate exists.
"""
from __future__ import annotations

import time
from typing import Optional

from core.status_sections import register_status_section
from core.status_types import SEVERITY_WARN, HealthItem, Section, _guarded


def income_rule_section(data_dir: Optional[str]) -> Section:
    from core.wallet import income_rule as R
    path = R.store_path(data_dir)
    state = R.load(path)          # raises on an unreadable store
    rule = R.rule_of(state)
    raw = state.get("rule") if isinstance(state.get("rule"), dict) else None
    unverified = bool(raw and raw.get("ended_at") is None and rule is None)
    unknown = [b for b in state.get("buys") or () if b.get("state") == R.B_UNKNOWN]
    held = R.open_buy(state)
    sec = Section(name="income_rule", data={
        "set": rule is not None, "rule_id": rule.id if rule else (raw or {}).get("id"),
        "rule": rule.line() if rule else None, "verified": not unverified,
        "owed_usd": R.owed_usd(state), "in_flight": held.get("id") if held else None,
        "unknown_buys": [b.get("id") for b in unknown]})
    lines = R.status_lines(state, rule=rule, limit=1)
    if unverified:
        lines[0] = (f"Rule: {raw.get('id')} is stored but does not verify — "
                    f"it allocates nothing")
        sec.health.append(HealthItem(
            key="income_rule_unverified", severity=SEVERITY_WARN,
            text="the stored income rule does not verify against the wallet key, so it "
                 "allocates nothing",
            remedy="/wallet income end, then set it again"))
    if unknown:
        sec.health.append(HealthItem(
            key="income_rule_unknown_buy", severity=SEVERITY_WARN,
            text=f"{len(unknown)} income-rule buy(s) ended unknown — the debt they "
                 f"cover is held, not paid",
            remedy="check the transaction, then /wallet income settle <id> landed|lost go"))
    if rule is None and raw is None and not state.get("entries") and not state.get("buys"):
        lines = ["Rule: none set — no income is allocated"]   # nothing owed, nothing bought
    sec.lines = lines
    return sec


def mandates_section(data_dir: Optional[str], now: Optional[float] = None) -> Section:
    from core.wallet import standing_grant as SG
    now = time.time() if now is None else now
    rows = SG.mandates(SG.store_path(data_dir))   # raises on an unreadable store
    live = [m for m in rows if m.live(now)]
    sec = Section(name="mandates", data={"live": [], "ended_count": len(rows) - len(live)})
    if not live:
        sec.lines.append("none live — every owner-card act waits for your tap"
                         + (f" ({len(rows)} ended or used up)" if rows else ""))
        return sec
    bad = []
    for m in live:
        ok = bool(SG._verified(m))
        sec.data["live"].append({"id": m.id, "verbs": list(m.verbs), "chains": list(m.chains),
                                 "used_count": m.used_count, "count": m.count,
                                 "used_usd": m.used_usd, "total_usd": m.total_usd,
                                 "ends_at": m.ends_at, "verified": ok})
        sec.lines.append(m.line(now) + ("" if ok else " — does not verify; it grants nothing"))
        if not ok:
            bad.append(m.id)
    if rows and len(rows) > len(live):
        sec.lines.append(f"{len(rows) - len(live)} ended or used up")
    if bad:
        sec.health.append(HealthItem(
            key="mandate_unverified", severity=SEVERITY_WARN,
            text=f"mandate(s) {', '.join(bad)} do not verify against the wallet key, so "
                 f"every act they name still waits for your card",
            remedy="/nft mandate end <id>, then grant it again with /nft trust"))
    return sec


register_status_section(
    "income_rule", lambda ctx: _guarded("income_rule", income_rule_section, ctx.data_dir),
    tenant=False)
register_status_section(
    "mandates", lambda ctx: _guarded("mandates", mandates_section, ctx.data_dir, ctx.now),
    tenant=False)


__all__ = ["income_rule_section", "mandates_section"]
