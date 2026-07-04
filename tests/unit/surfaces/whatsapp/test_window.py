import os
from core.surfaces.send_policy import SendDecision
from surfaces.whatsapp.window import WindowTracker
from surfaces.whatsapp.surface import WhatsAppSurface


def test_window_open_allows_closed_requires_template(tmp_path):
    wt = WindowTracker(os.path.join(tmp_path, "win.db"))
    wt.touch("15550001111", now=1000.0)
    surf = WhatsAppSurface(client=object())
    surf.attach_window(wt)
    sk = "agent:main:whatsapp:dm:15550001111"
    assert surf.can_send_now(sk, now=1000.0 + 3600) == SendDecision.ALLOW
    assert surf.can_send_now(sk, now=1000.0 + 90000) == SendDecision.TEMPLATE_ONLY


def test_unknown_recipient_requires_template(tmp_path):
    wt = WindowTracker(os.path.join(tmp_path, "win.db"))
    surf = WhatsAppSurface(client=object()); surf.attach_window(wt)
    assert surf.can_send_now("agent:main:whatsapp:dm:999", now=1.0) == SendDecision.TEMPLATE_ONLY
