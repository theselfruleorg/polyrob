"""R-4: core owns the inbound-act contract; surfaces.telegram registers the impl.

core.surfaces.inbound_webhook used to import surfaces.telegram.{harness,inbound}
at the TOP level — a core→surfaces upward edge. The contract (InboundResult +
act_on_inbound) now lives in core.surfaces.act; the telegram harness registers
the shared RouteDecision→TaskAgent dispatch at import time.
"""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def test_inbound_webhook_has_no_surfaces_import():
    tree = ast.parse((ROOT / "core" / "surfaces" / "inbound_webhook.py").read_text())
    offenders = [
        n.module for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("surfaces.")
    ] + [
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
        for a in n.names if a.name.startswith("surfaces.")
    ]
    assert not offenders, f"core module imports the surface tier: {offenders}"


def test_inboundresult_identity_preserved():
    from core.surfaces.act import InboundResult as canonical
    from surfaces.telegram.inbound import InboundResult as via_telegram
    assert canonical is via_telegram


@pytest.mark.asyncio
async def test_unregistered_actor_raises():
    import core.surfaces.act as act
    orig = act._INBOUND_ACTOR
    act._INBOUND_ACTOR = None
    try:
        with pytest.raises(RuntimeError, match="no inbound actor"):
            await act.act_on_inbound(object(), object())
    finally:
        act._INBOUND_ACTOR = orig


def test_telegram_harness_registers_actor():
    import surfaces.telegram.harness as harness
    import core.surfaces.act as act
    assert act._INBOUND_ACTOR is harness.act_on_inbound


def test_whatsapp_inbound_registers_actor_transitively():
    """A concrete WebhookSurface module must guarantee the actor is registered."""
    import surfaces.whatsapp.inbound  # noqa: F401
    import core.surfaces.act as act
    assert act._INBOUND_ACTOR is not None
