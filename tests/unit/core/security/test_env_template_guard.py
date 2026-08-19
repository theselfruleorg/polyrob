"""A .env TEMPLATE is documentation, not a credential.

Goal 5ead947671b2 ("Owner deploy package: mainnet-ready x402 endpoint") failed
three times on:

    RuntimeError: Error executing action filesystem_write_file: Refusing to
    access a credential/secret file: x402-paywall/deploy/.env.example

`.env.example` is the canonical name for the file that documents which variables
a deployment needs, with placeholder values. Refusing to write one makes it
impossible for the agent to produce a deployable package, and protects nothing:
the guard exists to keep real secrets out of agent hands, and a template holds
none. The real files (.env, .env.production, polyrob.env) stay refused.
"""
from pathlib import Path

import pytest

from core.security.secret_guard import is_credential_file


@pytest.mark.parametrize("name", [
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.dist",
    "x402-paywall/deploy/.env.example",
    "config/.env.production.example",
    "env.example",
])
def test_templates_are_writable(name):
    assert is_credential_file(Path(name)) is False, f"{name} is a template, not a secret"


@pytest.mark.parametrize("name", [
    ".env",
    ".env.production",
    ".env.development",
    ".env.local",
    "polyrob.env",
    "prod.env",
    "config/.env.production",
    "id_ed25519",
    "server.pem",
    "auth.json",
])
def test_real_credential_files_stay_refused(name):
    assert is_credential_file(Path(name)) is True, f"{name} must stay refused"


def test_the_exemption_is_scoped_to_env_files_only():
    """The exemption must not become a general "append .example" bypass.

    NB the guard already did not catch `id_ed25519.example` / `server.pem.example`
    before this change — `id_ed25519` is an exact-name glob and `*.pem` does not
    match `*.pem.example`, so those names were never refused. That is a
    PRE-EXISTING gap in CREDENTIAL_NAME_GLOBS, not one the template exemption
    opened, and closing it is a separate change with its own blast radius. What
    this test pins is that the new rule is not the thing letting them through.
    """
    from core.security.secret_guard import _is_env_template

    assert _is_env_template("id_ed25519.example") is False
    assert _is_env_template("server.pem.example") is False
    assert _is_env_template("auth.json.example") is False
    assert _is_env_template(".env.example") is True
    assert _is_env_template("polyrob.env.sample") is True
