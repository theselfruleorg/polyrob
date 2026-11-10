// 113 W5 item 8 — the small fixes: the 1 s tick stops on a hidden tab (P-1),
// the trusted-token list is folded and the token form has visible labels
// (IA-4, A-4), and the no-wallet state names the one action (IA-4).
import { describe, it, expect, vi, afterEach } from "vitest";
import { visibleTicker } from "../static/app/transcript.js";
import { tokensSection } from "../static/app/money-tokens.js";
import { emptyState } from "../static/app/money.js";

afterEach(() => vi.useRealTimers());

function fakeDoc(hidden) {
  const listeners = {};
  return {
    hidden,
    addEventListener: (name, fn) => { listeners[name] = fn; },
    removeEventListener: (name) => { delete listeners[name]; },
    fire(name) { listeners[name] && listeners[name](); },
    listeners,
  };
}

describe("visibleTicker (P-1)", () => {
  it("ticks while visible, stops when hidden, catches up on return", () => {
    vi.useFakeTimers();
    const doc = fakeDoc(false);
    const fn = vi.fn();
    const stop = visibleTicker(fn, 1000, doc);
    vi.advanceTimersByTime(3000);
    expect(fn).toHaveBeenCalledTimes(3);
    doc.hidden = true; doc.fire("visibilitychange");
    vi.advanceTimersByTime(5000);
    expect(fn).toHaveBeenCalledTimes(3);
    doc.hidden = false; doc.fire("visibilitychange");
    expect(fn).toHaveBeenCalledTimes(4);          // at once on return
    vi.advanceTimersByTime(1000);
    expect(fn).toHaveBeenCalledTimes(5);
    stop();
    vi.advanceTimersByTime(5000);
    expect(fn).toHaveBeenCalledTimes(5);
    expect(doc.listeners.visibilitychange).toBeUndefined();
  });

  it("a page that opens hidden does not tick", () => {
    vi.useFakeTimers();
    const fn = vi.fn();
    visibleTicker(fn, 1000, fakeDoc(true));
    vi.advanceTimersByTime(3000);
    expect(fn).not.toHaveBeenCalled();
  });
});

const COPY = {
  tok_title: "Tokens", tok_show: "Show the {count} trusted tokens",
  tok_form_chain: "Chain", tok_form_address: "Contract address", tok_form_symbol: "Symbol",
  tok_trust: "Trust",
};
const ROWS = Array.from({ length: 15 }, (_, i) => (
  { symbol: `T${i}`, chain: "base", address: `0x${i}`, source: "canonical" }));

describe("Money › Book tokens (IA-4, A-4)", () => {
  it("folds the trusted list behind one summary", () => {
    const node = tokensSection({ trusted: ROWS }, COPY, false);
    const fold = node.querySelector("details.token-fold");
    expect(fold).not.toBeNull();
    expect(fold.open).toBe(false);
    expect(fold.querySelector("summary").textContent).toBe("Show the 15 trusted tokens");
    expect(fold.querySelectorAll("li").length).toBe(15);
  });

  it("draws no fold for an empty list", () => {
    expect(tokensSection({ trusted: [] }, COPY, false).querySelector("details")).toBeNull();
  });

  it("labels each form field with visible text, not a placeholder", () => {
    const form = tokensSection({ trusted: [] }, COPY, false).querySelector("form");
    const labels = [...form.querySelectorAll("label.token-field")];
    expect(labels.map((l) => l.firstChild.textContent)).toEqual(["Chain", "Contract address", "Symbol"]);
    labels.forEach((l) => {
      const input = l.querySelector("input");
      expect(input).not.toBeNull();
      expect(input.getAttribute("placeholder")).toBeNull();
    });
  });

  it("the no-wallet state says what to do", () => {
    const node = emptyState(null, { empty_title: "No wallet", empty_body: "B", empty_how: "Run it" });
    const lines = [...node.querySelectorAll(".state-body")].map((p) => p.textContent);
    expect(lines).toEqual(["B", "Run it"]);
  });
});
