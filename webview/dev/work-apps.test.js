// 043 WS-P2 §5 — Work › Apps: everything Rob built you can open, by reach.
//
// The owner's ask was ONE place where everything Rob has built is reachable,
// in three tiers by reach: online app services, published pages/files with a
// link, and files still in a chat folder. What this tab must get right is the
// same set of honest-state rules the rest of 043 turns on:
//
//   * three tiers render by reach — an app with a public URL links out;
//   * "nothing built" is the empty state ONLY when every store answered and
//     every one was empty — a store that errored is NAMED, never swept into
//     "nothing built";
//   * an unreadable store is a dashed entry with its reason, never a confident
//     empty list.
//
// It lives beside work-now.test.js because the vitest rig's root IS webview/dev/.
import { describe, it, expect } from "vitest";
import {
  appEntry,
  copyFrom,
  emptyState,
  madeSection,
  onlineSection,
  publishedSection,
  renderApps,
} from "../static/app/work-apps.js";

const COPY = {
  loading: "Reading what Rob has built.",
  online_title: "Online", online_aside: "{count} in all",
  online_empty: "Rob has not put an app online yet.",
  online_unreadable: "I could not read the app registry.",
  online_unreadable_why: "Why it failed", online_open: "Open it",
  status_live: "Online and healthy.", status_deploying: "Going online now.",
  status_approved: "Approved — going online.",
  status_pending: "Built and waiting for you. The decision is in the Inbox.",
  status_unhealthy: "Online but not answering.",
  status_failed: "It stopped after repeated failures.",
  status_stopped: "You took this down. The code is still here.",
  status_paused: "Paused.",
  published_title: "Published", published_aside: "pages and files with a link",
  published_empty: "Nothing is published with a link yet.",
  published_unreadable: "I could not read the published pages.",
  published_unreadable_why: "Why it failed",
  col_what: "What", col_link: "Link", open_link: "Open",
  made_title: "Made, but not shared", made_aside: "files from your chats",
  made_empty: "No files are waiting in a chat folder.",
  made_unreadable: "I could not read the workspace files.",
  made_unreadable_why: "Why it failed",
  col_file: "File", col_kind: "Kind",
  made_note: "These live in the folder of the chat that made them.",
  empty_title: "Rob has not built anything yet.",
  empty_body: "Ask it to build something and it will appear here.",
  empty_action: "Ask Rob to build something",
};

describe("Online — one app service entry per row, linking out when live", () => {
  it("a live app links out from its title and offers Open it", () => {
    const entry = appEntry({ slug: "summaries", status: "live",
      public_url: "https://summaries.example.com", where: "healthy for 6d" }, COPY);
    expect(entry.dataset.status).toBe("live");
    expect(entry.classList.contains("is-running")).toBe(true);
    const link = entry.querySelector(".entry-title a");
    expect(link.getAttribute("href")).toBe("https://summaries.example.com");
    const open = entry.querySelector(".entry-actions a.btn");
    expect(open.getAttribute("href")).toBe("https://summaries.example.com");
    expect(open.textContent).toBe("Open it");
    expect(entry.textContent).toContain("Online and healthy");
  });

  it("a pending app is needs-you (its decision is in the Inbox) and has no link", () => {
    const entry = appEntry({ slug: "price-watch", status: "pending",
      pending_reason: "waiting for approval" }, COPY);
    expect(entry.classList.contains("is-needs-you")).toBe(true);
    expect(entry.querySelector(".entry-title a")).toBeNull();
    expect(entry.querySelector(".entry-actions")).toBeNull();
    expect(entry.textContent).toContain("waiting for approval");
  });

  it("a taken-down app is stopped and still shows its code is here", () => {
    const entry = appEntry({ slug: "tip-jar", status: "stopped" }, COPY);
    expect(entry.classList.contains("is-stopped")).toBe(true);
    expect(entry.textContent).toContain("The code is still here");
  });

  it("an unreadable registry is a dashed entry with its reason", () => {
    const section = onlineSection({ error: "db locked" }, COPY);
    expect(section.querySelector(".entry.is-unknown")).not.toBeNull();
    expect(section.textContent).toContain("db locked");
  });

  it("an empty registry is a plain notice, not a dashed one", () => {
    const section = onlineSection({ apps: [] }, COPY);
    expect(section.querySelector(".entry.is-unknown")).toBeNull();
    expect(section.textContent).toContain(COPY.online_empty);
  });
});

describe("Published / Made — the artifact ledger, partitioned by URL", () => {
  const arts = {
    artifacts: [
      { id: "1", path: "march-report", kind: "page", url: "https://x/r/march", verdict: "ok" },
      { id: "2", path: "summary.md", kind: "report", url: null, verdict: "ok" },
    ],
    error: null,
  };

  it("a published row links out; the table carries the link", () => {
    const published = arts.artifacts.filter((a) => a.url);
    const section = publishedSection(published, arts, COPY);
    const anchors = section.querySelectorAll("a");
    expect([...anchors].some((a) => a.getAttribute("href") === "https://x/r/march")).toBe(true);
    expect(section.querySelector(".entry.is-unknown")).toBeNull();
  });

  it("a made-but-not-shared row is a file with no link, and names the chat-folder rule", () => {
    const made = arts.artifacts.filter((a) => !a.url);
    const section = madeSection(made, arts, COPY);
    expect(section.textContent).toContain("summary.md");
    expect(section.querySelector("a")).toBeNull();
    expect(section.textContent).toContain(COPY.made_note);
  });

  it("an unreadable ledger is dashed on both tiers, never a confident empty list", () => {
    const failed = { error: "unreadable" };
    expect(publishedSection([], failed, COPY).querySelector(".entry.is-unknown")).not.toBeNull();
    expect(madeSection([], failed, COPY).querySelector(".entry.is-unknown")).not.toBeNull();
  });

  it("an empty ledger is a plain notice on each tier", () => {
    const empty = { artifacts: [], error: null };
    expect(publishedSection([], empty, COPY).textContent).toContain(COPY.published_empty);
    expect(madeSection([], empty, COPY).textContent).toContain(COPY.made_empty);
  });
});

describe("renderApps — three tiers by reach, honest empty, honest failure", () => {
  it("nothing built AND every store answered empty is the empty state", () => {
    const root = document.createElement("div");
    const which = renderApps(root, { apps: [] }, { artifacts: [], error: null }, COPY);
    expect(which).toBe("empty");
    expect(root.querySelector(".state-title").textContent).toBe(COPY.empty_title);
    expect(root.querySelector('a.btn[href="/new"]')).not.toBeNull();
  });

  it("a store that ERRORED is never swept into 'nothing built'", () => {
    const root = document.createElement("div");
    const which = renderApps(root, { error: "registry down" }, { artifacts: [], error: null }, COPY);
    expect(which).toBe("apps");
    expect(root.querySelector(".state-title")).toBeNull(); // not the empty state
    expect(root.textContent).toContain("registry down"); // named instead
  });

  it("apps present renders three tiers and partitions the ledger by URL", () => {
    const root = document.createElement("div");
    const which = renderApps(root,
      { apps: [{ slug: "a", status: "live", public_url: "https://a" }] },
      { artifacts: [
        { id: "1", path: "p.html", kind: "page", url: "https://p" },
        { id: "2", path: "f.csv", kind: "data", url: null },
      ], error: null }, COPY);
    expect(which).toBe("apps");
    expect(root.textContent).toContain(COPY.online_title);
    expect(root.textContent).toContain(COPY.published_title);
    expect(root.textContent).toContain(COPY.made_title);
    expect(root.textContent).toContain("p.html");
    expect(root.textContent).toContain("f.csv");
  });
});

describe("copyFrom is a plain object of the dataset", () => {
  it("reads the dataset shallowly", () => {
    const node = document.createElement("div");
    node.dataset.online_title = "Online";
    expect(copyFrom(node).online_title).toBe("Online");
  });
});

describe("emptyState names the one action", () => {
  it("links to a new chat", () => {
    const state = emptyState(COPY);
    expect(state.querySelector('a.btn[href="/new"]').textContent).toBe(COPY.empty_action);
  });
});
