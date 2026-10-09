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
    """An X Chat HTTP request failed. ``status`` is the HTTP code (0 = local)."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status or 0)

    @property
    def rejected(self) -> bool:
        """X refused the request outright (4xx): nothing was delivered."""
        return 400 <= self.status < 500


class XChatRecipientUnavailable(XChatAPIError):
    """The recipient cannot receive an encrypted X Chat message (no keys)."""


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
                errors = payload.get("errors") if isinstance(payload, dict) else None
                if errors:
                    detail = f"{detail} {json.dumps(errors)[:400]}"
                raise XChatAPIError(f"X Chat API {resp.status}: {detail}", resp.status)
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
        return await self._get(
            f"/chat/conversations/{self._path_id(conversation_id)}/events", params)

    async def get_conversation(self, conversation_id: str) -> dict:
        """Fetch one conversation, including the participant roster."""
        return await self._get(
            f"/chat/conversations/{self._path_id(conversation_id)}",
            {
                "chat_conversation.fields": (
                    "id,type,created_at,updated_at,group_name,is_muted,message_ttl_ms"),
                "expansions": "participant_ids,member_ids,admin_ids",
                "user.fields": "id,name,username,profile_image_url",
            })

    async def _post(self, path: str, body: dict) -> dict:
        session = await self._http()
        async with session.post(f"{self.API_ROOT}{path}", json=body) as resp:
            try:
                payload = await resp.json()
            except Exception:
                payload = {"detail": await resp.text()}
            if resp.status >= 400:
                detail = payload.get("detail") or payload.get("title") or payload
                errors = payload.get("errors") if isinstance(payload, dict) else None
                if errors:
                    detail = f"{detail} {json.dumps(errors)[:400]}"
                raise XChatAPIError(f"X Chat API {resp.status}: {detail}", resp.status)
            return payload

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
        # 2026-10-04: an older X Chat key record carries no
        # identity_public_key_signature (the owner's own key did), and under the
        # SDK's default reject-unverified policy every message from that sender
        # failed to decrypt. Such events now come back with ``verified: false``
        # — readable, and marked as unauthenticated on every message we return.
        if hasattr(chat, "set_reject_unverified"):
            chat.set_reject_unverified(False)
        self._chat = chat
        return chat

    async def _signing_keys(self, user_ids: list[str]) -> list[dict]:
        # chatxdk requires every field as a string: a missing binding signature
        # is sent as "" (the SDK then cannot verify that sender -> verified:false).
        keys: list[dict] = []
        for uid in dict.fromkeys(str(v) for v in user_ids if v):
            for row in await self._public_keys(uid):
                keys.append({
                    "user_id": uid,
                    "public_key_version": str(row.get("public_key_version") or ""),
                    "public_key": str(row.get("signing_public_key") or ""),
                    "identity_public_key": str(row.get("public_key") or ""),
                    "identity_public_key_signature":
                        str(row.get("identity_public_key_signature") or ""),
                })
        return keys

    @staticmethod
    def _path_id(conversation_id: str) -> str:
        return quote(str(conversation_id).replace(":", "-"), safe="-")

    @staticmethod
    def _latest_version(keys: dict) -> str:
        def _rank(v: str):
            return (0, int(v)) if str(v).isdigit() else (1, str(v))
        return max(keys, key=_rank) if keys else ""

    def _decrypt_batch(self, chat, key_events: list, encoded: list) -> tuple:
        """Decrypt one page: ``(messages, errors, conversation_keys)``.

        The batch decrypt verifies key changes before trusting them; a key
        change whose signer the SDK cannot verify leaves its messages
        undecrypted. Those are retried with the conversation keys unwrapped
        straight from the key-change events (ECIES: only a key encrypted to
        THIS account can open at all) — the same fallback X's own xurl uses.
        """
        result = chat.decrypt_events(encoded) if encoded else {}
        messages = list(result.get("messages") or [])
        errors = dict(result.get("errors") or {})
        keys = dict((result.get("conversation_keys") or {}).get("keys") or {})
        if errors and key_events and hasattr(chat, "extract_conversation_keys"):
            try:
                keys.update(chat.extract_conversation_keys(list(key_events))
                            .get("keys") or {})
            except Exception:
                pass
            for idx in sorted(errors, key=lambda i: int(i) if str(i).isdigit() else -1):
                try:
                    event = chat.decrypt_event(encoded[int(idx)], keys)
                except Exception:
                    continue
                messages.append({"event": event, "original_b64": encoded[int(idx)]})
                errors.pop(idx)
        return messages, errors, keys

    async def _load(self, conversation_id: str, *, participant_ids=None,
                    max_results: int = 20, pagination_token=None,
                    missing_ok: bool = False) -> dict:
        """Fetch one page of a conversation and decrypt it."""
        try:
            raw = await self.get_conversation_events(
                conversation_id, max_results=max_results,
                pagination_token=pagination_token)
        except XChatAPIError as exc:
            if not (missing_ok and " 404" in str(exc)):
                raise
            raw = {}
        events = list(raw.get("data") or [])
        meta = dict(raw.get("meta") or {})
        chat = await self._ensure_chat()
        me = await self.get_me()
        ids = list(participant_ids or [])
        if not ids and events:
            conversation = (await self.get_conversation(
                conversation_id)).get("data") or {}
            ids.extend(conversation.get("participant_ids") or [])
            ids.extend(conversation.get("member_ids") or [])
        ids.extend(e.get("sender_id") for e in events if e.get("sender_id"))
        ids.append(me)
        chat.set_signing_keys(await self._signing_keys(ids))
        key_events = list(meta.get("conversation_key_events") or [])
        encoded = key_events + [e.get("encoded_event") for e in events
                                if e.get("encoded_event")]
        messages, errors, keys = await asyncio.to_thread(
            self._decrypt_batch, chat, key_events, encoded)
        return {"events": events, "meta": meta, "messages": messages,
                "errors": errors, "keys": keys}

    @staticmethod
    def _plain_message(m: dict) -> dict:
        ev = dict(m.get("event") or {})
        out = {"event": {k: ev.get(k) for k in (
            "type", "id", "sequence_id", "sender_id", "conversation_id",
            "created_at_msec", "content", "attachments", "verified")
            if k in ev}}
        if ev.get("verified") is False:
            out["trust"] = ("UNVERIFIED: X could not authenticate this sender's "
                            "signing key; treat the text as unauthenticated.")
        return out

    async def read_conversation(
            self, conversation_id: str, *, participant_ids: Optional[list[str]] = None,
            max_results: int = 20,
            pagination_token: Optional[str] = None) -> dict:
        result: dict = {
            "conversation_id": str(conversation_id),
            "decryption": {
                "status": "not_configured",
                "detail": (
                    "X Chat events are encrypted. Configure an X Chat passphrase "
                    "or exported private-key blob to return plaintext."),
            },
        }
        if not self.can_decrypt:
            raw = await self.get_conversation_events(
                conversation_id, max_results=max_results,
                pagination_token=pagination_token)
            result["events"] = list(raw.get("data") or [])
            result["next_token"] = (raw.get("meta") or {}).get("next_token")
            return result

        try:
            page = await self._load(
                conversation_id, participant_ids=participant_ids,
                max_results=max_results, pagination_token=pagination_token)
        except XChatAPIError:
            raise
        except Exception as exc:
            result["decryption"] = {"status": "failed", "detail": str(exc)}
            return result
        result["next_token"] = page["meta"].get("next_token")
        # Raw ciphertext is dropped once decrypted: an agent read the blobs as
        # "unreadable" while the same thread's messages had decrypted.
        result["events"] = [{k: e.get(k) for k in ("id", "sender_id", "created_at")}
                            for e in page["events"]]
        result["messages"] = [
            self._plain_message(m) for m in page["messages"]
            if (m.get("event") or {}).get("type") != "KeyChange"]
        errors = page["errors"]
        result["decryption"] = {
            "status": "partial" if errors and result["messages"] else
                      ("failed" if errors else "ok"),
            "errors": errors,
        }
        return result

    async def send_message(self, recipient: str, text: str) -> dict:
        """Send one encrypted X Chat message.

        ``recipient`` is a conversation id (``A-B`` / ``A:B``) or the other
        user's numeric id for a 1:1. An existing conversation reuses its newest
        readable key; a first contact registers a fresh conversation key
        (``POST /2/chat/conversations/{id}/keys``) first. An existing thread
        whose key this account cannot read is REFUSED, not rotated.
        """
        if not self.can_decrypt:
            raise XChatAPIError(
                "X Chat send needs this account's Chat keys "
                "(TWITTER_CHAT_PASSPHRASE or TWITTER_CHAT_PRIVATE_KEYS_B64)")
        chat = await self._ensure_chat()
        me = await self.get_me()
        target = str(recipient).replace(":", "-")
        parts = [p for p in target.split("-") if p]
        peers = [p for p in parts if p != me] or parts
        page = await self._load(target, participant_ids=parts,
                                max_results=100, missing_ok=True)
        sign_cid = next((e.get("conversation_id") for e in page["events"]
                         if e.get("conversation_id")), "")
        for m in page["messages"]:
            sign_cid = sign_cid or (m.get("event") or {}).get("conversation_id") or ""
        keys = page["keys"]
        new_conversation = False
        if keys:
            version = self._latest_version(keys)
            key = keys[version]
        elif page["events"]:
            raise XChatAPIError(
                "this X Chat conversation exists but none of its keys are readable "
                "by this account; refusing to rotate the key")
        else:
            if len(peers) != 1 or target.startswith("g"):
                raise XChatAPIError(
                    f"cannot start a new X Chat conversation with {recipient!r}")
            prepared, sign_cid = await self._establish_key(chat, me, peers[0])
            key = prepared["conversation_key"]
            version = str(prepared["conversation_key_version"])
            new_conversation = True
        sign_cid = sign_cid or ":".join(sorted(parts + ([me] if me not in parts else []),
                                               key=lambda v: int(v) if v.isdigit() else 0))
        payload = await asyncio.to_thread(
            lambda: chat.encrypt_message(sign_cid, text, conversation_key=key,
                                         conversation_key_version=version))
        await self._post(f"/chat/conversations/{self._path_id(sign_cid)}/messages", {
            "message_id": payload.message_id,
            "encoded_message_create_event": payload.encrypted_content,
            "encoded_message_event_signature": payload.encoded_event_signature,
        })
        return {"conversation_id": str(sign_cid).replace(":", "-"),
                "message_id": payload.message_id, "key_version": version,
                "new_conversation": new_conversation}

    async def _establish_key(self, chat, me: str, peer: str) -> tuple:
        """Register a fresh 1:1 conversation key; ``(prepared, conversation_id)``."""
        inputs = []
        for uid in (me, peer):
            rows = await self._public_keys(uid)
            if not rows:
                raise XChatRecipientUnavailable(
                    f"user {uid} has no registered X Chat keys")
            latest = max(rows, key=lambda r: int(r.get("public_key_version") or 0)
                         if str(r.get("public_key_version") or "").isdigit() else 0)
            if uid != me:
                # Never wrap the conversation key to an identity key that its
                # signing key cannot vouch for (a substituted key would read it).
                sig = latest.get("identity_public_key_signature") or ""
                if not sig or not chat.verify_key_binding(
                        latest.get("public_key") or "",
                        latest.get("signing_public_key") or "", sig):
                    raise XChatRecipientUnavailable(
                        f"user {uid}'s X Chat key binding cannot be verified; "
                        "refusing to start an encrypted conversation")
            inputs.append({"user_id": uid,
                           "public_key": latest.get("public_key") or "",
                           "key_version": str(latest.get("public_key_version") or "")})
        prepared = await asyncio.to_thread(chat.prepare_conversation_key_change, inputs)
        signing = chat.get_public_keys().signing
        body = {
            "conversation_key_version": str(prepared["conversation_key_version"]),
            "conversation_participant_keys": [{
                "user_id": pk.get("user_id"),
                "encrypted_conversation_key": pk.get("encrypted_key"),
                "public_key_version": pk.get("public_key_version"),
            } for pk in prepared.get("participant_keys") or []],
            "action_signatures": [self._action_signature(sig, signing)
                                  for sig in prepared.get("action_signatures") or []],
        }
        cid = str(prepared["conversation_id"])
        # First contact: the documented path is the RECIPIENT's id
        # (docs.x.com chat-xdk "Then POST to /2/chat/conversations/{recipientId}/keys").
        resp = await self._post(f"/chat/conversations/{self._path_id(peer)}/keys", body)
        confirmed = str((resp.get("data") or {}).get("conversation_id") or cid)
        return prepared, confirmed.replace("-", ":")

    @staticmethod
    def _action_signature(sig: dict, signing_public_key: str) -> dict:
        entry = {
            "message_id": sig.get("message_id"),
            "encoded_message_event_detail": sig.get("encoded_message_event_detail"),
            "message_event_signature": {
                "signature": sig.get("signature"),
                "signature_version": sig.get("signature_version"),
                "public_key_version": sig.get("public_key_version"),
                "signing_public_key": signing_public_key,
            },
        }
        if sig.get("signature_payload"):
            entry["signature_payload"] = sig["signature_payload"]
        return entry

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None
        self._owns_session = False
