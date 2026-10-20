"""ONE config control plane (proposal 018 P1): describe / explain / search / set.

POLYROB's configuration lives in several composed stores — the env-flag catalog
(``core/flags.py``), the typed per-tenant preferences overlay
(``core/prefs.py``), the ONE env ladder (``core/paths.py``: process env > the
instance file > ``~/.polyrob/.env``), and the
model-resolution ladder (``core/runtime_config.py``). Each is internally
consistent; what never existed was ONE seam that composes them, so every
surface (REPL ``/config``, ``polyrob config``, webview, Telegram, the agent's
``preferences`` action) re-derived its own partial view.

This module is that seam. It REPLACES NO STORE — every write routes to the
existing writer with its existing validation/quarantine semantics:

- pref  → ``write_preference`` (safe / guarded+confirm) or
          ``propose_pref_change`` (guarded, queued for owner review)
- flag  → shape-checked ``KEY=value`` upsert into :func:`home_env_path` —
          the instance file ``<deployed data home>/.env`` on a deployed box,
          else ``~/.polyrob/.env`` — refused when a higher layer pins the key
          (:func:`shadowed_write_refusal`); ``/etc/polyrob/polyrob.env`` is
          NEVER written from here

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
    origin: str  # pref:<uid> | env:systemd (<path>) | env:shell | env:file (<path>) | env:<FLAG> | built-in* | default(...)


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
    #: The post_write_notes (also appended to ``message``), for a caller that
    #: renders its own first line (``polyrob config set``, REPL ``/config set``).
    notes: tuple = ()
    #: 026 P5: True when the write was also applied to this process's env.
    live: bool = False
    #: Closest documented key, on an unknown-key refusal.
    suggestion: str = ""


# ---------------------------------------------------------------------------
# describe / effective / explain
# ---------------------------------------------------------------------------

def known_keys() -> list:
    """Every describable key, prefs first then flags (for completers/pickers)."""
    from core.config_policy.members import MEMBER_FLAGS
    from core.flags import REGISTRY
    from core.prefs import PREF_SCHEMA
    return list(PREF_SCHEMA.keys()) + list(REGISTRY.keys()) + sorted(MEMBER_FLAGS)


def describe(key: str, *, user_id: Optional[str] = None,
             home_dir=None, include_chain: bool = False) -> SettingInfo:
    """Uniform view of one setting; raises ``KeyError`` for an unknown key."""
    from core.prefs import PREF_SCHEMA
    if key in PREF_SCHEMA:
        return _describe_pref(key, user_id, home_dir, include_chain)
    from core.flags import flag_for
    if flag_for(key) is not None:
        return _describe_flag(key, include_chain)
    from core.config_policy.members import is_member
    if is_member(key):
        return _describe_member(key, include_chain)
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
            from core.config_policy.members import OVERRIDES_ENV, flag_raw, is_member
            raw = flag_raw(spec.env_flag)
            if raw is not None and raw.strip() != "":
                label = (f"env:{OVERRIDES_ENV}" if is_member(spec.env_flag)
                         else f"env:{spec.env_flag}")
                rungs.append(Source(raw, label))
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
    from core.flags import flag_for, is_secret_flag, resolve_flag
    flag = flag_for(key)
    resolved = resolve_flag(key, dict(os.environ), dynamic_flag_default)
    secret = is_secret_flag(key)
    chain = ()
    if include_chain:
        rungs = []
        # 080 WS-1: the same three labels on every seat — env:systemd (the
        # deployed EnvironmentFile), env:shell (a process export), env:file.
        readable, sysval = _systemd_value(key)
        if readable and sysval is not None:
            rungs.append(Source(_mask(key, sysval), f"env:systemd ({_systemd_env_file()})"))
        raw = os.environ.get(key)
        origin = process_env_origin(key)
        if origin == "shell" or (origin == "systemd" and not (readable and sysval is not None)):
            label = ("env:shell" if origin == "shell"
                     else f"env:systemd ({_systemd_env_file()})")
            rungs.append(Source(_mask(key, raw), label))
        for cand in _existing_env_files():
            try:
                from core.env_file import read_env_file
                vals = read_env_file(cand)
                if key in vals:
                    rungs.append(Source(_mask(key, vals[key]), f"env:file ({cand})"))
            except Exception:
                continue
        dyn = dynamic_flag_default(key)
        if dyn is not None:
            rungs.append(Source(_mask(key, dyn[0]), str(dyn[1])))
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


def _describe_member(key: str, include_chain: bool) -> SettingInfo:
    """A posture member (D7): no env row of its own. The bundle sets it; the
    ``POLYROB_OVERRIDES`` line changes it."""
    from core.config_policy.members import (OVERRIDES_ENV, bundle_default, member_flag,
                                            member_raw, member_source)
    try:
        from core.flags_catalog import DERIVED_FROM
        bundles = ", ".join(DERIVED_FROM.get(key, ())) or "a posture bundle"
    except Exception:
        bundles = "a posture bundle"
    chain = ()
    if include_chain:
        rungs = []
        raw = member_raw(key)
        if raw is not None:
            rungs.append(Source(raw, f"env:{OVERRIDES_ENV}"))
        rungs.append(Source(bundle_default(key), "bundle default"))
        chain = tuple(rungs)
    return SettingInfo(
        key=key, namespace="flag", kind="bool", group="Posture bundles",
        description=(f"posture member — its bundle sets it ({bundles}); change only this "
                     f"member with {OVERRIDES_ENV}={key}=true|false"),
        effective=member_flag(key), source=member_source(key), applies="restart",
        sensitivity="flag", enforcement="enforced", secret=False, chain=chain,
    )


def _mask(key: str, value: str) -> str:
    from core.flags import is_secret_flag
    from core.security.redaction import redact_config_urls
    return "(set, masked)" if is_secret_flag(key) else redact_config_urls(value)


def _flag_applies(key: str) -> str:
    # Conservative truth: env flags configure the NEXT process. Access-time
    # consumers pick changes up sooner, but promising "live" per-flag needs the
    # per-group audit (018 P2 metadata work) — until then, restart never lies.
    if key in _IMPORT_FROZEN_FLAGS:
        return "restart (frozen at import)"
    return "restart"


def _existing_env_files() -> list:
    """Existing .env candidate paths of the ONE ladder, highest first."""
    try:
        from core.paths import env_file_candidates
        return [c.path for c in env_file_candidates() if c.path.exists()]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# provenance of a process-env value (080 WS-1)
# ---------------------------------------------------------------------------

#: key -> (value, path) for every env var a ladder FILE put into this process
#: (``polyrob.app.bootstrap.load_env`` records it). A process value that is not
#: here — or no longer equals the loaded value — was exported by the shell or
#: the service manager.
_FILE_LOADED: dict = {}


def note_file_loaded(path, values: dict) -> None:
    """Record the keys a ladder file is about to set (absent from the env)."""
    for key, val in (values or {}).items():
        if val is not None and key not in os.environ:
            _FILE_LOADED[key] = (str(val), str(path))


def reset_file_loaded_env() -> None:
    """Forget the file-loaded record (tests)."""
    _FILE_LOADED.clear()


def _systemd_value(key: str):
    """``(readable, value)`` for *key* in the deployed systemd env file.

    ``readable`` False = a deployed box whose file this euid cannot read (the
    service identities: root 0600). ``(True, None)`` = not deployed, or the file
    does not hold the key."""
    try:
        import core.admin_data_home as adh
        if not adh._deployment_evidence():
            return True, None
        return True, adh.deployed_env_value(key)
    except Exception:
        return False, None


def process_env_origin(key: str) -> Optional[str]:
    """Where this process's value of *key* came from: ``file`` | ``systemd`` |
    ``shell`` — or None when the key is not in the process env."""
    raw = os.environ.get(key)
    if raw is None or str(raw).strip() == "":
        return None
    loaded = _FILE_LOADED.get(key)
    if loaded is not None and loaded[0] == str(raw):
        return "file"
    readable, sysval = _systemd_value(key)
    if not readable or (sysval is not None and str(sysval) == str(raw)):
        return "systemd"
    return "shell"


def _systemd_env_file() -> str:
    from core.admin_data_home import DEPLOYED_ENV_FILE
    return DEPLOYED_ENV_FILE


def pinning_layer(key: str):
    """The layer ABOVE the instance/home env file that pins *key*, or None.

    Returns ``(label, where, value)``: ``env:systemd`` (the deployed
    ``EnvironmentFile``, which every service of the instance starts with) or
    ``env:shell`` (a process-env value that did not come from a ladder file).
    The process env beats every file at the next start, so a different value
    written below it would apply nothing."""
    readable, sysval = _systemd_value(key)
    if readable and sysval is not None:
        return ("env:systemd", _systemd_env_file(), sysval)
    origin = process_env_origin(key)
    if origin == "systemd":
        return ("env:systemd", _systemd_env_file(), os.environ.get(key))
    if origin == "shell":
        return ("env:shell", "this process's environment (a shell or service export)",
                os.environ.get(key))
    return None


def shadowed_write_refusal(key: str, value) -> Optional[str]:
    """The refusal for a write a higher layer pins to a DIFFERENT value, or None."""
    try:
        layer = pinning_layer(key)
    except Exception:
        logger.debug("config_service: pin check failed for %s", key, exc_info=True)
        return None
    if layer is None or str(layer[2]) == str(value):
        return None
    label, where, _val = layer
    if label == "env:systemd":
        return (f"nothing written: `{key}` is set in {where}, which wins — "
                f"change it there")
    return (f"nothing written: `{key}` is set in {where}, which wins over "
            f"{home_env_path()} — unset or change it there")


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

# Console writability (S8, 024 §2.6, WEB-4) is a per-flag CLASS, declared in
# the Access column of docs/CONFIGURATION.md and generated into
# ``core.flags_catalog.ACCESS`` (2026-10-10). It used to be derived from the
# SPELLING of a name (segments, prefixes, suffixes, catalog-group words), so a
# new flag's protection depended on what it happened to be called.
#
#   console — the owner may write it from a remote surface (console, Telegram,
#             the API) as well as the local CLI;
#   file    — operator env file / local CLI only: it binds the owner, a money
#             bound, approval, posture or trust, a guard, a tool set, or where
#             data, code, inference or money goes. A remote write lands in
#             the instance env file every process of the instance reads, so a
#             remote surface must never set one of these;
#   secret  — file, and the value is masked.
#
# An UNKNOWN flag-shaped name is refused (fail closed); a typed preference key
# (``budget.wallet_daily_usd``) is not a flag and has its own trust ladder.
# Pinned by tests/unit/core/test_console_unwritable_flags.py.

#: A SAFETY stop: a remote surface may raise it (halt), never lower it — the
#: resume path is the owner pause record (`/resume`), not an env edit.
_RAISE_ONLY_FLAGS = frozenset({"AUTONOMY_HALT"})

# Flag names only (UPPER_SNAKE). A typed preference key (``budget.wallet_daily_usd``)
# reads money-shaped but has its OWN trust ladder (guarded ⇒ queued for owner
# review) and must never be swallowed by this rule.
_FLAG_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_console_unwritable(key: str) -> bool:
    """Whether *key* is an env flag no REMOTE surface may write (S8).

    True ⇒ the console/telegram/API PATCH surfaces refuse it at every posture;
    the local CLI (`polyrob config set`) is the only writer. Never raises.
    """
    name = str(key or "")
    if not _FLAG_NAME_RE.match(name):
        return False          # a pref key / anything non-flag-shaped
    try:
        from core.flags import access_class
        cls = access_class(name)
    except Exception:         # pragma: no cover — fail CLOSED on a broken import
        return True
    return cls != "console"   # file, secret, or unknown (None) ⇒ refused


def remote_write_refused(key: str, value) -> bool:
    """Whether a REMOTE surface must refuse writing *value* to *key*.

    :func:`is_console_unwritable` with one exception: a raise-only safety stop
    (``AUTONOMY_HALT``) may be SET to a true value from the console or a chat
    surface — the owner can always halt — but never cleared. Never raises."""
    if str(key or "") in _RAISE_ONLY_FLAGS:
        try:
            from core.env import parse_bool
            return parse_bool(str(value or ""), False, strict=True) is not True
        except Exception:     # pragma: no cover
            return True
    return is_console_unwritable(key)


def closest_key(key: str) -> Optional[str]:
    """Closest documented key across BOTH namespaces (prefs + catalog flags)."""
    hits = _suggest(key, n=1)
    return hits[0] if hits else None


def _suggest(key: str, n: int = 3) -> list:
    from core.key_suggest import suggest_keys
    try:
        names = known_keys()
    except Exception:
        return []
    return suggest_keys(str(key or ""), names, n=n)


def unknown_key_message(key: str) -> str:
    """The ONE refusal for an unknown setting: names up to three close keys."""
    hits = _suggest(key)
    if not hits:
        return f"unknown setting: {key} — not a documented flag or preference"
    tail = hits[0] if len(hits) == 1 else f"{', '.join(hits[:-1])} or {hits[-1]}"
    return f"unknown setting: {key} (did you mean {tail}?)"


def set_value(key: str, value: str, *, scope: Optional[str] = None,
              user_id: Optional[str] = None, home_dir=None,
              confirm: bool = False, surface: str = "local",
              allow_unknown: bool = False, live: bool = False) -> SetResult:
    """Route one write to the owning store (:func:`_route_write`). Never raises.

    077: a write that applies only at the next start is recorded as WAITING
    (``core.lifecycle.record_pending`` — ``/status``, ``polyrob doctor`` and
    ``polyrob service status`` show it until the service restarts), and the
    reply names the restart that applies it when a service is running.
    """
    res = _route_write(key, value, scope=scope, user_id=user_id, home_dir=home_dir,
                       confirm=confirm, surface=surface, allow_unknown=allow_unknown,
                       live=live)
    if not (res.ok and str(res.applies or "").startswith("restart")):
        return res
    try:
        from core import lifecycle
        lifecycle.record_pending(key, scope or "global")
        store = str(res.store or "")
        hint = lifecycle.restart_hint(
            surface, written=Path(store) if store.endswith(".env") else None)
    except Exception:  # noqa: BLE001 — a hint never fails a completed write
        logger.debug("config_service: pending-restart record skipped", exc_info=True)
        hint = ""
    if not hint:
        return res
    from dataclasses import replace
    return replace(res, message=f"{res.message}\n{hint}", notes=(*res.notes, hint))


def _route_write(key: str, value: str, *, scope: Optional[str] = None,
                 user_id: Optional[str] = None, home_dir=None,
                 confirm: bool = False, surface: str = "local",
                 allow_unknown: bool = False, live: bool = False) -> SetResult:
    """Route one write to the owning store. Never raises.

    scope: ``user`` (preferences.toml — required for pref keys) or ``global``
    (the home ``.env`` — :func:`home_env_path`) for flag keys. ``project``
    (``./.polyrob/.env``) is REFUSED for an env key: the local CLI never loads
    a per-directory env file (a cloned directory could supply it), so the
    write would have no effect (:func:`project_scope_refusal`).
    Omitted scope defaults to the key's natural store (pref→user,
    flag→global).

    surface: ``local`` (CLI/REPL, the default) or a remote surface label
    (``console``, ``telegram``, …). The credential-surface refusal is enforced
    HERE — in the oracle — not only at the webview call site, so any surface
    that grows a flag-write path inherits it (UX assessment 2026-08-07, Q9).

    026 P2 — this is the ONE write path: ``polyrob config set``, the REPL
    ``/config set`` and the webview PATCH all call it.

    allow_unknown: the CLI ``--force`` escape hatch — write an uncataloged KEY
    raw. ``surface="local"`` only. A secret-shaped KEY (``core.secrets``) is
    writable raw from the local surface without it (a credential needs no
    catalog row to be stored).

    live: the caller is the process that will READ the flag (the REPL). A key
    in ``core.config_policy.live_apply.LIVE_APPLY_SAFE`` is then also set in
    ``os.environ`` (026 P5). Money, approval, ingress and import-frozen flags
    are never live, whatever the caller asks.
    """
    from core.prefs import PREF_SCHEMA
    if scope is not None and scope not in _SCOPES:
        return SetResult(False, "refused", f"unknown scope: {scope}")
    # M12 (2026-09-23): a CR/LF/NUL in a value is never legitimate and, in an
    # env write, splits the line into a second flag. Refused for every store.
    if value is not None and any(c in str(value) for c in ("\r", "\n", "\0")):
        return SetResult(False, "invalid",
                         f"invalid value for {key}: a value must not contain "
                         "CR, LF or NUL")
    if surface != "local" and remote_write_refused(key, value):
        return SetResult(
            False, "refused",
            f"'{key}' selects the agent's owner binding, money bounds, approval "
            f"policy, trust posture or credential surface and is not writable "
            f"from a remote surface — set it from the local CLI "
            f"(`polyrob config set {key} <value>`)")
    if key in PREF_SCHEMA:
        return _set_pref(key, value, user_id, home_dir, confirm)
    if scope == "project":
        return SetResult(False, "refused", project_scope_refusal(key))
    from core.config_policy.members import is_member
    if is_member(key):
        return _set_member(key, value, scope or "global", surface=surface, live=live)
    from core.flags import REGISTRY, pattern_flag_for
    if key in REGISTRY or pattern_flag_for(key) is not None:
        return _set_flag(key, value, scope or "global", surface=surface, live=live)
    if surface == "local" and (allow_unknown or _is_secret_shaped(key)):
        return _set_raw(key, value, scope or "global", forced=allow_unknown)
    hint = closest_key(key)
    if hint:
        return SetResult(False, "refused",
                         f"unknown key: {key} (did you mean {hint}?)",
                         suggestion=hint)
    return SetResult(False, "refused",
                     f"unknown key: {key} — not a documented flag or preference")


def _is_secret_shaped(key: str) -> bool:
    try:
        from core.secrets import is_secret_key
        return bool(is_secret_key(key))
    except Exception:
        return False


def _write_env(path: Path, key: str, value: str, scope: str) -> Optional[str]:
    """The ONE env-file write (0600). Returns an error string, or None on success.

    There is no project-file write: :func:`set_value` refuses ``scope="project"``
    before any writer runs, and :func:`_env_path` refuses it again."""
    try:
        from core.env_file import upsert_env_var
        upsert_env_var(path, key, str(value), secure=True)
        # 080 WS-1: the instance file is read by every service identity of the
        # instance (group polyrob-data), so it is 0640, not 0600.
        from core.runtime_paths import instance_env_path
        if instance_env_path() == Path(path):
            os.chmod(path, 0o640)
    except Exception as e:
        return f"write failed: {e}"
    return None


def _set_raw(key: str, value: str, scope: str, *, forced: bool) -> SetResult:
    """An uncataloged KEY from the local surface: ``--force`` or a secret."""
    if scope == "user":
        return SetResult(False, "refused",
                         f"'{key}' is an env key — scope must be global")
    from core.env_file import env_write_error
    write_err = env_write_error(key, str(value))
    if write_err:
        return SetResult(False, "invalid", write_err)
    pinned = shadowed_write_refusal(key, value)
    if pinned:
        return SetResult(False, "refused", pinned)
    path = _env_path(scope)
    err = _write_env(path, key, value, scope)
    if err:
        return SetResult(False, "invalid", err)
    display = "(set, masked)" if _is_secret_shaped(key) else _mask(key, str(value))
    tail = " (--force override)" if forced else " (takes effect: restart)"
    return SetResult(True, "written", f"set {key}={display} in {path}{tail}",
                     store=str(path), applies="restart")


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


def _set_flag(key: str, value: str, scope: str, *, surface: str = "local",
              live: bool = False) -> SetResult:
    from core.flags import REGISTRY, is_secret_flag, pattern_flag_for
    from core.prefs import shape_of_default, value_matches_shape
    flag = REGISTRY.get(key) or pattern_flag_for(key)
    if flag is None:
        return SetResult(False, "refused",
                         f"unknown key: {key} — not a documented flag or preference")
    if scope == "user":
        return SetResult(False, "refused",
                         f"'{key}' is an env flag — scope must be global")
    # A secret-shaped NAME (``core.secrets.is_secret_key`` — e.g. *_TOKENS) is
    # never shape-validated: the refusal would echo the value back.
    if not (is_secret_flag(key) or _is_secret_shaped(key)):
        # 026 P1.4: enum-shaped flags reject invalid members with the valid set
        # (a typo'd AUTONOMY_MODE used to write cleanly and silently degrade).
        from core.config_policy.flag_enums import enum_error
        enum_err = enum_error(key, value)
        if enum_err:
            from core.security.redaction import redact_config_urls
            return SetResult(False, "invalid", redact_config_urls(enum_err))
        shape = shape_of_default(flag.default_doc)
        if not value_matches_shape(value, shape):
            return SetResult(
                False, "invalid",
                f"{key} expects a {shape} value (documented default: "
                f"{flag.default_doc}); got {_mask(key, value)!r}")
    from core.env_file import env_write_error
    write_err = env_write_error(key, str(value))
    if write_err:
        return SetResult(False, "invalid", write_err)
    pinned = shadowed_write_refusal(key, value)
    if pinned:
        return SetResult(False, "refused", pinned)
    path = _env_path(scope)
    err = _write_env(path, key, value, scope)
    if err:
        return SetResult(False, "invalid", err)
    applies = _flag_applies(key)
    note = ""
    if key in _IMPORT_FROZEN_FLAGS:
        note = (" — this value is frozen at import: the running process never "
                "re-reads it; it takes effect on the next start")
    applied_live = False
    if live and surface == "local":
        from core.config_policy.live_apply import live_apply_allowed
        if live_apply_allowed(key):
            os.environ[key] = str(value)
            _FILE_LOADED[key] = (str(value), str(path))  # it came from this file write
            applied_live = True
            applies = "live"
    display = ("(set, masked)" if (is_secret_flag(key) or _is_secret_shaped(key))
               else _mask(key, str(value)))
    effect = ("applies: live in this session + persisted for the next start"
              if applied_live else "takes effect: restart")
    message = f"set {key}={display} in {path} ({effect}){note}"
    notes = tuple(post_write_notes(key, str(value), scope, surface=surface))
    for extra in notes:
        message += "\n" + extra
    return SetResult(True, "written", message, store=str(path), applies=applies,
                     notes=notes, live=applied_live)


def _set_member(key: str, value: str, scope: str, *, surface: str = "local",
                live: bool = False) -> SetResult:
    """D7: a posture member is written as its entry in the ``POLYROB_OVERRIDES``
    line of the same env file (a stale direct line of the member is removed).
    ``POLYROB_OVERRIDES`` is Access ``file``, so a remote surface never gets here."""
    from core.config_policy.live_apply import live_apply_allowed
    from core.config_policy.members import OVERRIDES_ENV, with_override
    from core.env import _EXPLICIT_TRUE, _FALSEY
    if scope == "user":
        return SetResult(False, "refused",
                         f"'{key}' is an env flag — scope must be global")
    low = str(value or "").strip().lower()
    if not (low in _EXPLICIT_TRUE or (low and low in _FALSEY)):
        return SetResult(False, "invalid",
                         f"{key} is a posture member and expects true or false; got {value!r}")
    path = _env_path(scope)
    try:
        from core.env_file import read_env_file
        current = read_env_file(path).get(OVERRIDES_ENV, "")
    except Exception as e:
        return SetResult(False, "invalid", f"cannot read {path}: {e}")
    res = _set_flag(OVERRIDES_ENV, with_override(current, key, value), scope,
                    surface=surface, live=False)
    if not res.ok:
        return res
    try:
        from core.env_file import remove_env_var
        remove_env_var(path, key, secure=True)
    except Exception:
        logger.debug("config_service: stale member line not removed for %s", key, exc_info=True)
    applied_live = False
    applies = res.applies
    if live and surface == "local" and live_apply_allowed(key):
        os.environ[OVERRIDES_ENV] = with_override(os.environ.get(OVERRIDES_ENV), key, value)
        _FILE_LOADED[OVERRIDES_ENV] = (os.environ[OVERRIDES_ENV], str(path))
        applied_live, applies = True, "live"
    effect = ("applies: live in this session + persisted for the next start"
              if applied_live else "takes effect: restart")
    word = "true" if low in _EXPLICIT_TRUE else "false"
    return SetResult(True, "written",
                     f"set {key}={word} as {OVERRIDES_ENV} entry in {path} ({effect})",
                     store=res.store, applies=applies, notes=res.notes, live=applied_live)


def home_env_path() -> Path:
    """The env file this process READS first — and so the only one it writes.

    The top tier of ``core.paths.env_file_candidates``: on a box with a deployed
    instance the ONE instance file ``<deployed data home>/.env`` (080 WS-1 —
    agent, console, email, gateway and a root CLI all resolve it), else
    ``$POLYROB_HOME/.env`` (the active profile's ``.env`` when a profile is
    selected). A write resolved any other way can land in a file ``load_env``
    never opens."""
    from core.paths import env_file_candidates
    return env_file_candidates()[0].path


def legacy_project_env_path() -> Path:
    """``./.polyrob/.env`` — the per-directory file polyrob NO LONGER loads.

    Named only so a refusal or a note can point at it; nothing writes it."""
    return Path.cwd() / ".polyrob" / ".env"


def project_scope_refusal(key: str = "") -> str:
    """The ONE refusal every writer shows for a ``--project`` env write."""
    what = f"{key} " if key else ""
    return (f"nothing written: polyrob does not load the project env file "
            f"{legacy_project_env_path()} (a cloned directory could supply it), "
            f"so a {what}write there has no effect. The file the CLI reads is "
            f"{home_env_path()} — run the command without --project.")


def _env_path(scope: str) -> Path:
    """The env file a ``user``/``global`` write lands in: :func:`home_env_path`.

    ``project`` has no file: polyrob does not load ``./.polyrob/.env``, so a
    write there would configure nothing (:func:`project_scope_refusal`)."""
    if scope == "project":
        raise ValueError(project_scope_refusal())
    return home_env_path()


def post_write_notes(key: str, value: str, scope: str, *,
                     surface: str = "local") -> list:
    """Honesty notes for a just-written flag (026 P0.6 / P1.5 / P1.6).

    ONE builder every writer calls (`set_value`, `polyrob config set`, REPL
    `/config set`), so the shadow/clamp/server stories cannot diverge:
      - P0.6: a write into the project file, which is never loaded; a
        process-env value that differs from the effective file value.
      - P1.6 clamp echo: AUTONOMY_MODE=autonomous evaluates the would-be
        single-owner clamp NOW and names the missing prerequisite.
    Never raises; returns [] on any resolution error.
    """
    notes: list = []
    try:
        from core.env_file import read_env_file
        project_path = legacy_project_env_path()
        global_path = _env_path("global")
        glob_vals = read_env_file(global_path)
        # A project checkout is untrusted input: ``core.paths.env_file_candidates``
        # does not load ``./.polyrob/.env`` at all, so it shadows nothing — and a
        # write INTO it configures nothing.
        if scope == "project":
            notes.append(
                f"note: {project_path} is not loaded (a project directory cannot "
                f"configure the agent); write it to {global_path} instead with "
                f"`polyrob config set {key} … --global`")
        effective_file = glob_vals.get(key)
        env_raw = os.environ.get(key)
        # 080 WS-1: a value this process loaded from the file itself is not a
        # shell/systemd export — the old note blamed one falsely (sim B).
        if (env_raw is not None and str(env_raw).strip() != ""
                and effective_file is not None
                and str(env_raw) != str(effective_file)
                and process_env_origin(key) in ("shell", "systemd")):
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
    return notes
