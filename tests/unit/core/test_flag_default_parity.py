"""The documented default of a flag equals the literal default the code reads.

``doctor --flags`` and ``config get`` report the DOCUMENTED default (the catalog
cell). When a read site says ``bool_env("X", True)`` but the row says OFF, every
operator surface states the opposite of the runtime (VOICE_TRANSCRIPTION_ENABLED
was exactly that, fixed 2026-10-10). This scans every shipped literal
``*bool_env("X", True|False)`` and ``*int_env("X", <int>)`` read and compares it
with the catalog row, catalogue-wide.

A row whose default is DYNAMIC (it names POLYROB_LOCAL, a posture, a mode, or
says "under") is skipped: the literal there is the server fallback, not the
documented headline.
"""
import re
import subprocess
from pathlib import Path

from core.flags import REGISTRY

REPO = Path(__file__).resolve().parents[3]
SOURCE_DIRS = ("core", "polyrob", "modules", "tools", "agents", "cli", "webview",
               "surfaces", "cron", "api", "utils", "packs")

_BOOL_RE = re.compile(
    r"""\b\w*bool_env\(\s*['"]([A-Z][A-Z0-9_]+)['"]\s*,\s*(?:default\s*=\s*)?(True|False)\s*[,)]""")
_INT_RE = re.compile(
    r"""\b\w*int_env\(\s*['"]([A-Z][A-Z0-9_]+)['"]\s*,\s*(?:default\s*=\s*)?(-?[\d_]+)\s*[,)]""")
_DYNAMIC_RE = re.compile(r"POLYROB_LOCAL|POSTURE|_MODE\b|\bunder\b|\bmode\b", re.IGNORECASE)
_TRUE = ("on", "true", "yes", "1")
_FALSE = ("off", "false", "no", "0")


def _files():
    out = subprocess.run(["git", "ls-files", "--", *SOURCE_DIRS], cwd=REPO,
                         capture_output=True, text=True, timeout=30).stdout
    return [REPO / f for f in out.splitlines()
            if f.endswith(".py") and "/tests/" not in f and (REPO / f).is_file()]


def _sites(rx, conv):
    found = {}
    for path in _files():
        text = path.read_text(errors="ignore")
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            found.setdefault(m.group(1), []).append(
                (conv(m.group(2)), f"{path.relative_to(REPO)}:{line}"))
    return found


def _head(flag):
    return flag.default_doc.strip().strip("*").split(" ")[0].strip("`'\"*").lower()


def test_scanner_finds_reads():
    assert len(_sites(_BOOL_RE, lambda v: v == "True")) > 100


def test_documented_bool_default_matches_code():
    drift = []
    for name, sites in sorted(_sites(_BOOL_RE, lambda v: v == "True").items()):
        flag = REGISTRY.get(name)
        if flag is None or _DYNAMIC_RE.search(flag.default_doc):
            continue
        head = _head(flag)
        doc = True if head in _TRUE else False if head in _FALSE else None
        if doc is None:
            continue
        if any(value != doc for value, _ in sites):
            drift.append(f"{name}: doc {flag.default_doc[:40]!r} vs code {sites}")
    assert not drift, (
        "documented default contradicts the code's literal default — fix the row "
        "in docs/CONFIGURATION.md (then both generators) or the code:\n" + "\n".join(drift))


def test_one_flag_one_literal_bool_default():
    split = {n: s for n, s in _sites(_BOOL_RE, lambda v: v == "True").items()
             if len({v for v, _ in s}) > 1}
    assert not split, f"the same flag is read with both True and False defaults: {split}"


def test_documented_int_default_matches_code():
    drift = []
    for name, sites in sorted(_sites(_INT_RE, lambda v: int(v.replace("_", ""))).items()):
        flag = REGISTRY.get(name)
        if flag is None or flag.kind != "int" or _DYNAMIC_RE.search(flag.default_doc):
            continue
        try:
            doc = int(flag.default_doc.strip().strip("*").split(" ")[0].strip("`'\"*"))
        except ValueError:
            continue
        if doc not in {value for value, _ in sites}:
            drift.append(f"{name}: doc {flag.default_doc[:40]!r} vs code {sites}")
    assert not drift, "documented int default matches no code read:\n" + "\n".join(drift)
