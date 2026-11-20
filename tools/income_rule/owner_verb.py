"""``/wallet income`` — the owner's income allocation rule (proposal 122 W4).

The one place the rule is written: an owner seat, a quote first and then ``go`` (one owner
decision). The agent has no verb for it (``/wallet`` proposals carry only ``trust`` and
``untrust``), and ``core.wallet.income_rule`` refuses every context that is not an owner-seat
call.

    /wallet income                                   the rule, the debt, the last buys
    /wallet income <kinds> <P>% to <token> on <network> [min <usd>] [go]
    /wallet income end [go]                          end the rule (the debt stays, shown)
    /wallet income settle <buy> landed|lost [go]     your word on a buy of unknown end

``<kinds>``: ``all`` or a comma list of ``fees``, ``nft``, ``payments``, ``trading``.
"""
from typing import List, Optional

USAGE = ("Usage: /wallet income — the rule and its debt\n"
         "/wallet income <all|fees,nft,payments,trading> <percent>% to <token> on <network> "
         "[min <usd>] [go] — set THE rule (the token must be pinned)\n"
         "/wallet income end [go] · /wallet income settle <buy> landed|lost [go]")


def _status(user_id: str) -> str:
    from core.wallet import income_rule as R
    try:
        state = R.load(R.store_path())
    except Exception as exc:  # noqa: BLE001 — unreadable is not "no rule"
        return f"❌ The income rule could not be read ({exc}); nothing is assumed."
    return "\n".join(["Income rule:"] + R.status_lines(state, rule=R.rule_of(state)))


def _parse_set(words: List[str]):
    """``(kinds, percent, token, chain, min_usd)`` or raises ``RuleError``."""
    from core.wallet import income_rule as R
    low = [w.lower() for w in words]
    if len(words) < 6 or low[2] != "to" or low[4] != "on":
        raise R.RuleError(USAGE)
    kinds = R.parse_kinds(words[0])
    try:
        percent = float(words[1].rstrip("%"))
    except ValueError:
        raise R.RuleError(f"{words[1]!r} is not a percent") from None
    min_usd = R.DEFAULT_MIN_USD
    rest = low[6:]
    if rest:
        if len(rest) != 2 or rest[0] != "min":
            raise R.RuleError(USAGE)
        try:
            min_usd = float(rest[1].lstrip("$"))
        except ValueError:
            raise R.RuleError(f"{words[7]!r} is not a USD amount") from None
    return kinds, percent, words[3], words[5], min_usd


def income_reply(args: List[str], user_id: Optional[str], *, seat: str = "owner") -> str:
    """The ``/wallet income …`` reply. Never raises."""
    from core.surfaces.go_words import is_go_word
    from core.wallet import income_rule as R
    from core.wallet.token_trust import owner_seat_ctx
    if not user_id:
        return "Only the owner sets the income rule."
    words = [str(a) for a in (args or []) if str(a).strip()]
    go = bool(words) and is_go_word(words[-1])
    if go:
        words = words[:-1]
    if not words:
        return _status(user_id)
    typed = " ".join(words)
    head = words[0].lower()
    try:
        ctx = owner_seat_ctx(user_id, seat)
        if head == "end" and len(words) == 1:
            if not go:
                return ("This ends the income rule. The debt stays, shown, and a new rule "
                        f"pays it.\n\nAdd `go`: /wallet income {typed} go")
            old = R.end_rule(ctx)
            return "✅ The income rule ended." if old else "There is no rule to end."
        if head == "settle" and len(words) == 3 and words[2].lower() in ("landed", "lost"):
            landed = words[2].lower() == "landed"
            if not go:
                return (f"This says buy {words[1]} "
                        + ("landed: its income is paid." if landed
                           else "did not land: its income is owed again and a new buy pays it.")
                        + f"\n\nAdd `go`: /wallet income {typed} go")
            buy = R.settle_unknown(ctx, words[1], landed=landed)
            return f"✅ {R.buy_line(buy)}"
        kinds, percent, token, chain, min_usd = _parse_set(words)
        target = R.check_target(chain, token)
        if not go:
            preview = R.Rule(id="(new)", created_at=0, kinds=kinds, percent=percent,
                             chain=target["chain"], token=target["address"],
                             symbol=target["symbol"], min_usd=min_usd)
            return (f"The income rule: {preview.line()}.\n"
                    "Each income adds its share to a debt; one buy pays the debt through the "
                    "normal money path (your limits, the pause and the lanes apply; a buy above "
                    "the no-ask limit comes to you as a decision). A rule already set ends; its "
                    "debt stays and this rule pays it. Income before now never counts.\n\n"
                    f"Add `go`: /wallet income {typed} go")
        realized = None
        if "trading" in kinds:
            from tools.income_rule.sources import realized_totals
            try:
                realized = {k: v["usd"] for k, v in realized_totals(user_id).items()}
            except Exception:  # noqa: BLE001 — set_rule names the refusal
                realized = None
        rule = R.set_rule(ctx, kinds=kinds, percent=percent, chain=chain, token=token,
                          min_usd=min_usd, realized=realized)
        return f"✅ Income rule {rule.id}: {rule.line()}."
    except R.RuleError as exc:
        return f"❌ {exc}"
    except Exception as exc:  # noqa: BLE001
        return f"❌ The income rule was not changed: {exc}"


__all__ = ["USAGE", "income_reply"]
