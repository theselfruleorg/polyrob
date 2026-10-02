"""058 T1.7 — requirements.txt and deploy_prod.sh name the SAME prod extras.

Two copies of one list; this is the drift check. ⚠️ ``scripts/`` does not ship in
the public tree, so the test SKIPS (never fails) when the script is absent.
"""
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]


def test_requirements_txt_extras_equal_prod_extras():
    script = _REPO / "scripts/deploy_prod.sh"
    if not script.is_file():
        pytest.skip("deploy_prod.sh is not part of the public tree")
    # 066 P1: requirements.txt is the hashed closure of these extras, generated
    # by scripts/gen_lazy_closures.py from its own `# extras:` line.
    ex = set(re.search(r"^# extras: ([a-z0-9,\-]+)", (_REPO / "requirements.txt").read_text(), re.M).group(1).split(","))
    pe = set(re.search(r'^PROD_EXTRAS="?([a-z,\-]+)', script.read_text(), re.M).group(1).split(","))
    assert ex == pe, f"requirements.txt {sorted(ex)} != PROD_EXTRAS {sorted(pe)}"
    # 067 (one install): the packs ship inside polyrob; their SDK extras are prod's.
    assert "# packs:" not in (_REPO / "requirements.txt").read_text()
    assert {"twitter", "anysite", "crypto"} <= ex, "the X, discovery and markets packs' SDKs"
    assert {"docs", "media"} <= ex, "docs + media are not optional for prod (PDF parsing, invoice cards)"
    assert not {"gemini", "anthropic"} & ex, "prod runs OpenRouter; a provider extra here is a decision, not a default"
