"""One map from the surface a line arrived on to its seat (audit via and turn seat)."""
from core.surfaces.seat_names import seat_via, turn_seat


def test_the_audit_via_names_the_surface():
    assert seat_via("telegram") == "telegram"
    assert seat_via("webview") == "console"
    assert seat_via("slack") == "slack"
    assert seat_via("") == "telegram"


def test_the_turn_seat_is_a_known_seat_or_owner():
    assert turn_seat("telegram") == "telegram"
    assert turn_seat("webview") == "console"
    assert turn_seat("cli") == "repl" and turn_seat("repl") == "repl"
    assert turn_seat("slack") == "owner" and turn_seat("discord") == "owner"


def test_both_seats_read_this_map():
    from tools.controller.call_card_run import seat_of
    from surfaces.telegram.status_ops import _seat_via
    from types import SimpleNamespace as NS
    assert seat_of("webview") == "console"
    src = NS(surface_id="webview")
    assert _seat_via(NS(inbound=NS(identity=NS(source=src)))) == "console"
