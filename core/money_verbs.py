"""The owner money verbs, contributed to ``core.verbs`` (067 P5a).

Moved out of ``core.verbs._CORE_TABLE`` unchanged (same words, same order) and
registered through :func:`core.verbs.register_verbs` — the call the wallet pack
makes after 067 P5b. No per-seat handler reference yet: every seat still runs
these verbs from its own built-in branch (Telegram ``harness._handle_owner_admin``
+ ``surfaces/telegram/*_ops.py``, the REPL ``cli/ui/commands/h_*.py``
registrations, the dispatcher's ``_COMMANDS`` literal). P5b moves those
branches into the pack and passes them here as ``handlers_by_seat``.

Imports nothing but ``core.verbs``.
"""
from core.verbs import Verb, register_verbs

MONEY_VERBS: tuple = (
    Verb("/book", "money",
         "My ledger against every money chain: one verdict, then what disagrees"),
    Verb("/wallet", "money", "Wallet addresses, network, and spend caps; set the no-ask ceiling or trust a token"),
    Verb("/invoices", "money", "What I have billed and who owes me"),
    Verb("/settle", "money", "Mark an invoice paid"),
    Verb("/trade", "money", "Start a run that carries the money verb"),
    Verb("/bridge", "money", "Move native value from one chain to another"),
    Verb("/launch", "money", "Launch a token on the launchpad"),
    Verb("/deploy", "money", "Deploy a fixed-supply token"),
    Verb("/lp", "money", "Liquidity positions: inspect, quote, add, remove, or collect fees"),
    Verb("/claim", "money", "Collect the creator fees a launchpad already owes me"),
    Verb("/nft", "money", "Collectibles I hold: look, send one, or revoke an approval"),
    Verb("/dapp", "money", "Web pages my wallet is connected to, and how to cut one off"),
    Verb("/paid", "money", "Paid room actions: status, pricing, and offers"),
    Verb("/pay", "money", "Pay for one x402 resource, up to a price you name"),
)

#: 044 I10: the two money verbs a group room may run — `/paid` configures THAT
#: room, `/book` is a read answered in the owner's DM (same as before 067 P5a:
#: neither was in the Telegram room-refusal set).
ROOM_VERBS = ("/paid", "/book")

register_verbs(MONEY_VERBS, source="core.money_verbs", room_verbs=ROOM_VERBS)

#: W1 (token management): a holding's lifecycle, owner seats only. The first
#: rows contributed WITH per-seat handlers (the 067 P5a convention): Telegram
#: runs them after its owner gate and refuses them in a room; the REPL
#: registers them through ``h_contributed``. Both call ``core.wallet.token_trust``.
HOLDINGS_VERBS: tuple = (
    Verb("/writeoff", "money",
         "Write a holding off: the loss is its recorded cost, and nothing is sold"),
    Verb("/unquarantine", "money",
         "Undo the quarantine of a look-alike holding, so it counts as open again"),
)

#: Owner-UX 2026-09-26: the most basic wallet action had no owner verb on any
#: seat. Quote by default; `go` sends (the owner typed the address — no second
#: tap). The per-tx and daily caps bind it and are shown first.
SEND_VERBS: tuple = (
    Verb("/send", "money",
         "Send native value or a token to an address: a quote, then `go`"),
)

#: 2026-09-27: the most common money action had no verb on any seat — only
#: agent prose through /trade. Same quote → card → confirm shape as /send.
SWAP_VERBS: tuple = (
    Verb("/swap", "money",
         "Swap one token for another: a quote with the minimum you receive, then confirm"),
)

register_verbs(
    SWAP_VERBS,
    {"telegram": {"/swap": "surfaces.telegram.swap_ops:swap_verb"},
     "repl": {"/swap": "cli.ui.commands.h_swap:h_swap"}},
    source="core.money_verbs.swap")

register_verbs(
    SEND_VERBS,
    {"telegram": {"/send": "surfaces.telegram.send_ops:send_verb"},
     "repl": {"/send": "cli.ui.commands.h_send:h_send"}},
    source="core.money_verbs.send")

register_verbs(
    HOLDINGS_VERBS,
    {"telegram": {"/writeoff": "surfaces.telegram.holdings_ops:writeoff_verb",
                  "/unquarantine": "surfaces.telegram.holdings_ops:unquarantine_verb"},
     "repl": {"/writeoff": "cli.ui.commands.h_holdings:h_writeoff",
              "/unquarantine": "cli.ui.commands.h_holdings:h_unquarantine"}},
    source="core.money_verbs.holdings")

#: 071: "check this address" had no owner verb and no agent tool. Read-only:
#: a wallet's holdings or a token's report (any chain), or a ticker's
#: candidates. Owner seats only — in a room the agent answers through
#: ``defi_data.wallet_holdings`` itself.
CHECK_VERBS: tuple = (
    Verb("/check", "money",
         "Look at any address or ticker: a wallet's holdings or a token's report"),
)

register_verbs(
    CHECK_VERBS,
    {"telegram": {"/check": "surfaces.telegram.check_ops:check_verb"},
     "repl": {"/check": "cli.ui.commands.h_check:h_check"}},
    source="core.money_verbs.check")
