"""Local browser launch policy; remote endpoints require operator isolation."""
from core.security.host_execution import wallet_custody_enabled
from core.env import bool_env


def require_local_browser_allowed():
    """Refuse a LOCAL Chromium launch beside the signer.

    The sentence comes from ``core.security.browser_rail`` so it names the real
    state: no endpoint configured (install remedy) vs. an endpoint that is
    configured but unused/unreachable (service remedy). The old fixed sentence
    told the prod agent to "configure a separately isolated remote browser"
    while one was configured and running.
    """
    if wallet_custody_enabled():
        from core.security.browser_rail import browser_rail_status
        status = browser_rail_status()
        raise RuntimeError(status.refusal() or (
            "Local Chromium is unavailable in a custody process; connect to the "
            "configured remote browser instead (BROWSER_CDP_URL / BROWSER_WSS_URL)."))


def remote_connect_refusal(kind: str, exc: BaseException) -> str:
    """The sentence for a failed remote connect. Names a failure CLASS, never the URL."""
    from core.security.browser_rail import SERVICE_REMEDY
    return (f"The remote browser endpoint ({kind}) is configured but not reachable "
            f"({type(exc).__name__}) — {SERVICE_REMEDY}.")


def chromium_sandbox_enabled(config, args):
    require_local_browser_allowed()
    disabled = bool(config.use_no_sandbox)
    if disabled and not bool_env("POLYROB_LOCAL", False):
        raise ValueError("Unsandboxed Chromium is restricted to explicit local, non-custody development")
    dangerous = {"--no-sandbox", "--disable-setuid-sandbox",
                 "--disable-namespace-sandbox", "--disable-seccomp-filter-sandbox"}
    if not disabled and any(str(arg).split("=", 1)[0] in dangerous for arg in args):
        raise ValueError("Sandbox-disabling Chromium arguments require explicit use_no_sandbox opt-in")
    return not disabled


def desktop_launch_kwargs(*, headless: bool, args=None) -> dict:
    """Launch kwargs for an OWNER CEREMONY Chromium (x-account capture/signup, pfp
    render) — the launches that live outside ``tools/browser/browser.py``.

    Same policy as the agent's browser, in one place: refused beside the signer
    (``require_local_browser_allowed``), a scrubbed environment (never the
    process env — a desktop shell that has sourced ``wallet.env`` would otherwise
    hand the seed to Chromium), and the Chromium sandbox ON. Works with both the
    sync and async Playwright APIs: ``pw.chromium.launch(**desktop_launch_kwargs(...))``.
    """
    require_local_browser_allowed()
    from tools.browser.child_env import build_browser_env
    kwargs = {"headless": headless, "env": build_browser_env({}), "chromium_sandbox": True}
    if args:
        kwargs["args"] = list(args)
    return kwargs
