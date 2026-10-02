"""Host execution cannot share a trust domain with signing credentials."""
from core.env import bool_env
from core.security.custody_env import holds_custody_secret


def wallet_custody_enabled() -> bool:
    """Treat an enabled wallet or any supported signing secret as custody.

    Never construct a wallet or return credential contents just to decide
    whether model-controlled subprocesses may run on the host.

    066 P0.2: the seeds leave ``os.environ`` once the wallet config loads, so
    the secret half reads through :func:`holds_custody_secret` (env, then the held
    copy) — a loaded custody process never stops counting as custody.
    """
    if bool_env("AGENT_WALLET_ENABLED", False):
        return True
    return holds_custody_secret()


def signer_holds_the_keys() -> bool:
    """066 §5.6: True only when ``WALLET_SIGNER=remote`` is ACTIVE AND VERIFIED.

    Active = the flag says ``remote``. Verified = this process built its
    ``RemoteWallet``, got the signer's identity and it matched the wallet of
    record, AND this process holds no custody secret (env or held copy). Any
    doubt answers False, which keeps the custody refusal.
    """
    try:
        from core.signer import MODE_REMOTE, signer_mode
        if signer_mode() != MODE_REMOTE:
            return False
        from core.security.custody_env import holds_custody_secret
        if holds_custody_secret():
            return False
        from core.signer.remote import remote_verified
        return remote_verified()
    except Exception:
        return False


def host_execution_refusal():
    # 066 §5.6: with the key in polyrob-signer, custody no longer means "this
    # process holds a key", so host children (git, LSP, MCP stdio, @diff, lazy
    # installs) return to their scrubbed env + per-tool gates. Untrusted code
    # still goes to the sandbox backend.
    if signer_holds_the_keys():
        return None
    if wallet_custody_enabled():
        return ("host execution refused while wallet custody is enabled; "
                "use a sandbox backend or a separate development instance without signing credentials")
    return None
