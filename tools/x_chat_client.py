"""OAuth2 client for X's encrypted Chat API.

The Chat HTTP API returns ciphertext.  Plaintext is available only when the
official ``chatxdk`` package can unlock/import this account's Chat keys.  This
module keeps that distinction explicit: callers always receive a
``decryption`` status and never interpret ciphertext as an empty inbox.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import Any, Optional
from urllib.parse import quote

import aiohttp


class XChatAPIError(RuntimeError):
    """An X Chat HTTP request failed."""


class XChatClient:
    API_ROOT = "https://api.x.com/2"

    def __init__(self, access_token: str, *, session: Any = None,
                 private_keys_b64: Optional[str] = None,
                 key_version: Optional[str] = None,
                 passphrase: Optional[str] = None) -> None:
        self.access_token = str(access_token or "")
        self._session = session
        self._owns_session = False
        self._private_keys_b64 = (
            private_keys_b64
            if private_keys_b64 is not None
            else os.getenv("TWITTER_CHAT_PRIVATE_KEYS_B64", "")
        )
        self._key_version = (
            key_version
            if key_version is not None
            else os.getenv("TWITTER_CHAT_KEY_VERSION", "")
        )
        self._passphrase = (
            passphrase
            if passphrase is not None
            else os.getenv("TWITTER_CHAT_PASSPHRASE", "")
        )
        self._chat = None
        self._me_id: Optional[str] = None

    @property
    def can_decrypt(self) -> bool:
        return bool(self._private_keys_b64 or self._passphrase)

    async def _http(self):
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.access_token}"})
            self._owns_session = True
        return self._session

    async def _get(self, path: str, params: Optional[dict] = None) -> dict:
        session = await self._http()
        async with session.get(f"{self.API_ROOT}{path}", params=params) as resp:
            try:
                payload = await resp.json()
            except Exception:
                payload = {"detail": await resp.text()}
            if resp.status >= 400:
                detail = payload.get("detail") or payload.get("title") or payload
                raise XChatAPIError(f"X Chat API {resp.status}: {detail}")
            return payload

    async def get_me(self) -> str:
        if self._me_id is None:
            payload = await self._get("/users/me")
            self._me_id = str((payload.get("data") or {}).get("id") or "")
            if not self._me_id:
                raise XChatAPIError("X Chat /users/me returned no user id")
        return self._me_id

    async def get_conversations(self, *, max_results: int = 20,
                                pagination_token: Optional[str] = None) -> dict:
        params = {
            "max_results": max(1, min(int(max_results), 100)),
            "chat_conversation.fields": (
                "id,type,created_at,updated_at,group_name,is_muted,message_ttl_ms"),
            "expansions": "participant_ids,member_ids,admin_ids",
            "user.fields": "id,name,username,profile_image_url",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return await self._get("/chat/conversations", params)

    async def get_conversation_events(
            self, conversation_id: str, *, max_results: int = 20,
            pagination_token: Optional[str] = None) -> dict:
        params = {
            "max_results": max(1, min(int(max_results), 100)),
            "chat_message_event.fields": (
                "id,conversation_id,conversation_token,created_at,encoded_event,"
                "is_trusted,message_event_signature,previous_id,sender_id"),
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        cid = quote(str(conversation_id).replace(":", "-"), safe="-")
        return await self._get(f"/chat/conversations/{cid}/events", params)

    async def get_conversation(self, conversation_id: str) -> dict:
        """Fetch one conversation, including the participant roster."""
        cid = quote(str(conversation_id).replace(":", "-"), safe="-")
        return await self._get(
            f"/chat/conversations/{cid}",
            {
                "chat_conversation.fields": (
                    "id,type,created_at,updated_at,group_name,is_muted,message_ttl_ms"),
                "expansions": "participant_ids,member_ids,admin_ids",
                "user.fields": "id,name,username,profile_image_url",
            })

    async def _public_keys(self, user_id: str) -> list[dict]:
        fields = (
            "public_key_version,public_key,signing_public_key,"
            "identity_public_key_signature,juicebox_config")
        payload = await self._get(
            f"/users/{quote(str(user_id), safe='')}/public_keys",
            {"public_key.fields": fields})
        return list(payload.get("data") or [])

    async def _ensure_chat(self):
        if self._chat is not None:
            return self._chat
        try:
            from chat_xdk import Chat
        except ImportError as exc:
            raise RuntimeError(
                "X Chat decryption is configured but chatxdk is not installed; "
                "install polyrob[twitter]") from exc

        me_id = await self.get_me()
        own_records = await self._public_keys(me_id)
        if not own_records:
            raise RuntimeError("X Chat has no registered public-key record")
        version = str(self._key_version or
                      own_records[0].get("public_key_version") or "")
        if self._private_keys_b64:
            chat = Chat()
            blob = base64.b64decode(self._private_keys_b64, validate=True)
            # chatxdk exposes positional-only native bindings.
            await asyncio.to_thread(chat.import_keys, blob, version)
        else:
            config = own_records[0].get("juicebox_config")
            if not config:
                raise RuntimeError(
                    "X Chat public-key record has no secure-backup configuration")
            chat = Chat(json.dumps(config))
            await asyncio.to_thread(chat.unlock, self._passphrase)
        chat.set_identity(me_id, version)
        chat.set_cache_keys(True)
        self._chat = chat
        return chat

    async def _signing_keys(self, user_ids: list[str]) -> list[dict]:
        keys: list[dict] = []
        for uid in dict.fromkeys(str(v) for v in user_ids if v):
            for row in await self._public_keys(uid):
                keys.append({
                    "user_id": uid,
                    "public_key_version": row.get("public_key_version"),
                    "public_key": row.get("signing_public_key"),
                    "identity_public_key": row.get("public_key"),
                    "identity_public_key_signature":
                        row.get("identity_public_key_signature"),
                })
        return keys

    async def read_conversation(
            self, conversation_id: str, *, participant_ids: Optional[list[str]] = None,
            max_results: int = 20,
            pagination_token: Optional[str] = None) -> dict:
        raw = await self.get_conversation_events(
            conversation_id, max_results=max_results,
            pagination_token=pagination_token)
        events = list(raw.get("data") or [])
        meta = dict(raw.get("meta") or {})
        result = {
            "conversation_id": str(conversation_id),
            "events": events,
            "next_token": meta.get("next_token"),
            "decryption": {
                "status": "not_configured",
                "detail": (
                    "X Chat events are encrypted. Configure an X Chat passphrase "
                    "or exported private-key blob to return plaintext."),
            },
        }
        if not self.can_decrypt:
            return result

        try:
            chat = await self._ensure_chat()
            ids = list(participant_ids or [])
            if not ids:
                conversation = (await self.get_conversation(
                    conversation_id)).get("data") or {}
                ids.extend(conversation.get("participant_ids") or [])
                ids.extend(conversation.get("member_ids") or [])
            ids.append(await self.get_me())
            signing_keys = await self._signing_keys(ids)
            chat.set_signing_keys(signing_keys)
            encoded = list(meta.get("conversation_key_events") or [])
            encoded.extend(e.get("encoded_event") for e in events
                           if e.get("encoded_event"))
            decrypted = await asyncio.to_thread(chat.decrypt_events, encoded)
            result["messages"] = list(decrypted.get("messages") or [])
            result["decryption"] = {
                "status": "ok",
                "errors": decrypted.get("errors") or {},
            }
        except Exception as exc:
            result["decryption"] = {"status": "failed", "detail": str(exc)}
        return result

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None
        self._owns_session = False
