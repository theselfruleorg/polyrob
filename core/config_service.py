"""ONE config control plane (proposal 018 P1): describe / explain / search / set.

POLYROB's configuration lives in several composed stores — the env-flag catalog
(~400 documented flags, ``core/flags.py``), the typed per-tenant preferences
overlay (``core/prefs.py``), a 7-file env ladder (``core/paths.py``), and the
model-resolution ladder (``core/runtime_config.py``). Each is internally
consistent; what never existed was ONE seam that composes them, so every
surface (REPL ``/config``, ``polyrob config``, webview, Telegram, the agent's
``preferences`` action) re-derived its own partial view.

This module is that seam. It REPLACES NO STORE — every write routes to the
existing writer with its existing validation/quarantine semantics:

- pref  → ``write_preference`` (safe / guarded+confirm) or
          ``propose_pref_change`` (guarded, queued for owner review)
- flag  → shape-checked ``KEY=value`` upsert into the project
          (``./.polyrob/.env``) or global (``~/.polyrob/.env``) env file
          (the ``polyrob config set`` contract; server/operator files
          ``config/.env.*`` are NEVER written from here)

Security invariants (pinned by tests/unit/core/test_config_service.py):
secrets are never readable back through any accessor (masking via
``core.flags.is_secret_flag`` / ``resolve_flag``); guarded prefs keep the
confirm/queue pipeline; unknown keys hard-refuse (no ``--force`` here — that
stays a CLI-only escape hatch); import-frozen security flags are writable for
the NEXT process but the result says so explicitly.
"""
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Flags snapshotted at import for security (core/config_policy/policy.py WS-7 /
# compute-posture; tools/controller/approval.py). A file write configures the
# NEXT process; the RUNNING one never re-reads them. Since 026 P1.1 the next
# start DOES read them from the .polyrob/.env ladder (load_env re-freezes once
# after file layering), so "takes effect on the next start" is finally true.
# NOTE: these are the real ENV VAR names — the timeout flag is
# APPROVAL_TIMEOUT_SEC (shared with the generic approval seam); the old
# "PAYMENT_APPROVAL_TIMEOUT_SEC" row was a phantom that matched nothing.
_IMPORT_FROZEN_FLAGS = frozenset({
    "AGENT_COMPUTE_POSTURE",
    "PAYMENT_APPROVAL_MODE",
    "APPROVAL_TIMEOUT_SEC",
    "APPROVAL_GRANT_TTL_HOURS",
    "APPROVAL_REQUIRED_TOOLS",
    "APPROVAL_PROVIDER",
})

_SCOPES = ("user", "project", "global")


@dataclass(frozen=True)
class Source:
    """One rung of a setting's provenance chain (display-safe value)."""
    value: object
    origin: str  # pref:<uid> | env:process | env:<FLAG> | env-file:<path> | built-in* | default(...)


@dataclass(frozen=True)
class SettingInfo:
    key: str
    namespace: str            # "pref" | "flag"
    kind: str                 # bool|int|float|str|list|enum
    group: str
    description: str
    effective: object         # display-safe (secrets masked)
    source: str
    applies: str              # live|next-turn|next-session|restart
    sensitivity: str          # safe|guarded|flag
    enforcement: str          # enforced|advisory
    secret: bool
    chain: tuple = field(default_factory=tuple)  # populated by explain()


@dataclass(frozen=True)
class SetResult:
    ok: bool
    outcome: str              # written | queued | refused | invalid
    message: str
    store: str = ""
    applies: str = ""


# ---------------------------------------------------------------------------
# describe / effective / explain
# ---------------------------------------------------------------------------

def known_keys() -> list:
    """Every describable key, prefs first then flags (for completers/pickers)."""
    from core.flags import REGISTRY
    from core.prefs import PREF_SCHEMA
    return list(PREF_SCHEMA.keys()) + list(REGISTRY.keys())


def describe(key: str, *, user_id: Optional[str] = None,
             home_dir=None, include_chain: bool = False) -> SettingInfo:
    """Uniform view of one setting; raises ``KeyError`` for an unknown key."""
    from core.prefs import PREF_SCHEMA
    if key in PREF_SCHEMA:
        return _describe_pref(key, user_id, home_dir, include_chain)
    from core.flags import REGISTRY
    if key in REGISTRY:
        return _describe_flag(key, include_chain)
    raise KeyError(key)


def effective(key: str, *, user_id: Optional[str] = None, home_dir=None) -> object:
    return describe(key, user_id=user_id, home_dir=home_dir).effective


def explain(key: str, *, user_id: Optional[str] = None, home_dir=None) -> SettingInfo:
    """`describe` + the full provenance chain (`git config --show-origin` style)."""
    return describe(key, user_id=user_id, home_dir=home_dir, include_chain=True)


def _describe_pref(key: str, user_id, home_dir, include_chain: bool) -> SettingInfo:
    from core.prefs import PREF_SCHEMA, _builtin_default, display_effective
    spec = PREF_SCHEMA[key]
    value, source = display_effective(key, user_id, home_dir)
    chain = ()
    if include_chain:
        rungs = []
        try:
            from core.prefs import load_preferences
            pref = load_preferences(home_dir, user_id).get(key)
            if pref is not None:
                rungs.append(Source(pref, f"pref:{user_id or 'anonymous'}"))
        except Exception:
            logger.debug("config_service: preference rung unreadable for %s", key, exc_info=True)
        if spec.env_flag:
            raw = os.environ.get(spec.env_flag)
            if raw is not None and raw.strip() != "":
                rungs.append(Source(raw, f"env:{spec.env_flag}"))
        builtin = _builtin_default(spec)
        if builtin is not None:
            rungs.append(Source(builtin[0], builtin[1]))
        chain = tuple(rungs)
    return SettingInfo(
        key=key, namespace="pref", kind=spec.type,
        group=key.split(".", 1)[0], description=spec.description,
        effective=value, source=source, applies=spec.applies,
        sensitivity=spec.sensitivity, enforcement=spec.enforcement,
        secret=False, chain=chain,
    )


def _describe_flag(key: str, include_chain: bool) -> SettingInfo:
    from core.config_policy.flag_defaults import dynamic_flag_default
    from core.flags import REGISTRY, is_secret_flag, resolve_flag
    flag = REGISTRY[key]
    resolved = resolve_flag(key, dict(os.environ), dynamic_flag_default)
    secret = is_secret_flag(key)
    chain = ()
    if include_chain:
        rungs = []
        raw = os.environ.get(key)
        if raw is not None and raw.strip() != "":
            rungs.append(Source(_mask(key, raw), "env:process"))
        for cand in _existing_env_files():
            try:
                from core.env_file import read_env_file
                vals = read_env_file(cand)
                if key in vals:
                    rungs.append(Source(_mask(key, vals[key]), f"env-file:{cand}"))
            except Exception:
                continue
        dyn = dynamic_flag_default(key)
        if dyn is not None:
            rungs.append(Source(dyn[0], str(dyn[1])))
        rungs.append(Source(
            "(unset)" if secret else resolve_flag(key, {}, None).value,
            "built-in:catalog"))
        chain = tuple(rungs)
    return SettingInfo(
        key=key, namespace="flag", kind=flag.kind, group=flag.group,
        # 030 WS-F1: a real description — the generator used to drop the doc's
        # "What it does" column, so this restated the default as the description.
        description=(getattr(flag, "description", "") or
                     f"documented default: {flag.default_doc}"),
        effective=resolved.value, source=resolved.source,
        applies=_flag_applies(key), sensitivity="flag",
        enforcement="enforced", secret=secret, chain=chain,
    )


def _mask(key: str, value: str) -> str:
    from core.flags import is_secret_flag
    return "(set, masked)" if is_secret_flag(key) else value


def _flag_applies(key: str) -> str:
    # Conservative truth: env flags configure the NEXT process. Access-time
    # consumers pick changes up sooner, but promising "live" per-flag needs the
    # per-group audit (018 P2 metadata work) — until then, restart never lies.
    if key in _IMPORT_FROZEN_FLAGS:
        return "restart (frozen at import)"
    return "restart"


def _existing_env_files() -> list:
    """Existing .env candidate paths, local ladder first then server ladder
    (deduped). Attribution is read-only display — showing every file that
    holds the key is honest regardless of which ladder loaded this process."""
    try:
        from core.paths import env_file_candidates
        seen, out = set(), []
        for local in (True, False):
            for cand in env_file_candidates(local_mode=local):
                p = cand.path
                if p in seen:
                    continue
                seen.add(p)
                if p.exists():
                    out.append(p)
        return out
    except Exception:
        return []


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def search(query: str, *, user_id: Optional[str] = None, home_dir=None,
           limit: int = 50) -> list:
    """Fuzzy search across BOTH namespaces (key, group, description). An empty
    query lists everything (prefs first) up to *limit*."""
    import difflib

    from core.flags import REGISTRY
    from core.prefs import PREF_SCHEMA
    q = (query or "").strip().lower()

    def _score(key: str, group: str, description: str) -> Optional[int]:
        if not q:
            return 3
        k = key.lower()
        if k.startswith(q):
            return 0
        if q in k:
            return 1
        if q in group.lower() or q in description.lower():
            return 2
        if difflib.SequenceMatcher(None, q, k).ratio() > 0.75:
            return 2
        return None

    scored = []
    for key, spec in PREF_SCHEMA.items():
        s = _score(key, key.split(".", 1)[0], spec.description)
        if s is not None:
            scored.append((s, 0, key))
    for key, flag in REGISTRY.items():
        s = _score(key, flag.group, flag.default_doc)
        if s is not None:
            scored.append((s, 1, key))
    scored.sort()
    out = []
    for _s, _ns, key in scored[:limit]:
        try:
            out.append(describe(key, user_id=user_id, home_dir=home_dir))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# set_value
# ---------------------------------------------------------------------------

# 024 §2.6/§7.5: flags that select WHERE the agent's inference goes or WHERE
# its credentials live are credential-equivalent — a compromised/spoofed owner
# console must not be able to redirect them. Writable from the local CLI/REPL
# only; the webview PATCH surface refuses them even at local/own_ops posture.
CONSOLE_UNWRITABLE_FLAGS = frozenset({
    "LLM_CUSTOM_PROVIDERS",
    "POLYROB_AUTH_STORE",
    "LLM_AUTH_STORE_ENABLED",
    "LLM_CREDENTIAL_BORROW",
})

# S8 (agent + wallet security evaluation, 2026-09-14). The set above named four
# flags; the write it guards is far wider. A remote flag write lands in
# ``./.polyrob/.env``, which ``polyrob.service`` loads AFTER
# ``/etc/polyrob/polyrob.env`` — so it OUTRANKS the operator's own env file on
# the next restart. Four families can therefore hand the instance over, and are
# refused on every non-local surface:
#
#   owner binding  — who the agent obeys (POLYROB_OWNER_*/BOT_OWNER_*/
#                    TELEGRAM_OWNER_ID/ALLOWED_*/*ALLOWLIST*/pairing)
#   money bounds   — the caps, venues and endpoints the spend guard measures
#                    against (WALLET/USD/CAP/RPC/TREASURY segments, X402_*MAX*)
#   approval       — whether a money verb needs an owner tap at all (APPROVAL)
#   posture/trust  — POLYROB_LOCAL, AUTONOMY_MODE, *_POSTURE, the console's own
#                    gating (WEBGATE_*/WEBVIEW_READ_ONLY/WEBVIEW_AUTH_ENABLED)
#                    and the host-reach surface (CODE_EXEC_*/SHELL/SELF_ENV)
#
# …plus every secret-shaped flag (``is_secret_flag`` — the wallet master seed
# lives there). Matching is by NAME SEGMENT (split on ``_``), so a cap added
# tomorrow is covered without editing this file; over-blocking is the deliberate
# bias (the remedy — the local CLI — is one command and is named in the refusal).
# Known collateral: ALLOWED_REASONING_TURNS, OWNER_DIGEST_ENABLED and friends are
# ordinary knobs caught by the ALLOWED_/OWNER rules. Pinned by
# tests/unit/core/test_console_unwritable_flags.py.
_UNWRITABLE_SEGMENTS = frozenset({
    "OWNER", "PAIRING",                       # owner binding / identity
    "WALLET", "USD", "CAP", "RPC", "TREASURY",  # money bounds + endpoints
    "APPROVAL",                               # approval policy
    "POSTURE",                                # trust posture ladders
})
_UNWRITABLE_PREFIXES = ("WEBGATE_", "CODE_EXEC_", "ALLOWED_")
_UNWRITABLE_EXACT = frozenset({
    "POLYROB_LOCAL", "POLYROB_LOCAL_OWNER", "AUTONOMY_MODE",
    "WEBVIEW_READ_ONLY", "WEBVIEW_AUTH_ENABLED", "WEBVIEW_HOST",
    "WEBVIEW_ALLOW_LOCAL_POSTURE",
    "DELEGATE_BLOCKED_TOOLS", "SELF_ENV_ENABLED", "SHELL_TOOLS_ENABLED",
    "ADMIN_WALLETS", "OUTBOUND_POLICY", "X402_PAYMENT_RECIPIENT",
})
# Flag names only (UPPER_SNAKE). A typed preference key (``budget.wallet_daily_usd``)
# reads money-shaped but has its OWN trust ladder (guarded ⇒ queued for owner
# review) and must never be swallowed by this denylist.
_FLAG_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_console_unwritable(key: str) -> bool:
    """Whether *key* is an env flag no REMOTE surface may write (S8).

    True ⇒ the console/telegram/API PATCH surfaces refuse it at every posture;
    the local CLI (`polyrob config set`) is the only writer. Never raises.
    """
    name = str(key or "")
    if name in CONSOLE_UNWRITABLE_FLAGS:
        return True
    if not _FLAG_NAME_RE.match(name):
        return False          # a pref key / anything non-flag-shaped
    if name in _UNWRITABLE_EXACT or name.startswith(_UNWRITABLE_PREFIXES):
        return True
    segments = set(name.split("_"))
    if segments & _UNWRITABLE_SEGMENTS:
        return True
    if "ALLOWLIST" in name or "ALLOWLISTED" in name:
        return True
    if name.startswith("X402_") and "MAX" in segments:
        return True
    try:
        from core.flags import is_secret_flag
        return bool(is_secret_flag(name))
    except Exception:         # pragma: no cover — fail CLOSED on a broken import
        return True


def set_value(key: str, value: str, *, scope: Optional[str] = None,
              user_id: Optional[str] = None, home_dir=None,
              confirm: bool = False, surface: str = "local") -> SetResult:
    """Route one write to the owning store. Never raises.

    scope: ``user`` (preferences.toml — required for pref keys), ``project``
    (``./.polyrob/.env``) or ``global`` (``~/.polyrob/.env``) for flag keys.
    Omitted scope defaults to the key's natural store (pref→user,
    flag→project).

    surface: ``local`` (CLI/REPL, the default) or a remote surface label
    (``console``, ``telegram``, …). The credential-surface refusal is enforced
    HERE — in the oracle — not only at the webview call site, so any surface
    that grows a flag-write path inherits it (UX assessment 2026-08-07, Q9).
    """
    from core.prefs import PREF_SCHEMA
    if scope is not None and scope not in _SCOPES:
        return SetResult(False, "refused", f"unknown scope: {scope}")
    if surface != "local" and is_console_unwritable(key):
        return SetResult(
            False, "refused",
            f"'{key}' selects the agent's owner binding, money bounds, approval "
            f"policy, trust posture or credential surface and is not writable "
            f"from a remote surface — set it from the local CLI "
            f"(`polyrob config set {key} <value>`)")
    if key in PREF_SCHEMA:
        return _set_pref(key, value, user_id, home_dir, confirm)
    from core.flags import REGISTRY, pattern_flag_for
    if key in REGISTRY or pattern_flag_for(key) is not None:
        return _set_flag(key, value, scope or "project", surface=surface)
    return SetResult(False, "refused",
                     f"unknown key: {key} — not a documented flag or preference")


def _set_pref(key: str, value, user_id, home_dir, confirm: bool) -> SetResult:
    from core.prefs import (PREF_SCHEMA, SENSITIVITY_GUARDED, preferences_path,
                            propose_pref_change, write_preference)
    spec = PREF_SCHEMA[key]
    if not user_id:
        return SetResult(False, "refused",
                         f"'{key}' is a per-user preference — a tenant user_id "
                         "is required")
    if spec.sensitivity == SENSITIVITY_GUARDED and not confirm:
        ok, err = propose_pref_change(user_id, key, value, home_dir)
        if not ok:
            return SetResult(False, "invalid", err)
        return SetResult(True, "queued",
                         f"'{key}' is guarded — change queued for owner review "
                         "(promote via /pending)", store="pending", applies=spec.applies)
    ok, err = write_preference(home_dir, user_id, key, value)
    if not ok:
        return SetResult(False, "invalid", err)
    path = preferences_path(home_dir, user_id)
    return SetResult(True, "written", f"set {key} (applies: {spec.applies})",
                     store=str(path), applies=spec.applies)


def _set_flag(key: str, value: str, scope: str, *, surface: str = "local") -> SetResult:
    from core.flags import REGISTRY, is_secret_flag, pattern_flag_for
    from core.prefs import shape_of_default, value_matches_shape
    flag = REGISTRY.get(key) or pattern_flag_for(key)
    if flag is None:
        return SetResult(False, "refused",
                         f"unknown key: {key} — not a documented flag or preference")
    if scope == "user":
        return SetResult(False, "refused",
                         f"'{key}' is an env flag — scope must be project or global")
    if not is_secret_flag(key):
        # 026 P1.4: enum-shaped flags reject invalid members with the valid set
        # (a typo'd AUTONOMY_MODE used to write cleanly and silently degrade).
        from core.config_policy.flag_enums import enum_error
        enum_err = enum_error(key, value)
        if enum_err:
            return SetResult(False, "invalid", enum_err)
        shape = shape_of_default(flag.default_doc)
        if not value_matches_shape(value, shape):
            return SetResult(
                False, "invalid",
                f"{key} expects a {shape} value (documented default: "
                f"{flag.default_doc}); got {value!r}")
    path = _env_path(scope)
    try:
        from core.env_file import upsert_env_var
        upsert_env_var(path, key, str(value), secure=True)
        if scope == "project":
            try:
                from core.gitignore import ensure_polyrob_gitignored
                ensure_polyrob_gitignored(Path.cwd(), require_git_repo=True)
            except Exception:
                logger.debug("gitignore guard failed (non-fatal)", exc_info=True)
    except Exception as e:
        return SetResult(False, "invalid", f"write failed: {e}")
    applies = _flag_applies(key)
    note = ""
    if key in _IMPORT_FROZEN_FLAGS:
        note = (" — this value is frozen at import: the running process never "
                "re-reads it; it takes effect on the next start")
    display = "(set, masked)" if is_secret_flag(key) else str(value)
    message = f"set {key}={display} in {path} (takes effect: restart){note}"
    for extra in post_write_notes(key, str(value), scope, surface=surface):
        message += "\n" + extra
    return SetResult(True, "written", message, store=str(path), applies=applies)


def _env_path(scope: str) -> Path:
    from core.paths import polyrob_home
    if scope == "global":
        return polyrob_home() / ".env"
    return Path.cwd() / ".polyrob" / ".env"


def post_write_notes(key: str, value: str, scope: str, *,
                     surface: str = "local") -> list:
    """Honesty notes for a just-written flag (026 P0.6 / P1.5 / P1.6).

    ONE builder every writer calls (`set_value`, `polyrob config set`, REPL
    `/config set`), so the shadow/clamp/server stories cannot diverge:
      - P0.6 scope shadowing: a global write that the project file outranks;
        a process-env value that differs from the effective file value.
      - P1.6 clamp echo: AUTONOMY_MODE=autonomous evaluates the would-be
        single-owner clamp NOW and names the missing prerequisite.
      - P1.5 server honesty: a remote surface (webview/telegram) whose serving
        process reads the server env ladder is told the .polyrob write applies
        to CLI runs only.
    Never raises; returns [] on any resolution error.
    """
    notes: list = []
    try:
        from core.env_file import read_env_file
        project_path = _env_path("project")
        global_path = _env_path("global")
        proj_vals = read_env_file(project_path)
        glob_vals = read_env_file(global_path)
        if scope == "global" and key in proj_vals \
                and str(proj_vals[key]) != str(value):
            notes.append(
                f"note: shadowed by {project_path} — {key} is set there and "
                f"project beats global; this write has no effect until you run "
                f"`polyrob config unset {key}`")
        effective_file = proj_vals.get(key, glob_vals.get(key))
        env_raw = os.environ.get(key)
        if (env_raw is not None and str(env_raw).strip() != ""
                and effective_file is not None
                and str(env_raw) != str(effective_file)):
            notes.append(
                f"note: this process started with {key}={_mask(key, env_raw)}; "
                "a shell/systemd-exported value wins over every file at the "
                "next start — if it is exported there, change it there too")
    except Exception:
        logger.debug("post_write_notes shadow check failed", exc_info=True)
    if key == "AUTONOMY_MODE" and str(value).strip().lower() == "autonomous":
        try:
            from core.config_policy.policy import full_autonomy_clamp_reason
            reason = full_autonomy_clamp_reason()
            if reason:
                notes.append(
                    f"note: autonomous will CLAMP to supervised — {reason}. "
                    "Bind an owner (set POLYROB_OWNER_USER_ID, or run `polyrob "
                    "init`) and ensure POLYROB_LOCAL=1, then restart")
            else:
                notes.append("autonomy mode after restart: autonomous (effective)")
        except Exception:
            logger.debug("post_write_notes clamp echo failed", exc_info=True)
    if surface != "local":
        try:
            from core.config_policy.policy import local_mode_enabled
            if not local_mode_enabled():
                notes.append(
                    "note: this serving process reads the server env ladder "
                    "(config/.env.* / systemd EnvironmentFile), not "
                    ".polyrob/.env — the write applies to CLI runs only")
        except Exception:
            logger.debug("post_write_notes server-ladder check failed", exc_info=True)
    return notes
