"""Webgate config object — the deployment posture SSOT.

POLYROB's `webview/` was built multitenant-first (JWT/SIWE auth, ownership,
profile/billing/admin pages, bound on `0.0.0.0`). The *primitive* is the
single-user, local-first webgate: loopback bind, no auth, no admin pages, every
session owned by the local owner. Own-ops (public status page + owner login)
and multitenant (full SaaS UI) are layers on top, gated by posture.

This module is the single source of truth for the deployment posture
(``local`` | ``own_ops`` | ``multitenant``) and the derived bind/ownership
decisions. It is consulted at seam points in ``webview/server.py``
(middleware short-circuit, ownership short-circuit, page/router mount-gate)
and in ``webview/server_launcher.py`` (bind host/port).

Landmine (AGENTS.md): ``BotConfig.get(flag, default)`` is a ``getattr`` that
silently returns the default — NEVER use it for these flags. Read ``os.environ``
directly here.
"""
import logging
import os
import re
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, Request

from core.env import bool_env
from core.instance import (
    console_display_name as _console_display_name,
    resolve_owner_user_id,
)

logger = logging.getLogger(__name__)

_POSTURES = ("local", "own_ops", "multitenant")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _explicit_host_override() -> Optional[str]:
    """WEBGATE_HOST / WEBVIEW_HOST, if set — read directly (no bind_host() call, to
    avoid the posture<->bind_host circular dependency; see B1 docstring)."""
    return os.environ.get("WEBGATE_HOST", os.environ.get("WEBVIEW_HOST"))


def posture() -> str:
    """The deployment posture: "local" | "own_ops" | "multitenant". SSOT.

    Resolution order:
      1. Explicit POLYROB_POSTURE (case-insensitive) — wins outright.
      2. WEBGATE_MULTITENANT=true (back-compat) -> "multitenant".
      3. Derive from an explicit WEBGATE_HOST/WEBVIEW_HOST override: loopback -> "local",
         anything else -> "own_ops".
      4. No explicit host override and WEBGATE_MULTITENANT is not truthy -> "local"
         (today's default: loopback, no auth — Posture 0 must not regress).

    An explicit POLYROB_POSTURE that doesn't match one of the valid values (a typo,
    e.g. "own-ops") is NOT silently ignored (B1-LOW): it logs a warning naming the
    bad value, then falls through to the rest of the derivation below — so a
    misconfigured operator gets a signal instead of silently landing on "local"
    (no auth) when they thought they set a public posture.
    """
    explicit_raw = os.environ.get("POLYROB_POSTURE", "").strip()
    explicit = explicit_raw.lower()
    if explicit in _POSTURES:
        return explicit
    if explicit_raw:
        logger.warning(
            "POLYROB_POSTURE=%r is not a recognized posture (expected one of %s) — "
            "ignoring it and deriving the posture instead.",
            explicit_raw, _POSTURES,
        )

    if bool_env("WEBGATE_MULTITENANT", False):
        return "multitenant"

    host = _explicit_host_override()
    if host is not None:
        return "local" if host.strip() in _LOOPBACK_HOSTS else "own_ops"

    return "local"


def is_multitenant() -> bool:
    """True when the multitenant layer (auth/ownership/admin pages) is enabled.

    Back-compat accessor — equivalent to ``posture() == "multitenant"``.
    """
    return posture() == "multitenant"


def is_own_ops() -> bool:
    """True when posture() == "own_ops" (public own-ops instance, owner-login gated)."""
    return posture() == "own_ops"


def is_local() -> bool:
    """True when posture() == "local" (loopback, no auth — the primitive)."""
    return posture() == "local"


def requires_owner_login() -> bool:
    """True for own_ops/multitenant — console access needs SOME authenticated identity.

    False only for "local": the loopback operator IS the owner, no login needed.
    """
    return posture() != "local"


def activity_enabled() -> bool:
    """Whether the global ``/activity`` stream (page + APIs + socket room) is on.

    ``WEBVIEW_ACTIVITY_ENABLED``, default true — it is owner/admin-gated in
    every non-local posture, so on-by-default is safe.
    """
    return bool_env("WEBVIEW_ACTIVITY_ENABLED", True)


def read_only() -> bool:
    """Monitoring-only console: mutating endpoints refuse with 403.

    ``WEBVIEW_READ_ONLY``, default false. Used for deployments where the
    webview observes an autonomous agent (e.g. the headless VPS) and the
    interactive path (send-message → :9000 API) is not available.
    """
    return bool_env("WEBVIEW_READ_ONLY", False)


#: HTTP methods that change state. The read-only/CSRF guards below are mounted as
#: route ``dependencies=`` and therefore also see the GETs of the same router, so
#: both start by asking this question.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: EXACT paths exempt from the read-only refusal — never a prefix.
#:
#: Two families, both of which mutate something that is not console or agent
#: state, and both of which a read-only console genuinely needs:
#:
#: - **authentication.** A login POST mutates only the CALLER's own session
#:   cookie, and prod runs a READ-ONLY own_ops console today: refusing
#:   ``POST /owner-login`` would lock the owner out of the monitoring console
#:   entirely. ``/api/auth/nonce`` + ``/api/auth/verify`` are the wallet/SIWE
#:   equivalent (challenge, then signature → cookie).
#: - **the loopback machine rails.** ``/api/internal/emit`` (the agent's
#:   telemetry fast-push) and the per-session stream POST carry the LIVE feed a
#:   monitoring console exists to display. They are fed by the agent process over
#:   loopback and each enforces its own ``127.0.0.1`` check; refusing them under
#:   ``WEBVIEW_READ_ONLY`` would kill the telemetry fast path and token streaming
#:   in exactly the posture that only watches.
#:
#: ⚠️ EXACT, because the prefix form (``"/api/auth/"``) also exempted
#: ``POST /api/auth/api-keys`` and ``DELETE /api/auth/api-keys/{prefix}`` — a
#: read-only console could still MINT and revoke durable A2A credentials
#: (``api/auth_endpoints.py``). Never widen this to a prefix; add the exact path.
_READ_ONLY_EXEMPT_PATHS = frozenset({
    "/owner-login",
    "/api/auth/nonce",
    "/api/auth/verify",
    "/api/internal/emit",
})

#: The one exempt path that carries a parameter: ``POST
#: /api/webview/sessions/{session_id}/stream``. Anchored and single-segment, so
#: it cannot match a deeper path.
_STREAM_PATH_RE = re.compile(r"^/api/webview/sessions/[^/]+/stream$")


def _read_only_exempt(path: str) -> bool:
    """Whether *path* is one of the few mutations a read-only console still runs."""
    return path in _READ_ONLY_EXEMPT_PATHS or bool(_STREAM_PATH_RE.match(path))


async def read_only_guard(request: Request) -> None:
    """THE read-only check for the console (043 W3) — mounted as a dependency.

    Before this there were five hand-rolled variants (``pages._mutation_refused``,
    ``pages._pfp_setup_refused``, four inline ``read_only()`` tests, two more in
    ``server.py``, plus a task-router-only guard) and three mutating routes with
    no check at all. One function, mounted via :data:`MUTATION_DEPS` on every
    mutating route and on every router mounted from outside ``webview/``, is the
    only way "is this refused on a read-only console?" has a single answer.

    Reads (GET/HEAD/OPTIONS) always pass — a read-only console is still a
    console. The refusal detail names the flag so the operator knows the remedy.
    The few mutations a read-only console still runs are an EXACT path set — see
    :data:`_READ_ONLY_EXEMPT_PATHS`.
    """
    if request.method.upper() not in MUTATING_METHODS:
        return
    if not read_only():
        return
    if _read_only_exempt(request.url.path):
        return
    raise HTTPException(status_code=403,
                        detail="Console is read-only (WEBVIEW_READ_ONLY)")


def _origin_of(url_like: str):
    """``(host, port_or_None)`` from an Origin/Referer value, or None if unusable."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit((url_like or "").strip())
    except ValueError:
        return None
    if not parts.hostname:
        return None
    port = parts.port
    if port is None:
        port = {"http": 80, "https": 443}.get((parts.scheme or "").lower())
    return (parts.hostname.lower(), port)


async def csrf_guard(request: Request) -> None:
    """Same-origin check on every console mutation (043 W1) — the other half of
    :data:`MUTATION_DEPS`.

    The console authenticates with an AMBIENT cookie (``auth_token``, 7 days on
    own_ops), so before this, every ``POST /api/webgate/*`` was reachable from
    any page the owner happened to have open: pause the agent, settle an
    invoice, approve an app, write a config flag. The tree's only CSRF defence
    was the owner-LOGIN form's double-submit token — which protects the one POST
    that has no session yet.

    The check: on a mutating method, the ``Origin`` (else ``Referer``) host must
    equal the request's own ``Host``. ``Host`` is a browser-FORBIDDEN request
    header, so a cross-origin page cannot make the two agree — the same
    reasoning ``server.py::_cors_origin_allowed`` already relies on for the
    Socket.IO handshake. Ports are compared with the scheme default filled in,
    so ``https://console`` against a proxied request carrying no explicit port
    is the same origin rather than a 403 on every mutation.

    ⚠️ The "no header" branch is a deliberate PASS, not a hole — and it is
    narrowed by a second condition: **no cookie either**. A browser attaches
    ``Origin`` to every cross-origin mutation (fetch, XHR and form POST alike)
    and the page cannot suppress it; that is the whole basis of this check. So a
    request with NEITHER ``Origin`` NOR ``Referer`` is not a browser-driven
    cross-site request: it is a machine client — the telemetry push to
    ``/api/internal/emit`` (localhost-only), the agent's stream POST, a
    bearer-token API caller, curl/CI. Refusing those would break the live
    telemetry rail to buy nothing. Requiring them to carry NO cookie makes the
    pass match its own justification exactly: the CSRF risk is the AMBIENT
    credential, so a request that presents one is held to the origin check even
    when it claims no origin.
    """
    if request.method.upper() not in MUTATING_METHODS:
        return
    stated = request.headers.get("origin") or request.headers.get("referer")
    if not stated and not request.headers.get("cookie"):
        return
    claimed = _origin_of(stated) if stated else None
    host_header = request.headers.get("host") or ""
    scheme = request.url.scheme or "http"
    mine = _origin_of(f"{scheme}://{host_header}") if host_header else None
    if mine is None:
        mine = (request.url.hostname or "", request.url.port
                or {"http": 80, "https": 443}.get(scheme))
    if claimed is None or claimed != mine:
        logger.warning("CSRF refusal: %s %s claims origin %r, host is %r",
                       request.method, request.url.path, stated, host_header)
        raise HTTPException(
            status_code=403,
            detail=("Cross-origin request refused: the Origin header does not "
                    "match this console's host" if stated else
                    "Cross-origin request refused: a cookie-bearing request must "
                    "state its Origin"))


#: The dependency list every mutating console route carries. Kept as ONE list so
#: a new cross-cutting request guard is added in a single place.
MUTATION_DEPS: List = [Depends(read_only_guard), Depends(csrf_guard)]


def bind_host() -> str:
    """Address the webgate binds to.

    Multitenant/own_ops -> ``0.0.0.0`` (an operator fronts it with a reverse proxy);
    local -> ``127.0.0.1`` (loopback only — the owner's own machine).
    An explicit ``WEBGATE_HOST``/``WEBVIEW_HOST`` override always wins.
    """
    default = "127.0.0.1" if is_local() else "0.0.0.0"
    host = os.environ.get("WEBGATE_HOST", os.environ.get("WEBVIEW_HOST", default))
    # P1 finalization: local posture has NO auth (the loopback operator IS the owner),
    # so a non-loopback bind would expose an UNAUTHENTICATED console to the network.
    # Refuse the override and force loopback — to serve the console on the network,
    # use an authenticated posture (own_ops/multitenant).
    if is_local() and not _is_loopback_host(host):
        logging.getLogger(__name__).error(
            "WEBGATE_HOST=%r ignored in local posture — a non-loopback bind would expose "
            "an UNAUTHENTICATED console to the network. Forcing 127.0.0.1. Use "
            "POLYROB_POSTURE=own_ops or multitenant (authenticated) for a network bind.",
            host,
        )
        return "127.0.0.1"
    return host


def _is_loopback_host(host: str) -> bool:
    """True if ``host`` binds to loopback only (safe for the no-auth local posture)."""
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "::1", "localhost", "") or h.startswith("127.")


def bind_port() -> int:
    """Port the webgate binds to (default 5050 — the service port-of-record)."""
    return int(os.environ.get("WEBGATE_PORT", os.environ.get("WEBVIEW_PORT", "5050")))


#: The refusal a writable, owner-less console answers with. One sentence, used by
#: both the boot assertion and the per-call guard below.
UNBOUND_OWNER_MESSAGE = (
    "POLYROB_OWNER_USER_ID is not set — a writable console needs a bound owner")


def owner_is_bound(env: Optional[Dict[str, str]] = None) -> bool:
    """Whether an owner was actually CHOSEN, rather than defaulted to.

    ``resolve_owner_principal(default_to_instance=False)`` is the strict
    resolution (``POLYROB_OWNER_USER_ID`` / ``BOT_OWNER_USER_ID`` / the first
    ``SURFACE_SUPER_ADMIN_USER_IDS`` entry). ``POLYROB_LOCAL_OWNER`` counts too:
    it is THIS module's own owner override, ranked between an explicit owner and
    the local tenant in ``resolve_owner_user_id``, so an operator who named the
    owner there has bound one and must not be told otherwise.
    """
    from core.instance import resolve_owner_principal
    src = os.environ if env is None else env
    if resolve_owner_principal(src, default_to_instance=False):
        return True
    return bool((src.get("POLYROB_LOCAL_OWNER") or "").strip())


def local_owner_id() -> str:
    """The single owner of this instance in single-user mode.

    The precedence (bound owner -> ``POLYROB_LOCAL_OWNER`` -> the local tenant
    ``"local"``) lives in ``core.instance.resolve_owner_user_id`` — the ONE
    resolver, so every owner-attribution call site (this console read, the
    ``polyrob owner …`` verbs, the REPL/CLI identity, x402 machine-income tenant
    stamping) agrees on the same owner.

    W7 (043): unbound, this answers ``"local"``, so ``_effective_user_id`` scopes
    every read AND write to that tenant. On a single-user box that IS the
    operator's tenant and the console reads what the REPL wrote; on a headless
    server beside an agent bound to a different owner it is still the wrong
    tenant — nothing errors, the pages render honest-LOOKING empty lists (no
    goals, no invoices, no pending items), and console writes land there too.

    The REFUSAL for that lives at BOOT (``posture_guard.assert_writable_console``,
    called from ``server.py::startup_event``), not here. Deliberate: a writable,
    owner-less console cannot exist in a running process, so raising per call
    would only fire in a process that never ran the startup hook — a test
    harness, an embedder — where it turns a configuration problem into a 500 on
    every page with none of the boot refusal's remedy text. What this DOES do is
    say the true thing once, so such a process is not silently mis-scoped; the
    state is also reported by ``GET /api/webgate/doctor`` as ``owner_bound``.
    """
    if not read_only() and not owner_is_bound():
        _warn_unbound_owner_once()
    return resolve_owner_user_id()


_warned_unbound_owner = False
_warned_shared_registry = False


def warn_shared_registry_once() -> None:
    """Say the W8 risk once on a workstation console (see posture_guard)."""
    global _warned_shared_registry
    if _warned_shared_registry:
        return
    _warned_shared_registry = True
    logger.warning(
        "SESSION_REGISTRY_BACKEND is not 'sqlite' — this console's own agent "
        "cannot see sessions another polyrob process owns, so a message sent "
        "from here to one of them would RESUME it in this process. Set "
        "SESSION_REGISTRY_BACKEND=sqlite on both if you run a separate agent.")


def _warn_unbound_owner_once() -> None:
    global _warned_unbound_owner
    if _warned_unbound_owner:
        return
    _warned_unbound_owner = True
    logger.warning(
        "%s — this console is scoped to the tenant '%s', so its lists can look "
        "empty while the agent's own tenant is full. Set POLYROB_OWNER_USER_ID "
        "to the value the agent service uses.",
        UNBOUND_OWNER_MESSAGE, resolve_owner_user_id())


def console_display_name() -> str:
    """Product display name for the console shell. See core.instance.console_display_name."""
    return _console_display_name()


def _strip_scheme(url: str) -> str:
    return url.split("://", 1)[-1]


def branding_config() -> Dict[str, str]:
    """Env-driven UI copy for the console shell: support link, access-gate
    label, and footer domain links. All independently overridable so an OSS/
    instance deploy isn't locked to any one instance's domain / support handle /
    access-gate copy baked in at authoring time. Read fresh on every call (not
    memoized) so tests can monkeypatch env without a module reload.

    Keys: support_url, support_display, support_handle,
    brand_url, brand_display, org_url, org_display, terms_url, privacy_url.
    (access_gate_label was removed 2026-07-06 with the beta banner.)

    043 A12: ``brand_url``/``org_url``/``terms_url``/``privacy_url`` have NO
    placeholder-host default — an operator who never set the corresponding env
    var gets an empty string, not a fake domain (``your-polyrob-host.example``)
    or the framework author's own org site (``theselfrule.org``). The template
    only renders a footer link when its URL is non-empty. ``support_url`` is
    unaffected — its Telegram default is a real, working support channel.
    """
    support_url = os.environ.get("POLYROB_SUPPORT_URL", "https://t.me/tmachinrobot").strip()
    brand_url = os.environ.get("POLYROB_BRAND_URL", "").strip()
    org_url = os.environ.get("POLYROB_ORG_URL", "").strip()
    terms_url = os.environ.get("POLYROB_TERMS_URL", "").strip()
    if not terms_url and brand_url:
        terms_url = f"{brand_url}/terms"
    privacy_url = os.environ.get("POLYROB_PRIVACY_URL", "").strip()
    if not privacy_url and brand_url:
        privacy_url = f"{brand_url}/privacy"
    return {
        "support_url": support_url,
        "support_display": _strip_scheme(support_url),
        "support_handle": os.environ.get("POLYROB_SUPPORT_HANDLE", "@TMACHINROBOT").strip(),
        "brand_url": brand_url,
        "brand_display": _strip_scheme(brand_url) if brand_url else "",
        "org_url": org_url,
        "org_display": _strip_scheme(org_url) if org_url else "",
        "terms_url": terms_url,
        "privacy_url": privacy_url,
    }


def data_dir() -> str:
    """Runtime data home the console reads sidecar DBs from (goals.db/cron.db/
    memory.db/identity/…) — the webview-side wrapper over the ONE core policy
    seam ``core.runtime_paths.resolve_data_home`` shared with the CLI admin
    verbs. Env is honored here too so a standalone webview deploy
    (``scripts/deploy_webview.sh``) where ``core`` isn't importable still
    respects ``POLYROB_DATA_DIR`` and falls back to the legacy ``./data``
    instead of raising. Do NOT re-implement resolution logic at call sites.
    """
    env = os.environ.get("POLYROB_DATA_DIR")
    if env:
        return env
    try:
        from core.runtime_paths import resolve_data_home
        return str(resolve_data_home())
    except Exception:
        return "data"


__all__ = [
    "posture", "is_multitenant", "is_own_ops", "is_local", "requires_owner_login",
    "activity_enabled", "read_only",
    # 043 W1/W3 — the two request guards and the ONE list every mutating console
    # route carries.
    "MUTATING_METHODS", "MUTATION_DEPS", "read_only_guard", "csrf_guard",
    # 043 W7 — "does this console know whose it is?"
    "owner_is_bound", "warn_shared_registry_once", "UNBOUND_OWNER_MESSAGE",
    "bind_host", "bind_port", "local_owner_id", "console_display_name", "branding_config",
    "data_dir",
]


def in_process_task_agent():
    """The ``TaskAgent`` living in THIS process, or None.

    On a single-service console deploy it is the agent that owns the sessions;
    when the console is a SEPARATE service (prod Rob #1) it is a monitoring
    agent that does NOT own them (or None) — a caller must check
    ``route_session().is_local``, never trust mere presence. ``pages`` and
    ``server`` each carried this lookup by hand.
    """
    try:
        from core.container import DependencyContainer
        container = DependencyContainer.get_instance()
        agent = container.get_agent("task_agent")
        if not agent:
            agent = container.get_service("task_agent")
        return agent or None
    except Exception:
        return None
