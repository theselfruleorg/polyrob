// 043 §3 — the Work pane shows what a chat MADE beside what it DID.
//
// The pane the chat never had: Files (this chat's folder, three tiers) and a
// Timeline (one narrated line per action). What it must get right is small and
// specific:
//
//   * Files merges the workspace tree and the typed ledger into three tiers —
//     what is READY for you (a deliverable, WITH its verdict), Rob's own
//     WORKING files, and what you GAVE it (inbound/) — and a file that IS a
//     deliverable is shown once, in ready, never doubled into working;
//   * a deliverable's verdict is the ledger's typed value (ok / changed /
//     missing / unknown), never a guess;
//   * the Timeline is one line per action, narrated through the SAME narrator
//     the transcript uses, deduped by call id;
//   * an EMPTY folder is the empty state, never a blank; an UNREADABLE source is
//     a dashed entry with its sentence, never a confident empty list.
//
// ⚠️ Lives beside chats.test.js: the vitest rig's root IS `webview/dev/` and its
// include glob is rooted there — a file outside it would never run.
import { describe, it, expect } from "vitest";
import {
  buildFiles,
  copyFrom,
  bindToggle,
  drawFolder,
  folderLinkLabel,
  renderFiles,
  renderTimeline,
  timelineCount,
  timelineLines,
  toSeconds,
  verdictLabel,
} from "../static/app/workpane.js";

// The page hands these over as data-* on #workpane-copy; the Python ratchet
// tests/unit/webview/test_js_copy_handover.py is the hand-over check.
const COPY = {
  files_title: "Files",
  files_empty: "No files yet.",
  files_unreadable: "I could not load this chat's files.",
  files_tree_unreadable: "I could not load all files. These are only the finished ones.",
  files_ledger_unreadable: "I cannot tell which of these files are finished.",
  tier_ready: "Ready for you",
  tier_working: "Rob's working files",
  tier_given: "From you",
  show_all: "Show all {count}",
  shared_link: "Browse the shared folder ({count})",
  shared_link_more: "Browse the shared folder ({count} or more)",
  folder_link: "Browse this chat's folder ({count})",
  shared_title: "Shared files",
  shared_note: "Every chat and job writes here. These are not only this chat's files.",
  verdict_ok: "ready",
  verdict_changed: "changed since it was made",
  verdict_missing: "no longer there",
  verdict_unknown: "not checked",
  timeline_title: "Timeline",
  timeline_empty: "Nothing has happened in this chat yet.",
  timeline_unreadable: "I could not read what this chat did.",
  timeline_count: "{count} steps",
  timeline_count_one: "1 step",
  // The narrator's fallback keys, the same ones the transcript reads.
  act_did_work: "Ran a tool",
  act_failed: "An action did not finish",
};

/** The ledger's typed rows — `name` is the basename, `path` workspace-relative. */
const ARTIFACTS = [
  { id: "a1", name: "summary.md", path: "summary.md", kind: "report", url: null, verdict: "ok" },
  { id: "a2", name: "scratch.py", path: "scratch.py", kind: "code", url: null, verdict: "ok" },
];

/** The `?path=inbound&depth=1` tree. */
const INBOUND = { name: "inbound", type: "dir", children: [{ name: "photo.jpg", type: "file" }] };

function frame() {
  document.body.innerHTML =
    '<div id="files"></div><p id="files-state" hidden></p>' +
    '<div id="timeline"></div><p id="timeline-state" hidden></p>';
  return {
    files: document.getElementById("files"),
    filesState: document.getElementById("files-state"),
    timeline: document.getElementById("timeline"),
    timelineState: document.getElementById("timeline-state"),
  };
}

describe("Files: this chat's files come from the ledger (070 W0.15)", () => {
  it("ledger rows only", () => {
    const built = buildFiles(ARTIFACTS, null);
    expect(built.ready.map((a) => a.name)).toEqual(["summary.md"]);
    expect(built.working.map((a) => a.name)).toEqual(["scratch.py"]);
    expect(built.given).toEqual([]);
  });

  it("kinds split ready and working", () => {
    const built = buildFiles([
      { name: "p.html", kind: "page" }, { name: "r.md", kind: "report" },
      { name: "d.csv", kind: "data" }, { name: "c.py", kind: "code" },
      { name: "f.bin", kind: "file" },
    ], null);
    expect(built.ready.map((a) => a.name)).toEqual(["p.html", "r.md", "d.csv"]);
    expect(built.working.map((a) => a.name)).toEqual(["f.bin", "c.py"]); // newest first
  });

  it("inbound is given", () => {
    const built = buildFiles([], INBOUND);
    expect(built.given).toEqual([{ name: "photo.jpg", path: "inbound/photo.jpg" }]);
  });

  it("a shared tree draws the link with its count", () => {
    const { files, filesState } = frame();
    renderFiles(files, filesState, {
      artifacts: [], inbound: { children: [] },
      folder: { children: [], total: 50, truncated: false, shared: true },
    }, COPY);
    expect(filesState.textContent).toBe(COPY.files_empty);
    expect(files.querySelector(".folder-link").textContent).toBe("Browse the shared folder (50)");
    expect(folderLinkLabel({ total: 3, shared: false }, COPY)).toBe("Browse this chat's folder (3)");
  });

  it("an empty folder of this chat draws no link", () => {
    const { files, filesState } = frame();
    renderFiles(files, filesState, {
      artifacts: [], inbound: { children: [] },
      folder: { children: [], total: 0, truncated: false, shared: false },
    }, COPY);
    expect(files.querySelector(".folder-link")).toBeNull();
  });

  it("a truncated count says more", () => {
    expect(folderLinkLabel({ total: 500, truncated: true, shared: true }, COPY))
      .toBe("Browse the shared folder (500 or more)");
  });

  it("working caps at 8 with show all", () => {
    const many = Array.from({ length: 11 }, (_, i) => ({ name: `w${i}.py`, path: `w${i}.py`, kind: "code" }));
    const { files, filesState } = frame();
    renderFiles(files, filesState, { artifacts: many, inbound: { children: [] } }, COPY);
    const rows = () => [...files.querySelectorAll("tr")].filter((r) => r.textContent.match(/^w\d+\.py/));
    expect(rows().length).toBe(8);
    const btn = [...files.querySelectorAll("button")].find((b) => b.textContent === "Show all 11");
    expect(btn).toBeTruthy();
    btn.click();
    expect(rows().length).toBe(11);
  });

  it("ledger unreadable shows given and the named failure", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      artifacts: null, artifactsUnreadable: true, inbound: INBOUND,
    }, COPY);
    expect(drew).toBe("partial");
    expect(files.textContent).toContain(COPY.files_ledger_unreadable);
    expect(files.textContent).toContain("photo.jpg");
    expect(files.textContent).toContain(COPY.tier_given);
    expect(files.textContent).not.toContain(COPY.tier_working);
  });

  it("the shared folder opens on click, with its note, capped", async () => {
    const children = Array.from({ length: 10 }, (_, i) => ({ name: `f${i}`, type: i === 0 ? "dir" : "file" }));
    const asked = [];
    const list = document.createElement("div");
    await drawFolder(list, "", {
      sessionId: "s1",
      fetchTree: async (path) => { asked.push(path); return { children }; },
    }, COPY, true);
    expect(asked).toEqual([""]);
    expect(list.textContent).toContain(COPY.shared_title);
    expect(list.textContent).toContain(COPY.shared_note);
    expect(list.textContent).toContain("Show all 10");
    expect(list.querySelector("button").textContent).toBe("f0/");
  });
});

describe("a verdict is the ledger's typed value, never a guess", () => {
  it("names each of the four from copy", () => {
    expect(verdictLabel("ok", COPY)).toBe("ready");
    expect(verdictLabel("changed", COPY)).toBe("changed since it was made");
    expect(verdictLabel("missing", COPY)).toBe("no longer there");
    expect(verdictLabel("unknown", COPY)).toBe("not checked");
  });

  it("shows an unexpected verdict's own id rather than inventing a word", () => {
    expect(verdictLabel("teleporting", COPY)).toBe("teleporting");
  });
});

describe("renderFiles distinguishes the answers", () => {
  it("lists the ledger rows WITH their verdict and what you gave", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, { artifacts: ARTIFACTS, inbound: INBOUND }, COPY);
    expect(drew).toBe("rows");
    const text = files.textContent;
    expect(text).toContain("summary.md");
    expect(text).toContain("scratch.py");
    expect(text).toContain("photo.jpg");
    const deliverable = files.querySelector('tr[data-verdict="ok"]');
    expect(deliverable.querySelector(".why").textContent).toBe("ready");
  });

  it("no files is the empty state, not a blank", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, { artifacts: [], inbound: { children: [] } }, COPY);
    expect(drew).toBe("empty");
    expect(filesState.hidden).toBe(false);
    expect(filesState.textContent).toBe(COPY.files_empty);
  });

  it("says it could not read rather than showing an empty list", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      artifacts: null, inbound: null, inboundUnreadable: true,
    }, COPY);
    expect(drew).toBe("unreadable");
    expect(files.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(files.textContent).toContain(COPY.files_unreadable);
  });

  it("the three why lines are gone", () => {
    const { files, filesState } = frame();
    renderFiles(files, filesState, { artifacts: ARTIFACTS, inbound: INBOUND }, COPY);
    const heads = [...files.querySelectorAll("tr")].filter((r) => !r.querySelector(".val"));
    heads.forEach((h) => expect(h.querySelector(".why")).toBeNull());
  });
});

describe("the inbound read failing is named, never a confident-empty", () => {
  it("names the failure and still shows the ledger rows", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      artifacts: ARTIFACTS, inbound: null, inboundUnreadable: true,
    }, COPY);
    expect(drew).toBe("partial");
    expect(files.textContent).toContain(COPY.files_tree_unreadable);
    expect(files.textContent).toContain("summary.md");
  });
});

describe("the Timeline is one narrated line per action", () => {
  const EVENTS = [
    { type: "user_message", timestamp: 100, data: { text: "go" } },
    { type: "tool_result", timestamp: 106,
      data: { call_id: "c1", success: true, narration: "Read your last week of notes" } },
    { type: "tool_result", timestamp: 114,
      data: { call_id: "c2", success: true, narration: "Fetched prices for 6 tokens" } },
    // the same call, running then done — one line, its terminal state:
    { type: "tool_started", timestamp: 118, data: { call_id: "c3" } },
    { type: "tool_result", timestamp: 132,
      data: { call_id: "c3", success: false, narration: "Charting the funding rates" } },
    { type: "step", timestamp: 140, data: {} }, // not an action → not a line
  ];

  it("narrates each action, deduped by call id, timed off the first", () => {
    const lines = timelineLines(EVENTS, COPY);
    expect(lines.map((l) => l.line)).toEqual([
      "Read your last week of notes",
      "Fetched prices for 6 tokens",
      "Charting the funding rates",
    ]);
    expect(lines[0].time).toBe("0:00");
    expect(lines[1].time).toBe("0:08");
    // c3's terminal state is a failure:
    expect(lines[2].state).toBe("stopped");
  });

  it("falls back to a name-free copy line, never a machine name", () => {
    const lines = timelineLines(
      [{ type: "tool_result", timestamp: 1, data: { call_id: "x", success: true } }],
      COPY,
    );
    expect(lines[0].line).toBe(COPY.act_did_work);
  });

  it("renders narrated .act rows and marks a failed one", () => {
    const { timeline, timelineState } = frame();
    const drew = renderTimeline(timeline, timelineState, EVENTS, COPY);
    expect(drew).toBe("rows");
    const acts = timeline.querySelectorAll(".act");
    expect(acts.length).toBe(3);
    expect(acts[0].querySelector(".act-what").textContent)
      .toBe("Read your last week of notes");
    expect(acts[2].className).toContain("is-stopped");
  });

  it("a chat with no actions is the empty state, not a blank", () => {
    const { timeline, timelineState } = frame();
    const drew = renderTimeline(timeline, timelineState, [
      { type: "user_message", timestamp: 1, data: { text: "hi" } },
    ], COPY);
    expect(drew).toBe("empty");
    expect(timelineState.hidden).toBe(false);
    expect(timelineState.textContent).toBe(COPY.timeline_empty);
    expect(timeline.children.length).toBe(0);
  });

  it("a failed read is a dashed entry, not an empty timeline", () => {
    const { timeline, timelineState } = frame();
    const drew = renderTimeline(timeline, timelineState, null, COPY);
    expect(drew).toBe("unreadable");
    expect(timeline.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(timeline.textContent).toContain(COPY.timeline_unreadable);
  });
});

describe("the timeline counts the same steps as the thread", () => {
  it("the timeline skips send_message and done", () => {
    const lines = timelineLines([
      { type: "tool_result", timestamp: 1, data: { call_id: "a", action_name: "read_file", narration: "Read a file", success: true } },
      { type: "tool_result", timestamp: 2, data: { call_id: "b", action_name: "send_message", narration: "x", success: true } },
      { type: "tool_result", timestamp: 3, data: { call_id: "c", action_name: "done", narration: "y", success: true } },
    ], COPY, 0);
    expect(lines.map((l) => l.line)).toEqual(["Read a file"]);
  });

  it("dedupes by event id first, then call id", () => {
    const e = { _id: "x1", type: "tool_result", timestamp: 1, data: { narration: "One line" } };
    const lines = timelineLines([e, { ...e }], COPY, 0);
    expect(lines.length).toBe(1);
  });
});

describe("070 E.13 — the Timeline counts steps, singular-safe", () => {
  it("one step is singular, many are steps, never actions", () => {
    expect(timelineCount(1, COPY)).toBe("1 step");
    expect(timelineCount(3, COPY)).toBe("3 steps");
    expect(timelineCount(0, COPY)).toBe("0 steps");
    expect(timelineCount("—", COPY)).toBe("— steps");
    expect(timelineCount(3, COPY)).not.toMatch(/action/);
  });
});

describe("timestamps in seconds or milliseconds", () => {
  it("normalises a millisecond timestamp to seconds", () => {
    expect(toSeconds(1_700_000_000_000)).toBe(1_700_000_000);
    expect(toSeconds(1_700_000_000)).toBe(1_700_000_000);
    expect(toSeconds("nope")).toBeNaN();
  });
});

describe("the copy crosses from Python on data attributes", () => {
  it("reads every word off the element the shell renders", () => {
    const node = document.createElement("div");
    node.dataset.files_empty = "none yet";
    node.dataset.verdict_ok = "ready";
    const copy = copyFrom(node);
    expect(copy.files_empty).toBe("none yet");
    expect(copy.verdict_ok).toBe("ready");
  });

  it("is empty, not broken, when the element is missing", () => {
    expect(copyFrom(null)).toEqual({});
  });

  it("a partial read shows the inbound sentence", () => {
    const node = document.createElement("div");
    node.setAttribute("data-files_tree_unreadable",
      "I could not load all files. These are only the finished ones.");
    const { files, filesState } = frame();
    renderFiles(files, filesState, { artifacts: ARTIFACTS, inboundUnreadable: true }, copyFrom(node));
    const entry = files.querySelector(".entry.is-unknown");
    expect(entry).not.toBeNull();
    expect(entry.textContent).toContain("I could not load all files.");
  });
});


describe("070 W0.16 — the drawer toggle", () => {
  it("opens and closes the pane, and Escape closes it", () => {
    document.body.innerHTML =
      '<button id="t" aria-expanded="false"></button><aside id="p"></aside>';
    const toggle = document.getElementById("t");
    const pane = document.getElementById("p");
    bindToggle(toggle, pane);
    toggle.click();
    expect(pane.classList.contains("is-open")).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(pane.classList.contains("is-open")).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});

describe("FE17 — a refresh keeps what the person opened", () => {
  const settle = async () => { for (let i = 0; i < 6; i++) await new Promise((r) => setTimeout(r, 0)); };

  it("the open folder and an open sub-folder survive a redraw", async () => {
    const view = { folderOpen: false, openDirs: new Set(), showAll: new Set() };
    const trees = {
      "": { children: [{ name: "src", type: "dir" }, { name: "a.txt", type: "file" }] },
      src: { children: [{ name: "main.py", type: "file" }] },
    };
    const data = () => ({
      artifacts: [], inbound: { children: [] }, sessionId: "s1", view,
      folder: { children: [], total: 2, truncated: false, shared: false },
      fetchTree: async (path) => trees[path],
    });
    const { files, filesState } = frame();
    renderFiles(files, filesState, data(), COPY);
    files.querySelector(".folder-link").click();
    await settle();
    [...files.querySelectorAll("button")].find((b) => b.textContent === "src/").click();
    await settle();
    expect(files.textContent).toContain("main.py");
    // the 5 s refresh redraws the pane from fresh reads
    renderFiles(files, filesState, data(), COPY);
    await settle();
    expect(files.querySelector(".folder-link").getAttribute("aria-expanded")).toBe("true");
    expect(files.textContent).toContain("main.py");
  });

  it("a 'show all' stays shown after a redraw", () => {
    const view = { folderOpen: false, openDirs: new Set(), showAll: new Set() };
    const many = Array.from({ length: 11 }, (_, i) => ({ name: `w${i}.py`, path: `w${i}.py`, kind: "code" }));
    const { files, filesState } = frame();
    const rows = () => [...files.querySelectorAll("tr")].filter((r) => r.textContent.match(/^w\d+\.py/));
    renderFiles(files, filesState, { artifacts: many, inbound: { children: [] }, view }, COPY);
    [...files.querySelectorAll("button")].find((b) => b.textContent === "Show all 11").click();
    renderFiles(files, filesState, { artifacts: many, inbound: { children: [] }, view }, COPY);
    expect(rows().length).toBe(11);
  });
});
