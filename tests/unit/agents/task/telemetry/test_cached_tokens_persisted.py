"""Regression: the persisted llm_usage JSON must record cached_tokens.

During the 2026-06-20 multi-model cost study, telemetry was found to compute a
cache-aware ``cost_estimate`` per call but persist only ``prompt/completion_tokens``
+ ``cost_estimate`` — NOT ``cached_tokens``. That makes any offline cost recompute
from the ``llm_usage`` JSON cache-blind (it overstates cost ~2x for models with
prompt caching, e.g. GLM-5.2). Persisting ``cached_tokens`` makes per-call cost
auditable from the files.
"""
import glob
import json


def test_capture_llm_usage_persists_cached_tokens(tmp_path, monkeypatch):
    from agents.task.path import PathManager
    import agents.task.telemetry.service as svc

    test_pm = PathManager(data_root=str(tmp_path))
    monkeypatch.setattr(svc, "pm", lambda: test_pm)

    t = svc.ProductTelemetry()
    t.posthog_enabled = False  # feed writing stays on → telemetry enabled

    t.capture_llm_usage(
        component="agent",
        purpose="next_action",
        model_name="z-ai/glm-5.2",
        duration_seconds=1.0,
        success=True,
        prompt_tokens=10000,
        completion_tokens=200,
        cached_tokens=8192,
        session_id="sess-cache-test",
    )

    files = glob.glob(str(tmp_path / "**" / "llm_usage" / "*.json"), recursive=True)
    assert files, "no llm_usage JSON written"
    data = json.load(open(files[0]))
    assert data.get("cached_tokens") == 8192, (
        f"cached_tokens not persisted; keys={list(data.keys())}")
