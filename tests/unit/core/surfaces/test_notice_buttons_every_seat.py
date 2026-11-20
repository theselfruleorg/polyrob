"""A proactive notice's buttons reach every chat seat, not only Telegram: a
Slack or Discord owner taps Confirm / "Show the job" like a Telegram owner.
Email cannot tap, so it gets no actions (its footer names the taps)."""
import pytest

from core.surfaces.envelopes import Action


def _container(seen):
    class Router:
        async def send_message_ex(self, chat, text, surface_id="telegram", **kw):
            seen.append({"chat": chat, "surface": surface_id, **kw})
            return "sent"

    class C:
        def get_service(self, n):
            return {"message_router": Router()}.get(n)

    return C()


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["slack", "discord"])
async def test_a_notice_with_buttons_carries_them_on_a_chat_seat(surface):
    from core.surfaces import user_delivery as ud
    seen = []
    out = await ud.deliver_user_message(
        _container(seen), "u1", "Job failed.", source="cron", recipient_override="C1",
        recipient_surface=surface, buttons=[Action("Show the job", "/status")],
        event_log=None)
    assert out == "sent", out
    assert seen and seen[0]["surface"] == surface
    assert [a.command for a in seen[0].get("actions") or []] == ["/status"]


@pytest.mark.asyncio
async def test_email_gets_no_actions():
    from core.surfaces import user_delivery as ud
    seen = []
    await ud.deliver_user_message(
        _container(seen), "u1", "Job failed.", source="cron", recipient_override="o@x.test",
        recipient_surface="email", buttons=[Action("Show the job", "/status")],
        event_log=None)
    assert seen and "actions" not in seen[0]
