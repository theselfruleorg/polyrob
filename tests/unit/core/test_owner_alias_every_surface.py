"""110 W1: the configured owner id is the ONLY way a network sender becomes the owner
tenant — on every surface whose sender id the platform signs, never on a forgeable one."""
import pytest

from core.instance import owner_surface_alias

PRINCIPAL = "u_owner"

SIGNED = [
    ("slack", "OWNER_SLACK_ID", "U0OWNER1", "U0OTHER1"),
    ("discord", "OWNER_DISCORD_ID", "100000000000000001", "100000000000000002"),
    ("signal", "OWNER_SIGNAL_ID", "0f0f0f0f-0000-4000-8000-000000000001",
     "0f0f0f0f-0000-4000-8000-000000000002"),
    ("whatsapp", "OWNER_WHATSAPP_ID", "+15550000001", "15550000002"),
    ("feishu", "OWNER_FEISHU_ID", "ou_owner0000000000000000000000001", "ou_other00000000001"),
    ("dingtalk", "OWNER_DINGTALK_ID", "0123456789", "9876543210"),
]


@pytest.mark.parametrize("surface,env_key,owner,other", SIGNED)
def test_only_the_configured_id_is_the_owner(surface, env_key, owner, other):
    env = {env_key: owner, "POLYROB_OWNER_USER_ID": PRINCIPAL}
    assert owner_surface_alias(owner, surface, env=env) == PRINCIPAL
    assert owner_surface_alias(other, surface, env=env) is None
    assert owner_surface_alias("", surface, env=env) is None
    # no configured id, or no bound principal: nobody is aliased
    assert owner_surface_alias(owner, surface, env={"POLYROB_OWNER_USER_ID": PRINCIPAL}) is None


def test_a_phone_id_matches_with_or_without_its_plus_and_only_exactly():
    env = {"OWNER_WHATSAPP_ID": "+15550000001", "POLYROB_OWNER_USER_ID": PRINCIPAL}
    assert owner_surface_alias("15550000001", "whatsapp", env=env) == PRINCIPAL
    assert owner_surface_alias("1555000000", "whatsapp", env=env) is None
    assert owner_surface_alias("155500000011", "whatsapp", env=env) is None


def test_a_forgeable_surface_is_never_aliased():
    env = {"POLYROB_OWNER_EMAIL": "owner@example.com", "POLYROB_OWNER_USER_ID": PRINCIPAL}
    assert owner_surface_alias("owner@example.com", "email", env=env) is None


def test_a_surface_id_is_not_matched_across_surfaces():
    # The Slack owner's id written by someone on Discord is not the owner there.
    env = {"OWNER_SLACK_ID": "U0OWNER1", "POLYROB_OWNER_USER_ID": PRINCIPAL}
    assert owner_surface_alias("U0OWNER1", "discord", env=env) is None


def test_an_unknown_surface_is_never_aliased():
    env = {"OWNER_NOPE_ID": "x", "POLYROB_OWNER_USER_ID": PRINCIPAL}
    assert owner_surface_alias("x", "nope", env=env) is None
