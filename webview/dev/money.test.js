// 043 A34/A35 §3.4 — Money › Book renders a book a person can trust.
//
// The book leads because on 2026-08-25 the agent published "book flat" while
// holding three positions and no seat could have shown the owner otherwise.
// What this tab must get right is small and specific, and it is the same set of
// honest-state rules the money page turns on:
//
//   * the verdict is a TYPED value from the reader (clean / disagreement /
//     unverified / no_ledger), rendered as its value — never a regex over the
//     verb's prose, and a disagreement or an unverified verdict NEVER reads as
//     green agreement;
//   * a value no store carries reads `—` WITH its reason, never `$0.00` and
//     never a blank — a confident zero on the money page is the worst failure
//     the screen has;
//   * an unreadable chain is a DASHED group with its reason, and the positions
//     total then names what it left out — a failed read is unverified, never
//     zero, never folded silently into the sum;
//   * no wallet is the empty state, not an empty table and three zeroes.
//
// It lives beside worklog.test.js because the vitest rig's root IS webview/dev/.
import { describe, it, expect, vi } from "vitest";
import {
  bridgeEntry,
  chainUnreadable,
  chainsSection,
  copyFrom,
  creationRow,
  emptyState,
  excludedChains,
  fmtAmount,
  fmtUsd,
  formatVars,
  inflightSection,
  invoiceRow,
  invoiceState,
  isEmpty,
  machineIncomeBanner,
  madeSection,
  moveRow,
  positionsSection,
  recentMovesSection,
  relTime,
  render,
  renderCash,
  renderInvoices,
  renderLimits,
  renderMoves,
  runSection,
  settleInvoice,
  unreadableEntry,
  verdictBanner,
  walletSection,
} from "../static/app/money.js";

const COPY = {
  clean: "The ledger and the chains agree.",
  disagreement: "Rob's ledger and a chain disagree about what it holds.",
  unverified: "I could not verify the book on every chain.",
  no_ledger: "Rob has not written down a ledger to check against yet.",
  checked: "Checked {when}.",
  recheck: "Check again",
  holds_title: "What Rob holds",
  holds_count: "{count} positions",
  col_position: "Position",
  col_chain: "Chain",
  col_amount: "Amount",
  col_worth: "Worth now",
  col_since: "Since entry",
  entry_at: "bought at {price}",
  no_positions: "Rob has not written down a position on any chain it can read.",
  total_label: "Positions, at today's prices",
  total_excludes: "not counted here: {chains}",
  chains_title: "The chains Rob checked",
  chains_age: "last read {when}",
  chain_read: "Read in full.",
  chain_unread: "I could not read this chain.",
  chain_unread_why: "Why",
  run_title: "The trading run",
  run_aside: "lives in Work",
  run_body: "Everything Rob trades lands under Moves.",
  run_watch: "Watch it in Work",
  empty_title: "Rob has no wallet yet.",
  empty_body: "Give Rob a wallet and it can show you the book.",
  empty_why: "Why",
  loading: "Reading the book.",
  unreadable: "I could not read the book.",
  unreadable_why: "Why it failed",
  when_now: "just now",
  when_min: "{count} min ago",
  when_hour: "{count} h ago",
  when_day: "{count} d ago",
};

const DASH = "—";

function priced(over = {}) {
  return {
    symbol: "NVDAX", address: "0xabc", chain: "robinhood", amount: 18.5,
    worth_now: 141.2, worth_now_reason: null,
    entry: null, entry_reason: "no entry recorded",
    since_entry: null, since_entry_reason: "no entry recorded",
    state: "matched", ...over,
  };
}

describe("the verdict leads and is a typed value, never a regex over prose", () => {
  for (const v of ["clean", "disagreement", "unverified", "no_ledger"]) {
    it(`renders ${v} as its own value with its own sentence`, () => {
      const node = verdictBanner({ verdict: v, checked_at: 0 }, COPY);
      expect(node.dataset.verdict).toBe(v);
      expect(node.textContent).toContain(COPY[v]);
    });
  }

  it("a clean book is the only green branch", () => {
    const node = verdictBanner({ verdict: "clean" }, COPY);
    expect(node.style.borderLeft).toContain("--running");
  });

  it("disagreement never renders as agreement (green)", () => {
    const node = verdictBanner({ verdict: "disagreement" }, COPY);
    expect(node.classList.contains("is-stopped")).toBe(true);
    expect(node.classList.contains("is-running")).toBe(false);
    expect(node.style.borderLeft).not.toContain("--running");
  });

  it("unverified is dashed and never green", () => {
    const node = verdictBanner({ verdict: "unverified" }, COPY);
    expect(node.style.borderLeft).toContain("dashed");
    expect(node.style.borderLeft).not.toContain("--running");
  });

  it("a verdict the reader did not name renders itself, never a guess of clean", () => {
    const node = verdictBanner({ verdict: "surprise" }, COPY);
    expect(node.dataset.verdict).toBe("surprise");
    expect(node.style.borderLeft).not.toContain("--running");
  });

  it("carries the cache age in plain words", () => {
    const now = Date.now();
    const node = verdictBanner({ verdict: "clean", checked_at: now / 1000 - 240 },
                               COPY, { nowMs: now });
    expect(node.textContent).toContain("4 min ago");
  });
});

describe("positions — the ledger's rows, honest per cell", () => {
  it("sums only the priced rows into the total", () => {
    const data = { rows: [priced()], chains: {} };
    const section = positionsSection(data, COPY);
    const total = section.querySelector("tr[data-total]");
    expect(total.textContent).toContain("$141.20");
  });

  it("a null worth reads a dash with its reason, never $0.00", () => {
    const row = priced({ worth_now: null, worth_now_reason: "no confident price" });
    const section = positionsSection({ rows: [row], chains: {} }, COPY);
    const cell = section.querySelector('td[data-label="Worth now"]');
    expect(cell.textContent).toContain(DASH);
    expect(cell.textContent).toContain("no confident price");
    expect(cell.textContent).not.toContain("$0.00");
  });

  it("since entry with no datum reads a dash with its reason", () => {
    const section = positionsSection({ rows: [priced()], chains: {} }, COPY);
    const cell = section.querySelector('td[data-label="Since entry"]');
    expect(cell.textContent).toContain(DASH);
    expect(cell.textContent).toContain("no entry recorded");
  });

  it("shows the entry price in the why line when the store carries it", () => {
    const row = priced({ entry: 7.63, entry_reason: null });
    const section = positionsSection({ rows: [row], chains: {} }, COPY);
    expect(section.querySelector(".why").textContent).toBe("bought at $7.63");
  });

  it("with no positions it says so rather than drawing an empty table", () => {
    const section = positionsSection({ rows: [], chains: {} }, COPY);
    expect(section.querySelector("table")).toBeNull();
    expect(section.textContent).toContain(COPY.no_positions);
  });
});

describe("an unreadable chain is a dashed group; the total names what it excluded", () => {
  const data = {
    verdict: "unverified",
    checked_at: 0,
    rows: [
      priced(),
      priced({ symbol: "WETH", address: "0xdef", chain: "base",
               amount: 0.124, worth_now: null,
               worth_now_reason: "the Base node did not answer", state: "unknown" }),
    ],
    chains: {
      base: { verdict: "unverified", error: "the Base node did not answer" },
      robinhood: { verdict: "clean", error: null },
    },
  };

  it("names exactly the chains whose read failed", () => {
    expect(chainUnreadable(data.chains.base)).toBe(true);
    expect(chainUnreadable(data.chains.robinhood)).toBe(false);
    expect(excludedChains(data)).toEqual(["base"]);
  });

  it("draws the unreadable chain as a dashed group with its reason", () => {
    const section = chainsSection(data, COPY, 0);
    const base = section.querySelector('[data-chain="base"]');
    expect(base.classList.contains("is-unknown")).toBe(true);
    expect(base.textContent).toContain(COPY.chain_unread);
    expect(base.textContent).toContain("the Base node did not answer");
    const rob = section.querySelector('[data-chain="robinhood"]');
    expect(rob.classList.contains("is-unknown")).toBe(false);
    expect(rob.textContent).toContain(COPY.chain_read);
  });

  it("the total sums only the readable chain and names what it left out", () => {
    const total = positionsSection(data, COPY).querySelector("tr[data-total]");
    expect(total.textContent).toContain("$141.20"); // robinhood only
    expect(total.textContent).not.toContain("$0.00");
    expect(total.textContent).toContain("base"); // excluded, named
  });
});

describe("no wallet is the empty state", () => {
  it("nothing to read is empty, not an empty table of zeroes", () => {
    expect(isEmpty({ chains: {}, rows: [] })).toBe(true);
    expect(isEmpty({ chains: { base: {} }, rows: [] })).toBe(false);
    const root = document.createElement("div");
    const which = render(root, { chains: {}, rows: [] }, COPY);
    expect(which).toBe("empty");
    expect(root.querySelector(".state-title").textContent).toBe(COPY.empty_title);
  });

  it("the empty state carries the machine reason behind a disclosure", () => {
    const state = emptyState({ error: "on-chain sight is off" }, COPY);
    expect(state.textContent).toContain("on-chain sight is off");
  });

  it("a reading renders the book, verdict first", () => {
    const root = document.createElement("div");
    const which = render(root,
      { verdict: "clean", checked_at: 0, rows: [priced()],
        chains: { robinhood: { verdict: "clean" } } }, COPY);
    expect(which).toBe("book");
    const first = root.firstElementChild;
    expect(first.classList.contains("banner")).toBe(true);
    expect(first.dataset.verdict).toBe("clean");
  });

  it("a failed fetch is a dashed state with its reason, never an empty book", () => {
    const root = document.createElement("div");
    const which = render(root, { unreadable: "503" }, COPY);
    expect(which).toBe("unreadable");
    expect(root.textContent).toContain(COPY.unreadable);
    expect(root.textContent).toContain("503");
  });
});

describe("fmtUsd writes a sub-cent value in full, never $0.00", () => {
  it("keeps a worthless price legible", () => {
    const s = fmtUsd(0.0000004);
    expect(s).not.toBe("$0.00");
    expect(s).toContain("0.0000004");
  });
  it("formats a normal figure to two places with separators", () => {
    expect(fmtUsd(612.4)).toBe("$612.40");
    expect(fmtUsd(1234.5)).toBe("$1,234.50");
  });
  it("carries a sign and dashes an unknown", () => {
    expect(fmtUsd(-5)).toBe("-$5.00");
    expect(fmtUsd(null)).toBeNull();
    expect(fmtUsd(undefined)).toBeNull();
  });
});

describe("small helpers", () => {
  it("fmtAmount separates thousands and dashes an unknown", () => {
    expect(fmtAmount(4210000)).toBe("4,210,000");
    expect(fmtAmount(null)).toBeNull();
  });
  it("relTime reads the reader's own timestamp into plain words", () => {
    const now = Date.now();
    expect(relTime(now / 1000 - 30, COPY, now)).toBe("just now");
    expect(relTime(now / 1000 - 7200, COPY, now)).toBe("2 h ago");
    expect(relTime(NaN, COPY, now)).toBe("");
  });
  it("copyFrom is a plain object of the dataset", () => {
    const node = document.createElement("div");
    node.dataset.clean = "ok";
    expect(copyFrom(node).clean).toBe("ok");
  });
  it("the trading run is navigation, pointing back to Work", () => {
    const section = runSection(COPY);
    expect(section.querySelector("a.btn").getAttribute("href")).toBe("/work");
  });
  it("an unreadable entry names why behind a disclosure", () => {
    const node = unreadableEntry("boom", COPY);
    expect(node.textContent).toContain(COPY.unreadable);
    expect(node.textContent).toContain("boom");
  });
});

// --- 043 N2: Moves / Cash / Invoices / Limits ------------------------------- #
//
// The other four tabs keep the SAME honest-state rules the Book turns on:
//   * Cash NEVER sums the two ledgers — the "no single number" line sits
//     exactly where a naive design would put a total, and an unavailable block
//     reads `—` with its reason, never a fabricated $0.00;
//   * a bridge in flight reads "in flight, never failed" — a re-sent bridge
//     pays twice;
//   * an invoice settle posts through http.js's same-origin POST, to the
//     existing owner-attestation route, and the row says back exactly what the
//     server answered;
//   * Limits shows today's used/limit from the ledger caps and links to Agent.

const MV = {
  mv_inflight_title: "In flight", mv_inflight_aside: "{count} still moving",
  mv_inflight_empty: "Nothing is bridging right now.",
  mv_inflight_unreadable: "I could not read what is in flight.",
  mv_inflight_unreadable_why: "Why it failed",
  mv_bridge_route: "{origin} to {dest}",
  mv_bridge_line: "{amount}, sent {age} ago and not landed yet.",
  mv_bridge_safe: "Rob will not send it again — a bridge sent twice pays twice — and it is 'in flight', never 'failed'.",
  mv_onchain: "See it on-chain",
  mv_recent_title: "Recent moves", mv_recent_empty: "Rob has not moved anything yet.",
  mv_recent_unreadable: "I could not read the recent moves.",
  mv_recent_unreadable_why: "Why it failed",
  mv_recent_partial: "{count} row(s) could not be read.",
  mv_recent_note: "A quote Rob asked for but did not send is not a move.",
  mv_col_when: "When", mv_col_what: "What", mv_col_chain: "Chain", mv_col_amount: "Amount",
  mv_no_amount: "a permission, not a spend", mv_amount_unknown: "not recorded",
  mv_made_title: "What Rob has made", mv_made_aside: "{count} on-chain",
  mv_made_empty: "Rob has not deployed or launched anything.",
  mv_made_unreadable: "I could not read what Rob has made.",
  mv_made_unreadable_why: "Why it failed",
  mv_made_partial: "{count} row(s) could not be read.",
  mv_col_token: "What", mv_col_address: "Address", mv_col_cost: "Cost",
  when_now: "just now", when_min: "{count} min ago", when_hour: "{count} h ago",
  when_day: "{count} d ago",
};

describe("Moves › in flight — a bridge past its deadline is in flight, never failed", () => {
  const now = Date.now();
  const bridges = {
    bridges: [{ id: "r-9f2c41", state: "in_flight", origin_chain: "solana",
                dest_chain: "robinhood", amount_usd: 61.1,
                created_at: now / 1000 - 180, origin_link: "https://x/tx/abc" }],
    unreadable: null,
  };

  it("reads 'in flight, never failed' and links the ORIGIN tx", () => {
    const entry = bridgeEntry(bridges.bridges[0], MV, now);
    expect(entry.dataset.state).toBe("in_flight");
    expect(entry.textContent).toContain("solana to robinhood");
    expect(entry.textContent).toContain("$61.10");
    expect(entry.textContent).toContain("in flight");
    expect(entry.textContent).toContain("never");
    const link = entry.querySelector("a.btn");
    expect(link.getAttribute("href")).toBe("https://x/tx/abc");
  });

  it("an unreadable store is dashed with its reason, never a confident empty list", () => {
    const section = inflightSection({ bridges: null, unreadable: "db locked" }, MV, now);
    const dashed = section.querySelector(".entry.is-unknown");
    expect(dashed).not.toBeNull();
    expect(section.textContent).toContain("db locked");
  });

  it("an empty board is a plain notice", () => {
    const section = inflightSection({ bridges: [], unreadable: null }, MV, now);
    expect(section.querySelector(".entry.is-unknown")).toBeNull();
    expect(section.textContent).toContain(MV.mv_inflight_empty);
  });
});

describe("Moves › recent moves — honest per cell, never a silent drop", () => {
  const now = Date.now();
  it("a $0 allowance reads a permission, not $0.00", () => {
    const tr = moveRow({ action: "approve", amount_usd: 0, chain: "base", ts: now / 1000 - 60 }, MV, now);
    const amt = tr.querySelector('td[data-label="Amount"]');
    expect(amt.textContent).toContain("—");
    expect(amt.textContent).toContain("permission");
    expect(amt.textContent).not.toContain("$0.00");
  });
  it("a priced move shows the amount and an on-chain link when the url is known", () => {
    const tr = moveRow({ action: "swap", amount_usd: 20, chain: "robinhood",
                         url: "https://x/tx/1", ts: now / 1000 - 60 }, MV, now);
    expect(tr.querySelector('td[data-label="Amount"]').textContent).toContain("$20.00");
    expect(tr.querySelector("a.btn").getAttribute("href")).toBe("https://x/tx/1");
  });
  it("an unavailable store is dashed, an unreadable row is counted and named", () => {
    expect(recentMovesSection({ moves: null, unavailable: "boom" }, MV, now)
      .querySelector(".entry.is-unknown")).not.toBeNull();
    const section = recentMovesSection(
      { moves: [{ action: "swap", amount_usd: 5, ts: now / 1000 }], unreadable_rows: 2 }, MV, now);
    expect(section.textContent).toContain("2 row(s) could not be read");
  });
});

describe("Moves › what Rob has made — unavailable is not 'nothing made'", () => {
  const now = Date.now();
  it("renders a creation with its address and a link", () => {
    const tr = creationRow({ action: "deploy_token", chain: "base",
                             address: "0x7c1a", usd: 3.2, url: "https://x/token/1",
                             ts: now / 1000 - 60 }, MV, now);
    expect(tr.textContent).toContain("deploy_token");
    expect(tr.textContent).toContain("0x7c1a");
    expect(tr.textContent).toContain("$3.20");
    expect(tr.querySelector("a.btn").getAttribute("href")).toBe("https://x/token/1");
  });
  it("creations === null is dashed with its reason (not an empty 'made nothing')", () => {
    const section = madeSection({ creations: null, reason: "telemetry unreadable" }, MV, now);
    expect(section.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(section.textContent).toContain("telemetry unreadable");
  });
  it("no creations is the plain 'nothing deployed' notice", () => {
    const section = madeSection({ creations: [] }, MV, now);
    expect(section.querySelector(".entry.is-unknown")).toBeNull();
    expect(section.textContent).toContain(MV.mv_made_empty);
  });
});

describe("renderMoves draws all three tiers, each failing on its own", () => {
  it("one failing reader dashes its tier and never blanks the others", () => {
    const root = document.createElement("div");
    const which = renderMoves(root,
      { bridges: [], unreadable: null },
      { moves: null, unavailable: "moves db down" },
      { creations: [{ action: "deploy_token", chain: "base", address: "0x1" }] },
      MV, { nowMs: Date.now() });
    expect(which).toBe("moves");
    // moves tier dashed, made tier still rendered
    expect(root.textContent).toContain("moves db down");
    expect(root.textContent).toContain("deploy_token");
  });
});

const CASH = {
  cash_income_what: "Income", cash_spent_what: "Spent",
  cash_whose: "Rob's own money, last {days} days",
  cash_paid_invoices: "Paid invoices", cash_waiting: "Waiting to be paid",
  cash_balance_now: "USDC in the treasury now", cash_net: "Left after income and spend",
  cash_no_sum: "there is no single number that combines them",
  cash_runtime_title: "What it costs you to run Rob", cash_runtime_aside: "your money, not Rob's",
  cash_runtime_window: "Last {days} days", cash_runtime_total: "Since the start",
  cash_runtime_calls: "over {count} model calls",
  cash_provider_left: "Left with your provider",
  cash_provider_why: "a balance is a network read; an unknown reads a dash",
  cash_unreadable: "I could not read the cash flow.", cash_unreadable_why: "Why it failed",
  cash_dash_why: "not trustworthy right now",
  wallet_title: "Wallet", wallet_ready: "Wallet ready",
  wallet_public_only: "Public identity only", wallet_disabled: "Wallet disabled",
  wallet_unavailable: "Wallet unavailable", wallet_unavailable_why: "Why",
  wallet_network: "Configured network: {network}",
  wallet_signing_ready: "Signing is guarded; this page cannot send.",
  wallet_signing_unavailable: "Signing is unavailable here.",
  wallet_accounts: "Accounts", wallet_balances: "Last known balances",
  wallet_balances_cached: "cached reading", wallet_balances_stale: "stale reading",
  wallet_balances_unread: "Unknown is not zero.",
  wallet_col_role: "Role", wallet_col_network: "Network",
  wallet_col_address: "Address", wallet_col_use: "Use", wallet_col_chain: "Chain",
  wallet_col_native: "Native", wallet_col_usdc: "USDC",
  wallet_receive: "Can receive", wallet_do_not_fund: "do not fund",
  wallet_balance_unknown: "not in snapshot", wallet_unaccounted: "Spending is blocked",
  wallet_unaccounted_body: "Resolve the submitted transaction first.",
};

function ledger(over = {}) {
  return {
    window_days: 7, settled_payments: 3, note: null,
    treasury: { income_usd: 128.4, spend_usd: 61.12, pending_usd: 45, pending_count: 1,
                balance_usd: 141.2, net_usd: 67.28, available: true },
    runtime: { spend_window_usd: 4.86, spend_total_usd: 212.4, calls_window: 1204,
               calls_total: 61880, provider_balance_usd: null, available: true },
    caps: {}, ...over,
  };
}

describe("Cash — the two ledgers, NEVER summed", () => {
  it("renders both books and the no-sum line, and combines no figures", () => {
    const root = document.createElement("div");
    const which = renderCash(root, ledger(), CASH);
    expect(which).toBe("cash");
    const figures = [...root.querySelectorAll(".book-figure")].map((n) => n.textContent);
    expect(figures).toContain("$128.40");
    expect(figures).toContain("$61.12");
    expect(root.querySelector(".no-sum").textContent).toContain("no single number");
    // The forbidden total (income + spend, or treasury + runtime) never appears.
    expect(root.textContent).not.toContain("$189.52"); // 128.40 + 61.12
    expect(root.textContent).not.toContain("$194.38"); // 128.40 + 4.86 + 61.12 …
  });

  it("an unavailable treasury reads — with its reason, never $0.00", () => {
    const root = document.createElement("div");
    renderCash(root, ledger({
      note: "wallet metering is off",
      treasury: { income_usd: 0, spend_usd: 0, pending_usd: 0, pending_count: 0,
                  balance_usd: null, net_usd: 0, available: false },
    }), CASH);
    const income = root.querySelector(".book.is-earned .book-figure");
    expect(income.textContent).toContain("—");
    expect(income.textContent).toContain("wallet metering is off");
    expect(income.textContent).not.toContain("$0.00");
  });

  it("runtime has no net and a null provider balance reads — with its reason", () => {
    const root = document.createElement("div");
    renderCash(root, ledger(), CASH);
    const section = root.querySelector(".section");
    expect(section.textContent).toContain("over 1,204 model calls");
    expect(section.textContent).toContain(CASH.cash_provider_why);
    expect(section.textContent).not.toContain("$0.00");
  });

  it("a failed ledger read is dashed, never zeros", () => {
    const root = document.createElement("div");
    expect(renderCash(root, { error: "503" }, CASH)).toBe("unreadable");
    expect(root.textContent).toContain("503");
  });
});

describe("Cash — the owner wallet is wired as a read-only identity", () => {
  const wallet = {
    state: "ready", signing_available: true, network: "mainnet",
    accounts: [
      { role: "operational", family: "evm", address: "0xabc", receive: true },
      { role: "hyperliquid", family: "evm", address: "0xdef", receive: false },
    ],
    balances: { state: "cached", chains: [
      { chain: "base", native: 0.2, symbol: "ETH", usdc: 41.5 },
    ] },
    unaccounted_submissions: [{ chain: "base", tx_hash: "0xpending" }],
    errors: [],
  };

  it("shows addresses, receive safety and cached balances without a send control", () => {
    const section = walletSection(wallet, CASH);
    expect(section.textContent).toContain("0xabc");
    expect(section.textContent).toContain("Can receive");
    expect(section.textContent).toContain("do not fund");
    expect(section.textContent).toContain("0.2 ETH");
    expect(section.textContent).toContain("$41.50");
    expect(section.querySelector("button")).toBeNull();
  });

  it("makes an unaccounted broadcast a visible spending block", () => {
    const section = walletSection(wallet, CASH);
    expect(section.querySelector(".is-needs-you").textContent).toContain("Spending is blocked");
    expect(section.textContent).toContain("0xpending");
  });

  it("does not turn an unread balance into zero", () => {
    const section = walletSection({ ...wallet,
      balances: { state: "unread", chains: [] }, unaccounted_submissions: [] }, CASH);
    expect(section.textContent).toContain("Unknown is not zero");
    expect(section.textContent).not.toContain("$0.00");
  });

  it("keeps the wallet visible when the cash-flow ledger is unavailable", () => {
    const root = document.createElement("div");
    expect(renderCash(root, { error: "ledger down" }, CASH, { wallet })).toBe("unreadable");
    expect(root.textContent).toContain("0xabc");
    expect(root.textContent).toContain("ledger down");
  });
});

const INV = {
  inv_title: "Who owes Rob money", inv_aside: "{amount} outstanding",
  inv_empty: "No one owes Rob money yet.",
  inv_unreadable: "I could not read the invoices.", inv_unreadable_why: "Why it failed",
  inv_col_who: "Who", inv_col_for: "For", inv_col_amount: "Amount", inv_col_state: "State",
  inv_state_pending: "unpaid", inv_state_completed: "paid", inv_state_expired: "expired",
  inv_late: "{count} days late", inv_settle: "Mark paid",
  inv_settle_done: "marked paid", inv_settle_failed: "The console could not reach the server.",
  inv_note: "Rob watches the treasury address.",
  inv_machine_title: "Machine payments are not counted here yet.",
  inv_machine_body: "so this page can undercount what Rob earned.",
};

describe("Invoices — typed state, the A20 note, and an owner settle", () => {
  const now = Date.now();
  it("a completed invoice is the only 'paid' branch", () => {
    expect(invoiceState({ status: "completed" }, INV, now).cls).toBe("is-running");
    expect(invoiceState({ status: "completed" }, INV, now).word).toBe("paid");
  });
  it("a pending invoice past its deadline reads N days late", () => {
    const st = invoiceState({ status: "pending", deadline: now / 1000 - 6 * 86400 }, INV, now);
    expect(st.word).toContain("6 days late");
    expect(st.cls).toBe("is-needs-you");
  });
  it("a pending row offers Mark paid; a completed one does not; read-only shows none", () => {
    expect(invoiceRow({ request_id: "a", status: "pending" }, INV, now, false)
      .querySelector("button[data-settle]")).not.toBeNull();
    expect(invoiceRow({ request_id: "b", status: "completed" }, INV, now, false)
      .querySelector("button[data-settle]")).toBeNull();
    expect(invoiceRow({ request_id: "c", status: "pending" }, INV, now, true)
      .querySelector("button[data-settle]")).toBeNull();
  });
  it("the machine-income banner is always present and named", () => {
    const root = document.createElement("div");
    renderInvoices(root, { invoices: [] }, INV, { nowMs: now });
    expect(machineIncomeBanner(INV).textContent).toContain("Machine payments");
    expect(root.textContent).toContain("Machine payments are not counted here yet");
  });
  it("a failed read is dashed and still carries the machine note", () => {
    const root = document.createElement("div");
    expect(renderInvoices(root, { error: "500" }, INV, { nowMs: now })).toBe("unreadable");
    expect(root.textContent).toContain("500");
    expect(root.textContent).toContain("Machine payments");
  });
  it("settle POSTs through http.js to the owner-attestation route", async () => {
    const fetcher = vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({ ok: true, message: "settled r1" }),
    }));
    const res = await settleInvoice("r1", INV, { fetcher });
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url, init] = fetcher.mock.calls[0];
    expect(url).toBe("/api/webgate/invoices/r1/settle");
    expect(init.method).toBe("POST");
    expect(res.ok).toBe(true);
    expect(res.message).toBe("settled r1");
  });
  it("a refused settle carries the server's message and does not claim success", async () => {
    const fetcher = vi.fn(async () => ({
      ok: false, status: 409, json: async () => ({ ok: false, message: "already settled" }),
    }));
    const res = await settleInvoice("r1", INV, { fetcher });
    expect(res.ok).toBe(false);
    expect(res.message).toBe("already settled");
  });
});

const LIM = {
  lim_title: "Today's spend limit", lim_used: "{used} of {cap} used today",
  lim_left: "{left} left", lim_no_cap: "No daily cap is set — aggregate spend is unbounded.",
  lim_unknown: "The daily cap could not be read.", lim_unknown_why: "Why",
  lim_body: "The limits Rob spends within live in Agent.", lim_link: "Open Agent",
};

describe("Limits — today's used/limit from the ledger, and a link to Agent", () => {
  it("shows used / cap and the remaining line", () => {
    const root = document.createElement("div");
    const which = renderLimits(root, ledger({
      caps: { daily_cap_usd: 100, daily_used_usd: 20, daily_left_usd: 80 } }), LIM);
    expect(which).toBe("limits");
    expect(root.textContent).toContain("$20.00 of $100.00 used today");
    expect(root.textContent).toContain("$80.00 left");
    expect(root.querySelector('a.btn[href="/agent"]')).not.toBeNull();
  });
  it("a disabled cap says so rather than showing a number", () => {
    const root = document.createElement("div");
    renderLimits(root, ledger({ caps: { wallet_daily_cap_state: "disabled" } }), LIM);
    expect(root.textContent).toContain("unbounded");
  });
  it("an unreadable ledger is dashed with its reason", () => {
    const root = document.createElement("div");
    expect(renderLimits(root, { error: "boom" }, LIM)).toBe("unreadable");
    expect(root.textContent).toContain("boom");
  });
});

describe("formatVars fills multiple tokens, values only", () => {
  it("replaces every named token and leaves unknown ones", () => {
    expect(formatVars("{a}/{b}", { a: 1, b: 2 })).toBe("1/2");
    expect(formatVars("{a}/{c}", { a: 1 })).toBe("1/{c}");
  });
});
