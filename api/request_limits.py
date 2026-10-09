"""Bound HTTP ingress before application parsers, including chunked bodies."""
import asyncio
import tempfile

from starlette.responses import JSONResponse

#: Multipart envelope a single-file upload costs on top of the file itself
#: (boundaries, part headers, the surrounding form fields).
MULTIPART_OVERHEAD_BYTES = 1024 * 1024


def max_body_bytes() -> int:
    """The ingress body ceiling, derived from the ONE upload cap.

    B14: this used to be a hardcoded 8 MiB while
    ``core.surfaces.inbound_attachments.upload_max_mb()`` allowed 20 MiB, so
    ``POST /api/task/sessions/{id}/workspace/upload`` advertised a limit this
    middleware rejected first — with a *different* error — for every file over
    8 MiB. One cap, plus the multipart envelope it actually travels in.
    Fail-open to the legacy 8 MiB if the cap cannot be read.
    """
    try:
        from core.surfaces.inbound_attachments import upload_max_mb
        return int(upload_max_mb() * 1024 * 1024) + MULTIPART_OVERHEAD_BYTES
    except Exception:
        return 8 * 1024 * 1024


# NOTE: there is deliberately no module-level `MAX_BODY_BYTES` constant. It had
# zero readers after B14 and, being evaluated at IMPORT, it bound the cap
# before env layering — the same import-time-binding landmine
# `tests/test_home_binding_ratchet.py` exists for. Call `max_body_bytes()`.
BODY_DEADLINE_SECONDS = 30
MAX_ACTIVE_BODY_READS = 16
#: API7: one client (trusted client IP) may hold at most this many of the
#: global body-read slots, so a single slow-body sender cannot take all of them.
MAX_ACTIVE_BODY_READS_PER_CLIENT = 4

#: Methods that carry no body unless a length or a transfer coding says so.
_BODYLESS_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "DELETE"})


def _declares_no_body(scope, lengths, headers) -> bool:
    """API7: a request with no body never needs an upload slot.

    ``Content-Length: 0`` says so outright; a GET/HEAD/OPTIONS/DELETE with no
    length and no ``Transfer-Encoding`` has no body under HTTP/1.1. Such a
    request (``/health``, every read) used to queue behind the slots too.
    """
    if lengths:
        return int(lengths[0]) == 0
    if any(k.lower() == b"transfer-encoding" for k, _ in headers):
        return False
    return str(scope.get("method", "")).upper() in _BODYLESS_METHODS


def _client_key(scope):
    """The trusted client identity for the per-client slot bound, or None."""
    try:
        from starlette.requests import Request

        from api.dependencies import get_trusted_client_ip, rate_key_for_ip
        # API-11: an IPv6 client is its /64, so it cannot take every slot by
        # rotating addresses inside one prefix.
        return rate_key_for_ip(get_trusted_client_ip(Request(scope)))
    except Exception:
        return None


class RequestBodyLimitMiddleware:
    """Bound spooled bodies through ingestion and replay; reject saturated uploads.

    A disk spool avoids holding every accepted body in memory here. Downstream
    parsers remain responsible for their own decoded/object expansion limits.
    WebSockets are governed by the transport's independent frame limits.
    """

    def __init__(self, app, max_bytes=None, timeout=BODY_DEADLINE_SECONDS,
                 concurrency=MAX_ACTIVE_BODY_READS,
                 per_client=MAX_ACTIVE_BODY_READS_PER_CLIENT):
        self.app = app
        # Resolved at construction (not import) so the derived upload cap is
        # read after env layering — see max_body_bytes().
        self.max_bytes = max_body_bytes() if max_bytes is None else max_bytes
        self.timeout = timeout
        self.slots = asyncio.Semaphore(concurrency)
        self.per_client = per_client
        self.client_slots = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        lengths = [v for k, v in headers if k.lower() == b"content-length"]
        try:
            if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
                raise ValueError
            if lengths and int(lengths[0]) > self.max_bytes:
                return await self._reject(413, scope, receive, send)
        except ValueError:
            return await self._reject(400, scope, receive, send)
        if _declares_no_body(scope, lengths, headers):
            return await self.app(scope, receive, send)
        client = _client_key(scope)
        if client is not None and self.client_slots.get(client, 0) >= self.per_client:
            return await self._reject(503, scope, receive, send)
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return await self._reject(503, scope, receive, send)
        if client is not None:
            self.client_slots[client] = self.client_slots.get(client, 0) + 1

        def free_slot():
            self.slots.release()
            if client is not None:
                left = self.client_slots.get(client, 1) - 1
                if left > 0:
                    self.client_slots[client] = left
                else:
                    self.client_slots.pop(client, None)

        try:
            spool = tempfile.SpooledTemporaryFile(max_size=256 * 1024)
        except Exception:
            free_slot()
            return await self._reject(503, scope, receive, send)
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                try:
                    spool.close()
                finally:
                    free_slot()

        try:
            size = 0
            deadline = asyncio.get_running_loop().time() + self.timeout
            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    message = await asyncio.wait_for(receive(), timeout=remaining)
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        return await self._reject(400, scope, receive, send)
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.max_bytes:
                        return await self._reject(413, scope, receive, send)
                    spool.write(chunk)
                    if not message.get("more_body", False):
                        break
                if lengths and size != int(lengths[0]):
                    return await self._reject(400, scope, receive, send)
                spool.seek(0)
            except asyncio.TimeoutError:
                return await self._reject(408, scope, receive, send)
            except OSError:
                return await self._reject(503, scope, receive, send)

            # Keep capacity reserved until the spool is consumed or the handler
            # exits. Release at final replay so long-lived streams do not occupy
            # upload slots after their body has been delivered.
            replayed = 0
            sent_final = False
            if size == 0:
                release()

            async def replay():
                nonlocal replayed, sent_final
                if sent_final:
                    return await receive()
                chunk = spool.read(65536) if size else b""
                replayed += len(chunk)
                sent_final = replayed >= size
                if sent_final:
                    release()
                return {"type": "http.request", "body": chunk, "more_body": not sent_final}

            await self.app(scope, replay, send)
        finally:
            release()

    async def _reject(self, status, scope, receive, send):
        messages = {400: "Invalid request length", 408: "Request body timed out",
                    413: "Request body too large", 503: "Upload capacity exhausted"}
        response = JSONResponse({"error": messages[status]}, status_code=status,
                                headers={"Connection": "close"})
        await response(scope, receive, send)
