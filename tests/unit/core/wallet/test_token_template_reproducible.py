"""The shipped token bytecode is reproducible from its source (085 W7).

Two layers. Always: the source and the bytes are pinned by hash, so an edit to
either one without the other fails here. When a ``solc`` 0.8.24 is on PATH: the
source is compiled with the pinned settings and compared byte for byte. The
command is in the ``core.wallet.token_template`` docstring.
"""
import hashlib
import json
import pathlib
import shutil
import subprocess

import pytest

from core.wallet import token_template as T

SOURCE = (pathlib.Path(T.__file__).resolve().parent / "contracts"
          / "FixedSupplyToken.sol")


def test_the_source_is_the_pinned_source():
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == T.SOURCE_SHA256, (
        "FixedSupplyToken.sol changed. Recompile with the command in the "
        "token_template docstring, paste INIT_CODE/RUNTIME, and update every "
        "pinned hash.")


def test_the_bytes_are_the_pinned_bytes():
    assert (hashlib.sha256(bytes.fromhex(T.INIT_CODE[2:])).hexdigest()
            == "2de19610617977489f82e194e8302404ce1ebcf9d56e78649b936bca5e34c80c")
    assert (hashlib.sha256(bytes.fromhex(T.RUNTIME[2:])).hexdigest()
            == "35cdf51bf272b63357f1784a8bb63379f76d194237b8d00849e3af4d01387cd4")


def _solc():
    path = shutil.which("solc")
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return path if T.SOLC_VERSION in out else None


@pytest.mark.skipif(_solc() is None, reason="solc 0.8.24 is not on PATH")
def test_the_source_compiles_to_the_shipped_bytes():
    out = subprocess.run(
        [_solc(), "--optimize", "--optimize-runs", str(T.OPTIMIZER_RUNS),
         "--evm-version", T.EVM_VERSION, "--no-cbor-metadata",
         "--combined-json", "bin,bin-runtime", str(SOURCE)],
        capture_output=True, text=True, timeout=120, check=True).stdout
    contracts = json.loads(out)["contracts"]
    built = next(v for k, v in contracts.items()
                 if k.endswith(":FixedSupplyToken"))
    assert built["bin"] == T.INIT_CODE[2:]
    assert built["bin-runtime"] == T.RUNTIME[2:]
