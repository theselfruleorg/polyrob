"""071 ratchet: what the MODEL is told about addresses matches what the code does.

2026-08-28 (Solana blind spot) and 2026-10-01 (a chat user's "check this
Solana wallet") had the same shape: text that denied or misdescribed a shipped
capability. The 10-01 instances were a "0x… only" field over a validator that
accepts base58, and a refusal naming a parameter that did not exist.
"""
import inspect
import re
from pathlib import Path

from pydantic import BaseModel

from tools.defi import data_tool
from tools.defi.data_tool import DefiDataTool

_ADDRESS_FIELDS = ("address", "contract", "token", "pool")
_SOURCES = [Path(data_tool.__file__),
            Path(data_tool.__file__).parents[2] / "core" / "wallet" / "address_kind.py"]


def _param_models():
    for name, obj in vars(data_tool).items():
        if inspect.isclass(obj) and issubclass(obj, BaseModel) and obj is not BaseModel \
                and obj.__module__ == data_tool.__name__:
            yield name, obj


def test_every_0x_address_field_states_its_chain_family():
    """A field that says "0x" must also say either that solana takes a base58
    mint, or that the verb is EVM-only. A bare "0x…" reads as "Solana is not
    supported" whichever is true."""
    bad = []
    for name, model in _param_models():
        for field, info in model.model_fields.items():
            if field not in _ADDRESS_FIELDS:
                continue
            desc = (info.description or "")
            if "0x" not in desc:
                continue
            if not re.search(r"solana|base58|EVM", desc, re.I):
                bad.append(f"{name}.{field}: {desc[:70]!r}")
    assert bad == [], "\n".join(bad)


def test_every_verb_named_in_a_remedy_exists():
    """`defi_data.<verb>(` in a refusal or description must be a real action."""
    missing = set()
    for src in _SOURCES:
        for verb in re.findall(r"defi_data\.(\w+)\(", src.read_text()):
            if not hasattr(DefiDataTool, verb):
                missing.add(verb)
    assert not missing, missing


def test_no_refusal_tells_the_model_to_pass_a_parameter_that_does_not_exist():
    text = Path(data_tool.__file__).read_text()
    assert "Pass an explicit address" not in text
