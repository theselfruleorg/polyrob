"""Phase-2 registrars of the X pack (067 P3b). Light on purpose: the tool
modules (tweepy, Playwright) are imported inside the registrars, so a missing
optional dependency drops ONE tool instead of refusing the whole pack."""
import logging

logger = logging.getLogger(__name__)

#: The 057 WS-A credential verdict for the X API tool (``tools.tool_disclosure``).
TWITTER_CREDENTIAL_GATE = (
    "twitter_api",
    "the X API refused this account (402 — credits). The owner must top up, or "
    "the browser rail (x_browser) carries the post instead.")


def twitter_receipt_verbs():
    """The X verbs whose SUCCESSFUL result is a receipt of the agent's own act (a
    tweet / message id, "ok for <user>") or the agent's own profile — never a
    third party's text — so they do not taint the turn (``read_taint``). Derived
    from pack.toml: every verb with an effect class (the writes) plus
    ``twitter_whoami``. Every READ of X (timeline, mentions, DMs, search, polls)
    still taints. Prod 2026-10-11 02:01: ``twitter_whoami`` tainted the owner's
    own turn and his own post raised a card."""
    from polyrob.sdk import action_ids_where
    writes = action_ids_where(tool="twitter", effect=lambda e: e not in ("none", "inherit"))
    return sorted(writes) + ["twitter_whoami"]


def register_twitter_tool(force: bool = False) -> bool:
    """The descriptor and class ``tools/descriptors.py`` / ``tools/__init__.py``
    used to hold. Registered whenever tweepy imports, as before; the CLI
    additionally requires the live gate (X credentials configured,
    ``polyrob_x.twitter_gate``); writes stay TWITTER_ENABLED-gated per action."""
    try:
        from polyrob_x.twitter_tool import TwitterTool
    except ImportError as exc:  # the `twitter` extra (tweepy) is absent
        logger.debug("twitter tool not registered: %s", exc)
        return False
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    from tools.tool_disclosure import register_credential_gate
    register_credential_gate("twitter", *TWITTER_CREDENTIAL_GATE)
    from polyrob.sdk import register_receipt_actions
    register_receipt_actions("twitter", twitter_receipt_verbs)  # lazy: no early policy view
    return register_optional_tool(
        "twitter", TwitterTool,
        ToolDescriptor(
            name="twitter",
            description=("X account API: public/account reads, legacy DM events, encrypted "
                         "X Chat inbox/thread reads, and gated post/engagement/DM writes. "
                         "Prefer anysite for broad public discovery; use x_browser when "
                         "the visible inbox is authoritative."),
            category=ToolCategory.COMMUNICATION,
            required_services=["rate_limit_manager"],
            optional_services=["cache_manager", "database_manager"],
            required_config=[],  # Uses OAuth tokens from DB
            init_priority=30,
            is_optional=True,
            rate_limited=True,
            rate_limit_settings={
                "default_wait": 900,  # 15 minutes
                "requests_per_minute": 300,
                "burst_limit": 50,
            },
        ),
        lambda: True, force=force)
