"""An action registered with an explicit param_model gets the MODEL, not its fields.

Prod 2026-10-05 08:04: `defi_data.wallet_activity(self, params, ...)` had no
annotation on `params`, so the registry splatted the fields as kwargs and every
call died with "got an unexpected keyword argument 'address'" — in the middle of
an owner turn. `token_origin` had the same shape. A dotted string annotation
(`"_mod.Model"`, under `from __future__ import annotations`) missed the same way.
"""
import asyncio

from pydantic import BaseModel

from tools.controller.registry.service import Registry


class LookupParams(BaseModel):
    address: str
    chain: str = "base"


def _run(r, name, params):
    return asyncio.run(r.execute_action(name, params))


def test_unannotated_params_receives_the_model():
    r = Registry()

    @r.action("lookup", param_model=LookupParams, tool="demo")
    async def lookup(params, execution_context=None):
        return f"{params.address}@{params.chain}"

    assert _run(r, "lookup", {"address": "0xabc"}) == "0xabc@base"


def test_dotted_string_annotation_receives_the_model():
    r = Registry()

    async def lookup2(params: "somemod.LookupParams", execution_context=None):
        return params.address

    r.action("lookup2", param_model=LookupParams, tool="demo")(lookup2)
    assert _run(r, "lookup2", {"address": "0xdef"}) == "0xdef"


def test_field_kwargs_actions_still_get_kwargs():
    r = Registry()

    @r.action("plain", tool="demo")
    async def plain(address: str, chain: str = "base"):
        return f"{address}/{chain}"

    assert _run(r, "plain", {"address": "0x1", "chain": "eth"}) == "0x1/eth"
