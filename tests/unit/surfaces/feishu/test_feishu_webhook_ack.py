"""OS6 — the Feishu webhook acks before the turn (journal) and its message-id
dedup covers Feishu's retry schedule (15 s, 5 min, 1 h, 6 h): a slow turn no
longer draws a re-signed (fresh) retry that re-runs after a 600 s dedup."""
import asyncio

import surfaces.feishu.harness as fh
from surfaces.feishu.webhook import DEDUP_WINDOW_S, FeishuWebhook


def test_dedup_covers_the_six_hour_retry():
    assert DEDUP_WINDOW_S > 6 * 3600


def test_ack_before_turn():
    assert FeishuWebhook.ack_before_turn is True


def test_built_webhook_has_a_journal_and_the_wide_window(tmp_path, monkeypatch):
    class _Client:
        def __init__(self, *a, **kw):
            self.base = "https://open.larksuite.com"

        async def bot_info(self):
            return {"open_id": "ou_bot"}

        async def send_message(self, chat_id, text):
            return None

    monkeypatch.setattr(fh, "FeishuClient", _Client)
    hook, _ = asyncio.run(fh.build_feishu_webhook(
        None, None, app_id="a", app_secret="b", encrypt_key="k",
        data_dir=str(tmp_path)))
    assert hook._journal is not None
    assert hook._idem.window_seconds >= DEDUP_WINDOW_S
