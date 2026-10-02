"""OS2 — a WhatsApp message whose (signed) ``timestamp`` is stale is dropped:
Meta retries for up to 7 days and a captured body re-verifies forever, while
the dedup only remembers a few minutes. Same rule as the Feishu webhook."""
import os
import time

from core.surfaces.idempotency import IdempotencyStore
import surfaces.whatsapp.inbound as wai
from surfaces.whatsapp.inbound import WhatsAppInbound


class _UD:
    def resolve_internal(self, raw, surface):
        return "u_" + raw


def _payload(ts):
    m = {"id": "wamid.1", "from": "15550001111", "type": "text",
         "text": {"body": "/send 1 ETH go"}}
    if ts is not None:
        m["timestamp"] = str(ts)
    return {"entry": [{"changes": [{"value": {"messages": [m]}}]}]}


def _wa(tmp_path):
    return WhatsAppInbound(IdempotencyStore(os.path.join(tmp_path, "i.db")),
                           user_directory=_UD())


def test_stale_message_is_dropped(tmp_path):
    old = int(time.time()) - wai.REPLAY_WINDOW_S - 60
    assert _wa(tmp_path).parse(_payload(old)) == []


def test_fresh_message_is_parsed(tmp_path):
    assert len(_wa(tmp_path).parse(_payload(int(time.time()) - 5))) == 1


def test_dedup_window_covers_the_replay_window():
    assert wai.DEDUP_WINDOW_S > wai.REPLAY_WINDOW_S


def test_harness_dedup_uses_the_wide_window(tmp_path):
    from tests.unit.surfaces.whatsapp.test_harness_wiring import _Container
    from surfaces.whatsapp.harness import build_whatsapp_harness
    c = _Container()
    build_whatsapp_harness(c, task_agent=object(), data_dir=str(tmp_path))
    inbound = c.get_service("webhook_surfaces")["whatsapp"]
    assert inbound._idem.window_seconds >= wai.DEDUP_WINDOW_S
