// 070 W0.14 — nothing polls while the socket is up.
import { describe, it, expect } from "vitest";
import { beat, liveState, onLiveChange, wireSocket } from "../static/app/live.js";

function fakeSocket() {
  const handlers = {};
  const emitted = [];
  const acks = [];
  return {
    handlers, emitted, acks,
    on(name, fn) { handlers[name] = fn; },
    emit(name, body, ack) { emitted.push([name, body]); acks.push(ack); },
    // the server's ack of the most recent join
    ack() { const fn = acks[acks.length - 1]; if (fn) fn(); },
    fire(name, body) { handlers[name] && handlers[name](body); },
  };
}

describe("the fallback tick", () => {
  it("no tick while connected", () => {
    const state = liveState();
    const socket = fakeSocket();
    const fired = [];
    wireSocket(socket, state, (name) => fired.push(name));
    socket.fire("connect");
    expect(socket.emitted[0][0]).toBe("join_activity");
    socket.ack();
    let heads = 0;
    beat(state, (name) => fired.push(name), () => { heads += 1; });
    expect(fired).not.toContain("polyrob:tick");
    expect(heads).toBe(1);
  });

  it("ticks after a disconnect", () => {
    const state = liveState();
    const socket = fakeSocket();
    wireSocket(socket, state, () => {});
    socket.fire("connect");
    socket.fire("disconnect");
    const fired = [];
    beat(state, (name) => fired.push(name), () => {});
    expect(fired).toEqual(["polyrob:tick"]);
  });

  it("ticks before the first connect and after a connect error", () => {
    const state = liveState();
    const fired = [];
    beat(state, (name) => fired.push(name), () => {});
    const socket = fakeSocket();
    wireSocket(socket, state, () => {});
    socket.fire("connect");
    socket.fire("connect_error");
    beat(state, (name) => fired.push(name), () => {});
    expect(fired).toEqual(["polyrob:tick", "polyrob:tick"]);
  });
});

describe("070 W0.17 — the banner clears and a refusal retries", () => {
  it("connect clears the banner", () => {
    const state = liveState();
    const socket = fakeSocket();
    const fired = [];
    wireSocket(socket, state, (name) => fired.push(name));
    socket.fire("connect");
    expect(fired).not.toContain("polyrob:live-ok");
    socket.ack();
    expect(fired).toContain("polyrob:live-ok");
  });

  it("rate_limited retries after retry_after", () => {
    const state = liveState();
    const socket = fakeSocket();
    const fired = [];
    const timers = [];
    wireSocket(socket, state, (name, body) => fired.push([name, body]), (fn, ms) => timers.push([fn, ms]));
    socket.fire("connect");
    socket.emitted.length = 0;
    socket.fire("error", { code: "rate_limited", retry_after: 60 });
    expect(fired.map((f) => f[0])).toContain("polyrob:live-refused");
    expect(timers.length).toBe(1);
    expect(timers[0][1]).toBe(60000);
    timers[0][0]();
    expect(socket.emitted).toEqual([["join_activity", {}]]);
    // FE18: the retried join is only sent — the notice stays until its ack
    expect(fired[fired.length - 1][0]).toBe("polyrob:live-refused");
    socket.ack();
    expect(fired[fired.length - 1][0]).toBe("polyrob:live-ok");
  });

  it("another refusal does not retry", () => {
    const state = liveState();
    const socket = fakeSocket();
    const timers = [];
    wireSocket(socket, state, () => {}, (fn, ms) => timers.push([fn, ms]));
    socket.fire("error", { code: "not_yours", retry_after: null });
    expect(timers).toEqual([]);
  });
});

describe("FE5 — connected only after an accepted join", () => {
  it("a refused join keeps the fallback tick running", () => {
    const state = liveState();
    const socket = fakeSocket();
    wireSocket(socket, state, () => {});
    socket.fire("connect");
    expect(state.connected).toBe(false);
    socket.fire("error", { code: "not_yours" });
    socket.ack(); // the refused join's ack arrives after its refusal
    expect(state.connected).toBe(false);
    const fired = [];
    beat(state, (name) => fired.push(name), () => {});
    expect(fired).toEqual(["polyrob:tick"]);
  });

  it("an accepted join stops the tick", () => {
    const state = liveState();
    const socket = fakeSocket();
    wireSocket(socket, state, () => {});
    socket.fire("connect");
    socket.ack();
    expect(state.connected).toBe(true);
  });
});

describe("FE4 — small reads refresh with a healthy socket", () => {
  it("an activity event, a good re-join and a visible tab re-read; the tick at once", () => {
    let reads = 0;
    const timers = [];
    onLiveChange(() => { reads += 1; }, { later: (fn) => timers.push(fn) });
    document.dispatchEvent(new CustomEvent("polyrob:activity"));
    document.dispatchEvent(new CustomEvent("polyrob:activity"));
    expect(timers.length).toBe(1); // debounced
    timers.shift()();
    expect(reads).toBe(1);
    document.dispatchEvent(new CustomEvent("polyrob:live-ok"));
    timers.shift()();
    expect(reads).toBe(2);
    document.dispatchEvent(new Event("visibilitychange"));
    timers.shift()();
    expect(reads).toBe(3);
    document.dispatchEvent(new CustomEvent("polyrob:tick"));
    expect(reads).toBe(4);
  });
});
