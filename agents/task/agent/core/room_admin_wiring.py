"""101: the room seat of one session, and the probes core may not make itself."""
import logging
from typing import Any

from core.env import bool_env
from core.surfaces.room_seat import RoomSeat, make_admin_post_hook

logger = logging.getLogger(__name__)


def _files_ok(orchestrator: Any) -> bool:
    """False when the session workspace is a shared project folder."""
    from agents.task.path import pm
    return not getattr(pm(), "_workspace_is_project_root", True)


def _browser_ok(orchestrator: Any) -> bool:
    """True only for a REMOTE browser (``BROWSER_CDP_URL`` / ``BROWSER_WSS_URL``).

    ⚠️ 101 R9, R20: a local Chromium is on this host's network, and the SSRF route guard
    does not see a redirect or a WebSocket: a 302 from a public page loaded a loopback
    page in a real browser. No flag on this box proves an egress firewall, so a room
    gets a browser only where the host's network is not its network. A desktop Chrome
    attach reuses the logged-in profile. Any fault reads False.
    """
    # The same strict read as tools/browser/_flags.py (the agents tier may not import it).
    if bool_env("BROWSER_ALLOW_PRIVATE_URLS", False, strict=True):
        return False
    container = getattr(orchestrator, "container", None)
    manager = container.get_service("browser_manager") if container is not None else None
    config = getattr(manager, "browser_config", None)
    if not (getattr(config, "cdp_url", None) or getattr(config, "wss_url", None)):
        return False
    live = getattr(getattr(manager, "browser", None), "browser_config", None)
    return not getattr(live, "chrome_instance_path", None)


def room_seat_for(controller: Any, orchestrator: Any) -> RoomSeat:
    """The seat the room gate reads; registers the taint and audit hook once."""
    seat = RoomSeat(orchestrator,
                    files_ok=lambda: _files_ok(orchestrator),
                    browser_ok=lambda: _browser_ok(orchestrator))

    def _caps():
        container = getattr(orchestrator, "container", None)
        return container.get_service("room_caps") if container is not None else None

    # 101 R8: an admin session's room_read and room_moderate see their own room only.
    controller._room_scope = lambda: seat.room() if seat.stamp else None
    if hasattr(controller, "register_post_tool_call_hook"):
        controller.register_post_tool_call_hook(make_admin_post_hook(seat, _caps),
                                                key="room_admin_taint")
    return seat
