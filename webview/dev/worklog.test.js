// 043 A16 / §5 — Work › Log renders a stream a person can read and trust.
//
// The honest version of the /activity terminal: the same events, read into the
// plain classes core/activity_class gives them. What it must get right is small
// and specific:
//
//   * it names every class it read, from the server's list, and filters to one;
//   * the machine's own lines (diagnostics) are OFF by default — a reading of
//     is_diagnostic, never a guess;
//   * the raw record is one disclosure away, PER ROW, and is the event exactly
//     as it was written;
//   * an unreadable source is a dashed entry with its reason, never a silent
//     drop and never a confident empty list.
//
// It lives beside chats.test.js because the vitest rig's root IS webview/dev/.
import { describe, it, expect } from "vitest";
import {
  chipNodes,
  classLabel,
  classesFrom,
  copyFrom,
  entryRow,
  filterEntries,
  relTime,
  render,
  unreadableEntry,
} from "../static/app/worklog.js";

const COPY = {
  everything: "Everything",
  class_goal: "Its own work",
  class_cron: "On a clock",
  class_money: "Money",
  class_message: "Messages to you",
  class_system: "Rob itself",
  class_tool: "Tools it used",
  raw: "Show the raw record",
  raw_hide: "Hide the raw record",
  header_what: "What happened",
  header_when: "When",
  loading: "Reading the log.",
  empty: "Nothing has happened yet.",
  unreadable: "I could not read the activity log.",
  unreadable_why: "Why it failed",
  when_now: "just now",
  when_min: "{count} min ago",
  when_hour: "{count} h ago",
  when_day: "{count} d ago",
};

const CLASSES = ["goal", "cron", "money", "message", "system", "tool"];

function entry(kind, cls, over = {}) {
  return {
    id: `${kind}:1`, ts: 1000, kind, cls, diagnostic: false,
    summary: `a ${cls} thing`, ...over,
  };
}

describe("the copy and the class list cross on data attributes", () => {
  it("reads the ordered class list the server rendered", () => {
    const node = document.createElement("div");
    node.dataset.classes = "goal,cron,money";
    expect(classesFrom(node)).toEqual(["goal", "cron", "money"]);
  });

  it("copyFrom is a plain object of the dataset", () => {
    const node = document.createElement("div");
    node.dataset.everything = "Everything";
    expect(copyFrom(node).everything).toBe("Everything");
  });

  it("names a class from copy, and never guesses one it was not given", () => {
    expect(classLabel("money", COPY)).toBe("Money");
    expect(classLabel("martian", COPY)).toBe("martian");
  });
});

describe("diagnostics are off by default", () => {
  const rows = [
    entry("goal_run", "goal"),
    entry("step", "system", { diagnostic: true }),
  ];

  it("hides a diagnostic line until it is asked for", () => {
    const shown = filterEntries(rows, {});
    expect(shown.map((r) => r.kind)).toEqual(["goal_run"]);
  });

  it("shows diagnostics when the switch is on", () => {
    const shown = filterEntries(rows, { showDiagnostics: true });
    expect(shown.map((r) => r.kind)).toEqual(["goal_run", "step"]);
  });

  it("filters to one class", () => {
    const many = [entry("goal_run", "goal"), entry("wallet_spend", "money")];
    expect(filterEntries(many, { cls: "money" }).map((r) => r.kind))
      .toEqual(["wallet_spend"]);
  });
});

describe("relative time is drawn from the event's own timestamp", () => {
  const now = 1_000_000; // ms
  it("says just now under a minute", () => {
    expect(relTime(now / 1000 - 30, COPY, now)).toBe("just now");
  });
  it("counts minutes, hours and days from copy templates", () => {
    expect(relTime(now / 1000 - 120, COPY, now)).toBe("2 min ago");
    expect(relTime(now / 1000 - 7200, COPY, now)).toBe("2 h ago");
    expect(relTime(now / 1000 - 172800, COPY, now)).toBe("2 d ago");
  });
  it("is empty for a missing timestamp, never a guess", () => {
    expect(relTime(undefined, COPY, now)).toBe("");
  });
});

describe("a row shows its class and, on request, the raw record", () => {
  it("carries the summary, the class label and the time", () => {
    const row = entryRow(entry("wallet_spend", "money", { summary: "Bought NVDAX" }),
      COPY, 1000 * 1000);
    expect(row.querySelector(".what").textContent).toContain("Bought NVDAX");
    expect(row.querySelector(".why").textContent).toBe("Money");
    expect(row.querySelector(".num").textContent).toBeTruthy();
  });

  it("a raw toggle reveals the payload exactly as it was written", () => {
    const row = entryRow(
      entry("goal_run", "goal", { payload: { goal_id: "g7" } }), COPY, 1000 * 1000);
    const pre = row.querySelector("pre");
    const toggle = row.querySelector("button");
    expect(pre.hidden).toBe(true);
    expect(toggle.textContent).toBe("Show the raw record");
    toggle.dispatchEvent(new Event("click"));
    expect(pre.hidden).toBe(false);
    expect(pre.textContent).toContain("g7");
    expect(toggle.textContent).toBe("Hide the raw record");
  });

  it("has no raw toggle when the server sent no payload", () => {
    const row = entryRow(entry("goal_run", "goal"), COPY, 1000 * 1000);
    expect(row.querySelector("button")).toBeNull();
    expect(row.querySelector("pre")).toBeNull();
  });
});

describe("an unreadable source is a dashed entry with its reason", () => {
  it("renders the dashed entry, its sentence and the machine reason", () => {
    const node = unreadableEntry("OSError: disk is on fire", COPY);
    expect(node.className).toContain("is-unknown");
    expect(node.querySelector(".entry-title").textContent)
      .toBe("I could not read the activity log.");
    expect(node.querySelector(".entry-meta").textContent)
      .toContain("disk is on fire");
  });
});

describe("render distinguishes the three answers", () => {
  function frame() {
    document.body.innerHTML =
      '<div id="stream"></div><p id="state" hidden></p>';
    return {
      root: document.getElementById("stream"),
      state: document.getElementById("state"),
    };
  }

  it("an unreadable read is a dashed entry, not an empty list", () => {
    const { root, state } = frame();
    const drew = render(root, { entries: [], unreadable: "boom" }, state, COPY);
    expect(drew).toBe("unreadable");
    expect(root.querySelector(".entry.is-unknown")).not.toBeNull();
  });

  it("a genuine empty stream is the empty state", () => {
    const { root, state } = frame();
    const drew = render(root, { entries: [], unreadable: null }, state, COPY);
    expect(drew).toBe("empty");
    expect(state.hidden).toBe(false);
    expect(state.textContent).toBe("Nothing has happened yet.");
  });

  it("rows render a table, newest first, filtered", () => {
    const { root, state } = frame();
    const data = {
      entries: [entry("goal_run", "goal"), entry("step", "system", { diagnostic: true })],
      unreadable: null,
    };
    const drew = render(root, data, state, COPY, { nowMs: 1000 * 1000 });
    expect(drew).toBe("rows");
    // The diagnostic row is filtered out by default.
    expect(root.querySelectorAll("tbody tr").length).toBe(1);
  });
});

describe("the chips are the class set, plus Everything", () => {
  it("builds Everything then one chip per class in order", () => {
    const nodes = chipNodes(CLASSES, COPY, "", () => {});
    const labels = nodes.map((n) => n.textContent);
    expect(labels[0]).toBe("Everything");
    expect(labels.slice(1)).toEqual(
      ["Its own work", "On a clock", "Money", "Messages to you", "Rob itself",
        "Tools it used"]);
  });

  it("marks the active chip and calls back on click", () => {
    let picked = null;
    const nodes = chipNodes(CLASSES, COPY, "money", (id) => { picked = id; });
    const money = nodes.find((n) => n.dataset.cls === "money");
    expect(money.getAttribute("aria-pressed")).toBe("true");
    expect(money.classList.contains("is-on")).toBe(true);
    const goal = nodes.find((n) => n.dataset.cls === "goal");
    goal.dispatchEvent(new Event("click"));
    expect(picked).toBe("goal");
  });
});
