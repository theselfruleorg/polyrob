"""CR-H07 follow-up (2026-09-23): deploy_token refuses salt/vanity, so its
schema must not advertise them to the model as a feature."""
from tools.defi.trade_tool import DeployContractParams, DeployTokenParams


def test_deploy_token_schema_says_salt_and_vanity_are_refused():
    fields = DeployTokenParams.model_fields
    for name in ("salt", "vanity"):
        text = fields[name].description
        assert "REFUSED" in text
        assert "SAME ADDRESS ON EVERY CHAIN" not in text


def test_deploy_contract_keeps_its_salt():
    assert "CREATE2" in DeployContractParams.model_fields["salt"].description
