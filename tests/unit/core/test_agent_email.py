"""resolve_agent_email — the agent's OWN sender address (Task 1, 2026-08-18 plan).

Resolution order: POLYROB_AGENT_EMAIL env -> provisioned managed inbox
(<data_home>/agent_mail.json) -> GMAIL_EMAIL legacy fallback -> None.
"""
import json

from core.instance import agent_mail_state_path, resolve_agent_email


def test_env_override_wins(tmp_path):
    (tmp_path / "agent_mail.json").write_text(
        json.dumps({"inbox_id": "in_1", "address": "prov@agentmail.to"}))
    assert resolve_agent_email(
        env={"POLYROB_AGENT_EMAIL": "me@x.dev"}, data_home=tmp_path) == "me@x.dev"


def test_provisioned_state_second(tmp_path):
    (tmp_path / "agent_mail.json").write_text(
        json.dumps({"inbox_id": "in_1", "address": "prov@agentmail.to"}))
    assert resolve_agent_email(env={}, data_home=tmp_path) == "prov@agentmail.to"


def test_gmail_fallback_then_none(tmp_path):
    assert resolve_agent_email(
        env={"GMAIL_EMAIL": "op@gmail.com"}, data_home=tmp_path) == "op@gmail.com"
    assert resolve_agent_email(env={}, data_home=tmp_path) is None


def test_corrupt_state_skipped(tmp_path):
    (tmp_path / "agent_mail.json").write_text("{not json")
    assert resolve_agent_email(env={}, data_home=tmp_path) is None


def test_non_address_values_ignored(tmp_path):
    # A value without "@" is never returned from any tier.
    (tmp_path / "agent_mail.json").write_text(json.dumps({"address": "not-an-address"}))
    assert resolve_agent_email(
        env={"POLYROB_AGENT_EMAIL": "also-bad", "GMAIL_EMAIL": ""},
        data_home=tmp_path) is None


def test_state_path_shape(tmp_path):
    assert agent_mail_state_path(tmp_path).name == "agent_mail.json"
    assert agent_mail_state_path(tmp_path).parent == tmp_path


# --- Task 2: EMAIL_PROVIDER resolution (core/config_policy/policy.py) ----------

from core.config_policy.policy import email_provider


def test_provider_unset_no_key_is_smtp():
    assert email_provider(env={}) == "smtp"


def test_provider_unset_with_key_is_agentmail():
    assert email_provider(env={"AGENTMAIL_API_KEY": "am_test"}) == "agentmail"


def test_provider_explicit_smtp_wins_over_key():
    assert email_provider(env={"EMAIL_PROVIDER": "smtp",
                               "AGENTMAIL_API_KEY": "am_test"}) == "smtp"


def test_provider_explicit_agentmail():
    assert email_provider(env={"EMAIL_PROVIDER": "agentmail"}) == "agentmail"


def test_provider_junk_value_is_safe_smtp():
    assert email_provider(env={"EMAIL_PROVIDER": "sendgrid"}) == "smtp"
