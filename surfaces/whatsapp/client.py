"""Thin WhatsApp Cloud API (Graph) client. Injectable: tests pass a fake with the same
async methods. Real impl uses httpx + WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN."""
import logging
import os

logger = logging.getLogger(__name__)
_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppClient:
    def __init__(self, *, phone_number_id: str = None, access_token: str = None) -> None:
        self._pnid = phone_number_id or os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
        self._token = access_token or os.getenv("WHATSAPP_ACCESS_TOKEN", "")

    async def _post(self, payload: dict) -> dict:
        import httpx
        url = f"{_BASE}/{self._pnid}/messages"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()

    async def send_text(self, to: str, text: str, reply_to: str = None) -> dict:
        payload = {"messaging_product": "whatsapp", "to": to,
                   "type": "text", "text": {"body": text}}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        return await self._post(payload)

    async def mark_read(self, message_id: str) -> dict:
        return await self._post({"messaging_product": "whatsapp",
                                 "status": "read", "message_id": message_id})

    async def send_template(self, to: str, name: str, lang: str = "en_US",
                            params: list = None) -> dict:
        components = ([{"type": "body", "parameters": [{"type": "text", "text": p}
                                                       for p in params]}] if params else [])
        return await self._post({"messaging_product": "whatsapp", "to": to, "type": "template",
                                 "template": {"name": name, "language": {"code": lang},
                                              "components": components}})

    async def download_media(self, media_id: str, *, max_bytes: int = None):
        """The bytes of one inbound media item, or None when over the cap (OS15).

        The cap is ``INBOUND_MEDIA_MAX_MB`` (the one inbound-media limit). It
        is enforced on the declared ``file_size`` AND while reading, so a lying
        size cannot make the process buffer a huge body.
        """
        import httpx
        if max_bytes is None:
            from core.surfaces.inbound_attachments import inbound_media_max_mb
            max_bytes = int(inbound_media_max_mb() * 1024 * 1024)
        headers = {"Authorization": f"Bearer {self._token}"}
        async with httpx.AsyncClient(timeout=30) as c:
            meta = (await c.get(f"{_BASE}/{media_id}", headers=headers)).json()
            url = meta.get("url")
            if not url:
                return b""
            try:
                declared = int(meta.get("file_size") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > max_bytes:
                logger.warning("whatsapp media %s: %s bytes over the %s-byte cap",
                               media_id, declared, max_bytes)
                return None
            chunks, size = [], 0
            async with c.stream("GET", url, headers=headers) as resp:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        logger.warning("whatsapp media %s: body passed the %s-byte cap",
                                       media_id, max_bytes)
                        return None
                    chunks.append(chunk)
            return b"".join(chunks)
