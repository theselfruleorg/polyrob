"""026 P4 — one remedy grammar for a flag that is off.

Every owner-facing hint names the fix as ``core.remedy.flag_remedy`` builds it
(`enable: `polyrob config set KEY true --global` (takes effect: restart)`).
The ``set KEY=true`` / ``set KEY=1`` idiom names no verb, no file and no restart,
and is FORBIDDEN outside ``core/remedy.py``.

SHRINK-ONLY: the stragglers below are agent-facing tool errors and startup log
lines that predate the grammar. A file may lose entries (lower its count in the
same commit); it may never gain one, and no new file may appear.
"""
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TIERS = ("core", "modules", "agents", "tools", "api", "cli", "surfaces", "webview", "cron",
          "packs")
_IDIOM = re.compile(r"\b[Ss]et [A-Z][A-Z0-9_]{3,}=(?:true|1)\b")

#: file -> the number of legacy literals it may still carry (shrink-only).
ALLOWED = {
    "api/auth_endpoints.py": 5,
    "core/initialization.py": 2,
    "core/llm_auth/flows/registry.py": 1,
    "tools/browser/browser.py": 1,
    "tools/controller/delegation.py": 1,
    "tools/defi/data_tool.py": 2,
    "tools/defi/spl_deploy_verb.py": 1,
    "tools/defi/trade_tool.py": 3,
    "tools/filesystem.py": 1,
    "tools/mcp/self_install.py": 1,
    "tools/x402/service.py": 2,
    "cli/commands/whatsapp.py": 1,
    "packs/x/polyrob_x/twitter_tool.py": 1,
}


def _counts() -> Counter:
    out: Counter = Counter()
    for tier in _TIERS:
        for path in (ROOT / tier).rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or rel == "core/remedy.py":
                continue
            n = len(_IDIOM.findall(path.read_text(encoding="utf-8", errors="replace")))
            if n:
                out[rel] = n
    return out


def test_no_new_set_key_equals_true_idiom():
    grown = {f: n for f, n in _counts().items() if n > ALLOWED.get(f, 0)}
    assert not grown, (
        f"new `set KEY=true` remedy literal(s): {grown} — build the hint with "
        "core.remedy.flag_remedy(KEY) instead")


def test_allowlist_is_tight():
    counts = _counts()
    loose = {f: (a, counts.get(f, 0)) for f, a in ALLOWED.items() if counts.get(f, 0) < a}
    assert not loose, f"lower ALLOWED to the real count (allowed, actual): {loose}"


def test_the_grammar():
    from core.remedy import flag_command, flag_remedy
    assert flag_remedy("KB_ENABLED") == (
        "enable: `polyrob config set KB_ENABLED true --global` (takes effect: restart)")
    assert flag_command("KB_ENABLED", "1") == "polyrob config set KB_ENABLED true --global"
    assert flag_remedy("AUTONOMY_ENABLED") == (
        "enable: `polyrob autonomy on` (takes effect: restart)")
    assert flag_command("AUTONOMY_ENABLED", "false") == "polyrob autonomy off"
    assert flag_remedy("X", want="false").startswith("disable: `polyrob config set X false")
    assert flag_command("CRON_ENABLED", scope_hint=False) == "polyrob config set CRON_ENABLED true"
