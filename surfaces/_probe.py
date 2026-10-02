"""The credential probe contract (064 F6): prove a surface's credential with a READ.

``polyrob surfaces probe <id>`` imports ``<spec.module>.probe`` and awaits
``probe(env)``. A probe NEVER sends a message: it asks the platform "who am I"
(Telegram ``getMe``, Discord ``/users/@me``, Slack ``auth.test`` …).

Honest reads: a missing credential or a network fault is
``ProbeResult.unavailable(<reason>)`` — never a silent zero, never a pass. A
probe's ``detail`` never carries a secret: exceptions are reported by TYPE
only, because an HTTP error's text can quote the URL, and Telegram's URL
contains the bot token.
"""
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

#: Seconds one probe request may take.
PROBE_TIMEOUT_S = 10


@dataclass(frozen=True)
class ProbeResult:
    state: str        # "ok" | "failed" | "unavailable"
    detail: str = ""

    @classmethod
    def ok(cls, detail: str = "") -> "ProbeResult":
        return cls("ok", detail)

    @classmethod
    def failed(cls, detail: str) -> "ProbeResult":
        return cls("failed", detail)

    @classmethod
    def unavailable(cls, reason: str) -> "ProbeResult":
        return cls("unavailable", reason)

    def render(self) -> str:
        if self.state == "unavailable":
            return f"unavailable({self.detail})"
        return f"{self.state}{(' — ' + self.detail) if self.detail else ''}"


def missing(env: Mapping[str, str], *keys: str) -> Optional[ProbeResult]:
    """``unavailable(missing …)`` when any of ``keys`` is blank, else None."""
    absent = [k for k in keys if not (env.get(k) or "").strip()]
    return ProbeResult.unavailable("missing " + ", ".join(absent)) if absent else None


async def http_json(method: str, url: str, *, headers: Optional[dict] = None,
                    json: Any = None) -> Tuple[Optional[int], Any, str]:
    """``(status, payload, error)`` for one request. ``error`` is the exception
    TYPE only (never its text — see the module note)."""
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as s, \
                s.request(method, url, headers=headers or {}, json=json) as resp:
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = None
            return resp.status, payload, ""
    except Exception as e:
        return None, None, type(e).__name__
