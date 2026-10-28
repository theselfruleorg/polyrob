"""The ``http_request`` tool (093 F1): one outbound HTTP call with a JSON body.

``web_fetch`` reads (GET only); a two-call REST enrollment (POST) had no rail but
the shell, and the shell was broken on prod (2026-10-28, 402Mob). This verb is
that rail, kept narrow:

- ONE SSRF validator: every call goes through ``tools.web_fetch.fetcher.safe_fetch``
  (per-hop validation, IP pinning, caps). A write follows no redirect; a GET
  follows one only to the SAME host.
- No ambient credentials: a fresh client per call (no cookie jar carried over, no
  env key, no stored credential). Only the headers the call names are sent.
- The response body is capped (bytes on the wire, then characters returned) and
  framed as untrusted data (``untrusted_output`` on the capability row).
- Posture (093 D2): free on a genuine owner turn; a sub-agent, a leaf or a
  re-entry never sends. In an autonomous session the FIRST request to a domain
  asks the owner once through the durable owner queue (``OwnerQueueApprover``,
  the bridge's pattern), keyed on the domain; an approved domain is remembered
  (the fulfilled ask is the record), a decline holds 7 days, and a full
  ``/pause`` stops the run's requests.

NO ``from __future__ import annotations`` — the Registry introspects the action
closure's param model.
"""
import asyncio
import json
import logging
import re
from typing import Any, Dict, Literal, Optional, Set, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tools.base_tool import BaseTool

logger = logging.getLogger(__name__)

#: The registered action name (tool ``http_request``, method ``http_request``).
ACTION = "http_request"
#: Bytes read from the wire before the call is refused (``safe_fetch`` cap).
MAX_RESPONSE_BYTES = 1_048_576
_TIMEOUT_SEC = 30.0
#: Headers the client owns: the agent may not set them.
_FORBIDDEN_HEADERS = frozenset({
    "host", "content-length", "transfer-encoding", "connection", "accept-encoding",
    "upgrade", "te", "trailer", "keep-alive", "proxy-authorization", "proxy-connection",
})
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}$")
_MAX_HEADERS = 20

#: (user id, host) pairs approved in this process — a cache of the board.
_APPROVED: Set[Tuple[str, str]] = set()
#: A decline holds this long; then the next autonomous run asks again (one
#: mistaken tap must not block a domain forever).
DECLINE_TTL_SEC = 7 * 86400


def _json_text(value):
    """The tool-call layer may deep-parse a JSON-looking string; accept both."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


class HttpRequestParams(BaseModel):
    """Parameters for ``http_request`` (plain Pydantic — native-schema safe)."""

    model_config = ConfigDict(extra="forbid")
    url: str = Field(..., max_length=2048, description="Full http(s) URL of the endpoint.")
    method: Literal["POST", "PUT", "PATCH", "DELETE", "GET"] = Field(
        "POST", description="HTTP method. GET is for an API read; web_fetch reads pages.")
    body_json: str = Field("", max_length=65536, description=(
        "JSON body (an object or array), sent as application/json. Empty = no body."))
    headers_json: str = Field("{}", max_length=8192, description=(
        "JSON object of extra request headers, e.g. {\"Authorization\": \"Bearer …\"}. "
        "Nothing is added for you: no cookie, no stored key."))
    max_chars: int = Field(20000, ge=1, le=100_000, description=(
        "Characters of the response body to return."))

    _bodies = field_validator("body_json", "headers_json", mode="before")(
        classmethod(lambda cls, v: _json_text(v)))


def _parse_headers(text: str) -> Dict[str, str]:
    """The request headers, or ``ValueError`` naming what is wrong."""
    raw = json.loads(text or "{}")
    if not isinstance(raw, dict):
        raise ValueError("headers_json must be a JSON object")
    if len(raw) > _MAX_HEADERS:
        raise ValueError(f"at most {_MAX_HEADERS} headers")
    out: Dict[str, str] = {}
    for name, value in raw.items():
        name = str(name).strip()
        if not _HEADER_NAME_RE.match(name):
            raise ValueError(f"bad header name {name!r}")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise ValueError(f"header {name!r} is set by the client, not by the call")
        value = "" if value is None else str(value)
        if "\r" in value or "\n" in value or len(value) > 4096:
            raise ValueError(f"bad value for header {name!r}")
        out[name] = value
    return out


def _parse_body(text: str) -> Any:
    if not (text or "").strip():
        return None
    body = json.loads(text)
    if not isinstance(body, (dict, list)):
        raise ValueError("body_json must be a JSON object or array")
    return body


def _ask_board():
    from agents.task.goals.board import GoalBoard
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(None))


def domain_decision(user_id: str, host: str) -> Optional[str]:
    """``"approved"`` / ``"rejected"`` when the owner already decided requests to
    *host* for *user_id*, else ``None``. The record is the owner-queue ask
    (keyed on the domain), so the remembered domain lives in the one durable asks
    store. An unreadable store is undecided (the call asks), never approved."""
    if (user_id, host) in _APPROVED:
        return "approved"
    from core.goal_vocab import ASK_FULFILLED, ASK_REJECTED
    from tools.controller.approval_queue import TOOL_APPROVAL_ASK_KIND, compute_request_hash
    want = compute_request_hash(ACTION, {"domain": host}, user_id)
    try:
        board = _ask_board()
        for status in (ASK_FULFILLED, ASK_REJECTED):
            for ask in board.asks(user_id=user_id, status=status):
                p = ask.payload or {}
                if p.get("ask_kind") != TOOL_APPROVAL_ASK_KIND or p.get("request_hash") != want:
                    continue
                if p.get("decision") == "approved":
                    _APPROVED.add((user_id, host))
                    return "approved"
                if status == ASK_REJECTED or p.get("decision") == "rejected":
                    import time
                    when = getattr(ask, "updated_at", None) or getattr(ask, "created_at", None)
                    try:
                        fresh = when is None or time.time() - float(when) < DECLINE_TTL_SEC
                    except (TypeError, ValueError):
                        fresh = True
                    if fresh:
                        return "rejected"
    except Exception:
        logger.warning("http_request: the asks store is unreadable; asking again",
                       exc_info=True)
    return None


async def _ask_owner_for_domain(user_id: str, host: str, method: str, url: str,
                                execution_context) -> bool:
    """Ask the owner once (durable ask, keyed on the domain). Bounded wait."""
    from tools.controller.approval import approval_wait_timeout_sec
    from tools.controller.approval_queue import OwnerQueueApprover
    approver = OwnerQueueApprover(user_id=user_id or None)
    shown = {"domain": host, "method": method, "url": url,
             "reason": ("first http_request to this domain from an autonomous run; an "
                        "approval covers every later request to it")}
    try:
        return bool(await asyncio.wait_for(
            approver.request(ACTION, shown, execution_context,
                             hash_params={"domain": host}),
            timeout=approval_wait_timeout_sec("owner_queue")))
    except asyncio.TimeoutError:
        return False


def _autonomous(execution_context) -> bool:
    from agents.task.session_class import is_autonomous_session
    return is_autonomous_session(getattr(execution_context, "session_id", None))


class HttpRequestTool(BaseTool):
    """One outbound HTTP call (POST/PUT/PATCH/DELETE, or GET) with a JSON body."""

    def __init__(self, name: str = "http_request", config=None, container=None):
        super().__init__(name=name, config=config, container=container)
        self._enabled = True

    @property
    def required_config(self) -> Dict[str, str]:
        return {}

    async def _domain_gate(self, host: str, method: str, url: str,
                           execution_context) -> Optional[str]:
        """``None`` when this call may go to *host*, else the refusal text (093 D2).

        Free only on a genuine owner turn: the owner tenant, the top agent, not a
        self-wake / delegation re-entry. A delegated sub-agent or a leaf never
        sends (it read pages the owner never saw); the main autonomous run asks
        the owner once per domain, and a full ``/pause`` stops it."""
        from core.security.owner_turn import owner_turn_refusal
        refusal = owner_turn_refusal(execution_context, verb="http_request",
                                     does="sends an HTTP write",
                                     public="send an HTTP write")
        if refusal:
            return (f"{refusal}. Report the request back; the owner's turn or the main "
                    "run can send it.")
        if not _autonomous(execution_context):
            return None
        try:
            from core.autonomy_control import allows
            dec = allows("outbound_comms")
            if not dec.allowed:
                return f"autonomy is {dec.reason}; /resume brings it back."
        except Exception:
            logger.debug("http_request pause probe failed (fail-open)", exc_info=True)
        uid = str(getattr(execution_context, "user_id", None) or "")
        decided = domain_decision(uid, host)
        if decided == "approved":
            return None
        if decided == "rejected":
            return (f"the owner declined requests to {host} (the decline holds 7 days). Do "
                    "not retry; say in your report if the task needs this domain.")
        if await _ask_owner_for_domain(uid, host, method, url, execution_context):
            _APPROVED.add((uid, host))
            return None
        return (f"the owner has not approved requests to {host} yet. This run asked once: "
                "the owner decides it with one tap (it also shows in /pending), and a "
                "later run sends to this domain once approved. Do not retry in a loop.")

    @BaseTool.action(
        description=(
            "Send ONE HTTP request with a JSON body (POST/PUT/PATCH/DELETE; GET for an API "
            "read) and return the status and the response body. Use it for a REST call that "
            "writes: an enrollment, a webhook, a form API. Only the headers you give are sent "
            "(no cookie, no stored key). For reading a page use web_fetch; for a signed "
            "wallet challenge use wallet_sign_challenge."
        ),
        param_model=HttpRequestParams,
    )
    async def http_request(self, params: HttpRequestParams, execution_context=None):
        url = (params.url or "").strip()
        method = params.method
        try:
            headers = _parse_headers(params.headers_json)
            body = _parse_body(params.body_json)
        except (ValueError, json.JSONDecodeError) as e:
            return self._ar(error=f"http_request: {e}")
        if body is not None and method == "GET":
            return self._ar(error="http_request: a GET carries no body; use POST/PUT/PATCH")
        from core.security.pinned_resolver import url_host_key
        host = url_host_key(url)
        if not host or not url.lower().startswith(("https://", "http://")):
            return self._ar(error="http_request: url must be a full http(s) URL")

        refusal = await self._domain_gate(host, method, url, execution_context)
        if refusal:
            return self._ar(error=f"http_request refused: {refusal}")

        from tools.web_fetch.fetcher import WebFetchError, safe_fetch
        try:
            result = await safe_fetch(
                url, method=method, json_body=body, extra_headers=headers,
                max_bytes=MAX_RESPONSE_BYTES, timeout_sec=_TIMEOUT_SEC,
                same_host_redirects=True)
        except WebFetchError as e:
            return self._ar(error=f"http_request blocked or failed: {e}")
        except Exception as e:  # network / TLS / timeout
            return self._ar(error=f"http_request failed: {type(e).__name__}: {e}")

        text = result.body.decode("utf-8", errors="replace")
        cut = len(text) > params.max_chars
        text = text[:params.max_chars]
        tail = f"\n[… truncated at {params.max_chars} characters]" if cut else ""
        return self._ar(content=(
            f"HTTP {result.status} from {method} {result.final_url} "
            f"(content-type: {result.content_type})\n{text}{tail}"))
