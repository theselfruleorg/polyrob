"""Provider-list ratchet (proposal 024, P0).

The ProviderSpec registry (``modules/llm/provider_spec.py``) is the ONE table
describing LLM providers; the historical hand-maintained provider lists are now
derivations of it (the ``LLM_PROVIDER_REGISTRY`` kill-switch and its legacy literal
tables were removed 2026-08-29). This ratchet keeps the seam tax from regrowing: any NEW
production file that mentions 4+ distinct built-in provider names as string
literals is treated as a new hardcoded provider list and fails the scan —
derive from ``provider_spec.get_specs()`` instead, or allowlist the file here
with a rationale.

The allowlist may only SHRINK. Current entries:
- the registry itself + the remaining derivation seams;
- genuinely per-provider behavior tables that are NOT provider *lists*
  (cache strategies, per-provider parameter quirks, token pricing display).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SCAN_DIRS = ["agents", "api", "cli", "core", "cron", "modules", "surfaces",
             "tools", "utils", "webview"]
CANONICAL = "modules/llm/provider_spec.py"

_NAMES = ("openai", "anthropic", "gemini", "openrouter", "nvidia", "deepseek")
_NAME_RE = re.compile(r"['\"](" + "|".join(_NAMES) + r")['\"]")
_THRESHOLD = 4  # distinct provider names as string literals

ALLOWLISTED_PROVIDER_AWARE_FILES = frozenset({
    # seam files: derivations (three kill-switch literal sites left with the switch)
    "core/config.py",
    "modules/llm/llm_client_registry.py",
    "modules/llm/llm_factory.py",
    "modules/llm/model_registry.py",
    # S4 (2026-08-29): ModelProvider enum + canonical-name/cache-multiplier tables moved
    # here out of model_registry.py — per-provider behaviour tables, not a provider list.
    "modules/llm/model_types.py",
    "tools/controller/registry/schema_generators.py",
    "tools/controller/registry/service.py",
    # per-provider behavior (not provider lists)
    "agents/task/constants.py",           # MODEL_FAMILY_INSTRUCTIONS + aux router
    # S5 (2026-08-29): the per-provider token-usage/response-shape helpers moved out
    # of agents/task/utils.py into the LLM layer — behaviour table, not a list.
    "modules/llm/usage_extract.py",
    "agents/task/agent/core/model_introspection.py",  # provider display names
    "webview/stats_service.py",           # usage display grouping
})


def _findings():
    found = set()
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for py in base.rglob("*.py"):
            rel = py.relative_to(ROOT).as_posix()
            if rel == CANONICAL:
                continue
            distinct = set(_NAME_RE.findall(py.read_text(errors="ignore")))
            if len(distinct) >= _THRESHOLD:
                found.add(rel)
    return found


def test_no_new_hardcoded_provider_lists():
    new = _findings() - ALLOWLISTED_PROVIDER_AWARE_FILES
    assert not new, (
        "NEW file(s) with a hardcoded multi-provider list:\n  "
        + "\n  ".join(sorted(new))
        + "\nDerive from modules/llm/provider_spec.get_specs() (the one provider "
        "table) instead of enumerating provider names; if the file is genuinely "
        "per-provider *behavior*, allowlist it here with a rationale."
    )


def test_allowlist_entries_still_exist():
    gone = ALLOWLISTED_PROVIDER_AWARE_FILES - _findings()
    assert not gone, (
        "Stale allowlist row(s) — the file no longer clusters provider names; "
        "delete the row(s) so the ratchet tightens:\n  " + "\n  ".join(sorted(gone))
    )
