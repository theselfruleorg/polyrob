"""111 W1 — the ONE account-kind table, its ERC-165 ids and every custom-error selector.

Each literal is computed here from its signature (keccak), and — when ``../polyrob-desk`` is
beside this repo — checked against the Solidity source it was copied from.
"""
import os
import re

import pytest

from core.wallet import abi, account_kinds as AK, collection_errors as CE, custom_errors, erc6551

_DESK = os.path.normpath(os.path.join(os.path.dirname(__file__), *[".."] * 4, "..",
                                      "polyrob-desk", "contracts", "src"))
_desk = pytest.mark.skipif(not os.path.isdir(_DESK), reason="../polyrob-desk is not beside this repo")


def _xor_id(signatures):
    out = 0
    for s in signatures:
        out ^= int(abi.selector(s), 16)
    return f"0x{out:08x}"


# ------------------------------------------------------------------ the rows
def test_the_table_has_the_two_standards_and_no_product_name():
    assert list(AK.KINDS) == ["tokenbound_v3", "erc6551p"]
    assert (AK.TOKENBOUND_V3.label, AK.ERC6551P.label) == ("Tokenbound AccountV3", "ERC-6551P account")
    assert AK.kind_named("erc6551p") is AK.ERC6551P and AK.kind_named("polyrob_account") is None
    for k in AK.KINDS.values():
        assert "polyrob" not in (k.name + k.label + k.limits_text).lower()


def test_the_tokenbound_row_is_the_builtin_pin():
    v3 = AK.TOKENBOUND_V3
    assert v3.builtin_implementation == erc6551.ACCOUNT_V3_IMPL
    assert v3.builtin_code_sha256 == erc6551.CODE_SHA256[erc6551.ACCOUNT_V3_IMPL.lower()]
    assert (v3.has_lock, v3.has_forwarder, v3.signs_as_owner, v3.errors) == (True, True, True, {})


def test_the_erc6551p_row():
    k = AK.ERC6551P
    assert (k.builtin_implementation, k.builtin_code_sha256) == (None, None)
    assert (k.has_lock, k.has_forwarder, k.signs_as_owner) == (False, False, False)
    assert k.standing_approvals == "refused" and k.sale_ready_view == "saleReadyOf(uint256)"
    assert k.journal_getter == "JOURNAL()" and k.errors is AK.ERC6551P_ERRORS


# ------------------------------------------------------------------ ERC-165 ids
IERC6551P_COLLECTION = ["readyToSell(uint256)", "cancelReady(uint256)", "saleReadyOf(uint256)",
                        "transferCount(uint256)", "openTicket(uint256,address,address)",
                        "ticketConsumed(uint256)", "closeTicket(uint256)", "MARKET()", "ZONE()"]
ITRANSFER_SAFE_ACCOUNT = ["inBatch()"]


def test_each_erc165_id_is_the_xor_of_its_interface():
    assert _xor_id(IERC6551P_COLLECTION) == AK.ERC6551P.collection_iface == "0x6b7481e3"
    assert _xor_id(ITRANSFER_SAFE_ACCOUNT) == AK.ERC6551P.account_iface == "0x92ad233c"


def _interface_functions(path, name):
    src = open(path).read()
    body = re.search(r"interface\s+" + name + r"\b[^{]*\{(.*?)\n\}", src, re.S).group(1)
    sigs = []
    for m in re.finditer(r"function\s+(\w+)\s*\(([^)]*)\)", body):
        types = [p.strip().split()[0] for p in m.group(2).split(",") if p.strip()]
        sigs.append(f"{m.group(1)}({','.join(types)})")
    return sigs


@_desk
def test_the_erc165_ids_match_the_solidity_interfaces():
    coll = _interface_functions(os.path.join(_DESK, "interfaces", "IERC6551P.sol"),
                                "IERC6551PCollection")
    acct = _interface_functions(os.path.join(_DESK, "interfaces", "ITransferSafe.sol"),
                                "ITransferSafeAccount")
    assert sorted(coll) == sorted(IERC6551P_COLLECTION)
    assert acct == ITRANSFER_SAFE_ACCOUNT
    assert _xor_id(coll) == AK.ERC6551P.collection_iface
    assert _xor_id(acct) == AK.ERC6551P.account_iface


# ------------------------------------------------------------------ error tables
TABLES = {"account": AK.ERC6551P_ERRORS, "collection": CE.COLLECTION_ERRORS,
          "sale": CE.SALE_ERRORS, "sale rails": CE.SALE_RAIL_ERRORS}


@pytest.mark.parametrize("name", sorted(TABLES))
def test_every_selector_is_computed_from_its_signature(name):
    for sel, (sig, text) in TABLES[name].items():
        assert abi.selector(custom_errors.canonical(sig)) == sel, (name, sig)
        assert text and sel == sel.lower()


#: The Solidity files each table was copied from: every error declared there is in the table.
SOURCES = {
    "account": ["account/PolyrobAccount.sol"],
    "collection": ["kit/ERC6551PCollection.sol", "kit/AccountedERC721.sol",
                   "kit/modules/AgentIdentity.sol", "Polyrob.sol"],
    "sale": ["drop/PolyrobSale.sol"],
    "sale rails": ["sale/PolyrobMarket.sol", "sale/Conditions.sol", "sale/PolyrobZone.sol"],
}


@_desk
@pytest.mark.parametrize("name", sorted(SOURCES))
def test_every_error_of_the_source_is_in_its_table(name):
    declared = set()
    for rel in SOURCES[name]:
        for m in re.finditer(r"^\s*error\s+(\w+)\(([^)]*)\);", open(os.path.join(_DESK, rel)).read(),
                             re.M):
            declared.add(custom_errors.canonical(f"{m.group(1)}({m.group(2)})"))
    have = {custom_errors.canonical(sig) for sig, _ in TABLES[name].values()}
    assert declared == have


# ------------------------------------------------------------------ decoding with arguments
def test_an_error_is_decoded_with_its_arguments():
    target = "0x" + "77" * 20
    data = "0xe4073933" + "00" * 12 + target[2:] + "095ea7b3" + "00" * 28
    assert custom_errors.decode(data, AK.ERC6551P_ERRORS).startswith(
        f"Refused(target={target}, selector=0x095ea7b3) (")
    assert custom_errors.decode("0x72fc940b" + f"{3:064x}", CE.SALE_RAIL_ERRORS).startswith(
        "ConditionFailed(index=3)")
    assert custom_errors.decode("0x51481720" + f"{10**16:064x}", CE.SALE_ERRORS).startswith(
        "WrongPrice(expected=10000000000000000)")
    assert custom_errors.decode("0x665c1c57", CE.SALE_ERRORS).startswith("NotListed() (")
    # short data: named, the arguments said to be unreadable (never guessed)
    assert "arguments unreadable" in custom_errors.decode("0xe4073933", AK.ERC6551P_ERRORS)
    assert custom_errors.decode("0x12345678", AK.ERC6551P_ERRORS) is None
    assert custom_errors.decode(None, AK.ERC6551P_ERRORS) is None


# ------------------------------------------------------------------ one limits sentence
def test_the_limits_sentence_is_shared_by_the_tools_the_views_and_the_skill():
    from tools.agent_nft.tool import AgentNftTool
    from tools.defi.trade_params import SwapParams, TransferParams
    text = AK.limits_text()
    assert all(k.limits_text in text for k in AK.KINDS.values())
    # once in the defi_trade family (transfer); the others point at it
    assert text in TransferParams.model_fields["account"].description
    assert text not in SwapParams.model_fields["account"].description
    assert "defi_trade.transfer" in SwapParams.model_fields["account"].description
    assert text in AgentNftTool.agent_nft_snapshot._description
    skill = os.path.join(os.path.dirname(__file__), *[".."] * 4, "data", "prompts", "skills",
                         "agent-identity", "SKILL.md")
    body = " ".join(open(skill).read().split())
    for k in AK.KINDS.values():
        assert k.limits_text in body, k.name
