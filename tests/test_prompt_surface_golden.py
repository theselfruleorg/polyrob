"""081 WS-0.1 — the committed prompt surface is the one the code renders.

``tests/golden/prompt_surface/`` holds, per surface (owner Telegram DM, owner
REPL, cron, goal, room, correspondent, leaf), the system text the model
receives (one header per foundation layer, then the step-1 blocks), the
``tools[]`` it is offered, and ``sizes.json`` (chars and ``chars // 4``
tokens per layer). A change to prompt text or to a tool schema must
regenerate it in the same commit, so the diff shows what moved and what it
costs::

    python devtools/gen_prompt_surfaces.py

On a git conflict inside the golden, do not merge it by hand: take either
side and regenerate. ``_REGEN=1 pytest tests/test_prompt_surface_golden.py``
rewrites it too.
"""
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "gen_prompt_surfaces", ROOT / "devtools" / "gen_prompt_surfaces.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


GEN = _generator()


@pytest.fixture(scope="module")
def fresh(tmp_path_factory):
    out = tmp_path_factory.mktemp("prompt_surface") / "fresh"
    GEN.render(out)
    return out


def test_the_committed_prompt_surface_is_current(fresh):
    if os.environ.get("_REGEN") == "1":  # pragma: no cover - the rewrite path
        if GEN.GOLDEN.exists():
            shutil.rmtree(GEN.GOLDEN)
        shutil.copytree(fresh, GEN.GOLDEN)
        return
    diff = GEN.differences(GEN.GOLDEN, fresh)
    assert not diff, (
        "the prompt-surface golden is stale:\n  " + "\n  ".join(diff[:30])
        + "\nsize change (tokens = chars // 4):\n" + (GEN.size_delta(GEN.GOLDEN, fresh)
                                                      or "  (sizes unchanged)")
        + f"\nregenerate: {GEN.REGEN}")


def test_every_surface_renders_its_foundation_and_tools(fresh):
    sizes = json.loads((fresh / "sizes.json").read_text(encoding="utf-8"))
    for surface in GEN.SURFACES:
        text = (fresh / f"{surface}.system.txt").read_text(encoding="utf-8")
        assert text.startswith("===== system_prompt =====\n"), surface
        assert "===== tool_catalog =====" in text, surface
        tools = json.loads((fresh / f"{surface}.tools.json").read_text(encoding="utf-8"))
        assert tools["tools"] and sizes[surface]["tools"]["count"] == len(tools["tools"])
        for row in tools["tools"]:
            assert ((fresh / "tools" / f"{row['name']}.json").exists()
                    or (fresh / "tools" / f"{row['name']}.{surface}.json").exists())


def test_the_render_names_the_regime_it_is_for(fresh):
    """The surfaces are not seven copies of one session: a room and a
    correspondent never carry the owner's self-context, and the leaf is a child."""
    def text(s):
        return (fresh / f"{s}.system.txt").read_text(encoding="utf-8")
    for public in ("room", "correspondent"):
        assert "===== self_context" not in text(public)
    assert "===== self_context" in text("owner_telegram_dm")
    assert text("leaf") != text("owner_telegram_dm")


def test_size_delta_names_the_layer_and_the_tools(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for d, chars, count in ((a, 400, 3), (b, 800, 4)):
        d.mkdir()
        (d / "sizes.json").write_text(json.dumps({"cron": {
            "layers": {"skill_catalog": {"chars": chars}, "system_prompt": {"chars": 10}},
            "tools": {"count": count, "tokens": count * 100},
            "total_tokens": chars // 4}}))
    delta = GEN.size_delta(a, b)
    assert "cron" in delta and "skill_catalog: 400 -> 800 chars (+100 tok)" in delta
    assert "system_prompt" not in delta and "3 -> 4 tools" in delta
    assert GEN.size_delta(a, a) == ""


def test_the_prod_variant_renders_the_owner_dm_with_the_prod_grants(fresh):
    """081 WS-3.1: the owner DM is also measured under the prod posture (the armed
    money rail, compute, the X rails), so its size is the size prod sends."""
    sizes = json.loads((fresh / "sizes.json").read_text(encoding="utf-8"))
    for surface in GEN.PROD_SURFACES:
        name = GEN.PROD_PREFIX + surface
        assert name in sizes, name
        names = [t["name"] for t in json.loads(
            (fresh / f"{name}.tools.json").read_text(encoding="utf-8"))["tools"]]
        assert any(n.startswith("defi_trade_") for n in names), name
        assert sizes[name]["tools"]["count"] > sizes[surface]["tools"]["count"]


def test_the_owner_first_call_is_at_most_45k_under_the_prod_posture(fresh):
    """081 bar 3 / WS-3.1 acceptance: the owner's first call (system + tool
    schemas) under the prod posture is at most 45K tokens (was 58K here, 90K
    measured on prod with its larger identity docs).

    093 (2026-10-28) raised it to 45.7K for the operational self-awareness
    surface (propose_setting + agent_status(tool=), the pinned harness manual,
    the owner-seat harness rule) after trimming the rest; trim, don't raise."""
    sizes = json.loads((fresh / "sizes.json").read_text(encoding="utf-8"))
    assert sizes[GEN.PROD_PREFIX + "owner_telegram_dm"]["total_tokens"] <= 45_700
