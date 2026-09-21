"""AgentMail managed-inbox HTTP client (no SDK dependency — plain httpx).

API: https://docs.agentmail.to — Bearer auth, base https://api.agentmail.to.
Endpoints used:

- ``POST /v0/inboxes``                                  (idempotent provisioning)
- ``POST /v0/inboxes/{inbox_id}/messages/send``
- ``GET  /v0/inboxes/{inbox_id}/messages``
- ``GET  /v0/inboxes/{inbox_id}/messages/{message_id}``

Thread-anchor contract: the correspondent registry binds outbound mail by the
RFC 5322 ``Message-ID`` the sender minted (``send_email_ex`` returns it; a
reply's ``In-Reply-To`` exact-matches it). Over HTTP we mint our own Message-ID
and pass it via the API's custom ``headers`` map — and, because a provider MAY
rewrite that header on the wire, we ALSO record ``thread_id -> minted mid`` in a
sidecar map so the receive side (surfaces/email/fetchers.py::AgentMailFetcher)
can synthesize the anchor for a reply that lost it. Belt and suspenders; either
path alone closes the loop.

State files (under the data home, next to the other sidecar stores):
- ``agent_mail.json``         — {"inbox_id", "address", "provisioned_at"}
  (read by core/instance.py::resolve_agent_email — keep the shape in sync).
- ``agent_mail_threads.json`` — {thread_id: minted_message_id}, bounded.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import tempfile
from datetime import datetime, timezone
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import httpx

from core.exceptions import APIError
from core.instance import agent_mail_state_path

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.agentmail.to"
THREADS_FILENAME = "agent_mail_threads.json"
_THREAD_MAP_MAX = 500


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.remove(tmp_name)
        except OSError:
            pass
        raise


def normalize_agentmail_attachments(raw: Any) -> List[dict]:
    """AgentMail attachment objects -> the ONE ``{filename, mime, data}`` shape.

    The same shape ``surfaces.email.harness._attachments`` produces for IMAP, so
    everything downstream (``_attachment_media`` -> ``Media`` -> the inbound
    media rail) is transport-blind (D26).

    ⚠️ ``data`` is ``None`` when the provider gave metadata but no bytes (it
    lists attachments with an id and offers them on a separate endpoint). The
    entry is KEPT (D64): an attachment that exists and could not be read must
    be NAMED on the turn with its reason. Dropping it is how the agent comes to
    answer as though the mail were empty.
    """
    out: List[dict] = []
    for att in (raw or []):
        if not isinstance(att, dict):
            continue
        data = None
        blob = att.get("content") or att.get("data")
        if isinstance(blob, (bytes, bytearray)):
            data = bytes(blob)
        elif isinstance(blob, str) and blob:
            try:
                data = base64.b64decode(blob, validate=False)
            except Exception as e:
                logger.warning("agentmail attachment %r is not decodable base64: %s",
                               att.get("filename"), e)
                data = None
        out.append({
            "filename": att.get("filename") or att.get("name"),
            "mime": att.get("content_type") or att.get("mime") or att.get("type"),
            "data": data,
        })
    return out


class AgentMailClient:
    """Async HTTP client for one AgentMail inbox (the agent's own address)."""

    def __init__(self, api_key: str, *, data_home: Optional[Path] = None,
                 base_url: str = DEFAULT_BASE_URL,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        if not (api_key or "").strip():
            raise ValueError("AgentMailClient requires a non-empty api_key")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._data_home = Path(data_home) if data_home is not None else None
        self._http: Optional[httpx.AsyncClient] = None
        self._state: Optional[dict] = None
        self._threads: Optional[Dict[str, str]] = None

    def __repr__(self) -> str:  # never echo the key
        return f"AgentMailClient(base_url={self._base_url!r}, inbox={self.address!r})"

    # -- state ------------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return agent_mail_state_path(self._data_home)

    @property
    def _threads_path(self) -> Path:
        return self._state_path.parent / THREADS_FILENAME

    def _load_state(self) -> Optional[dict]:
        if self._state is not None:
            return self._state
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if raw.get("inbox_id") and raw.get("address"):
                self._state = raw
                return raw
        except Exception:
            pass
        return None

    @property
    def inbox_id(self) -> Optional[str]:
        state = self._load_state()
        return state.get("inbox_id") if state else None

    @property
    def address(self) -> Optional[str]:
        state = self._load_state()
        return state.get("address") if state else None

    def _load_threads(self) -> Dict[str, str]:
        if self._threads is None:
            try:
                raw = json.loads(self._threads_path.read_text(encoding="utf-8"))
                self._threads = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                self._threads = {}
        return self._threads

    def minted_mid_for_thread(self, thread_id: str) -> Optional[str]:
        """Our minted RFC Message-ID for a provider thread, if we sent into it."""
        return self._load_threads().get(str(thread_id))

    def _record_thread(self, thread_id: str, mid: str) -> None:
        threads = self._load_threads()
        threads[str(thread_id)] = mid
        # Bound the map: drop oldest insertions beyond the cap (dict is ordered).
        while len(threads) > _THREAD_MAP_MAX:
            threads.pop(next(iter(threads)))
        try:
            _atomic_write_json(self._threads_path, threads)
        except Exception as e:
            logger.warning("agentmail: thread map write failed: %s", e)

    # -- http -------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
                transport=self._transport,
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = await self._client().request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise APIError(f"agentmail {method} {path} failed: {e}")
        if resp.status_code >= 400:
            raise APIError(
                f"agentmail {method} {path} -> HTTP {resp.status_code}: "
                f"{resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as e:
            raise APIError(f"agentmail {method} {path}: non-JSON response: {e}")

    # -- api --------------------------------------------------------------

    async def provision(self, instance_id: str,
                        username: Optional[str] = None) -> dict:
        """Ensure the agent's inbox exists; persist + return the state dict.

        Idempotent two ways: a persisted ``agent_mail.json`` short-circuits
        without any network call, and the API-side ``client_id`` makes a retry
        of the POST return the same inbox rather than minting a second one.
        """
        state = self._load_state()
        if state:
            return state
        body: Dict[str, Any] = {"client_id": f"polyrob-{instance_id}"}
        if username:
            body["username"] = username
        data = await self._request("POST", "/v0/inboxes", json=body)
        address = data.get("email") or data.get("inbox_id") or ""
        state = {
            "inbox_id": data.get("inbox_id") or address,
            "address": address,
            "provisioned_at": datetime.now(timezone.utc).isoformat(),
        }
        if not (state["inbox_id"] and "@" in str(state["address"])):
            raise APIError(f"agentmail: unusable provisioning response: {data}")
        _atomic_write_json(self._state_path, state)
        self._state = state
        logger.info("agentmail: provisioned agent inbox %s", state["address"])
        return state

    def _attachment_payload(self, path: str) -> dict:
        filename = os.path.basename(path)
        ctype, _ = mimetypes.guess_type(path)
        with open(path, "rb") as f:
            content = base64.b64encode(f.read()).decode("ascii")
        return {"filename": filename,
                "content_type": ctype or "application/octet-stream",
                "content": content}

    async def send(
        self,
        to: Union[str, List[str]],
        subject: str,
        text: str,
        *,
        html: Optional[str] = None,
        cc: Optional[Union[str, List[str]]] = None,
        bcc: Optional[Union[str, List[str]]] = None,
        attachments: Optional[List[str]] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[str] = None,
    ) -> str:
        """Send from the agent's inbox; return the minted RFC Message-ID."""
        if not self.inbox_id:
            raise APIError("agentmail: inbox not provisioned — call provision() first")
        domain = None
        try:
            domain = (self.address or "").split("@", 1)[1] or None
        except IndexError:
            pass
        mid = make_msgid(domain=domain) if domain else make_msgid()

        headers: Dict[str, str] = {"Message-ID": mid}
        if in_reply_to:
            headers["In-Reply-To"] = in_reply_to
            headers["References"] = references or in_reply_to
        elif references:
            headers["References"] = references

        body: Dict[str, Any] = {"to": to, "subject": subject, "text": text,
                                "headers": headers}
        if html:
            body["html"] = html
        if cc:
            body["cc"] = cc
        if bcc:
            body["bcc"] = bcc
        if attachments:
            parts = []
            for path in attachments:
                try:
                    parts.append(self._attachment_payload(path))
                except Exception as e:
                    logger.warning(
                        "agentmail: skipping unreadable attachment %r: %s", path, e)
            if parts:
                body["attachments"] = parts

        data = await self._request(
            "POST", f"/v0/inboxes/{self.inbox_id}/messages/send", json=body)
        thread_id = data.get("thread_id")
        if thread_id:
            self._record_thread(thread_id, mid)
        return mid

    async def list_messages(self, limit: int = 25) -> List[dict]:
        """Most-recent message summaries (no bodies — use :meth:`get_message`)."""
        if not self.inbox_id:
            raise APIError("agentmail: inbox not provisioned — call provision() first")
        data = await self._request(
            "GET", f"/v0/inboxes/{self.inbox_id}/messages",
            params={"limit": limit})
        msgs = data.get("messages")
        return list(msgs) if isinstance(msgs, list) else []

    async def get_message(self, message_id: str) -> dict:
        """Full single message (text / extracted_text / threading headers)."""
        if not self.inbox_id:
            raise APIError("agentmail: inbox not provisioned — call provision() first")
        return await self._request(
            "GET", f"/v0/inboxes/{self.inbox_id}/messages/{message_id}")
