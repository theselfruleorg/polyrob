"""h_config.py — the ``/config`` REPL slash-command handler (owner-UX P1 T7).

Reads/writes the SAME two knob layers the rest of the CLI already resolves:

- **typed per-user preferences** (``core.prefs`` — ``PREF_SCHEMA``/
  ``validate_pref``/``write_preference``/``resolve_with_source``), a curated
  dotted-key namespace (``style.verbosity``, ``budget.wallet_daily_usd``, …);
- **env-flag toggles** documented in ``docs/CONFIGURATION.md`` and mirrored in
  ``core.flags_catalog.CATALOG`` (``CODE_EXEC_ENABLED``, ``GOAL_DAILY_QUOTA``,
  …), written to the project-scope ``./.polyrob/.env`` the same way
  ``polyrob config set`` does (``cli/commands/config.py::_upsert_env``).

Subcommands: ``list [group]``, ``get KEY``, ``set KEY VALUE [--confirm]``,
``check``. The core logic is the pure function ``cmd_config(ctx, args) ->
str`` over a tiny ``ConfigCtx`` dataclass (``user_id``, ``home_dir``) so it is
testable without a live REPL (see ``tests/unit/cli/test_h_config.py``); the
registered REPL closure in ``handlers.py`` builds a ``ConfigCtx`` from the
session ``CommandContext`` (mirroring how ``h_self.py`` resolves its
``home_dir``) and emits the returned string.

A ``set`` first tries the key as a preference (dotted, e.g. ``style.tone``),
then as a catalog env-flag (exact name, or a documented ``<...>`` dynamic
pattern by regex). Unknown in both namespaces gets a closest-match suggestion
computed across BOTH (``difflib`` over ``PREF_SCHEMA`` keys + catalog names).
Guarded preferences require an explicit ``--confirm`` suffix (Phase 1 — no
``/approve`` flow yet). Fail-open throughout: this module never raises into
the REPL dispatcher (write failures degrade to an error string, not an
exception) and never prints a secret env VALUE, only key names.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from cli.ui import candy


@dataclass
class ConfigCtx:
    """Everything ``/config`` needs — testable without a live REPL session."""

    user_id: str
    home_dir: Any


def cmd_config(ctx: ConfigCtx, args: List[str]) -> str:
    """Handle ``/config list|get|set|check`` and return the rendered reply.

    Empty *args* defaults to ``list`` (mirrors ``/kb``'s bare-invocation
    default). Never raises — every branch degrades to a friendly string.
    """
    sub = args[0].lower() if args else "list"
    rest = args[1:]
    try:
        if sub == "list":
            return _cmd_list(ctx, rest)
        if sub == "get":
            return _cmd_get(ctx, rest)
        if sub == "set":
            return _cmd_set(ctx, rest)
        if sub == "check":
            return _cmd_check(ctx, rest)
        if sub == "explain":
            return _cmd_explain(ctx, rest)
        if sub == "search":
            return _cmd_search(ctx, rest)
    except Exception as exc:  # fail-open: never crash the REPL dispatcher
        return f"/config {sub} failed: {exc}"
    return (
        f"unknown /config subcommand: {sub!r}\n"
        "usage: /config list [group] | get KEY | set KEY VALUE [--confirm] | "
        "explain KEY | search QUERY | check"
    )


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _all_names() -> List[str]:
    """Both namespaces ``/config set|get`` resolve against, for suggestions."""
    from core.prefs import PREF_SCHEMA, catalog_names
    return list(PREF_SCHEMA.keys()) + catalog_names()


def _closest_match(key: str) -> Optional[str]:
    hits = difflib.get_close_matches(key, _all_names(), n=1)
    return hits[0] if hits else None


def _approval_gate_enforcement_warning(ctx: ConfigCtx) -> str:
    """owner-UX P1 final review (item 3): a fresh ``approvals.require`` entry is
    just a name in a gated set — it does nothing unless the resolved approval
    PROVIDER is something other than ``auto`` (allow-all). Silently no-op'ing
    an owner's "gate this tool" intent is worse than an env-flag-set-but-unused
    gap, since the owner just took an explicit UX action expecting enforcement.
    Warn (never block the write) when the effective provider still resolves to
    ``auto`` after this write. Fail-open to no warning on any resolution error.
    """
    try:
        from tools.controller.approval import frozen_approval_provider
        from core import prefs
        env_provider = frozen_approval_provider()
        effective_provider = prefs.resolve(
            "approvals.provider", ctx.user_id, ctx.home_dir,
            env_value=env_provider, default=env_provider,
        )
    except Exception:
        return ""
    if effective_provider == "auto":
        return (
            "\n⚠ gate registered but approval provider is 'auto' (allow-all) — "
            "set approvals.provider to interactive_cli to enforce"
        )
    return ""


def _display_value_source(ctx: ConfigCtx, key: str, spec) -> Tuple[Any, str]:
    """Value+source for ``list``/``get`` — the actual EFFECTIVE value, not a
    partial view (owner-UX P1 final review, item 4).

    Delegates to ``core.prefs.display_effective`` (lifted there in owner-UX P2
    T2 so the agent-callable ``preferences`` action can render identically
    without reaching into ``cli/``). ``spec`` is accepted for call-site
    compatibility but no longer consulted here — the core helper re-derives it
    from ``PREF_SCHEMA``.
    """
    from core.prefs import display_effective
    return display_effective(key, ctx.user_id, ctx.home_dir)


# ---------------------------------------------------------------------------
# list [group]
# ---------------------------------------------------------------------------


def _cmd_list(ctx: ConfigCtx, rest: List[str]) -> str:
    from core.prefs import PREF_SCHEMA

    group_filter = rest[0] if rest else None
    by_group: "dict[str, list[str]]" = {}
    for key in sorted(PREF_SCHEMA):
        group = key.split(".", 1)[0]
        if group_filter and group != group_filter:
            continue
        by_group.setdefault(group, []).append(key)

    if not by_group:
        if group_filter:
            return candy.empty(f"preferences in group {group_filter!r}", yet=False)
        return candy.empty("preferences defined", yet=False)

    lines: List[str] = []
    for i, group in enumerate(sorted(by_group)):
        if i:
            lines.append("")
        lines.append(candy.section(group))
        rows = []
        for key in by_group[group]:
            spec = PREF_SCHEMA[key]
            value, source = _display_value_source(ctx, key, spec)
            advisory = ", advisory" if spec.enforcement == "advisory" else ""
            rows.append((key, f"{value}   ({source}, applies: {spec.applies}{advisory})"))
        lines.append(candy.kv_lines(rows))
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# get KEY
# ---------------------------------------------------------------------------


def _cmd_get(ctx: ConfigCtx, rest: List[str]) -> str:
    if not rest:
        return "usage: /config get KEY"
    key = rest[0]

    from core.prefs import PREF_SCHEMA, SENSITIVITY_GUARDED

    spec = PREF_SCHEMA.get(key)
    if spec is not None:
        value, source = _display_value_source(ctx, key, spec)
        lines = [candy.kv_lines([(key, f"{value}   ({source})")]), f"applies: {spec.applies}"]
        if spec.description:
            lines.append(spec.description)
        if spec.enforcement == "advisory":
            lines.append("enforcement: advisory — steers the agent's prompt only; "
                         "no hard runtime gate")
        if spec.sensitivity == SENSITIVITY_GUARDED:
            lines.append("sensitivity: guarded — set with `set KEY VALUE --confirm`")
        return "\n".join(lines)

    from core.prefs import catalog_lookup
    hit = catalog_lookup(key)
    if hit is not None:
        group, documented_default = hit
        raw_env = os.environ.get(key)
        current = raw_env if raw_env is not None else "(unset)"
        return (
            f"{candy.kv_lines([(key, f'{current}   (env-flag, group: {group})')])}\n"
            f"documented default: {documented_default}"
        )

    hint = _closest_match(key)
    suffix = f" (did you mean {hint}?)" if hint else ""
    return f"unknown key: {key}{suffix}"


# ---------------------------------------------------------------------------
# explain KEY / search QUERY (018 P2a — the config-service views)
# ---------------------------------------------------------------------------


def _cmd_explain(ctx: ConfigCtx, rest: List[str]) -> str:
    """Full provenance chain for one key (`git config --show-origin` style)."""
    if not rest:
        return "usage: /config explain KEY"
    key = rest[0]
    from core import config_service
    try:
        info = config_service.explain(key, user_id=ctx.user_id,
                                      home_dir=ctx.home_dir)
    except KeyError:
        hint = _closest_match(key)
        suffix = f" (did you mean {hint}?)" if hint else ""
        return f"unknown key: {key}{suffix}"
    lines = [
        candy.kv_lines([(key, f"{info.effective}   ({info.source})")]),
        f"namespace: {info.namespace} | kind: {info.kind} | applies: {info.applies}",
    ]
    if info.enforcement == "advisory":
        lines.append("enforcement: advisory — steers the agent's prompt only")
    if info.description:
        lines.append(info.description)
    if info.chain:
        lines.append(candy.section("provenance (highest wins)"))
        lines.append(candy.kv_lines([(s.origin, str(s.value)) for s in info.chain]))
    return "\n".join(lines)


def _cmd_search(ctx: ConfigCtx, rest: List[str]) -> str:
    """Fuzzy search across BOTH namespaces (prefs + ~400 env flags)."""
    if not rest:
        return "usage: /config search QUERY"
    query = " ".join(rest)
    from core import config_service
    hits = config_service.search(query, user_id=ctx.user_id,
                                 home_dir=ctx.home_dir, limit=25)
    if not hits:
        return candy.empty(f"settings matching {query!r}", yet=False)
    rows = []
    for info in hits:
        advisory = ", advisory" if info.enforcement == "advisory" else ""
        rows.append((info.key,
                     f"{info.effective}   ({info.source}, applies: "
                     f"{info.applies}{advisory})"))
    return candy.kv_lines(rows)


# ---------------------------------------------------------------------------
# set KEY VALUE [--confirm]
# ---------------------------------------------------------------------------


def _cmd_set(ctx: ConfigCtx, rest: List[str]) -> str:
    if len(rest) < 2:
        return "usage: /config set KEY VALUE [--confirm]"
    key = rest[0]
    value_parts = rest[1:]
    confirm = "--confirm" in value_parts
    value_parts = [p for p in value_parts if p != "--confirm"]
    value = " ".join(value_parts).strip()
    if not value:
        return "usage: /config set KEY VALUE [--confirm]"

    from core.prefs import PREF_SCHEMA, SENSITIVITY_GUARDED, write_preference, load_preferences

    spec = PREF_SCHEMA.get(key)
    if spec is not None:
        if spec.sensitivity == SENSITIVITY_GUARDED and not confirm:
            return (
                f"'{key}' is guarded — change it with /approve or confirm via "
                f"`/config set {key} {value} --confirm`."
            )
        ok, err = write_preference(ctx.home_dir, ctx.user_id, key, value)
        if not ok:
            return f"error: {err}"
        coerced = load_preferences(ctx.home_dir, ctx.user_id).get(key)
        reply = f"Set {key} = {coerced} (applies: {spec.applies})."
        if key == "approvals.require":
            reply += _approval_gate_enforcement_warning(ctx)
        return reply

    from core.prefs import catalog_lookup, shape_of_default, value_matches_shape

    hit = catalog_lookup(key)
    if hit is not None:
        _group, documented_default = hit
        shape = shape_of_default(documented_default)
        if not value_matches_shape(value, shape):
            return (
                f"error: {key} expects a {shape} value (documented default: "
                f"{documented_default}); got {value!r}"
            )
        path = Path.cwd() / ".polyrob" / ".env"
        try:
            from cli.commands.config import _upsert_env
            _upsert_env(path, key, value, secure=True)
            try:
                from cli.gitignore import ensure_polyrob_gitignored
                ensure_polyrob_gitignored(Path.cwd(), require_git_repo=True)
            except Exception:
                pass  # gitignore housekeeping must never block a set
        except Exception as exc:
            return f"error: failed to write {key} to {path}: {exc}"
        return f"Set {key}={value} in {path} (takes effect: restart)."

    hint = _closest_match(key)
    suffix = f" (did you mean {hint}?)" if hint else ""
    return f"unknown key: {key}{suffix}"


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def _cmd_check(ctx: ConfigCtx, rest: List[str]) -> str:
    from core.paths import env_file_candidates
    from core.prefs import check_env_files, find_invalid_preferences

    # check_env_files merges later-paths-win, so feed it REVERSED precedence:
    # the SSOT helper's project-beats-home resolution survives the merge (R-1).
    cands = env_file_candidates(local_mode=True)
    findings = list(check_env_files(
        [c.path for c in reversed(cands) if c.tier in ("project", "home")]))

    for key, err in find_invalid_preferences(ctx.home_dir, ctx.user_id):
        findings.append(f"preferences.toml: {key}: {err}")

    # DEFAULT_PROVIDER-shadows-cli.json (cli-layer concern — core.prefs stays
    # cli-import-free): only meaningful when a default was actually persisted.
    try:
        from cli.config_store import env_default_override_note, get_default_model
        saved_provider, _saved_model = get_default_model()
        if saved_provider:
            note = env_default_override_note(saved_provider)
            if note:
                findings.append(note)
    except Exception:
        pass

    if not findings:
        return "config check: no issues found."
    lines = [f"config check: {len(findings)} finding(s):"]
    lines.extend(candy.bullet(f) for f in findings)
    return "\n".join(lines)
