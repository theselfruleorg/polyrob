"""064 order 0003 — the WebhookSurface ``decode_payload`` hook, and the Feishu
webhook fallback that rides it (url_verification plain + encrypted, signed
events, a bad ciphertext refused without a turn). Offline recorded shapes."""
import asyncio
import json
import os
import time

import pytest

from core.surfaces.idempotency import AcceptedEventJournal, IdempotencyStore
from core.surfaces.inbound_webhook import WebhookResponse, WebhookSurface
from surfaces.feishu.webhook import FeishuWebhook, decrypt, encrypt, signature

KEY = "test-encrypt-key-0003"
TOKEN = "vtok-0003"
BOT = "ou_bot0000000000000000000000000000"
USER = "ou_user000000000000000000000000"
IV = b"0123456789abcdef"


def _event(message_id="om_1", text="hello", token=TOKEN):
    return {"schema": "2.0",
            "header": {"event_id": "ev_1", "event_type": "im.message.receive_v1",
                       "token": token, "app_id": "cli_test"},
            "event": {"sender": {"sender_id": {"open_id": USER}, "sender_type": "user"},
                      "message": {"message_id": message_id, "chat_id": "oc_dm1",
                                  "chat_type": "p2p", "message_type": "text",
                                  "content": json.dumps({"text": text}), "mentions": []}}}


def _challenge(token=TOKEN):
    return {"challenge": "ch-123", "token": token, "type": "url_verification"}


def _signed(body: bytes, key=KEY, ts=None):
    ts = str(int(time.time())) if ts is None else str(ts)
    return {"X-Lark-Request-Timestamp": ts, "X-Lark-Request-Nonce": "n1",
            "X-Lark-Signature": signature(ts, "n1", key, body)}


class _Recorder(FeishuWebhook):
    """Records the payloads that reached parse (a turn would run for each)."""

    def __init__(self, tmp_path, **kw):
        super().__init__(IdempotencyStore(str(tmp_path / "d.db")), **kw)
        self.parsed = []

    def parse(self, payload):
        self.parsed.append(payload)
        return []


def _post(hook, body: bytes, headers=None):
    return asyncio.run(hook.handle_post(None, headers or {}, body, None))


# --- the crypto --------------------------------------------------------------

def test_decrypt_is_the_inverse_of_the_platform_encryption():
    blob = encrypt(KEY, {"a": 1}, IV)
    assert decrypt(KEY, blob) == {"a": 1}
    with pytest.raises(Exception):
        decrypt("other-key", blob)
    with pytest.raises(Exception):
        decrypt(KEY, "not base64 !!")


# --- the hook on the base class ----------------------------------------------

class _Rot(WebhookSurface):
    """A toy encrypted surface: the payload is {"enc": <reversed json>}."""
    surface_id = "rot"

    def __init__(self, tmp_path, journal=None):
        super().__init__(IdempotencyStore(str(tmp_path / "r.db")), journal=journal)
        self.answered, self.parsed, self.decoded = [], [], 0

    def verify_signature(self, headers, body):
        return True

    def decode_payload(self, payload, headers):
        self.decoded += 1
        if "enc" not in payload:
            return None
        return json.loads(payload["enc"][::-1])

    def answer_post(self, payload):
        self.answered.append(payload)
        if payload.get("type") == "challenge":
            return WebhookResponse.text(payload["value"])
        return None

    def parse(self, payload):
        self.parsed.append(payload)
        return []

    def idempotency_key(self, inbound):
        return ""


def test_the_hook_decodes_once_before_answer_post_and_parse(tmp_path):
    s = _Rot(tmp_path)
    body = json.dumps({"enc": json.dumps({"type": "event", "x": 1})[::-1]}).encode()
    assert _post(s, body) == {"ok": True}
    assert s.decoded == 1
    assert s.answered == [{"type": "event", "x": 1}]
    assert s.parsed == [{"type": "event", "x": 1}]


def test_a_decoded_challenge_is_answered_without_a_turn(tmp_path):
    s = _Rot(tmp_path)
    body = json.dumps({"enc": json.dumps({"type": "challenge", "value": "v"})[::-1]}).encode()
    out = _post(s, body)
    assert isinstance(out, WebhookResponse) and out.body == "v"
    assert s.parsed == []


def test_an_undecodable_payload_is_refused_without_a_turn(tmp_path):
    s = _Rot(tmp_path)
    out = _post(s, json.dumps({"plain": 1}).encode())
    assert isinstance(out, WebhookResponse) and out.status == 400
    assert s.answered == [] and s.parsed == []


def test_a_journal_replay_decodes_the_raw_body_again(tmp_path):
    journal = AcceptedEventJournal(str(tmp_path / "j.db"))
    s = _Rot(tmp_path, journal=journal)
    s.ack_before_turn = True
    body = json.dumps({"enc": json.dumps({"type": "event", "n": 2})[::-1]}).encode()
    journal.accept("rot:k1", "rot", body)
    assert asyncio.run(s._drain(None, None, ["rot:k1"])) == 1
    assert s.parsed == [{"type": "event", "n": 2}]


# --- Feishu: url_verification --------------------------------------------------

def test_a_plain_url_verification_is_answered_with_the_challenge(tmp_path):
    hook = _Recorder(tmp_path, verification_token=TOKEN, allow_unsigned=True)
    out = _post(hook, json.dumps(_challenge()).encode())
    assert isinstance(out, WebhookResponse)
    assert json.loads(out.body) == {"challenge": "ch-123"}
    assert hook.parsed == []


def test_an_encrypted_url_verification_is_answered_unsigned(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY, verification_token=TOKEN)
    body = json.dumps({"encrypt": encrypt(KEY, _challenge(), IV)}).encode()
    out = _post(hook, body)                       # the challenge carries no signature
    assert json.loads(out.body) == {"challenge": "ch-123"}


def test_a_wrong_verification_token_is_refused(tmp_path):
    hook = _Recorder(tmp_path, verification_token=TOKEN, allow_unsigned=True)
    out = _post(hook, json.dumps(_challenge(token="nope")).encode())
    assert isinstance(out, WebhookResponse) and out.status == 400


# --- Feishu: events ------------------------------------------------------------

def test_a_signed_encrypted_event_reaches_parse_decrypted(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY, verification_token=TOKEN)
    body = json.dumps({"encrypt": encrypt(KEY, _event(), IV)}).encode()
    assert _post(hook, body, _signed(body)) == {"ok": True}
    assert hook.parsed and hook.parsed[0]["header"]["event_type"] == "im.message.receive_v1"


def test_an_unsigned_encrypted_event_is_refused(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY)
    body = json.dumps({"encrypt": encrypt(KEY, _event(), IV)}).encode()
    out = _post(hook, body)
    assert isinstance(out, WebhookResponse) and out.status == 400
    assert hook.parsed == []


def test_a_bad_signature_is_refused_before_the_parse(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY)
    body = json.dumps({"encrypt": encrypt(KEY, _event(), IV)}).encode()
    out = _post(hook, body, _signed(body, key="wrong"))
    assert out == {"ok": False, "error": "bad signature"}
    assert hook.parsed == []


@pytest.mark.parametrize("skew", [-301, 301, 86400 * 30])
def test_a_validly_signed_but_stale_event_is_refused_as_a_replay(tmp_path, skew):
    """A captured signed event replayed after the dedup window must not re-run
    the owner's turn: the signature binds the timestamp, so a stale one fails."""
    hook = _Recorder(tmp_path, encrypt_key=KEY, verification_token=TOKEN)
    body = json.dumps({"encrypt": encrypt(KEY, _event(), IV)}).encode()
    out = _post(hook, body, _signed(body, ts=int(time.time()) + skew))
    assert out == {"ok": False, "error": "bad signature"}
    assert hook.parsed == []


def test_a_signed_event_with_a_non_numeric_timestamp_is_refused(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY)
    body = json.dumps({"encrypt": encrypt(KEY, _event(), IV)}).encode()
    out = _post(hook, body, _signed(body, ts="not-a-time"))
    assert out == {"ok": False, "error": "bad signature"}


def test_the_production_dedup_outlives_the_replay_window():
    from surfaces.feishu.webhook import DEDUP_WINDOW_S, REPLAY_WINDOW_S
    assert DEDUP_WINDOW_S >= 2 * REPLAY_WINDOW_S


def test_a_bad_ciphertext_is_refused_without_a_turn(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY)
    body = json.dumps({"encrypt": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="}).encode()
    out = _post(hook, body, _signed(body))
    assert isinstance(out, WebhookResponse) and out.status == 400
    assert hook.parsed == []


def test_plaintext_is_refused_when_encryption_is_on(tmp_path):
    hook = _Recorder(tmp_path, encrypt_key=KEY, verification_token=TOKEN)
    body = json.dumps(_event()).encode()
    out = _post(hook, body, _signed(body))
    assert isinstance(out, WebhookResponse) and out.status == 400


def test_no_secret_configured_refuses_everything(tmp_path):
    hook = _Recorder(tmp_path)
    out = _post(hook, json.dumps(_challenge()).encode())
    assert out == {"ok": False, "error": "bad signature"}


def test_the_real_parse_yields_the_dm_and_dedups_on_message_id(tmp_path):
    hook = FeishuWebhook(IdempotencyStore(str(tmp_path / "d.db")),
                         verification_token=TOKEN, bot_open_id=BOT, allow_unsigned=True)
    [inbound] = hook.parse(_event())
    assert inbound.text == "hello" and inbound.identity.raw_user_id == USER
    assert hook.idempotency_key(inbound) == "feishu:om_1"


# --- launch picks the transport ------------------------------------------------

def _ctx(tmp_path, warns, notes):
    from surfaces._launch import LaunchContext
    return LaunchContext(container=None, task_agent=None, data_dir=str(tmp_path),
                         port=8091, warn=warns.append, note=notes.append)


def test_webhook_transport_without_a_secret_is_skipped_loudly(tmp_path, monkeypatch):
    from surfaces.feishu import launch as mod
    for k, v in {"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b",
                 "FEISHU_TRANSPORT": "webhook"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FEISHU_ENCRYPT_KEY", raising=False)
    monkeypatch.delenv("FEISHU_VERIFICATION_TOKEN", raising=False)
    warns, notes = [], []
    assert asyncio.run(mod.launch(_ctx(tmp_path, warns, notes))) is None
    assert any("FEISHU_ENCRYPT_KEY" in w for w in warns)


def test_webhook_transport_needs_no_sdk_and_serves_on_the_shared_route(tmp_path, monkeypatch):
    from surfaces.feishu import harness, launch as mod

    async def fake_build(container, task_agent, **kw):
        class _C:
            async def close(self):
                return None
        return object(), _C()

    monkeypatch.setattr(harness, "build_feishu_webhook", fake_build)
    for k, v in {"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b",
                 "FEISHU_TRANSPORT": "webhook", "FEISHU_ENCRYPT_KEY": KEY}.items():
        monkeypatch.setenv(k, v)
    warns, notes = [], []
    launched = asyncio.run(mod.launch(_ctx(tmp_path, warns, notes)))
    assert launched is not None and launched.webhook is True and launched.run is None
    assert any("/webhooks/feishu" in n for n in notes)


# -- token-only mode is unsigned: refused unless the operator opts in ---------

def test_token_only_mode_refuses_every_event_without_the_opt_in(tmp_path):
    """A Verification Token alone is a static secret with no signature and no
    timestamp: one leaked body lets anyone type as any user, forever."""
    hook = _Recorder(tmp_path, verification_token=TOKEN)
    body = json.dumps({"type": "url_verification", "token": TOKEN,
                       "challenge": "c1"}).encode()
    assert hook.verify_signature({}, body) is False


def test_the_auth_gap_names_the_missing_key():
    from surfaces.feishu.webhook import UNSIGNED_OPT_IN, webhook_auth_gap
    assert webhook_auth_gap(KEY, "", False) is None
    assert webhook_auth_gap("", TOKEN, True) is None
    gap = webhook_auth_gap("", TOKEN, False)
    assert "FEISHU_ENCRYPT_KEY" in gap and UNSIGNED_OPT_IN in gap
    assert "FEISHU_ENCRYPT_KEY" in webhook_auth_gap("", "", True)


def test_token_only_webhook_launch_is_skipped_loudly(tmp_path, monkeypatch):
    from surfaces.feishu import launch as mod
    for k, v in {"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b",
                 "FEISHU_TRANSPORT": "webhook", "FEISHU_VERIFICATION_TOKEN": TOKEN}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FEISHU_ENCRYPT_KEY", raising=False)
    monkeypatch.delenv("FEISHU_WEBHOOK_ALLOW_UNSIGNED", raising=False)
    warns, notes = [], []
    assert asyncio.run(mod.launch(_ctx(tmp_path, warns, notes))) is None
    assert any("FEISHU_ENCRYPT_KEY is not set" in w for w in warns)


def test_token_only_webhook_launches_with_the_opt_in_and_warns(tmp_path, monkeypatch):
    from surfaces.feishu import harness, launch as mod
    seen = {}

    async def fake_build(container, task_agent, **kw):
        seen.update(kw)

        class _C:
            async def close(self):
                return None
        return object(), _C()

    monkeypatch.setattr(harness, "build_feishu_webhook", fake_build)
    for k, v in {"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b",
                 "FEISHU_TRANSPORT": "webhook", "FEISHU_VERIFICATION_TOKEN": TOKEN,
                 "FEISHU_WEBHOOK_ALLOW_UNSIGNED": "true"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("FEISHU_ENCRYPT_KEY", raising=False)
    warns, notes = [], []
    assert asyncio.run(mod.launch(_ctx(tmp_path, warns, notes))) is not None
    assert seen["allow_unsigned"] is True
    assert any("NOT signed" in w for w in warns)


def test_the_probe_names_the_missing_encrypt_key():
    from surfaces.feishu.probe import probe
    res = asyncio.run(probe({"FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "b",
                             "FEISHU_TRANSPORT": "webhook",
                             "FEISHU_VERIFICATION_TOKEN": TOKEN}))
    assert "FEISHU_ENCRYPT_KEY" in res.render()
