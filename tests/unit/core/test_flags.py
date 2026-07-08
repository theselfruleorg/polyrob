"""Flag registry (core/flags.py) — Wave D / SA-05: runtime-enumerable env flags.

Contract: every flag row in docs/CONFIGURATION.md is present in the registry
(doc rows ⊆ registry), resolution honors the canonical falsey-set parser, secrets
are masked, and dynamic (posture/local-derived) defaults flow through the hook.
"""
import re
from pathlib import Path

import pytest

from core.flags import REGISTRY, is_secret_flag, resolve_all, resolve_flag

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DOC = REPO_ROOT / "docs" / "CONFIGURATION.md"

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _doc_flag_names() -> set[str]:
    names: set[str] = set()
    for line in CONFIG_DOC.read_text().splitlines():
        if not line.startswith("| `"):
            continue
        first_cell = line.strip().strip("|").split("|")[0]
        for name in re.findall(r"`([^`]+)`", first_cell):
            name = name.strip()
            if _NAME_RE.match(name):
                names.add(name)
    return names


def test_configuration_doc_rows_subset_of_registry():
    doc_names = _doc_flag_names()
    assert doc_names, "CONFIGURATION.md parse yielded no flags — parser or doc broke"
    missing = doc_names - set(REGISTRY)
    assert not missing, (
        f"{len(missing)} flag(s) documented in docs/CONFIGURATION.md but absent from "
        f"core/flags.py registry — run `python scripts/gen_flags_catalog.py` to "
        f"regenerate core/flags_catalog.py: {sorted(missing)[:10]}"
    )


def test_registry_flags_have_group_and_default():
    for flag in REGISTRY.values():
        assert flag.name and flag.group
        assert flag.default_doc is not None


def test_explicit_env_wins_and_uses_falsey_set():
    # REFLECTION_LLM_ENABLED is documented default-ON; explicit falsey wins.
    r = resolve_flag("REFLECTION_LLM_ENABLED", {"REFLECTION_LLM_ENABLED": "off"})
    assert r.value is False
    assert r.source == "env"
    r = resolve_flag("REFLECTION_LLM_ENABLED", {"REFLECTION_LLM_ENABLED": "true"})
    assert r.value is True


def test_unset_bool_flag_resolves_documented_default():
    r = resolve_flag("GEMINI_PROMPT_CACHE", {})
    assert r.value is False
    assert r.source == "default"
    r = resolve_flag("REFLECTION_LLM_ENABLED", {})
    assert r.value is True


def test_int_flag_resolution():
    r = resolve_flag("GEMINI_CACHE_TTL_MIN", {})
    assert r.value == 10
    r = resolve_flag("GEMINI_CACHE_TTL_MIN", {"GEMINI_CACHE_TTL_MIN": "25"})
    assert r.value == 25


def test_secret_flags_masked():
    assert is_secret_flag("OPENAI_API_KEY")
    assert is_secret_flag("MCP_GATEWAY_TOKEN")
    assert not is_secret_flag("GOALS_ENABLED")
    assert not is_secret_flag("LLM_MAX_OUTPUT_TOKENS")
    assert not is_secret_flag("POLYROB_PROJECT_SECRET_REFUSE")
    r = resolve_flag("ANYSITE_API_KEY", {"ANYSITE_API_KEY": "sk-supersecret123"})
    assert "supersecret" not in str(r.value)
    assert r.value == "(set, masked)"
    r = resolve_flag("TELEGRAM_BOT_TOKEN", {})
    assert r.value == "(unset)"


def test_dynamic_default_hook():
    def dyn(name):
        if name == "GOALS_ENABLED":
            return True, "default(posture:test)"
        return None

    r = resolve_flag("GOALS_ENABLED", {}, dynamic_default=dyn)
    assert r.value is True
    assert r.source == "default(posture:test)"
    # explicit env still beats the dynamic default
    r = resolve_flag("GOALS_ENABLED", {"GOALS_ENABLED": "false"}, dynamic_default=dyn)
    assert r.value is False
    assert r.source == "env"


def test_resolve_all_covers_registry_and_groups():
    resolved = resolve_all({})
    assert len(resolved) == len(REGISTRY)
    groups = {r.group for r in resolved}
    assert "LLM / providers" in groups
    assert any("Autonomy" in g for g in groups)


def test_unknown_flag_raises():
    with pytest.raises(KeyError):
        resolve_flag("NOT_A_REAL_FLAG_XYZ", {})


def test_wallet_seed_and_password_hash_masked():
    # PAYMENT_MASTER_SEED is "KEEP SECRET!" wallet seed material; the owner
    # password hash enables offline cracking — doctor --flags must mask both.
    assert is_secret_flag("PAYMENT_MASTER_SEED")
    assert is_secret_flag("MASTER_SEED")
    assert is_secret_flag("POLYROB_OWNER_PASSWORD_HASH")
    r = resolve_flag("PAYMENT_MASTER_SEED", {"PAYMENT_MASTER_SEED": "hunter2seed"})
    assert "hunter2" not in str(r.value) and r.value == "(set, masked)"


def test_int_kind_survives_trailing_prose():
    # "`1440` (**720 POLYROB_LOCAL**)" must resolve 1440, not an opaque string.
    r = resolve_flag("SESSION_IDLE_MINUTES", {})
    assert r.value == 1440


def test_catalog_defaults_never_truncated():
    for flag in REGISTRY.values():
        assert not flag.default_doc.endswith("..."), (
            f"{flag.name} default cell is truncated — the generator must store "
            "full cells (kind/default inference reads them)")


def test_generator_parity_with_checked_in_catalog():
    """The checked-in catalog must be exactly what the checked-in generator
    produces (the standard generated-file contract).

    ``scripts/`` is private-only (never shipped to the public repo — the whole
    directory is on the publish denylist), so skip this parity check when the
    generator is absent. The catalog itself still ships and its contract tests
    above still run everywhere.
    """
    import subprocess
    import sys as _sys
    gen = REPO_ROOT / "scripts" / "gen_flags_catalog.py"
    if not gen.exists():
        import pytest
        pytest.skip("flags-catalog generator is private-only (scripts/ not shipped)")
    proc = subprocess.run(
        [_sys.executable, str(gen), "--check"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_bullet_documented_flags_are_registered():
    # Flags documented as bullets (e.g. WEB_FETCH_ALLOW_PRIVATE_URLS before it
    # got a table row, the _SAFE_LOCAL_FLAGS member list) must not be invisible.
    for name in ("SELF_WAKE_ENABLED", "WEB_FETCH_ALLOW_PRIVATE_URLS", "KB_ENABLED"):
        assert name in REGISTRY, name
