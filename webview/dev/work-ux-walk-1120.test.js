// Work (2026-11-20 browser walk): the Log reads the act's reason; Stop asks
// first and names the job; the goal board states its clock zone.
import { describe, it, expect, vi, afterEach } from "vitest";
import { entryRow, reasonText } from "../static/app/worklog.js";
import { cronRow, jobName, stopAsk, wireMutations } from "../static/app/work-now.js";
import { utcClock, utcTime } from "../static/app/work-goals.js";

const LOG_COPY = { reason: "Why: {why}", header_what: "What happened", class_tool: "Tools it used" };

describe("Work › Log shows the act's reason", () => {
  it("reads the why the act record carries, in the copy's frame", () => {
    expect(reasonText({ why: "the weekly update is due" }, LOG_COPY))
      .toBe("Why: the weekly update is due");
    expect(reasonText({}, LOG_COPY)).toBe("");
  });

  it("draws the reason on a row that carries one, and none on a row without", () => {
    const row = entryRow({ kind: "external_write", cls: "tool", summary: "posted",
                           why: "the update is due", ts: 1 }, LOG_COPY, 2000);
    const line = row.querySelector("[data-reason]");
    expect(line && line.textContent).toBe("Why: the update is due");
    const bare = entryRow({ kind: "external_write", cls: "tool", summary: "posted", ts: 1 },
                          LOG_COPY, 2000);
    expect(bare.querySelector("[data-reason]")).toBeNull();
  });
});

const NOW_COPY = {
  sched_cancel: "Stop", sched_pause: "Pause", sched_run: "Run now",
  sched_cancel_ask: "Stop {job} for good? It will not run again.",
  sched_cancel_yes: "Yes, stop it", sched_cancel_no: "Keep it", sched_a_job: "this job",
  sched_header_what: "What it does",
};

describe("Work › Scheduled jobs: Stop asks first and names the job", () => {
  afterEach(() => { vi.unstubAllGlobals(); });

  it("names a job by its name or the first line of its task, never its id", () => {
    expect(jobName({ id: "2d02db6b8071", task: "Summarise the week\nin detail" }))
      .toBe("Summarise the week");
    expect(jobName({ id: "2d02db6b8071", name: "Digest", task: "x" })).toBe("Digest");
  });

  it("the question names the job and offers a confirm and a keep", () => {
    const ask = stopAsk("Digest", "j1", NOW_COPY);
    expect(ask.textContent).toContain("Stop “Digest” for good?");
    expect(ask.querySelector("button[data-confirmed]").textContent).toBe("Yes, stop it");
    expect(ask.querySelector("button[data-cron-keep]").textContent).toBe("Keep it");
  });

  function table() {
    const tbody = document.createElement("tbody");
    tbody.appendChild(cronRow({ id: "j1", task: "Digest", status: "scheduled",
                                schedule_spec: "30m" }, NOW_COPY, Date.now(), false));
    document.body.appendChild(tbody);
    wireMutations({}, { rows: tbody }, NOW_COPY);
    return tbody;
  }

  it("a first click on Stop posts nothing; Keep puts the buttons back", () => {
    const fetcher = vi.fn();
    vi.stubGlobal("fetch", fetcher);
    const tbody = table();
    tbody.querySelector("button[data-cron='cancel']").click();
    expect(fetcher).not.toHaveBeenCalled();
    expect(tbody.querySelector("[data-ask]").textContent).toContain("“Digest”");
    expect(tbody.querySelector("button[data-cron='pause']").hidden).toBe(true);
    tbody.querySelector("button[data-cron-keep]").click();
    expect(tbody.querySelector("[data-ask]")).toBeNull();
    expect(tbody.querySelector("button[data-cron='pause']").hidden).toBe(false);
  });

  it("the confirm posts the stop and shows the server's named answer", async () => {
    const fetcher = vi.fn(async () => ({
      ok: true, status: 200,
      json: async () => ({ ok: true, message: "Stopped “Digest”. It will not run again." }),
    }));
    vi.stubGlobal("fetch", fetcher);
    const tbody = table();
    tbody.querySelector("button[data-cron='cancel']").click();
    tbody.querySelector("button[data-confirmed]").click();
    await vi.waitFor(() => expect(tbody.querySelector(".cron-answer")).not.toBeNull());
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(String(fetcher.mock.calls[0][0])).toContain("/api/webgate/cron/j1/cancel");
    expect(tbody.querySelector(".cron-answer").textContent).toBe(
      "Stopped “Digest”. It will not run again.");
    expect(tbody.querySelector("button[data-cron]")).toBeNull();
    expect(tbody.querySelector("[data-ask]")).toBeNull();
  });
});

describe("Work › Goals states its clock zone, as the header does", () => {
  it("says the UTC clock with its zone", () => {
    const ms = Date.UTC(2026, 10, 20, 21, 40);
    expect(utcClock(ms)).toBe("21:40 UTC");
    expect(utcTime(ms / 1000)).toMatch(/21:40 UTC$/);
    expect(utcClock(null)).toBe("");
    expect(utcTime(null)).toBe("");
  });
});
