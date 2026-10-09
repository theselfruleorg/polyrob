"""One limit, one source (2026-10-06).

The digest repeated a "cap mismatch" for days because one limit lived in two
stores with no sync: the agent's per-tx/daily caps and the signer's
``signer.toml``. The fix made the signer the envelope and the agent clamp to it
(``core/wallet/signer_envelope.py``). These checks keep it that way:

1. Every hard limit the signer enforces is a leg the agent clamps to. A new
   signer cap that the agent does not read is the old bug again.
2. Every leg the agent clamps to is reported by the signer's ``ping``.
3. The money-cap env flags are read only by their ONE resolver module (plus
   the listed readers). A second reader re-derives the cap without the owner
   prefs or the signer clamp — the drift this ratchet exists to stop.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: flag -> the files allowed to name it (outside tests/), each with its reason.
CAP_FLAG_READERS = {
    "AGENT_WALLET_MAX_PER_TX_USD": {
        "core/wallet/config.py",          # the resolver
        "core/wallet/tx_guard.py",        # resolver-failure fallback, signer-clamped
        "core/prefs.py",                  # the pref's env twin
        "cli/commands/doctor.py",         # validates the env (shows shown_caps)
        "cli/commands/wallet.py",         # `set-cap --env` writes it; init validates
    },
    "WALLET_DAILY_CAP_USD": {
        "core/wallet/config.py",
        "core/prefs.py",
        "core/config_policy/money_regime.py",   # "is a daily cap set" (via the resolver)
        "cli/commands/doctor.py",
        "cli/commands/wallet.py",
        "webview/pages.py",               # display fallback when the ledger has no cap
    },
    "DEFI_AUTONOMOUS_MAX_USD": {
        "core/wallet/tx_guard.py",        # the resolver (autonomous_max_usd)
        "core/prefs.py",
        "scripts/live_proof.py",          # reads an env FILE for an offline proof
    },
    "X402_AUTONOMOUS_MAX_USD": {
        "core/config_policy/spend_lane.py",   # the resolver
    },
}

_SKIP_DIRS = {"tests", ".git", "node_modules", "venv", ".venv", "build", "dist"}
_GENERATED = {"core/flags_catalog.py", "scripts/gen_flags_catalog.py"}


def _py_files():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts and rel.parts[0] in _SKIP_DIRS:
            continue
        if any(part.startswith(".") or part in ("site-packages", "__pycache__")
               for part in rel.parts):
            continue
        yield rel.as_posix(), p


def test_every_signer_cap_is_a_leg_the_agent_clamps_to():
    from core.signer.caps import _TABLES
    from core.wallet.signer_envelope import LEGS
    signer_limits = set(_TABLES["caps"]) | {"x402_max_window_sec"}
    missing = signer_limits - set(LEGS)
    assert not missing, (
        f"signer.toml enforces {sorted(missing)} but the agent never clamps to it — add the "
        f"leg to core/wallet/signer_envelope.LEGS and clamp the agent's limit to it")


def test_the_signer_ping_reports_every_leg():
    from core.signer.caps import SignerConfig
    from core.wallet.signer_envelope import LEGS
    cfg = SignerConfig(per_tx_usd=1.0, daily_usd=1.0, x402_per_payment_usd=1.0, chains=("base",),
                       socket="/s", state_dir="/d", client_uids=(998,))
    assert set(LEGS) <= set(cfg.summary())


def test_money_cap_flags_have_one_reader():
    pattern = re.compile(r"[\"'](" + "|".join(CAP_FLAG_READERS) + r")[\"']")
    offenders = []
    for rel, path in _py_files():
        if rel in _GENERATED:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for flag in set(pattern.findall(text)):
            if rel not in CAP_FLAG_READERS[flag]:
                offenders.append(f"{rel}: {flag}")
    assert not offenders, (
        "a money-cap env flag is read outside its resolver — call the resolver "
        "(core.wallet.config.effective_* / tx_guard.autonomous_max_usd / "
        "spend_lane.x402_autonomous_ceiling_usd) instead:\n  " + "\n  ".join(sorted(offenders)))
