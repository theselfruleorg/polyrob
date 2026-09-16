// 043 C7 — the Chats overlay renders a row a person can act on.
//
// "Sessions" was a top-level destination in the old fifteen. It is not a
// destination: it is the way back to a conversation you already had. So it is
// an overlay on the frame, and what it must get right is small and specific:
//
//   * a row says WHO started it (043 A17's `creator`), because a chat you had
//     and a run the agent started by itself at 4am are not the same thing;
//   * a row's state is one of THREE, and an unrecognised status is UNKNOWN
//     rather than one of the three — a status nobody has seen before is not
//     "done";
//   * a failed read renders the honest sentence, never an empty list. An empty
//     list is a claim.
//
// ⚠️ The 043 C7 brief names this file `tests/js/chats.test.js`. It lives beside
// nav.test.js instead, because the vitest rig's root IS `webview/dev/` and its
// include glob is rooted there — a file outside it would simply never run.
import { describe, it, expect, beforeEach } from "vitest";
import {
  copyFrom,
  creatorLabel,
  render,
  rowNode,
  statusClass,
  statusOf,
} from "../static/app/chats.js";

const COPY = {
  loading: "Reading your chats.",
  empty: "No chats yet.",
  unreadable: "I could not read the list.",
  untitled: "A chat with no first line",
  creator_owner: "you",
  creator_cli: "you, in a terminal",
  creator_api: "a program",
  creator_cron: "a schedule",
  creator_goal: "Rob, working on a goal",
  creator_correspondent: "someone Rob wrote to",
  creator_unknown: "started by someone I cannot name",
  status_running: "working",
  status_done: "finished",
  status_stopped: "stopped",
  status_unknown: "state unknown",
};

const FIXTURE = [
  { id: "s1", task: "Check the price feed", status: "running", creator: "owner",
    created: "2026-09-15 09:12" },
  { id: "s2", task: "Nightly reconcile", status: "completed", creator: "cron",
    created: "2026-09-15 04:00" },
  { id: "s3", task: "Deploy price-watch", status: "failed", creator: "goal",
    created: "2026-09-14 22:41" },
  { id: "s4", task: "A thing", status: "teleporting", creator: "martian",
    created: "2026-09-14 20:00" },
];

function frame() {
  document.body.innerHTML =
    '<div class="ledger" id="chats-list"></div><p id="chats-state" hidden></p>';
  return {
    list: document.getElementById("chats-list"),
    state: document.getElementById("chats-state"),
  };
}

describe("status", () => {
  it("maps every real session status to one of three states", () => {
    expect(statusOf("running")).toBe("running");
    expect(statusOf("created")).toBe("running");
    expect(statusOf("initializing")).toBe("running");
    expect(statusOf("resumed")).toBe("running");
    expect(statusOf("completed")).toBe("done");
    expect(statusOf("failed")).toBe("stopped");
    expect(statusOf("cancelled")).toBe("stopped");
    expect(statusOf("suspended")).toBe("stopped");
  });

  it("refuses to guess a status it has never seen", () => {
    expect(statusOf("teleporting")).toBe("unknown");
    expect(statusOf("")).toBe("unknown");
    expect(statusOf(undefined)).toBe("unknown");
  });

  it("uses only pill classes the design system defines", () => {
    const allowed = new Set(["is-running", "is-stopped", "is-unknown"]);
    ["running", "done", "stopped", "unknown", "nonsense"].forEach((s) => {
      expect(allowed.has(statusClass(s))).toBe(true);
    });
  });
});

describe("creator", () => {
  it("names who started the session", () => {
    expect(creatorLabel({ creator: "owner" }, COPY)).toBe("you");
    expect(creatorLabel({ creator: "cron" }, COPY)).toBe("a schedule");
    expect(creatorLabel({ creator: "goal" }, COPY)).toBe("Rob, working on a goal");
  });

  it("says it cannot name an unknown one rather than inventing a label", () => {
    expect(creatorLabel({ creator: "martian" }, COPY)).toBe(COPY.creator_unknown);
    expect(creatorLabel({}, COPY)).toBe(COPY.creator_unknown);
  });
});

describe("rows", () => {
  beforeEach(() => frame());

  it("renders one row per session, with its creator and its state", () => {
    const { list, state } = frame();
    expect(render(list, state, FIXTURE, COPY)).toBe("rows");
    const rows = list.querySelectorAll(".entry");
    expect(rows.length).toBe(4);
    expect(rows[0].dataset.state).toBe("running");
    expect(rows[1].dataset.state).toBe("done");
    expect(rows[2].dataset.state).toBe("stopped");
    expect(rows[3].dataset.state).toBe("unknown");
    const words = [...rows].map((r) => r.textContent);
    expect(words[0]).toContain("working");
    expect(words[0]).toContain("you");
    expect(words[1]).toContain("a schedule");
    expect(words[3]).toContain("state unknown");
    expect(words[3]).toContain(COPY.creator_unknown);
  });

  it("links a row to the session it is about", () => {
    const { list, state } = frame();
    render(list, state, FIXTURE, COPY);
    expect(list.querySelector(".entry").getAttribute("href")).toBe("/c/s1");
  });

  it("builds nodes, never markup, so a task can never be HTML", () => {
    const node = rowNode(
      { id: "x", task: '<img src=x onerror="alert(1)">', status: "running" },
      COPY,
    );
    expect(node.querySelector("img")).toBe(null);
    expect(node.querySelector(".entry-title").textContent).toContain("<img");
  });

  it("gives a session with no first line a name rather than a blank", () => {
    const node = rowNode({ id: "x", status: "running" }, COPY);
    expect(node.querySelector(".entry-title").textContent).toBe(COPY.untitled);
  });
});

describe("honest states", () => {
  it("says nothing is there only when it read that nothing is there", () => {
    const { list, state } = frame();
    expect(render(list, state, [], COPY)).toBe("empty");
    expect(state.hidden).toBe(false);
    expect(state.textContent).toBe(COPY.empty);
    expect(list.children.length).toBe(0);
  });

  it("says it could not read rather than showing an empty list", () => {
    const { list, state } = frame();
    expect(render(list, state, null, COPY)).toBe("unreadable");
    expect(state.textContent).toBe(COPY.unreadable);
    expect(state.textContent).not.toBe(COPY.empty);
    expect(list.children.length).toBe(0);
  });

  it("clears the previous answer before rendering the next", () => {
    const { list, state } = frame();
    render(list, state, FIXTURE, COPY);
    render(list, state, [], COPY);
    expect(list.children.length).toBe(0);
  });
});

describe("the copy crosses from Python on data attributes", () => {
  it("reads every word off the element the shell renders", () => {
    document.body.innerHTML =
      '<div id="chats-copy" data-empty="none yet" data-creator_owner="you" ' +
      'data-status_running="working"></div>';
    const copy = copyFrom(document.getElementById("chats-copy"));
    expect(copy.empty).toBe("none yet");
    // ⚠️ underscore, not hyphen: `dataset` camel-cases a hyphen away, and the
    // JS keys these by the creator/status value, which is snake_case.
    expect(copy.creator_owner).toBe("you");
    expect(copy.status_running).toBe("working");
  });

  it("is empty, not broken, when the element is missing", () => {
    expect(copyFrom(null)).toEqual({});
  });
});

describe("the live@agent chip (043 A32)", () => {
  const CHIP = { ...COPY, live_at_agent: "live in the agent" };

  it("renders the chip for a session live in Rob's own process", () => {
    const node = rowNode(
      { id: "x", task: "t", status: "running", runtime: "agent", owner_pid: 4242 },
      CHIP,
    );
    const chip = node.querySelector("[data-runtime='agent']");
    expect(chip).not.toBe(null);
    expect(chip.textContent).toBe("live in the agent");
    expect(chip.dataset.ownerPid).toBe("4242");
  });

  it("renders NO chip for a session that is not live in the agent", () => {
    ["here", "idle", undefined].forEach((runtime) => {
      const node = rowNode({ id: "x", task: "t", status: "running", runtime }, CHIP);
      expect(node.querySelector("[data-runtime='agent']")).toBe(null);
    });
  });

  it("renders the chip even when owner_pid is absent (still an honest fact)", () => {
    const node = rowNode({ id: "x", task: "t", status: "running", runtime: "agent" }, CHIP);
    const chip = node.querySelector("[data-runtime='agent']");
    expect(chip).not.toBe(null);
    expect(chip.dataset.ownerPid).toBe(undefined);
  });
});
