"""Owner ⇄ instance user_id alias (owner-instance identity model, 2026-07-03).

An authenticated Telegram *owner* must operate as the instance OWNER principal
(e.g. ``rob``) rather than the surface-hashed ``u_…`` id, so the owner's chat
shares the same tenant as autonomy (goals / memory / SELF docs). Only telegram is
aliased: its sender ids are platform-authenticated. Email ``From:`` / WhatsApp are
forgeable and are NEVER aliased (AGENTS.md keeps owner-by-email OFF in v1).

See docs/plans/2026-07-03-owner-instance-identity-model-HANDOFF.md.
"""
from core.instance import (
    owner_surface_alias,
    resolve_owner_email,
    resolve_owner_principal,
    resolve_owner_telegram_id,
)

OWNER_TG = "28436760"
OWNER_ENV = {"POLYROB_OWNER_USER_ID": "rob", "POLYROB_OWNER_TELEGRAM_ID": OWNER_TG}


# --- resolve_owner_email (E3: SSOT for the configured owner email) ------------

def test_owner_email_explicit_env():
    assert resolve_owner_email(env={"POLYROB_OWNER_EMAIL": "owner@example.com"}) == "owner@example.com"


def test_owner_email_bot_alias_env():
    assert resolve_owner_email(env={"BOT_OWNER_EMAIL": "owner@example.com"}) == "owner@example.com"


def test_owner_email_polyrob_wins_over_bot_alias():
    env = {"POLYROB_OWNER_EMAIL": "a@x.com", "BOT_OWNER_EMAIL": "b@y.com"}
    assert resolve_owner_email(env=env) == "a@x.com"


def test_owner_email_none_when_unset():
    assert resolve_owner_email(env={}) is None


def test_owner_email_ignores_non_email_value():
    assert resolve_owner_email(env={"POLYROB_OWNER_EMAIL": "not-an-email"}) is None


# --- resolve_owner_telegram_id (SSOT for the configured owner tg id) ----------

def test_owner_tg_explicit_env():
    assert resolve_owner_telegram_id(env={"POLYROB_OWNER_TELEGRAM_ID": OWNER_TG}) == OWNER_TG


def test_owner_tg_single_allowed_id_fallback():
    assert resolve_owner_telegram_id(env={"ALLOWED_TELEGRAM_USER_IDS": OWNER_TG}) == OWNER_TG


def test_owner_tg_explicit_wins_over_allowed():
    env = {"POLYROB_OWNER_TELEGRAM_ID": "111", "ALLOWED_TELEGRAM_USER_IDS": "222"}
    assert resolve_owner_telegram_id(env=env) == "111"


def test_owner_tg_ambiguous_multi_allowed_is_none():
    assert resolve_owner_telegram_id(env={"ALLOWED_TELEGRAM_USER_IDS": "111,222"}) is None


def test_owner_tg_none_when_unset():
    assert resolve_owner_telegram_id(env={}) is None


def test_owner_tg_ignores_non_numeric():
    # A raw tg id is numeric; a non-numeric value is not a usable chat id.
    assert resolve_owner_telegram_id(env={"POLYROB_OWNER_TELEGRAM_ID": "rob"}) is None


# --- owner_surface_alias (telegram-only) --------------------------------------

def test_owner_telegram_sender_aliases_to_principal():
    assert owner_surface_alias(OWNER_TG, "telegram", env=OWNER_ENV) == "rob"


def test_owner_surface_alias_equals_resolve_owner_principal():
    # The alias target IS the owner principal (handoff §4).
    assert (
        owner_surface_alias(OWNER_TG, "telegram", env=OWNER_ENV)
        == resolve_owner_principal(env=OWNER_ENV)
    )


def test_non_owner_telegram_sender_not_aliased():
    assert owner_surface_alias("99999999", "telegram", env=OWNER_ENV) is None


def test_email_never_aliased_even_for_owner_value():
    # Forgeable From: — never alias, even if the address string equals the owner tg id.
    assert owner_surface_alias(OWNER_TG, "email", env=OWNER_ENV) is None


def test_whatsapp_never_aliased_even_for_owner_value():
    assert owner_surface_alias(OWNER_TG, "whatsapp", env=OWNER_ENV) is None


def test_alias_uses_instance_default_when_no_explicit_principal():
    # Auto-derive: with ONLY the owner tg id configured (no explicit POLYROB_OWNER_USER_ID),
    # the principal falls back to the instance id ("rob"), so the owner is still aliased.
    # This is the whole point — the operator sets only POLYROB_OWNER_TELEGRAM_ID.
    env = {"POLYROB_OWNER_TELEGRAM_ID": OWNER_TG}
    assert owner_surface_alias(OWNER_TG, "telegram", env=env) == "rob"


def test_alias_uses_custom_instance_default():
    env = {"POLYROB_OWNER_TELEGRAM_ID": OWNER_TG, "POLYROB_INSTANCE_ID": "acme"}
    assert owner_surface_alias(OWNER_TG, "telegram", env=env) == "acme"


def test_no_alias_without_owner_tg_id():
    # Principal bound but no owner tg id configured -> cannot identify the owner sender.
    env = {"POLYROB_OWNER_USER_ID": "rob"}
    assert owner_surface_alias(OWNER_TG, "telegram", env=env) is None


def test_no_alias_for_empty_raw_id():
    assert owner_surface_alias("", "telegram", env=OWNER_ENV) is None
    assert owner_surface_alias(None, "telegram", env=OWNER_ENV) is None


def test_alias_via_single_allowed_id_binding():
    # Owner tg identified via a single-entry ALLOWED_TELEGRAM_USER_IDS.
    env = {"POLYROB_OWNER_USER_ID": "rob", "ALLOWED_TELEGRAM_USER_IDS": OWNER_TG}
    assert owner_surface_alias(OWNER_TG, "telegram", env=env) == "rob"
