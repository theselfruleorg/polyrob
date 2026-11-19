"""The health slot: the install checks that name no credential (120 W8).

ONE home for the line builders `polyrob doctor --full` printed from the CLI
seat — the optional extras, the packs, the browser install and rail, the
database schema, the data-home layout and the Docker sandbox. The doctor (and
the console System page through it) prints each line at its old place;
`agent_status(view="health")` prints :func:`health_slot_lines`, the same
builders over the same home.

⚠️ What does NOT live here, on purpose:

- the provider credential lines and their file provenance stay in the CLI
  (llm-auth.md: the agent's config surface reaches ``config_service.explain``
  only); so do the wallet, the treasury and the x402 endpoint lines;
- a check that needs a tier above ``core`` (the sqlite-vec probe, the code-exec
  backends, the live-activity probe, the service unit) stays in its seat. The
  code's schema head lives in ``migrations`` (tier 5): it reaches this module
  through :func:`register_schema_head`, which ``migrations.version_manager``
  calls at import; a process that never imported it says "unknown".

Every builder is a read. It never repairs, never writes, never raises. A check
that could not run says so in its own line; it never reads as healthy.
"""
import importlib.util
import logging
import os
from pathlib import Path
from typing import Callable, List, Mapping, Optional

logger = logging.getLogger(__name__)


# --- optional extras ------------------------------------------------------------------

def server_extra_line() -> str:
    """[server]-extra presence (O6) — `polyrob serve` raises a raw ImportError
    without it; say so BEFORE the user hits that. Probes come from the ONE map
    (`core.optional_extras.MODULES_FOR_EXTRA`) — 058 put python-magic in this
    extra, and a server without it REFUSES uploads."""
    from core.optional_extras import MODULES_FOR_EXTRA
    missing = [m for m in MODULES_FOR_EXTRA["server"] if importlib.util.find_spec(m) is None]
    if not missing:
        return "server extra: present (`polyrob serve` available)"
    return ("server extra: absent (" + ", ".join(missing) + " missing) — "
            "run `pip install 'polyrob[server]'` before `polyrob serve`")


def optional_extras_line(local_on: bool = False) -> str:
    """058: which optional extras this install carries, and how a missing one
    arrives — on first use (``LAZY_DEPS_MODE`` trusted|legacy) or by `pip install`
    (mode ``off``). One line, never a guess: an extra is present only when EVERY
    probe module imports.

    067 P0.2: the mode is the one the runtime resolves (``lazy_deps_mode()``,
    default ``trusted`` on the CLI AND on a server), not a re-derivation from a
    second boolean. ``local_on`` is kept for the caller's
    signature; the mode no longer depends on it."""
    from core.config_policy.capability_toggles import lazy_deps_mode
    from core.optional_extras import MODULES_FOR_EXTRA, extra_available
    watched = ("gemini", "anthropic", "docs", "media", "memory-vector", "browser", "crypto")
    present = [e for e in watched if e in MODULES_FOR_EXTRA and extra_available(e)]
    absent = [e for e in watched if e in MODULES_FOR_EXTRA and not extra_available(e)]
    mode = lazy_deps_mode()
    how = (f"install on first use (LAZY_DEPS_MODE={mode})" if mode != "off"
           else "pip install 'polyrob[<extra>]' (LAZY_DEPS_MODE=off — a server adds it to PROD_EXTRAS)")
    return (f"optional extras: present {', '.join(present) or '—'} · absent "
            f"{', '.join(absent) or '—'} · a missing one: {how}")


# --- packs ----------------------------------------------------------------------------

def packs_line(*, load: bool = True) -> str:
    """067 P2: installed / loaded / disabled / refused packs (``polyrob pack doctor``
    has the detail). ``load=True`` (the doctor) loads the enabled packs, as a
    session would; ``load=False`` (the agent, whose session loaded them at boot)
    only reads the state this process holds."""
    try:
        from core.packs.state import summary_line
        if load:
            from core.packs.loader import load_packs
            load_packs()
        return summary_line()
    except Exception as e:  # noqa: BLE001
        return f"packs: unavailable ({type(e).__name__}: {e})"


# --- the browser ----------------------------------------------------------------------

def playwright_line(env: Mapping[str, str]) -> str:
    """Playwright + chromium probe (O6) — the guide's own most-common issue.
    Filesystem heuristic over the browsers cache; never launches anything."""
    if importlib.util.find_spec("playwright") is None:
        return "playwright: not installed (browser tool unavailable; web_fetch unaffected)"
    override = (env.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
    if override:
        candidates = [Path(override)]
    else:
        home = Path.home()
        candidates = [
            home / "Library" / "Caches" / "ms-playwright",   # macOS
            home / ".cache" / "ms-playwright",               # Linux
        ]
        local_app = (env.get("LOCALAPPDATA") or "").strip()
        if local_app:
            candidates.append(Path(local_app) / "ms-playwright")  # Windows
    for cand in candidates:
        try:
            if cand.is_dir() and any(p.name.startswith("chromium") for p in cand.iterdir()):
                return "playwright: installed, chromium present"
        except OSError:
            continue  # an unreadable cache reads as "no chromium" (a warning, never healthy)
    return ("playwright: installed but NO chromium browser — run "
            "`python -m playwright install chromium`")


def browser_rail_line(*, refresh: bool = True, probe: bool = True) -> str:
    """The browser RAIL (custody-aware), distinct from the playwright INSTALL line
    above. A custody process cannot launch the chromium the line above found; it
    can only connect to a separately isolated browser. A loopback endpoint is
    probed (a local read). The doctor passes ``refresh=True`` (a fresh probe is
    what the owner asks for there); the agent passes ``probe=False``, the read
    `/status` makes: a remote endpoint is reported as configured, unprobed."""
    try:
        from core.security.browser_rail import browser_rail_status
        rail = browser_rail_status(refresh=refresh, probe=probe)
    except Exception as e:  # never fail doctor on a probe
        return f"browser rail: unreadable ({type(e).__name__})"
    return f"browser rail: {rail.line()}"


# --- the database schema --------------------------------------------------------------

#: ``() -> str`` — the code's migration HEAD. ``migrations.version_manager``
#: registers it at import: core may not import migrations (layering ratchet).
_SCHEMA_HEAD: Optional[Callable[[], str]] = None


def register_schema_head(fn: Optional[Callable[[], str]]) -> None:
    """Called once by the migrations tier at import. Last registration wins."""
    global _SCHEMA_HEAD
    _SCHEMA_HEAD = fn


def schema_status_line(data_home, *, code_head: Optional[Callable[[], str]] = None) -> str:
    """DB-schema-vs-code check (U10): compare bot.db's recorded schema version
    against the code's migration HEAD. Read-only; never raises. *code_head*
    defaults to the registered one (:func:`register_schema_head`)."""
    head_fn = code_head or _SCHEMA_HEAD
    if head_fn is None:
        return ("db schema: not checked here (this process did not load the migrations; "
                "`polyrob doctor --full` checks it)")
    try:
        head = head_fn()
    except Exception as exc:
        return (f"db schema: unknown (could not resolve code schema version: "
                f"{type(exc).__name__})")
    from core.runtime_paths import bot_db_default_path
    db_path = Path(bot_db_default_path(str(data_home)))
    if not db_path.is_file():
        return f"db schema: no bot.db yet (baselined at {head} on first boot)"
    try:
        from core.sqlite_util import read_only_connect
        con = read_only_connect(db_path)
        try:
            rows = con.execute("SELECT version FROM schema_versions").fetchall()
        finally:
            con.close()
    except Exception as exc:
        if "no such table" not in str(exc):
            return f"db schema: unreadable ({type(exc).__name__}: {exc})"
        rows = []
    if not rows:
        return "db schema: not versioned yet (stamped at next boot)"

    def _key(v: str):
        try:
            return tuple(int(p) for p in v.split("."))
        except Exception:
            return (0,)

    current = max((r[0] for r in rows), key=_key)
    if current == head:
        return f"db schema: {current} (up to date)"
    return (f"! db schema {current} behind code {head} — run "
            f"`python -m migrations.migrate upgrade`")


# --- the data-home layout -------------------------------------------------------------

def _bot_dbs(homes) -> List[Path]:
    from core.runtime_paths import bot_db_default_path
    seen, out = set(), []
    for h in homes:
        for c in (Path(bot_db_default_path(str(h))),
                  Path(bot_db_default_path(str(h / "data")))):
            try:
                key = c.resolve()
                if key in seen or not c.is_file() or c.stat().st_size == 0:
                    continue
            except OSError:
                continue
            seen.add(key)
            out.append(key)
    return out


def data_home_split_lines(env: Mapping[str, str], *,
                          unit_without_pin: Optional[Path] = None,
                          unit_read: bool = True) -> List[str]:
    """The data-home split as a CRITICAL item (2026-10-29): every bot.db, which
    home each kind of process reads, and the one line that pins them together.

    *unit_without_pin* is the installed user service unit that predates the
    ``POLYROB_DATA_DIR`` pin (the CLI seat reads it: the unit path is the
    installer's). A caller that did not read the unit passes
    ``unit_read=False``, and the all-clear line says the unit was not read."""
    from core.paths import polyrob_home
    from core.runtime_paths import bot_db_default_path, resolve_runtime_paths

    explicit = (env.get("POLYROB_DATA_DIR") or "").strip()
    local = resolve_runtime_paths(local=True).data_home
    server = resolve_runtime_paths(local=False).data_home
    home = polyrob_home()
    homes = [Path(explicit).expanduser()] if explicit else []
    homes += [local, server, home, home / "data"]
    dbs = _bot_dbs(homes)
    reads = Path(bot_db_default_path(str(local))).resolve()
    lines: List[str] = []
    if len(dbs) > 1:
        lines.append(
            "CRITICAL data home split: bot.db exists at " + ", ".join(str(d) for d in dbs)
            + f". This process reads {reads}. Stop the services, keep the database that "
            "holds your data, set POLYROB_DATA_DIR=<its home> in the service unit and the "
            ".env file, then restart (the home is the folder that holds database/).")
    if not explicit and local.resolve() != server.resolve():
        lines.append(
            f"CRITICAL data home split: the CLI reads {local}, a process without "
            f"POLYROB_LOCAL (the service, migrate) reads {server}. Set "
            "POLYROB_DATA_DIR=<the home that holds your data> in the service unit and "
            "the .env file.")
    if unit_without_pin is not None:
        lines.append(
            f"! service unit {unit_without_pin} does not pin POLYROB_DATA_DIR; run "
            "`polyrob service install` again so the service and the CLI read one home")
    if not lines:
        tail = "" if unit_read else "; the service unit is not read in this process"
        lines.append(f"data home: {explicit or local} (one home for every process{tail})")
    return lines


def legacy_tree_line(env: Mapping[str, str], data_home) -> Optional[str]:
    """The 1.4.0 ``<home>/data`` tree line (``core.legacy_data_tree``), or None."""
    from core.legacy_data_tree import legacy_tree_doctor_line
    return legacy_tree_doctor_line(Path(data_home), data_dir=env.get("DATA_DIR") or "",
                                   db_path=env.get("DB_PATH") or "")


# --- the agent's view -----------------------------------------------------------------

def _guard(name: str, build: Callable[[], object]) -> List[str]:
    """One check's lines; a check that raised is NAMED, never dropped."""
    try:
        out = build()
    except Exception as exc:  # noqa: BLE001 — one check never hides the others
        return [f"{name}: could not be checked ({type(exc).__name__}: {str(exc)[:120]})"]
    if out is None:
        return []
    return [out] if isinstance(out, str) else list(out)


def mask_paths(text: str, data_home) -> str:
    """Replace this install's own roots by their names: ``<data home>``,
    ``<polyrob home>`` and ``~``. `/status` prints no path; the agent's view of
    the slot keeps the layout (``<data home>/database/bot.db``) and drops the
    host's directory names."""
    import re
    from core.paths import polyrob_home
    roots = []
    for root, name in ((data_home, "<data home>"), (_safe(polyrob_home), "<polyrob home>"),
                       (_safe(Path.home), "~")):
        for form in {str(root or ""), _resolved(root)}:
            form = form.rstrip("/")
            if form and os.path.isabs(form):   # a relative root ("data") is never masked
                roots.append((form, name))
    for form, name in sorted(roots, key=lambda r: -len(r[0])):
        text = re.sub(re.escape(form) + r"(?![\w.-])", lambda _m, n=name: n, text)
    return text


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — an unknown root is simply not masked
        return None


def _resolved(root) -> str:
    try:
        return str(Path(root).resolve()) if root else ""
    except (OSError, RuntimeError):
        return ""


def health_slot_lines(env: Mapping[str, str], data_home) -> List[str]:
    """The slot as the agent reads it: the same builders the doctor prints, over
    the agent's own data home, with the read `/status` makes (no pack load, no
    probe of a remote browser), the paths masked (:func:`mask_paths`) and a
    secret-shaped string scrubbed. Never raises."""
    from core.security.sandbox_doctor import docker_sandbox_lines
    checks = (
        ("optional extras", lambda: optional_extras_line()),
        ("server extra", server_extra_line),
        ("packs", lambda: packs_line(load=False)),
        ("playwright", lambda: playwright_line(env)),
        ("browser rail", lambda: browser_rail_line(refresh=False, probe=False)),
        ("db schema", lambda: schema_status_line(data_home)),
        ("legacy data tree", lambda: legacy_tree_line(env, data_home)),
        ("data home", lambda: data_home_split_lines(env, unit_read=False)),
        ("docker sandbox", lambda: docker_sandbox_lines(dict(env))),
    )
    lines: List[str] = []
    for name, build in checks:
        lines.extend(_guard(name, build))
    from core.secret_scrub import scrub_secret_shapes
    return [scrub_secret_shapes(mask_paths(ln, data_home)) for ln in lines]


__all__ = ["browser_rail_line", "data_home_split_lines", "health_slot_lines",
           "legacy_tree_line", "mask_paths", "optional_extras_line", "packs_line",
           "playwright_line", "register_schema_head", "schema_status_line",
           "server_extra_line"]
