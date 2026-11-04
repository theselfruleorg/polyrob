"""Generalized registry of "the code cannot do X" claims made in shipped prompts.

Owner directive 2026-08-27 18:50 UTC (via the /dev rail): "we need deeper
evaluation and ensure prompts are dynamic where required by system and that
agent is system capability aware even if we extend functionality."

Background: `test_shipped_prompts_match_capability.py` caught one instance of
this bug class — two prompts asserted "Solana has no signer" as permanent fact,
months after the Solana rail shipped and made that false. The agent then told
its own owner "screen-only by design" verbatim, every run, because the prompt
it read said so. That was a one-off, hand-written regression test anchored to
one capability.

This file is the reusable form. A capability-denial claim in a shipped prompt
is exactly as failure-prone as denial logic in code, and unlike code it is
never exercised by normal test runs — nothing calls a SKILL.md. The only way
to catch a claim going stale is to re-check it against the thing it claims
about, on every test run, forever. Register the claim once here; when the
underlying capability changes, this test fails at the exact row that broke,
naming the file, the phrase, and why it's now wrong — instead of the claim
silently rotting until an owner notices the agent refusing something it can
do (as happened with Solana).

Adding a NEW capability-denial claim to a skill or prompt? Add a matching
CapabilityClaim row here in the same commit (see docs/SKILL_AUTHORING_STANDARD.md
"Capability claims must be re-checkable, never asserted as permanent fact").
"""
import dataclasses
import pathlib
from typing import Callable

ROOT = pathlib.Path(__file__).resolve().parents[4]


@dataclasses.dataclass(frozen=True)
class CapabilityClaim:
    id: str
    file: str  # repo-relative path to the prompt/skill making the claim
    phrase: str  # case-insensitive substring anchoring the claim in the file
    still_true: Callable[[], bool]  # must return True while the denial is accurate
    why: str  # what would have to ship for this to flip to False


def _no_nft_marketplace_integration() -> bool:
    """True while nothing can BUY or SELL an NFT.

    ⚠️ Re-scoped 2026-09-15. This predicate used to fire on any method whose
    name contained "nft" and on any `tools/**/nft*.py` file, so the moment the
    hold/see/send/revoke verbs shipped it reported the claim false — even though
    the claim it guards ("no rail yet" to a PURCHASE) was still perfectly true.
    A guard that fires on the wrong capability teaches people to edit the guard.

    The claim is about a MARKETPLACE: buying, selling, listing, bidding. Those
    are what `defi_trade` still cannot do, and they are what this now checks.
    Holding, transferring and revoking are shipped and are not evidence of it.
    """
    import tools.defi.trade_tool as trade_tool
    methods = [n.lower() for n in dir(trade_tool.DefiTradeTool)
               if not n.startswith("_")]
    trade_words = ("buy", "sell", "list", "bid", "offer", "sweep")
    if any("nft" in m and any(w in m for w in trade_words) for m in methods):
        return False
    tools_dir = ROOT / "tools"
    for pattern in ("**/marketplace*.py", "**/seaport*.py", "**/reservoir*.py",
                    "**/opensea*.py", "**/blur*.py"):
        if any(tools_dir.glob(pattern)):
            return False
    return True


# ---- 068 W2 procedure skills ----------------------------------------------

def _trade_tool_methods():
    import tools.defi.trade_tool as trade_tool
    return [n.lower() for n in dir(trade_tool.DefiTradeTool) if not n.startswith("_")]


def _no_native_order_types_on_defi_trade() -> bool:
    """True while defi_trade has no limit/stop/trigger/DCA/TWAP order verb."""
    words = ("limit_order", "stop", "trigger", "order", "dca", "twap", "take_profit")
    return not any(w in m for m in _trade_tool_methods() for w in words)


def _no_hyperliquid_trigger_order() -> bool:
    """True while the Hyperliquid service exposes no TP/SL trigger-order verb.

    Only PUBLIC methods are verbs: the private ``_trigger_orders_in_scope`` /
    ``_trigger_cancel_refusal`` helpers guard a cancel that would REMOVE an
    owner's stop (c2a5ff0a8); they place nothing."""
    import packs.markets.polyrob_markets.hyperliquid.service as hl
    names = [n.lower() for c in vars(hl).values() if isinstance(c, type)
             and hasattr(c, "place_limit_order") for n in dir(c) if not n.startswith("_")]
    return not any(w in n for n in names for w in ("trigger", "tpsl", "take_profit", "stop_loss"))


def _no_price_impact_field() -> bool:
    """True while neither swap_quote nor swap reports a price-impact figure."""
    for rel in ("tools/defi/data_tool.py", "tools/defi/trade_tool.py"):
        text = (ROOT / rel).read_text(encoding="utf-8").lower()
        if "price_impact" in text or "price impact" in text:
            return False
    return True


def _no_lending_verb() -> bool:
    """True while defi_trade has no lend/supply/borrow verb (call can reach a protocol)."""
    return not any(w in m for m in _trade_tool_methods()
                   for w in ("lend", "supply", "borrow", "aave", "morpho"))


def _x402_payer_is_base_exact_only() -> bool:
    """True while the payer has no Solana and no `upto` path."""
    text = (ROOT / "tools/x402/real_client.py").read_text(encoding="utf-8").lower()
    return "upto" not in text and "solana_x402" not in text


def _zero_cost_watch_is_defi_data_only() -> bool:
    """True while every $0 read_verb / precheck verb is a defi_data read (H4)."""
    from cron.read_job import _read_verbs
    return {v.split(".", 1)[0] for v in _read_verbs()} == {"defi_data"}


# ---- work-tool skills (skills review 2026-11-03, P4) ------------------------

def _browser_action_names():
    import ast
    tree = ast.parse((ROOT / "tools/browser/browser.py").read_text(encoding="utf-8"))
    return [n.name.lower() for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any("action" in ast.unparse(d) for d in n.decorator_list)]


def _no_site_login_store() -> bool:
    """True while no core browser verb saves or restores a site login and no
    credential store exists (storage_state lives only in the X pack)."""
    words = ("login", "credential", "storage_state", "cookie", "password")
    if any(w in a for a in _browser_action_names() for w in words):
        return False
    for base in ("tools", "core"):
        for pattern in ("**/*credential_store*.py", "**/*site_login*.py",
                        "**/*password_store*.py"):
            if any((ROOT / base).glob(pattern)):
                return False
    return True


def _no_browser_upload_verb() -> bool:
    return not any("upload" in a for a in _browser_action_names())


def _click_downloads_not_saved() -> bool:
    """True while nothing sets BrowserContextConfig.save_downloads_path."""
    import re
    pat = re.compile(r"save_downloads_path\s*=\s*(?!None)")
    for base in ("tools", "agents", "core", "polyrob", "packs"):
        for path in (ROOT / base).rglob("*.py"):
            if pat.search(path.read_text(encoding="utf-8", errors="ignore")):
                return False
    return True


def _no_core_web_search_tool() -> bool:
    from core.tool_capabilities import TOOL_CAPABILITIES
    return not any("search" in tid for tid in TOOL_CAPABILITIES)


def _no_stored_secret_reference() -> bool:
    """True while no entry point fills the registry's <secret> map
    (sensitive_data) and http_request adds no stored key."""
    roots = ["api", "cli", "surfaces", "polyrob", "cron", "webview",
             "agents/task/session", "agents/task/runtime"]
    files = [p for r in roots for p in (ROOT / r).rglob("*.py")]
    files += list((ROOT / "agents/task").glob("task_agent_*.py"))
    if any("sensitive_data" in p.read_text(encoding="utf-8", errors="ignore") for p in files):
        return False
    text = (ROOT / "tools/http_request/tool.py").read_text(encoding="utf-8")
    return "<secret>" not in text and "getenv" not in text and "os.environ" not in text


def _office_files_not_decoded_by_read_file() -> bool:
    from tools.filesystem_pdf import DOCUMENT_KINDS
    return not ({".xlsx", ".xls", ".doc", ".pptx"} & set(DOCUMENT_KINDS))


CLAIMS = (
    CapabilityClaim(
        id="nft-no-rail",
        file="data/prompts/skills/trade-execution/references/nfts.md",
        phrase="no rail yet",
        still_true=_no_nft_marketplace_integration,
        why="a MARKETPLACE verb (Seaport, Reservoir, or similar buy/sell/list) "
            "would need to ship on defi_trade or a new tool for this claim to "
            "become false. The 2026-09-15 hold/see/send/revoke verbs do NOT "
            "make it false -- they are not a purchase rail",
    ),
    # goal-create-excludes-money-tools RETIRED 2026-10-23 (086 W7): treasury-trading
    # no longer explains the goal grant; trading runs only under a mandate.
    CapabilityClaim(
        id="068-no-native-dex-orders",
        file="data/prompts/skills/exits/SKILL.md",
        phrase="no native stop, limit or trigger orders",
        still_true=_no_native_order_types_on_defi_trade,
        why="a limit/stop/trigger/DCA/TWAP order verb would need to ship on defi_trade",
    ),
    CapabilityClaim(
        id="068-no-native-dca-order",
        file="data/prompts/skills/dca/SKILL.md",
        phrase="there is no native dca or",
        still_true=_no_native_order_types_on_defi_trade,
        why="a recurring/DCA order verb would need to ship on defi_trade",
    ),
    CapabilityClaim(
        id="068-no-hl-trigger-order",
        file="packs/markets/polyrob_markets/skills/hyperliquid-trading/SKILL.md",
        phrase="there is no trigger (tp/sl) order verb here",
        still_true=_no_hyperliquid_trigger_order,
        why="a Hyperliquid trigger (TP/SL) order method would need to ship",
    ),
    CapabilityClaim(
        id="068-exits-no-hl-trigger-order",
        file="data/prompts/skills/exits/SKILL.md",
        phrase="but no trigger (tp/sl) order",
        still_true=_no_hyperliquid_trigger_order,
        why="a Hyperliquid trigger (TP/SL) order method would need to ship",
    ),
    CapabilityClaim(
        id="068-no-price-impact-field",
        file="data/prompts/skills/pre-trade-check/SKILL.md",
        phrase="there is no price-impact field",
        still_true=_no_price_impact_field,
        why="swap_quote or swap would need to report a price-impact figure",
    ),
    # 068-no-realized-pnl RETIRED 2026-10-02 (071 W3): the rail book records
    # realized P&L per token (core/open_positions.py position_realized) and
    # defi_data.positions reads it; position-journal no longer makes the claim.
    # 068-no-strategy-budget-or-breaker RETIRED 2026-10-23 (086 W7): the trading
    # mandate's allocation and latching breakers ship in core/money (086 W2).
    CapabilityClaim(
        id="068-no-lending-verb",
        file="data/prompts/skills/stable-cash/SKILL.md",
        phrase="has no dedicated verb yet",
        still_true=_no_lending_verb,
        why="a lending/supply verb would need to ship on defi_trade",
    ),
    CapabilityClaim(
        id="068-x402-base-exact-only",
        file="data/prompts/skills/x402-pay/SKILL.md",
        phrase="not supported yet: solana payments, the `upto` scheme",
        still_true=_x402_payer_is_base_exact_only,
        why="the x402 payer would need a Solana (solana_x402) or `upto` path",
    ),
    CapabilityClaim(
        id="standing-work-zero-cost-watch-defi-only",
        file="data/prompts/skills/standing-work/SKILL.md",
        phrase="a web page, a feed or any other source runs the model on every tick",
        still_true=_zero_cost_watch_is_defi_data_only,
        why="a non-defi_data read verb (e.g. a web_fetch watch) would need to join "
            "cron/read_job.py::_READ_JOB_TOOLS and the read_job policy rows",
    ),
    CapabilityClaim(
        id="p4-browser-no-site-login-store",
        file="data/prompts/skills/browser-automation/SKILL.md",
        phrase="there is no site-password store",
        still_true=_no_site_login_store,
        why="a core browser verb that saves/restores a site login, or a credential "
            "store, would need to ship",
    ),
    CapabilityClaim(
        id="p4-browser-no-upload-verb",
        file="data/prompts/skills/browser-automation/SKILL.md",
        phrase="there is no file-upload verb",
        still_true=_no_browser_upload_verb,
        why="a browser upload action would need to ship in tools/browser/browser.py",
    ),
    CapabilityClaim(
        id="p4-browser-click-download-not-saved",
        file="data/prompts/skills/browser-automation/SKILL.md",
        phrase="a download started by a click is not saved to the workspace",
        still_true=_click_downloads_not_saved,
        why="something would need to set BrowserContextConfig.save_downloads_path",
    ),
    CapabilityClaim(
        id="p4-core-no-browserless-search",
        file="data/prompts/skills/web-research/SKILL.md",
        phrase="browser-less web search",
        still_true=_no_core_web_search_tool,
        why="a core web-search tool would need to join core/tool_capabilities.py",
    ),
    CapabilityClaim(
        id="p4-api-no-stored-secret-reference",
        file="data/prompts/skills/api-integration/SKILL.md",
        phrase="a tool argument has no stored-secret reference",
        still_true=_no_stored_secret_reference,
        why="an entry point would need to fill sensitive_data (the <secret> map) or "
            "http_request would need a stored-key lookup",
    ),
    CapabilityClaim(
        id="p4-secret-handling-no-stored-secret-reference",
        file="data/prompts/skills/secret-handling/SKILL.md",
        phrase="a tool argument has no stored-secret reference",
        still_true=_no_stored_secret_reference,
        why="an entry point would need to fill sensitive_data (the <secret> map) or "
            "http_request would need a stored-key lookup",
    ),
    CapabilityClaim(
        id="p4-read-file-no-office-decode",
        file="data/prompts/skills/file-data-ops/SKILL.md",
        phrase="`filesystem_read_file` cannot decode them",
        still_true=_office_files_not_decoded_by_read_file,
        why="tools/filesystem_pdf.py::DOCUMENT_KINDS would need an office parser",
    ),
)


def test_every_registered_capability_claim_is_present_and_still_true():
    for claim in CLAIMS:
        path = ROOT / claim.file
        text = path.read_text(encoding="utf-8").lower()
        assert claim.phrase.lower() in text, (
            f"[{claim.id}] expected phrase {claim.phrase!r} not found in {claim.file} — "
            f"the claim was reworded or removed; update this registry entry to match "
            f"the new wording, or delete the row if the capability shipped and the "
            f"prompt was already updated")
        assert claim.still_true(), (
            f"[{claim.id}] {claim.file} still says {claim.phrase!r}, but this is now "
            f"FALSE: {claim.why}. The capability shipped — update the prompt to reflect "
            f"it (see the Solana incident this file exists to prevent), then remove or "
            f"rewrite this registry row.")
