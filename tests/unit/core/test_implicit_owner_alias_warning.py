"""CHAT-25: a single-entry ALLOWED_TELEGRAM_USER_IDS silently becomes the
owner alias (money verbs included). Startup must say so loudly unless
POLYROB_OWNER_TELEGRAM_ID confirms it."""
from core.instance import implicit_owner_alias_warning


def test_single_entry_allowlist_warns():
    msg = implicit_owner_alias_warning({"ALLOWED_TELEGRAM_USER_IDS": "4242"})
    assert msg and "4242" in msg and "POLYROB_OWNER_TELEGRAM_ID" in msg


def test_explicit_owner_id_is_quiet():
    assert implicit_owner_alias_warning({"ALLOWED_TELEGRAM_USER_IDS": "4242",
                                         "POLYROB_OWNER_TELEGRAM_ID": "4242"}) is None


def test_no_alias_is_quiet():
    assert implicit_owner_alias_warning({"ALLOWED_TELEGRAM_USER_IDS": "1,2"}) is None
    assert implicit_owner_alias_warning({}) is None
