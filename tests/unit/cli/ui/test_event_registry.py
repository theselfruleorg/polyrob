"""D2: a typed-event descriptor registry makes new core events one-line adds.

The extensibility contract: a capability the core adds later (a new feed event
type) surfaces in the CLI with ONE register_event(...) call — no edits to
events.py, state.py, or either renderer. This test registers a synthetic
``payment_made`` event and asserts the whole pipeline (normalize → state.apply →
render in its declared layer) works with zero core edits.
"""

from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from cli.ui import event_registry as er
from cli.ui.events import normalize
from cli.ui.plain_renderer import PlainRenderer
from cli.ui.rich_renderer import RichRenderer
from cli.ui.state import SessionState


@pytest.fixture
def _payment_spec():
    """Register a synthetic event for the duration of one test, then clean up."""
    spec = er.EventSpec(
        type="payment_made",
        parse=lambda d: er.RegisteredEvent(
            type="payment_made",
            data={"amount": float(d.get("data", {}).get("amount_usd", 0.0)),
                  "to": d.get("data", {}).get("recipient", "")},
            raw=d,
        ),
        apply=lambda s, e: setattr(s, "_payments_total",
                                   getattr(s, "_payments_total", 0.0) + e.data["amount"]),
        layer=er.Layer.DIALOG,
        render_line=lambda e: f"paid ${e.data['amount']:.2f} -> {e.data['to']}",
    )
    er.register_event(spec)
    yield spec
    er.unregister_event("payment_made")


def _feed(amount, to):
    return {"type": "payment_made", "data": {"amount_usd": amount, "recipient": to}}


def test_registered_event_normalizes_to_typed(_payment_spec):
    ev = normalize(_feed(12.5, "0xabc"))
    assert isinstance(ev, er.RegisteredEvent)
    assert ev.type == "payment_made"
    assert ev.data["amount"] == 12.5
    assert ev.data["to"] == "0xabc"


def test_registered_event_mutates_state(_payment_spec):
    state = SessionState()
    state.update(normalize(_feed(10.0, "x")))
    state.update(normalize(_feed(2.5, "y")))
    assert state._payments_total == 12.5


def test_unregistered_event_still_falls_back_to_info():
    """Without a spec, an unknown type is the harmless Info fallback (no crash)."""
    from cli.ui.events import Info
    ev = normalize({"type": "totally_unknown", "data": {}})
    assert isinstance(ev, Info)


def test_dialog_layer_renders_in_rich(_payment_spec):
    buf = StringIO()
    console = Console(file=buf, width=80, no_color=True, highlight=False)
    r = RichRenderer(SessionState(), console=console)
    r.on_event(normalize(_feed(7.0, "0xfeed")))
    out = buf.getvalue()
    assert "paid $7.00 -> 0xfeed" in out


def test_dialog_layer_renders_in_plain(_payment_spec):
    buf = StringIO()
    r = PlainRenderer(SessionState(), stream=buf)
    r.on_event(normalize(_feed(3.0, "0xcafe")))
    assert "paid $3.00 -> 0xcafe" in buf.getvalue()


def test_trace_layer_hidden_unless_verbose():
    spec = er.EventSpec(
        type="debug_ping",
        parse=lambda d: er.RegisteredEvent(type="debug_ping", data={}, raw=d),
        layer=er.Layer.TRACE,
        render_line=lambda e: "ping",
    )
    er.register_event(spec)
    try:
        buf = StringIO()
        console = Console(file=buf, width=80, no_color=True, highlight=False)
        r = RichRenderer(SessionState(), console=console)
        r.on_event(normalize({"type": "debug_ping", "data": {}}))
        assert "ping" not in buf.getvalue()  # trace hidden by default
        r.verbose = True
        r.on_event(normalize({"type": "debug_ping", "data": {}}))
        assert "ping" in buf.getvalue()      # shown under verbose
    finally:
        er.unregister_event("debug_ping")
