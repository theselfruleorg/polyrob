// Rule 1 of the design system, checked rather than promised: ONE nav component.
// It rotates axis at 768 px and it never forks.
//
// This runs over the mockup set (the specification). The same invariant is
// checked over the RENDERED console by tests/unit/webview/test_shell_one_nav.py,
// so the picture and the product are held to one bar.
//
// The mockups are PRIVATE: docs/design/ is denylisted from the public export,
// while webview/dev/ ships. So on a public checkout this file SKIPS rather than
// failing on a missing directory. The product-side invariant still runs there,
// in the Python test above.
import { describe, it, expect } from "vitest";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const MOCKUPS = path.resolve(HERE, "..", "..", "docs", "design", "040", "web");

const LABELS = ["New", "Inbox", "Work", "Money", "Agent"];

const HAVE_MOCKUPS = existsSync(MOCKUPS);
const SCREENS = HAVE_MOCKUPS
  ? readdirSync(MOCKUPS)
      .filter((f) => f.endsWith(".html"))
      .sort()
  : [];

function navOf(file) {
  const html = readFileSync(path.join(MOCKUPS, file), "utf8");
  const doc = new DOMParser().parseFromString(html, "text/html");
  return doc.querySelectorAll('nav[aria-label="Main"]');
}

/** The nav with everything PER-SCREEN removed: the current marker and the
 *  Inbox count. What is left must be one shared component. */
function skeleton(nav) {
  const copy = nav.cloneNode(true);
  copy.querySelectorAll(".nav-badge").forEach((b) => b.remove());
  copy.querySelectorAll(".nav-item").forEach((a) => {
    a.removeAttribute("aria-current");
    a.removeAttribute("aria-label");
  });
  return copy.outerHTML;
}

if (!HAVE_MOCKUPS) {
  it.skip("the mockup set is absent from this tree (it is private)", () => {});
}

describe.runIf(HAVE_MOCKUPS)("the mockup set", () => {
  it("has screens to check", () => {
    expect(SCREENS.length).toBeGreaterThanOrEqual(20);
  });
});

describe.runIf(HAVE_MOCKUPS).each(SCREENS)("%s", (file) => {
  it("has exactly one main nav", () => {
    expect(navOf(file).length).toBe(1);
  });

  it("carries the five destinations in order", () => {
    const items = [...navOf(file)[0].querySelectorAll(".nav-item")];
    expect(items.map((a) => a.querySelector(".nav-label").textContent)).toEqual(
      LABELS,
    );
  });

  it("marks at most one destination as current", () => {
    const current = navOf(file)[0].querySelectorAll('[aria-current="page"]');
    expect(current.length).toBeLessThanOrEqual(1);
  });

  it("keeps the count out of the link text and in the label", () => {
    const inbox = [...navOf(file)[0].querySelectorAll(".nav-item")].find(
      (a) => a.querySelector(".nav-label").textContent === "Inbox",
    );
    const badge = inbox.querySelector(".nav-badge");
    if (badge) {
      expect(badge.getAttribute("aria-hidden")).toBe("true");
      expect(inbox.getAttribute("aria-label")).toBeTruthy();
    }
  });
});

it.runIf(HAVE_MOCKUPS)("draws the same nav on every screen", () => {
  const first = skeleton(navOf(SCREENS[0])[0]);
  for (const file of SCREENS) {
    expect(skeleton(navOf(file)[0]), `${file} forked the nav`).toBe(first);
  }
});

it.runIf(HAVE_MOCKUPS)("puts a skip link before the nav on every screen", () => {
  for (const file of SCREENS) {
    const html = readFileSync(path.join(MOCKUPS, file), "utf8");
    const doc = new DOMParser().parseFromString(html, "text/html");
    const skip = doc.querySelector("a.skip-link");
    expect(skip, `${file} has no skip link`).toBeTruthy();
    expect(skip.getAttribute("href")).toBe("#main");
    expect(doc.querySelectorAll("#main").length, file).toBe(1);
  }
});
