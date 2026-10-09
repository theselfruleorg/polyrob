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
    from core.flags import REGISTRY
    from core.prefs import PREF_SCHEMA
    return list(PREF_SCHEMA.keys()) + list(REGISTRY.keys())


def describe(key: str, *, user_id: Optional[str] = None,
             home_dir=None, include_chain: bool = False) -> SettingInfo:
    """Uniform view of one setting; raises ``KeyError`` for an unknown key."""
    from core.prefs import PREF_SCHEMA
    if key in PREF_SCHEMA:
        return _describe_pref(key, user_id, home_dir, include_chain)
    from core.flags import flag_for
    if flag_for(key) is not None:
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
    from core.flags import flag_for, is_secret_flag, resolve_flag
    flag = flag_for(key)
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
    # 025: turning memory scopes OFF releases every quarantine at once.
    "MEMORY_SCOPES_ENABLED",
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
    "GUARD", "POLICY", "TRUST", "UNTRUSTED", "EGRESS", "SANDBOX",
    "HOST", "COMMAND", "SIGNER", "ENV", "DATA", "DOMAIN",
})
_UNWRITABLE_PREFIXES = (
    "WEBGATE_", "CODE_EXEC_", "ALLOWED_", "SHELL_", "DEFI_", "MCP_",
    "BROWSER_", "TOKEN_", "CORS_", "SKILLS_", "SELF_", "UNTRUSTED_",
)
_UNWRITABLE_EXACT = frozenset({
    "POLYROB_LOCAL", "POLYROB_LOCAL_OWNER", "AUTONOMY_MODE",
    "WEBVIEW_READ_ONLY", "WEBVIEW_AUTH_ENABLED", "WEBVIEW_HOST",
    "WEBVIEW_ALLOW_LOCAL_POSTURE",
    "DELEGATE_BLOCKED_TOOLS", "SELF_ENV_ENABLED", "SHELL_TOOLS_ENABLED",
    "ADMIN_WALLETS", "OUTBOUND_POLICY", "X402_PAYMENT_RECIPIENT",
    # 067 P2: which pack CODE runs in this process (host reach).
    "POLYROB_PACKS", "POLYROB_PACKS_DISABLED",
})
# WEB-4 (2026-10-07 review): the name families above still left security and
# trust knobs console-writable (WEB_FETCH_ALLOW_PRIVATE_URLS,
# HISTORY_SECRET_SCRUB, CORRESPONDENT_ACCESS_ENABLED, X402_TRUSTED_PROXIES,
# X402_PAYMENT_ADDRESS, X402_FACILITATOR_URL, POLYROB_TOOL_DENYLIST,
# FS_REALPATH_CONFINE, …). Three more rules, all derived — none names one flag:
#
#   catalog group — every flag the catalog files under a money group (the
#                   wallet / x402 / DeFi / trading / self-update sections of
#                   docs/CONFIGURATION.md) is operator-file-only;
#   trust words   — a name segment that switches a guard (ALLOW, PRIVATE,
#                   SCRUB, DENYLIST, CONFINE, PROXIES, …) or a tool exposure set
#                   (TOOLS, TOOLSET, IDS, RIG);
#   where-to      — a name that ENDS in a location (_URL, _PATH, _DIR, _FILE,
#                   _ROOT, _HOME, _IMAGE, _SCRIPT, …): where data, code or
#                   traffic goes.
_UNWRITABLE_GROUP_WORDS = ("wallet", "x402", "defi", "trading", "polyrob update")
_TRUST_SEGMENTS = frozenset({
    "ALLOW", "PRIVATE", "UNSIGNED", "SCRUB", "SCRUBBER", "SECRET", "REDACT",
    "DENYLIST", "CONFINE", "REALPATH", "PROXIES", "PROXY", "FORWARDED",
    "FACILITATOR", "CORRESPONDENT", "TRADE", "TRADING", "INSTALL", "THREAT",
    "PRIVILEGE", "SANITIZE", "PROTECT", "STRICT", "GATE", "SKIP", "REQUIRE",
    "PROVENANCE", "ENFORCED", "SECURITY", "AUTH", "OAUTH", "OAUTH2", "LOGIN",
    "SMTP", "IMAP", "GMAIL", "PHONE", "ADMIN", "OUTBOUND", "TOOLS", "TOOLSET",
    "IDS", "RIG", "HALT", "PROFILE", "PROFILES", "DEPS",
})
_LOCATION_SUFFIXES = (
    "_URL", "_URI", "_PATH", "_DIR", "_FILE", "_ROOT", "_HOME", "_IMAGE",
    "_BINARY", "_SCRIPT", "_REPO", "_PYPI", "_GATEWAY", "_CLIENT_ID", "_APP_ID",
    "_ADDRESS", "_DOMAINS", "_TREE", "_MANIFEST", "_PROVIDER", "_TRANSPORT",
    "_API_BASE", "_CONFIG", "_ACCOUNT", "_EMAIL",
)
_LOCATION_EXACT = frozenset({
    "ROB_LOCAL", "POLYROB_IN_DOCKER", "PRODUCTION", "ENVIRONMENT",
    "GROUP_CHAT_ENABLED", "GROUP_DEFAULT_MODE", "SINGULAR_CHAT_ENABLED",
    "APP_SERVICE_ENABLED",
})
#: The selector flags a model change rides on stay owner-writable: they pick a
#: registered provider, never an endpoint or a credential.
_WRITABLE_PROVIDER_SELECTORS = frozenset({
    "DEFAULT_PROVIDER", "CHAT_PROVIDER", "AUX_PROVIDER", "COMPACTION_PROVIDER",
})
#: Ordinary operational knobs a WEB-4 trust word or group caught by accident
#: (IMAP, GATE, REQUIRE, OUTBOUND): a poll interval, a cron cost gate, the
#: step parser's strictness and the send retry queue. None of them picks an
#: owner, a bound, a guard, a tool set or a destination, so the owner console
#: keeps them.
_WRITABLE_OPERATIONAL = frozenset({
    "EMAIL_IMAP_POLL_SEC", "WAKE_CHANGE_GATE", "REQUIRE_SCHEMA_KEYS",
    "OUTBOUND_QUEUE_ENABLED",
    # Natural-work sweep 2026-10-08: ordinary settings a catalog group (billing,
    # DeFi), a trust word (SCRUB, TOOLS, TOOLSET) or the _URL suffix caught. An
    # outage notice, provider failover, a notice hold, the console footer links,
    # display scrubbing (the stream scrub is always on) and the prompt-cache
    # shape of the tool list: none picks an owner, a bound, a guard, which tools
    # a run may use, or where data or money goes.
    "LLM_OUTAGE_NOTICE", "BILLING_FAILOVER_ENABLED", "TX_NOTIFY_SENT_HOLD_SEC",
    "POLYROB_SUPPORT_URL", "POLYROB_BRAND_URL", "POLYROB_ORG_URL",
    "POLYROB_TERMS_URL", "POLYROB_PRIVACY_URL",
    "THINK_SCRUBBER_ENABLED", "STREAM_BRAIN_SCRUB",
    "ANTHROPIC_DEFERRED_TOOLS", "STABLE_AUTONOMOUS_TOOLSET",
})
#: A SAFETY stop: a remote surface may raise it (halt), never lower it — the
#: resume path is the owner pause record (`/resume`), not an env edit.
_RAISE_ONLY_FLAGS = frozenset({"AUTONOMY_HALT"})


def _in_unwritable_group(name: str) -> bool:
    try:
        from core.flags import flag_for
        flag = flag_for(name)
    except Exception:         # pragma: no cover — fail CLOSED on a broken import
        return True
    if flag is None:
        return False
    group = str(flag.group or "").lower()
    return any(word in group for word in _UNWRITABLE_GROUP_WORDS)


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
    if name.startswith("X402_"):
        return True
    if name in _WRITABLE_OPERATIONAL:
        return False
    if segments & _TRUST_SEGMENTS or name in _LOCATION_EXACT:
        return True
    if (name.endswith(_LOCATION_SUFFIXES)
            and name not in _WRITABLE_PROVIDER_SELECTORS):
        return True
    if _in_unwritable_group(name):
        return True
    try:
        from core.flags import is_secret_flag
        return bool(is_secret_flag(name))
    except Exception:         # pragma: no cover — fail CLOSED on a broken import
        return True


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
    import difflib
    try:
        from core.prefs import PREF_SCHEMA, catalog_names
        names = list(PREF_SCHEMA.keys()) + list(catalog_names())
    except Exception:
        return None
    hits = difflib.get_close_matches(str(key or ""), names, n=1)
    return hits[0] if hits else None


def set_value(key: str, value: str, *, scope: Optional[str] = None,
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
    """The ONE env-file write (0600) + project gitignore housekeeping.
    Returns an error string, or None on success."""
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
        return f"write failed: {e}"
    return None


def _set_raw(key: str, value: str, scope: str, *, forced: bool) -> SetResult:
    """An uncataloged KEY from the local surface: ``--force`` or a secret."""
    if scope == "user":
        return SetResult(False, "refused",
                         f"'{key}' is an env key — scope must be project or global")
    from core.env_file import env_write_error
    write_err = env_write_error(key, str(value))
    if write_err:
        return SetResult(False, "invalid", write_err)
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
                         f"'{key}' is an env flag — scope must be project or global")
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


def home_env_path() -> Path:
    """The env file the local CLI READS — and so the only one it writes.

    The ``home`` tier of ``core.paths.env_file_candidates`` (``$POLYROB_HOME/
    .env``; the active profile's ``.env`` when a profile is selected). A write
    resolved any other way can land in a file ``load_env`` never opens."""
    from core.paths import env_file_candidates
    return env_file_candidates(local_mode=True)[0].path


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
    if scope == "project":
        return legacy_project_env_path()
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
