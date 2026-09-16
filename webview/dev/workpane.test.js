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
  flattenTree,
  renderFiles,
  renderTimeline,
  timelineLines,
  toSeconds,
  verdictLabel,
} from "../static/app/workpane.js";

const COPY = {
  files_title: "Files",
  files_empty: "This chat has not made or been given any files yet.",
  files_unreadable: "I could not read this chat's folder.",
  files_tree_unreadable: "I could not read this chat's folder, so I am showing only what it recorded as finished.",
  files_ledger_unreadable: "I could not read the record of what this chat finished, so I cannot tell which of these are ready for you.",
  tier_folder: "In this chat's folder",
  tier_folder_why: "everything it holds right now",
  tier_ready: "Ready for you",
  tier_ready_why: "Finished things",
  tier_working: "Working files",
  tier_working_why: "What Rob made to do the job, not for you",
  tier_given: "Things you gave Rob",
  tier_given_why: "What you handed it",
  verdict_ok: "ready",
  verdict_changed: "changed since it was made",
  verdict_missing: "no longer there",
  verdict_unknown: "not checked",
  timeline_title: "Timeline",
  timeline_empty: "Nothing has happened in this chat yet.",
  timeline_unreadable: "I could not read what this chat did.",
  timeline_count: "{count} actions",
  // The narrator's fallback keys, the same ones the transcript reads.
  act_did_work: "Ran a tool",
  act_failed: "An action did not finish",
};

/** A workspace tree with a deliverable, a working file and an inbound file. */
const TREE = {
  name: "workspace",
  type: "dir",
  children: [
    { name: "summary.md", type: "file" },
    { name: "scratch.py", type: "file" },
    { name: "inbound", type: "dir", children: [{ name: "photo.jpg", type: "file" }] },
  ],
};

/** The ledger's typed rows — `path` is a basename, as artifacts_api emits. */
const ARTIFACTS = [
  { id: "a1", path: "summary.md", kind: "report", url: null, verdict: "ok" },
];

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

describe("the workspace tree flattens to file paths", () => {
  it("walks directories but emits only files, relative to the root", () => {
    expect(flattenTree(TREE)).toEqual([
      "summary.md",
      "scratch.py",
      "inbound/photo.jpg",
    ]);
  });

  it("is an empty list for an empty or missing tree, never a crash", () => {
    expect(flattenTree({ children: [] })).toEqual([]);
    expect(flattenTree(null)).toEqual([]);
    expect(flattenTree(undefined)).toEqual([]);
  });
});

describe("Files merges the tree and the ledger into three tiers", () => {
  it("puts a deliverable in ready, a scratch file in working, inbound in given", () => {
    const built = buildFiles(TREE, ARTIFACTS);
    expect(built.ready.map((a) => a.name)).toEqual(["summary.md"]);
    expect(built.ready[0].verdict).toBe("ok");
    expect(built.working.map((f) => f.name)).toEqual(["scratch.py"]);
    expect(built.given.map((f) => f.name)).toEqual(["photo.jpg"]);
  });

  it("never doubles a deliverable into working", () => {
    // summary.md is BOTH a tree file and a ledger row; it must appear once.
    const built = buildFiles(TREE, ARTIFACTS);
    expect(built.working.some((f) => f.name === "summary.md")).toBe(false);
  });

  it("is safe with no ledger at all — every tree file is a working file", () => {
    const built = buildFiles(TREE, null);
    expect(built.ready).toEqual([]);
    expect(built.working.map((f) => f.name)).toEqual(["summary.md", "scratch.py"]);
    expect(built.given.map((f) => f.name)).toEqual(["photo.jpg"]);
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

describe("renderFiles distinguishes the three answers", () => {
  it("lists a workspace file and a typed artifact WITH its verdict", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      tree: TREE, artifacts: ARTIFACTS,
    }, COPY);
    expect(drew).toBe("rows");
    const text = files.textContent;
    // the deliverable, with its verdict:
    expect(text).toContain("summary.md");
    expect(text).toContain("ready"); // verdict_ok
    // the working file:
    expect(text).toContain("scratch.py");
    // the thing you gave it:
    expect(text).toContain("photo.jpg");
    // and the verdict rode a row it can be read off of:
    const deliverable = files.querySelector('tr[data-verdict="ok"]');
    expect(deliverable).not.toBeNull();
    expect(deliverable.querySelector(".why").textContent).toBe("ready");
  });

  it("an empty workspace is the empty state, not a blank", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      tree: { name: "workspace", type: "dir", children: [], empty: true },
      artifacts: [],
    }, COPY);
    expect(drew).toBe("empty");
    expect(filesState.hidden).toBe(false);
    expect(filesState.textContent).toBe(COPY.files_empty);
    expect(files.children.length).toBe(0);
  });

  it("says it could not read rather than showing an empty list", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      tree: null, artifacts: null,
    }, COPY);
    expect(drew).toBe("unreadable");
    expect(files.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(files.textContent).toContain(COPY.files_unreadable);
    expect(files.textContent).not.toBe(COPY.files_empty);
  });

  it("treats a tree ERROR flag as unreadable, not empty", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      tree: { name: "workspace", type: "dir", children: [], error: "boom" },
      artifacts: null,
    }, COPY);
    expect(drew).toBe("unreadable");
  });
});

describe("one source unreadable is named, never a confident-empty", () => {
  it("FOLDER unreadable, ledger fine: names the folder read, still shows deliverables", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      tree: null, // the folder read failed
      artifacts: ARTIFACTS, // the ledger read fine
    }, COPY);
    expect(drew).toBe("partial");
    // the folder read that failed is NAMED, not silently empty:
    expect(files.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(files.textContent).toContain(COPY.files_tree_unreadable);
    // the deliverables we DO have are still shown, with their verdict:
    expect(files.textContent).toContain("summary.md");
    const deliverable = files.querySelector('tr[data-verdict="ok"]');
    expect(deliverable).not.toBeNull();
    expect(deliverable.querySelector(".why").textContent).toBe("ready");
  });

  it("LEDGER unreadable, folder fine: names the ledger read and does NOT relabel a deliverable as working", () => {
    const { files, filesState } = frame();
    const drew = renderFiles(files, filesState, {
      tree: TREE, // the folder read fine (summary.md is a real deliverable in it)
      artifacts: null, // the ledger read failed
      artifactsUnreadable: true,
    }, COPY);
    expect(drew).toBe("partial");
    // the ledger read that failed is NAMED:
    expect(files.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(files.textContent).toContain(COPY.files_ledger_unreadable);
    // summary.md (a real deliverable) must NOT be relabeled under "Working files":
    expect(files.textContent).not.toContain(COPY.tier_working);
    // the folder's files sit under the NEUTRAL heading instead:
    expect(files.textContent).toContain(COPY.tier_folder);
    expect(files.textContent).toContain("summary.md");
    expect(files.textContent).toContain("scratch.py");
    // no verdict is guessed while the ledger is unknown:
    expect(files.querySelector("tr[data-verdict]")).toBeNull();
    // inbound is folder-derived and stays its own honest tier:
    expect(files.textContent).toContain("photo.jpg");
    expect(files.textContent).toContain(COPY.tier_given);
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
});
