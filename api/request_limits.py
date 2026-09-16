"""Bound HTTP ingress before application parsers, including chunked bodies."""
import asyncio
import tempfile

from starlette.responses import JSONResponse

MAX_BODY_BYTES = 8 * 1024 * 1024
BODY_DEADLINE_SECONDS = 30
MAX_ACTIVE_BODY_READS = 16


class RequestBodyLimitMiddleware:
    """Bound spooled bodies through ingestion and replay; reject saturated uploads.

    A disk spool avoids holding every accepted body in memory here. Downstream
    parsers remain responsible for their own decoded/object expansion limits.
    WebSockets are governed by the transport's independent frame limits.
    """

    def __init__(self, app, max_bytes=MAX_BODY_BYTES, timeout=BODY_DEADLINE_SECONDS,
                 concurrency=MAX_ACTIVE_BODY_READS):
        self.app = app
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.slots = asyncio.Semaphore(concurrency)

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
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.05)
        except asyncio.TimeoutError:
            return await self._reject(503, scope, receive, send)
        try:
            spool = tempfile.SpooledTemporaryFile(max_size=256 * 1024)
        except Exception:
            self.slots.release()
            return await self._reject(503, scope, receive, send)
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                try:
                    spool.close()
                finally:
                    self.slots.release()

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
