"""A cheap-map aux model must stay on the session's own provider (2026-09-15).

The bug this pins: ``resolve_aux_chain`` returned the cheap model with
``provider=None``, and ``_create_llm_from_config_async`` then auto-detects the
provider by looking the model up in the registry. Every GLM id in that registry is
owned by ``openrouter``, so a ``zai-coding`` session on a FLAT-RATE seat would have
sent its compaction / judge / reflection calls to a METERED provider — a real charge,
with nothing in the logs naming the move.
"""
import pytest

from agents.task.constants import AUX_MODEL_MAP, resolve_aux_chain


@pytest.fixture(autouse=True)
def _clean_aux_env(monkeypatch):
    for name in (
        "AUX_AUTO", "COMPACTION_AUTO_AUX", "AUX_PROVIDER", "COMPACTION_PROVIDER",
        "COMPACTION_MODEL", "AUX_MODEL_JUDGE",
    ):
        monkeypatch.delenv(name, raising=False)
    for slot in ("COMPACTION", "JUDGE", "REFLECTION"):
        for prefix in ("AUX_MODEL_", "AUX_PROVIDER_", "AUX_FALLBACK_"):
            monkeypatch.delenv(prefix + slot, raising=False)


class TestZaiSeatRouting:
    def test_map_has_both_zai_rows(self):
        assert AUX_MODEL_MAP["zai-coding"] == "glm-5.3-flash"
        assert AUX_MODEL_MAP["zai"] == "glm-5.3-flash"

    @pytest.mark.parametrize("task", ["compaction", "judge", "reflection"])
    def test_every_aux_slot_stays_on_the_flat_rate_seat(self, task, monkeypatch):
        monkeypatch.setenv("AUX_AUTO", "true")
        chain = resolve_aux_chain(task, provider="zai-coding")
        assert chain, f"{task} must resolve a cheap model on a zai-coding session"
        assert chain[0] == {"model": "glm-5.3-flash", "provider": "zai-coding"}

    def test_unconfigured_is_unchanged(self):
        # No AUX_AUTO => no aux model => the main model. Byte-identical to legacy.
        assert resolve_aux_chain("judge", provider="zai-coding") == []


class TestPrecedenceAndBlastRadius:
    def test_explicit_provider_env_still_wins(self, monkeypatch):
        monkeypatch.setenv("AUX_AUTO", "true")
        monkeypatch.setenv("AUX_PROVIDER_JUDGE", "openrouter")
        chain = resolve_aux_chain("judge", provider="zai-coding")
        assert chain[0]["provider"] == "openrouter"

    def test_generic_aux_provider_env_still_wins(self, monkeypatch):
        monkeypatch.setenv("AUX_AUTO", "true")
        monkeypatch.setenv("AUX_PROVIDER", "openrouter")
        assert resolve_aux_chain("judge", provider="zai-coding")[0]["provider"] == "openrouter"

    def test_an_explicit_model_is_not_pinned(self, monkeypatch):
        # Only a model that CAME FROM the cheap map is pinned. An operator naming
        # some other model keeps the legacy auto-detect.
        monkeypatch.setenv("AUX_MODEL_JUDGE", "gpt-5-mini")
        chain = resolve_aux_chain("judge", provider="zai-coding")
        assert chain[0] == {"model": "gpt-5-mini", "provider": None}

    @pytest.mark.parametrize("provider,model", [
        ("anthropic", "claude-haiku-4-5"),
        ("openai", "gpt-5-mini"),
        ("gemini", "gemini-flash"),
    ])
    def test_spec_backed_rows_pin_to_themselves(self, provider, model, monkeypatch):
        # Equivalent to the legacy auto-detect for these three, just explicit.
        monkeypatch.setenv("AUX_AUTO", "true")
        chain = resolve_aux_chain("judge", provider=provider)
        assert chain[0] == {"model": model, "provider": provider}

    def test_explicit_model_equal_to_the_map_value_is_not_pinned(self, monkeypatch):
        # Regression: provenance was first decided by comparing the resolved model
        # against the map value, which mislabels a deliberate operator override that
        # happens to name the same string. An explicit env is not an auto-route.
        monkeypatch.setenv("AUX_MODEL_COMPACTION", "gpt-5-mini")
        monkeypatch.setenv("AUX_PROVIDER_COMPACTION", "openai")
        monkeypatch.setenv("AUX_MODEL_REFLECTION", "claude-haiku-4-5")
        chain = resolve_aux_chain("reflection", "anthropic")
        assert chain[0] == {"model": "claude-haiku-4-5", "provider": None}

    def test_reflection_inherits_the_pin_from_compaction(self, monkeypatch):
        # Reflection with no env of its own inherits compaction's cheap-map model —
        # and must inherit the seat pin with it, or it lands on openrouter.
        monkeypatch.setenv("AUX_AUTO", "true")
        chain = resolve_aux_chain("reflection", provider="zai-coding")
        assert chain[0] == {"model": "glm-5.3-flash", "provider": "zai-coding"}

    def test_google_row_keeps_legacy_autodetect(self, monkeypatch):
        # "google" is not a provider SPEC name, so pinning it would look up a
        # "google_client" that does not exist and kill a working aux path.
        monkeypatch.setenv("AUX_AUTO", "true")
        chain = resolve_aux_chain("judge", provider="google")
        assert chain[0] == {"model": "gemini-flash", "provider": None}
