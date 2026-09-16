"""Local browser launch policy; remote endpoints require operator isolation."""
from core.security.host_execution import wallet_custody_enabled
from core.env import bool_env


def require_local_browser_allowed():
    if wallet_custody_enabled():
        raise RuntimeError(
            "Local Chromium is unavailable in a custody process. Configure a "
            "separately isolated remote browser; keep signing credentials off its host."
        )


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
