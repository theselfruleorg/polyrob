import { afterEach, expect, it, vi } from "vitest";

afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.resetModules();
  document.body.replaceChildren();
  delete document.body.dataset.isNew;
});

it("does not let a stale browser bearer override the console cookie", async () => {
  localStorage.setItem("auth_token", "stale.jwt.value");
  document.body.dataset.isNew = "true";
  document.body.innerHTML = `
    <form id="chat-composer" data-unreachable="Unavailable">
      <textarea id="chat-input">hello</textarea>
      <button id="chat-send-btn">Send</button>
    </form>
    <p id="chat-create-status"></p>`;
  const fetcher = vi.fn(async () => ({
    ok: false,
    status: 403,
    json: async () => ({ error: "Refused" }),
  }));
  vi.stubGlobal("fetch", fetcher);

  await import("../static/app/chat-open.js");
  document.querySelector("form").dispatchEvent(new Event("submit", { cancelable: true }));
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(fetcher).toHaveBeenCalledTimes(1);
  expect(fetcher.mock.calls[0][1].headers.Authorization).toBeUndefined();
  expect(document.querySelector("#chat-create-status").textContent).toBe("Refused");
});
