"""``devtools/catalog_diff.py``: dead and wrong catalog rows against the live list (106)."""
import importlib.util
import sys
from pathlib import Path

from modules.llm.model_types import ModelCapabilities, ModelConfig, ModelPricing, ModelProvider

_path = Path(__file__).resolve().parents[2] / "devtools" / "catalog_diff.py"
_spec = importlib.util.spec_from_file_location("catalog_diff", _path)
catalog_diff = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = catalog_diff  # dataclasses read the module of a deferred annotation
_spec.loader.exec_module(catalog_diff)


def _row(name="v/m", ctx=1000, out=100, price=(1.0, 0.1, 2.0), vision=False, tools=True):
    return ModelConfig(
        name=name, provider=ModelProvider.OPENROUTER, context_window=ctx, max_completion_tokens=out,
        pricing=ModelPricing(input_price=price[0], cached_input_price=price[1], output_price=price[2]),
        capabilities=ModelCapabilities(supports_vision=vision, supports_tools=tools))


def _live(ctx=1000, out=100, price=(1.0, 0.1, 2.0), image=False, tools=True):
    return {"context_length": ctx, "top_provider": {"max_completion_tokens": out},
            "pricing": {"prompt": str(price[0] / 1e6), "input_cache_read": str(price[1] / 1e6),
                        "completion": str(price[2] / 1e6)},
            "architecture": {"input_modalities": ["text"] + (["image"] if image else [])},
            "supported_parameters": ["tools"] if tools else []}


def _no_upstreams(_):
    raise AssertionError("a row at the list price must not read the upstreams")


def test_a_row_that_matches_is_clean():
    assert catalog_diff.diff_rows([_row()], {"v/m": _live()}, _no_upstreams) == []


def test_a_dead_row_is_flagged():
    found = catalog_diff.diff_rows([_row()], {}, _no_upstreams)
    assert [(f.kind, f.model) for f in found] == [("dead", "v/m")]


def test_sizes_vision_and_tools_are_flagged():
    live = _live(ctx=500, out=50, image=True, tools=False)
    text = " | ".join(str(f) for f in catalog_diff.diff_rows([_row()], {"v/m": live}, _no_upstreams))
    for needle in ("context 1000 vs live 500", "max output 100 vs live 50",
                   "vision False vs live True", "supports_tools"):
        assert needle in text


def test_a_small_window_difference_is_the_same_window():
    assert catalog_diff.diff_rows([_row(ctx=1000)], {"v/m": _live(ctx=1024)}, _no_upstreams) == []


def test_a_price_some_upstream_serves_is_not_wrong():
    served = {"prompt": (0.5, 1.5), "completion": (1.0, 3.0), "input_cache_read": (0.05, 0.2)}
    found = catalog_diff.diff_rows([_row()], {"v/m": _live(price=(0.5, 0.05, 1.0))},
                                   lambda _: served)
    assert found == []


def test_a_price_no_upstream_serves_is_wrong():
    served = {"prompt": (5.0, 6.0), "completion": (2.0, 2.0)}
    found = catalog_diff.diff_rows([_row()], {"v/m": _live(price=(5.0, 0.1, 2.0))},
                                   lambda _: served)
    assert [str(f) for f in found] == [
        "wrong v/m: input price 1 is outside the served range 5-6"]


def test_unreadable_upstreams_are_named():
    def boom(_):
        raise OSError("down")
    found = catalog_diff.diff_rows([_row()], {"v/m": _live(price=(9.0, 0.1, 2.0))}, boom)
    assert "unreadable" in str(found[0])


def test_the_live_shape_reads_into_ranges(monkeypatch):
    monkeypatch.setattr(catalog_diff, "_get_json", lambda url: {"data": {"endpoints": [
        {"pricing": {"prompt": "0.000001", "completion": "0.000002"}},
        {"pricing": {"prompt": "0.000003", "completion": "-1", "input_cache_read": "0.0000001"}},
    ]}})
    assert catalog_diff.fetch_endpoint_prices("v/m") == {
        "prompt": (1.0, 3.0), "completion": (2.0, 2.0), "input_cache_read": (0.1, 0.1)}
