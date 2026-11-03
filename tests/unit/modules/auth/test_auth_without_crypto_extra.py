"""API keys work on an install without the crypto extra.

`modules.auth` must not import `siwe_auth` (eth_account / web3) eagerly, or
`polyrob keys` and the API-key manager crash on a base or Docker install.
"""
import subprocess
import sys
import textwrap


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)],
                          capture_output=True, text=True, timeout=60)


_BLOCK_CRYPTO = """
import sys
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('eth_account', 'web3'):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return None
sys.meta_path.insert(0, _Block())
"""


def test_api_key_manager_imports_without_crypto_extra():
    r = _run(_BLOCK_CRYPTO + """
from modules.auth import APIKeyManager, TierManager, IdentityMapper
from modules.auth.api_key_manager import APIKeyManager as A
assert APIKeyManager is A
print('OK')
""")
    assert r.returncode == 0, r.stderr
    assert "OK" in r.stdout


def test_siwe_authenticator_still_reachable_lazily():
    from modules.auth import SIWEAuthenticator
    from modules.auth.siwe_auth import SIWEAuthenticator as S
    assert SIWEAuthenticator is S
