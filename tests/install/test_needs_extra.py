"""W11: the CI bare-install walk derives which core modules need an extra."""
from pathlib import Path

from tests.install.needs_extra import modules_needing_extras

REPO = Path(__file__).resolve().parents[2]


def test_the_wallet_modules_that_import_web3_at_module_level_need_crypto():
    needs = modules_needing_extras(REPO)
    assert needs.get("core.wallet.agent_wallet") == "crypto"
    assert needs.get("core.wallet.erc6551") == "crypto"      # eth_abi, module level


def test_guarded_and_lazy_imports_do_not_count():
    needs = modules_needing_extras(REPO)
    assert "core.optional_extras" not in needs and "core.lazy_deps" not in needs
    assert "core.config" not in needs and "core.packs.loader" not in needs
