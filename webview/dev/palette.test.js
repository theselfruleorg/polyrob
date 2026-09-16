// 043 A23 — the command palette lists exactly the one verb table, groups it the
// way the terminal groups it, and owns up to a command it does not know.
//
// What it must get right is small and specific:
//
//   * typing `/inv` narrows to `/invoices` in the money group — and NOT to
//     `/settle`, whose help says "invoice". A palette that matched prose would
//     make its own suggestions unpredictable;
//   * an unknown `/xyz` shows the honest "I read it as a message" line rather
//     than a blank list — that is what the console does with an unknown slash
//     (webview/console_commands.py);
//   * it lists EXACTLY `dispatcher._COMMANDS` minus `/task` and `/new`. Here
//     that is asserted against the committed artifact (verbs.en.json); the
//     artifact-vs-source equality is a Python contract test
//     (tests/unit/webview/test_verbs_json.py).
//
// The verb DATA is read from the real generated artifact, so this suite also
// proves the artifact is loadable and shaped the way the palette expects.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it } from "vitest";
import {
  copyFrom,
  filterVerbs,
  insertVerb,
  isUnknownSlash,
  matchVerb,
  normalizeQuery,
  render,
  verbRow,
} from "../static/app/palette.js";

// Read the REAL committed artifact, so this suite also proves it loads and is
// shaped the way the palette expects. jsdom gives `import.meta.url` a non-file
// scheme, so resolve from the process CWD instead — the rig runs from
// `webview/dev`, but tolerate a repo-root run too.
function loadVerbs() {
  const candidates = [
    resolve(process.cwd(), "../static/app/verbs.en.json"),
    resolve(process.cwd(), "webview/static/app/verbs.en.json"),
  ];
  for (const path of candidates) {
    try {
      return JSON.parse(readFileSync(path, "utf8"));
    } catch (err) {
      /* try the next candidate */
    }
  }
  throw new Error(`verbs.en.json not found from ${process.cwd()}`);
}

const VERBS = loadVerbs();

const COPY = {
  title: "Commands",
  placeholder: "Type a command, or just ask",
  narrow: "keep typing to narrow it",
  unknown: "That is not one of my commands. Press enter and I read it as a message.",
  empty: "Nothing here matches that.",
  close: "Close",
};

function results() {
  document.body.innerHTML = '<div id="palette-results" class="ledger"></div>';
  return document.getElementById("palette-results");
}

describe("the artifact is what the palette expects", () => {
  it("carries a group order and a flat verb list", () => {
    expect(Array.isArray(VERBS.group_order)).toBe(true);
    expect(Array.isArray(VERBS.verbs)).toBe(true);
    expect(VERBS.verbs.length).toBeGreaterThan(10);
    VERBS.verbs.forEach((v) => {
      expect(typeof v.name).toBe("string");
      expect(v.name.startsWith("/")).toBe(true);
      expect(VERBS.group_order).toContain(v.group);
      expect(v.help.length).toBeGreaterThan(0);
    });
  });

  it("excludes the two session-creating verbs the console has its own UI for", () => {
    const names = VERBS.verbs.map((v) => v.name);
    expect(names).not.toContain("/task");
    expect(names).not.toContain("/new");
  });
});

describe("normalizeQuery", () => {
  it("drops one leading slash and lower-cases", () => {
    expect(normalizeQuery("/INV")).toBe("inv");
    expect(normalizeQuery("inv")).toBe("inv");
    expect(normalizeQuery(" /inv ")).toBe("inv");
  });

  it("turns a bare slash into the empty query, which matches everything", () => {
    expect(normalizeQuery("/")).toBe("");
    expect(normalizeQuery("")).toBe("");
  });
});

describe("matching is on the name, never the help", () => {
  const invoices = VERBS.verbs.find((v) => v.name === "/invoices");
  const settle = VERBS.verbs.find((v) => v.name === "/settle");

  it("finds a verb by a prefix of its name", () => {
    expect(matchVerb(invoices, "inv")).toBe(true);
  });

  it("does NOT match a verb because its help mentions the word", () => {
    // /settle's help is "Mark an invoice paid" — a help-substring match would
    // pull it in on "inv", which is exactly the bug this guards.
    expect(settle.help.toLowerCase()).toContain("invoice");
    expect(matchVerb(settle, "inv")).toBe(false);
  });

  it("an empty query matches every verb", () => {
    VERBS.verbs.forEach((v) => expect(matchVerb(v, "")).toBe(true));
  });
});

describe("filterVerbs", () => {
  it("narrows `/inv` to `/invoices` in the money group, and nothing else", () => {
    const groups = filterVerbs(VERBS, "/inv");
    expect(groups.length).toBe(1);
    expect(groups[0].group).toBe("money");
    expect(groups[0].verbs.map((v) => v.name)).toEqual(["/invoices"]);
  });

  it("returns every group in group_order for the empty query", () => {
    const groups = filterVerbs(VERBS, "");
    const rendered = groups.map((g) => g.group);
    // Only groups that actually have verbs appear, and they keep source order.
    const expected = VERBS.group_order.filter((g) =>
      VERBS.verbs.some((v) => v.group === g));
    expect(rendered).toEqual(expected);
    const total = groups.reduce((n, g) => n + g.verbs.length, 0);
    expect(total).toBe(VERBS.verbs.length);
  });

  it("returns nothing for a slash that names no verb", () => {
    expect(filterVerbs(VERBS, "/xyz")).toEqual([]);
  });
});

describe("isUnknownSlash", () => {
  it("is true only for an explicit slash token that names nothing", () => {
    expect(isUnknownSlash(VERBS, "/xyz")).toBe(true);
  });

  it("is false for a known verb, a bare slash, and a plain word", () => {
    expect(isUnknownSlash(VERBS, "/inv")).toBe(false);
    expect(isUnknownSlash(VERBS, "/")).toBe(false);
    expect(isUnknownSlash(VERBS, "hello")).toBe(false);
    expect(isUnknownSlash(VERBS, "")).toBe(false);
  });
});

describe("render", () => {
  beforeEach(() => results());

  it("renders one button per verb for the empty query", () => {
    const container = results();
    expect(render(container, VERBS, "", COPY)).toBe("rows");
    const buttons = container.querySelectorAll("button.entry");
    expect(buttons.length).toBe(VERBS.verbs.length);
    const names = [...buttons].map((b) => b.dataset.verb);
    expect(names).not.toContain("/task");
    expect(names).not.toContain("/new");
  });

  it("renders section headers in group order", () => {
    const container = results();
    render(container, VERBS, "", COPY);
    const heads = [...container.querySelectorAll(".section-title")].map((h) =>
      h.textContent);
    const expected = VERBS.group_order.filter((g) =>
      VERBS.verbs.some((v) => v.group === g));
    expect(heads).toEqual(expected);
  });

  it("narrows to a single verb and names its group", () => {
    const container = results();
    expect(render(container, VERBS, "/inv", COPY)).toBe("rows");
    const buttons = container.querySelectorAll("button.entry");
    expect(buttons.length).toBe(1);
    expect(buttons[0].dataset.verb).toBe("/invoices");
    expect(container.querySelector(".section-title").textContent).toBe("money");
  });

  it("shows the 'I read it as a message' line for an unknown slash", () => {
    const container = results();
    expect(render(container, VERBS, "/xyz", COPY)).toBe("unknown");
    expect(container.querySelectorAll("button.entry").length).toBe(0);
    expect(container.textContent).toBe(COPY.unknown);
  });

  it("clears the previous answer before rendering the next", () => {
    const container = results();
    render(container, VERBS, "", COPY);
    render(container, VERBS, "/inv", COPY);
    expect(container.querySelectorAll("button.entry").length).toBe(1);
  });
});

describe("a verb row builds nodes, never markup", () => {
  it("cannot render a help string as HTML", () => {
    const node = verbRow(
      { name: "/x", group: "work", help: '<img src=x onerror="alert(1)">' },
      COPY,
    );
    expect(node.querySelector("img")).toBe(null);
    expect(node.querySelector(".entry-body").textContent).toContain("<img");
    expect(node.dataset.verb).toBe("/x");
    expect(node.querySelector(".val").textContent).toBe("/x");
  });
});

describe("selecting a verb does not clobber a half-typed draft", () => {
  function box(value) {
    const input = document.createElement("textarea");
    input.value = value;
    return input;
  }

  it("appends to draft text rather than overwriting it (the ⌘⇧P case)", () => {
    const input = box("draft text ");
    insertVerb(input, "/goals");
    expect(input.value).toBe("draft text /goals ");
  });

  it("adds a separator when the draft has no trailing space", () => {
    const input = box("draft text");
    insertVerb(input, "/goals");
    expect(input.value).toBe("draft text /goals ");
  });

  it("replaces a trailing slash-token being typed, keeping the words before it", () => {
    const input = box("draft text /go");
    insertVerb(input, "/goals");
    expect(input.value).toBe("draft text /goals ");
  });

  it("becomes just the verb when the composer is empty (the / case)", () => {
    const input = box("");
    insertVerb(input, "/goals");
    expect(input.value).toBe("/goals ");
  });

  it("replaces a lone slash-token with no leading words", () => {
    const input = box("/go");
    insertVerb(input, "/goals");
    expect(input.value).toBe("/goals ");
  });

  it("leaves a slash mid-word alone (a path is not a command token)", () => {
    const input = box("see text/path");
    insertVerb(input, "/goals");
    expect(input.value).toBe("see text/path /goals ");
  });
});

describe("the copy crosses from Python on data attributes", () => {
  it("reads every word off the element the shell renders", () => {
    document.body.innerHTML =
      '<div id="palette-copy" data-unknown="not a command" data-empty="none"></div>';
    const copy = copyFrom(document.getElementById("palette-copy"));
    expect(copy.unknown).toBe("not a command");
    expect(copy.empty).toBe("none");
  });

  it("is empty, not broken, when the element is missing", () => {
    expect(copyFrom(null)).toEqual({});
  });
});
