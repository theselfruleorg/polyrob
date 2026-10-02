"""The x pack (067 P3b): X (Twitter) — the API tool, the browser rail, the DM
chat surface, the ``twitter`` cron post channel, the ``polyrob x`` and
``polyrob x-account`` commands, the ``/x`` owner verb, the console's OAuth
callback, and the X skills.

The code half of the pack. Importing this module is cheap (core only); the tool
modules are named by reference and imported in the loader's phase 2. The policy
rows (capabilities, per-action policy, catalog permissions), the surface row and
the delivery-channel name are in ``pack.toml`` and register in phase 1, before
any pack code runs.
"""
import os
from pathlib import Path

from core.packs.spec import PackSpec, StatusSection, ToolContribution


def _x_oauth2_store_present() -> bool:
    """Does the encrypted X token store hold an ``x_oauth2`` record? Reads only
    the store's KEY index (opaque Fernet values), so no token is decrypted."""
    try:
        import json as _json
        from core.runtime_paths import resolve_data_home
        path = resolve_data_home() / ".x_session.json"
        if not path.is_file():
            return False
        raw = _json.loads(path.read_text(encoding="utf-8"))
        keys = raw.keys() if isinstance(raw, dict) else []
        return any(str(k).endswith("x_oauth2") for k in keys)
    except Exception:
        return False


def twitter_gate() -> bool:
    """The live ``twitter`` gate (was ``core.bootstrap._cli_extra_gate``): X API
    credentials are configured — an OAuth 2.0 user token (env or the encrypted
    store) or the OAuth 1.0a user-context pair. Without them the tool is dead
    weight on the CLI."""
    return bool(os.getenv("TWITTER_OAUTH2_ACCESS_TOKEN")
                or os.getenv("TWITTER_OAUTH2_REFRESH_TOKEN")
                or _x_oauth2_store_present()
                or (os.getenv("TWITTER_API_KEY") and os.getenv("TWITTER_ACCESS_TOKEN")))


def x_browser_gate() -> bool:
    """The live ``x_browser`` gate (``X_BROWSER_ENABLED``, default off). Reads the
    module attribute at call time, so a patch of
    ``polyrob_x.x_browser.registration.x_browser_enabled`` takes effect."""
    from polyrob_x.x_browser import registration
    return registration.x_browser_enabled()


def x_login_status() -> list:
    """The ``x login`` status section (``polyrob_x.status.build``), imported on
    first render so loading the pack stays cheap."""
    from polyrob_x.status import build
    return build()


def pack() -> PackSpec:
    return PackSpec(
        id="x",
        tools=(
            ToolContribution(id="twitter",
                             registrar="polyrob_x.registration:register_twitter_tool",
                             gate="polyrob_x:twitter_gate"),
            ToolContribution(id="x_browser",
                             registrar="polyrob_x.x_browser.registration:register_x_browser_tool",
                             gate="polyrob_x:x_browser_gate"),
        ),
        hooks={"cron.delivery_channel": {"twitter": "polyrob_x.cron_delivery:deliver_post"},
               # `/x login|status` — the owner's one-tap OAuth re-login (verbs are
               # rows in core.verbs; the seats resolve the handler references).
               "owner.verbs": "polyrob_x.owner_verbs:OWNER_VERBS"},
        cli=("polyrob_x.commands.x:x", "polyrob_x.commands.x_account:x_account"),
        # Mounted by the console under /api/packs/x (owner-only, except the
        # OAuth callback that pack.toml [console] public_paths declares).
        console_routers=("polyrob_x.console_routes:router",),
        status_sections=(StatusSection(name="x login", build=x_login_status),),
        skills_dir=Path(__file__).parent / "skills",
    )
