"""Every ServerConfig field has a reader (067 F4).

``core/config.py``'s pydantic ``ServerConfig`` reads the env for every field it
declares, so an unread field is a second, silent source of truth for a flag —
often with a default that differs from the raw reader the code actually uses.
067 F4 deleted 54 such fields. This test keeps a new one from arriving unread:
each field must be referenced (``.name`` or a ``"name"`` string, i.e. an
attribute read, a ``getattr`` or a ``config.get``) somewhere in the shipped
source tree outside ``core/config.py``, or sit on the allowlist below with the
in-file method that consumes it.

The match is deliberately loose (a common word like ``agent`` always matches),
so it errs toward keeping a field; it only ever catches a field nobody names.
"""
import re
import subprocess
from pathlib import Path

from core.config import ServerConfig

REPO_ROOT = Path(__file__).resolve().parents[3]

SOURCE_DIRS = (
    "core", "modules", "tools", "agents", "cli", "webview",
    "surfaces", "cron", "api", "utils",
    "packs",  # 067 P3: first-party packs read core config (perplexity_api_key)
)

# Fields consumed only by a ServerConfig method that is itself called from
# outside the file. Keep SMALL; each entry names its consumer.
ALLOWLIST = {
    "deepseek_api_url": "get_llm_config",
    "openrouter_site_url": "get_llm_config",
    "openrouter_site_name": "get_llm_config",
    "nvidia_api_url": "get_llm_config",
    # available_providers() reads f"{name}_api_key" for every PROFILES row.
    "openai_api_key": "get_llm_config / available_providers",
    "anthropic_api_key": "get_llm_config / available_providers",
    "gemini_api_key": "get_llm_config / available_providers",
    "deepseek_api_key": "get_llm_config / available_providers",
    "openrouter_api_key": "get_llm_config / available_providers",
    "nvidia_api_key": "get_llm_config / available_providers",
    "twitter_api_key": "get_twitter_config",
    "twitter_api_secret_key": "get_twitter_config",
    "twitter_access_token": "get_twitter_config",
    "twitter_access_token_secret": "get_twitter_config",
    "twitter_bearer_token": "get_twitter_config",
    "twitter_oauth2_access_token": "get_twitter_config",
    "twitter_chat_private_keys_b64": "get_twitter_config",
    "twitter_chat_key_version": "get_twitter_config",
    "twitter_chat_passphrase": "get_twitter_config",
    "mcp_servers_config": "_build_mcp_config_from_env (runs in __init__)",
    "services_ocr_temp_dir": "_ensure_directories (runs in __init__) creates it",
    "embedding_dimension": "get_embedding_config",
    # a dynamic getattr(config, f"{chain}_rpc_url") in the private ops script
    # scripts/diagnose_payment_issue.py (scripts/ does not ship).
    "polygon_rpc_url": "scripts/diagnose_payment_issue.py",
    "base_rpc_url": "scripts/diagnose_payment_issue.py",
    "arbitrum_rpc_url": "scripts/diagnose_payment_issue.py",
}


def _source_texts() -> list:
    if (REPO_ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "--", *SOURCE_DIRS], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=30, check=True)
        files = [REPO_ROOT / p for p in out.stdout.splitlines() if p.endswith(".py")]
    else:
        # git archive ships no index. Scan its exported source, retaining the
        # same directory and test exclusions instead of accepting an empty scan.
        files = [p for top in SOURCE_DIRS for p in (REPO_ROOT / top).rglob("*.py")]
    texts = []
    for f in files:
        if f == REPO_ROOT / "core/config.py":
            continue
        if any(part in ("tests", "test") for part in f.relative_to(REPO_ROOT).parts):
            continue
        try:
            texts.append(f.read_text(errors="ignore"))
        except OSError:
            continue
    return texts


def test_every_server_config_field_has_a_reader():
    texts = _source_texts()
    assert len(texts) > 500, "source scan found too few files — layout changed"
    unread = []
    for name in ServerConfig.model_fields:
        if name in ALLOWLIST:
            continue
        rx = re.compile(r"""\.%s\b|['"]%s['"]""" % (re.escape(name), re.escape(name)))
        if not any(rx.search(t) for t in texts):
            unread.append(name)
    assert not unread, (
        f"ServerConfig field(s) with no reader outside core/config.py: {unread}. "
        "Delete the field (read the env where it is used, through core.env), or "
        "add it to ALLOWLIST with the in-file method that consumes it."
    )


def test_allowlist_names_real_fields():
    stale = sorted(set(ALLOWLIST) - set(ServerConfig.model_fields))
    assert not stale, f"allowlist entries that are no longer fields: {stale}"


def test_source_scan_in_an_archive_includes_packs_but_not_tests(tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "REPO_ROOT", tmp_path)
    for name, body in {"core/reader.py": "core consumer", "core/config.py": "definition",
                       "packs/demo/pkg/reader.py": "pack consumer",
                       "packs/demo/tests/test_reader.py": "test is not a consumer"}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    assert sorted(_source_texts()) == ["core consumer", "pack consumer"]
