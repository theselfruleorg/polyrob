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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Flags snapshotted at import for security (core/config_policy/policy.py WS-7 /
# compute-posture; tools/controller/approval.py). A file write configures the
# NEXT process; the RUNNING one never re-reads them.
_IMPORT_FROZEN_FLAGS = frozenset({
    "AGENT_COMPUTE_POSTURE",
    "PAYMENT_APPROVAL_MODE",
    "PAYMENT_APPROVAL_TIMEOUT_SEC",
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
            pass
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
        description=f"documented default: {flag.default_doc}",
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

def set_value(key: str, value: str, *, scope: Optional[str] = None,
              user_id: Optional[str] = None, home_dir=None,
              confirm: bool = False) -> SetResult:
    """Route one write to the owning store. Never raises.

    scope: ``user`` (preferences.toml — required for pref keys), ``project``
    (``./.polyrob/.env``) or ``global`` (``~/.polyrob/.env``) for flag keys.
    Omitted scope defaults to the key's natural store (pref→user,
    flag→project).
    """
    from core.prefs import PREF_SCHEMA
    if scope is not None and scope not in _SCOPES:
        return SetResult(False, "refused", f"unknown scope: {scope}")
    if key in PREF_SCHEMA:
        return _set_pref(key, value, user_id, home_dir, confirm)
    from core.flags import REGISTRY
    if key in REGISTRY:
        return _set_flag(key, value, scope or "project")
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


def _set_flag(key: str, value: str, scope: str) -> SetResult:
    from core.flags import REGISTRY, is_secret_flag
    from core.prefs import shape_of_default, value_matches_shape
    flag = REGISTRY[key]
    if scope == "user":
        return SetResult(False, "refused",
                         f"'{key}' is an env flag — scope must be project or global")
    if not is_secret_flag(key):
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
    return SetResult(True, "written",
                     f"set {key}={display} in {path} (takes effect: restart){note}",
                     store=str(path), applies=applies)


def _env_path(scope: str) -> Path:
    from core.paths import polyrob_home
    if scope == "global":
        return polyrob_home() / ".env"
    return Path.cwd() / ".polyrob" / ".env"
