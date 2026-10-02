"""The agent-side client of ``polyrob-signer`` (066 P2).

One connection per request, a bounded timeout, and two failure types that say
different things and must never be confused:

* :class:`SignerRefused` — the signer ANSWERED and said no. Nothing was signed.
* :class:`SignerUnavailable` — no answer. ``sent`` is True when the whole
  request left this process before the failure: the signer may have acted on
  it, so the outcome is UNKNOWN (the caller holds its interlock), never "not
  sent".
"""
import socket
from typing import Any, Dict, Optional

from core.signer import protocol


class SignerError(RuntimeError):
    pass


class SignerRefused(SignerError):
    def __init__(self, code: str, reason: str, extra: Optional[Dict[str, Any]] = None):
        self.code = str(code)
        self.reason = str(reason)
        self.extra = dict(extra or {})
        super().__init__(f"{self.code}: {self.reason}")


class SignerUnavailable(SignerError):
    def __init__(self, message: str, *, sent: bool = False):
        self.sent = bool(sent)
        super().__init__(message)


class SignerClient:
    def __init__(self, path: Optional[str] = None, *, timeout: float = 60.0):
        if path is None:
            from core.signer import socket_path
            path = socket_path()
        self.path = str(path)
        self.timeout = float(timeout)

    def call(self, op: str, body: Optional[Dict[str, Any]] = None, *,
             timeout: Optional[float] = None) -> Dict[str, Any]:
        """The ``result`` of *op*, or :class:`SignerRefused`/:class:`SignerUnavailable`."""
        frame = protocol.encode_frame(protocol.request(op, body))
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout if timeout is None else float(timeout))
                sock.connect(self.path)
                sock.sendall(frame)
                sent = True
                response = protocol.recv_frame(sock)
        except (OSError, ConnectionError, protocol.ProtocolError) as exc:
            where = "after the request was sent" if sent else "before anything was sent"
            raise SignerUnavailable(
                f"polyrob-signer at {self.path} did not answer {op!r} ({where}): "
                f"{type(exc).__name__}: {exc}", sent=sent) from exc
        if response.get("ok") is True:
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        extra = {k: v for k, v in response.items() if k not in ("v", "ok", "code", "reason")}
        raise SignerRefused(response.get("code") or protocol.INTERNAL,
                            response.get("reason") or "refused without a reason", extra)
