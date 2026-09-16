"""deploy_guard — the CREATE shape's measurements.

Like the tx_guard suite, most of these are refusals: the module's job is to
refuse a deployment it cannot describe.
"""
import pytest

from core.wallet import deploy_guard, token_template
from core.wallet.simulation import Deltas

HOLDER = "0x2222222222222222222222222222222222222222"


class _Intent:
    """The subset of TxIntent assert_deploy reads."""

    def __init__(self, **kw):
        self.init_code = kw.get("init_code", "0x60006000")
        self.expected_runtime = kw.get("expected_runtime")
        self.immutable_slots = kw.get("immutable_slots", ())


def _deltas(**kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                holder_transfers=(), holder_approvals=(), gas_used=500_000,
                return_data="0x6080604052")
    base.update(kw)
    return Deltas(**base)


# -- address prediction ----------------------------------------------------

@pytest.mark.parametrize("nonce,expected", [
    # The canonical Ethereum test vectors for keccak(rlp([sender, nonce])).
    (0, "0xcd234A471b72ba2F1Ccf0A70FCABA648a5eeCD8d"),
    (1, "0x343c43A37D37dfF08AE8C4A11544c718AbB4fCF8"),
])
def test_predicts_the_canonical_create_address(nonce, expected):
    got = deploy_guard.predict_create_address(
        "0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", nonce)
    assert got == expected


def test_nonce_boundary_crosses_the_rlp_single_byte_form():
    """127 encodes as one byte, 128 as a length-prefixed string. A builder that
    gets this wrong names an address the contract never lands at."""
    a = deploy_guard.predict_create_address(
        "0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", 127)
    b = deploy_guard.predict_create_address(
        "0x6ac7ea33f8831ea9dcc53393aaa88b25a785dbf0", 128)
    assert a != b
    assert a == "0x06d9a77f5E4b311Bae8D559DB9CDB4dF94104aA0"
    assert b == "0x08e190dcB7b73F5fcDAbb43e102215c83659A76D"


def test_a_bad_sender_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        deploy_guard.predict_create_address("0xdeadbeef", 0)


# -- template comparison ---------------------------------------------------

def test_template_matches_when_only_immutable_slots_differ():
    template = bytes(20)
    produced = bytearray(template)
    produced[4] = 0xAA
    produced[5] = 0xBB
    assert deploy_guard.runtime_matches_template(
        bytes(produced), template, ((4, 2),)) is None


def test_a_difference_outside_an_immutable_slot_refuses():
    template = bytes(20)
    produced = bytearray(template)
    produced[9] = 0xAA
    why = deploy_guard.runtime_matches_template(bytes(produced), template, ((4, 2),))
    assert why and "byte 9" in why


def test_a_different_length_refuses():
    why = deploy_guard.runtime_matches_template(b"\x01\x02", b"\x01\x02\x03", ())
    assert why and "not the contract that was declared" in why


def test_a_slot_outside_the_template_refuses_rather_than_clipping():
    why = deploy_guard.runtime_matches_template(b"\x00" * 4, b"\x00" * 4, ((2, 99),))
    assert why and "outside" in why


# -- assert_deploy ---------------------------------------------------------

def test_a_clean_deploy_returns_facts():
    facts, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(), holder=HOLDER, nonce=7)
    assert why is None
    assert facts.predicted_address.startswith("0x")
    assert facts.runtime_size == 5
    assert facts.template_matched is False


def test_empty_runtime_refuses():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(return_data="0x"), holder=HOLDER, nonce=1)
    assert why and "EMPTY runtime code" in why


def test_missing_return_data_refuses():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(return_data=None), holder=HOLDER, nonce=1)
    assert why and "no return data" in why


def test_oversized_runtime_refuses():
    big = "0x" + "00" * (deploy_guard.MAX_RUNTIME_BYTES + 1)
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(return_data=big), holder=HOLDER, nonce=1)
    assert why and "EIP-170" in why


def test_oversized_init_code_refuses():
    huge = "0x" + "00" * (deploy_guard.MAX_INIT_BYTES + 1)
    _f, why = deploy_guard.assert_deploy(
        _Intent(init_code=huge), _deltas(), holder=HOLDER, nonce=1)
    assert why and "EIP-3860" in why


def test_a_constructor_that_moves_a_token_refuses():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(token_deltas={"0xabc": -5}), holder=HOLDER, nonce=1)
    assert why and "must not spend" in why


def test_a_constructor_that_emits_a_holder_transfer_refuses():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(holder_transfers=(("0xabc", "0xdef", 9),)),
        holder=HOLDER, nonce=1)
    assert why and "not a deployment" in why


def test_a_constructor_that_grants_an_allowance_refuses():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(holder_approvals=(("0xabc", "0xdef", 9),)),
        holder=HOLDER, nonce=1)
    assert why and "standing claim" in why


def test_a_measured_allowance_increase_refuses():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(allowance_deltas={("0xabc", "0xdef"): 5}),
        holder=HOLDER, nonce=1)
    assert why and "grants an allowance" in why


def test_no_nonce_refuses_rather_than_deploying_to_an_unnamed_address():
    _f, why = deploy_guard.assert_deploy(
        _Intent(), _deltas(), holder=HOLDER, nonce=None)
    assert why and "cannot be predicted" in why


def test_the_pinned_template_matches_itself():
    facts, why = deploy_guard.assert_deploy(
        _Intent(init_code=token_template.INIT_CODE,
                expected_runtime=token_template.RUNTIME,
                immutable_slots=token_template.IMMUTABLE_SLOTS),
        _deltas(return_data=token_template.RUNTIME), holder=HOLDER, nonce=3)
    assert why is None
    assert facts.template_matched is True
    assert facts.runtime_hash == token_template.RUNTIME_HASH


def test_a_substituted_immutable_still_matches_the_template():
    """The live case: a real deployment fills totalSupply and decimals, so the
    produced runtime is never byte-equal to the artifact."""
    produced = bytearray(bytes.fromhex(token_template.RUNTIME[2:]))
    for (offset, length) in token_template.IMMUTABLE_SLOTS:
        for i in range(offset, offset + length):
            produced[i] = 0x7F
    facts, why = deploy_guard.assert_deploy(
        _Intent(init_code=token_template.INIT_CODE,
                expected_runtime=token_template.RUNTIME,
                immutable_slots=token_template.IMMUTABLE_SLOTS),
        _deltas(return_data="0x" + bytes(produced).hex()), holder=HOLDER, nonce=3)
    assert why is None, why
    assert facts.template_matched is True


def test_a_tampered_template_byte_refuses():
    produced = bytearray(bytes.fromhex(token_template.RUNTIME[2:]))
    produced[0] ^= 0xFF
    _f, why = deploy_guard.assert_deploy(
        _Intent(init_code=token_template.INIT_CODE,
                expected_runtime=token_template.RUNTIME,
                immutable_slots=token_template.IMMUTABLE_SLOTS),
        _deltas(return_data="0x" + bytes(produced).hex()), holder=HOLDER, nonce=3)
    assert why and "not the contract that was declared" in why


# ==========================================================================
# CREATE2 — the deterministic path (042b)
# ==========================================================================

def test_the_create2_factory_pin_is_checksummed():
    from eth_utils import to_checksum_address
    assert to_checksum_address(deploy_guard.CREATE2_FACTORY) == \
        deploy_guard.CREATE2_FACTORY


def test_the_address_is_a_hash_of_the_code_so_a_byte_change_moves_it():
    """That property is the whole proof: the address commits to the init code,
    so a factory returning the address we computed from OUR bytes has deployed
    our bytes and no others."""
    salt = "0x" + "11" * 32
    a = deploy_guard.predict_create2_address(salt, "0x6080")
    b = deploy_guard.predict_create2_address(salt, "0x6081")
    assert a != b


def test_the_same_salt_and_code_give_the_same_address_every_time():
    """Which is what buys one address on every chain — the factory is deployed
    at an identical address with identical code on all five."""
    salt = "0x" + "ab" * 32
    assert deploy_guard.predict_create2_address(salt, token_template.INIT_CODE) \
        == deploy_guard.predict_create2_address(salt, token_template.INIT_CODE)


def test_the_live_factorys_own_answer_is_reproduced():
    """Measured against the deployed factory on base and robinhood 2026-09-13:
    `eth_call` to it with salt++initcode returned exactly this address."""
    args = token_template.encode_constructor_args(
        name="Rob Coin", symbol="ROB", decimals=18, supply_raw=10 ** 27)
    init = token_template.INIT_CODE + args
    got = deploy_guard.predict_create2_address("0x" + "11" * 32, init)
    assert got == "0x9947b39c9D1F33D69F2aE426A49705e230138Ad9"


def test_the_factory_calldata_is_salt_then_code_with_no_selector():
    data = deploy_guard.create2_calldata("0x" + "22" * 32, "0xdeadbeef")
    assert data == "0x" + "22" * 32 + "deadbeef"


@pytest.mark.parametrize("salt", ["0x1234", "0x", "", "not hex"])
def test_a_salt_that_is_not_32_bytes_refuses(salt):
    with pytest.raises(ValueError):
        deploy_guard.predict_create2_address(salt, "0x6080")


# -- vanity mining ---------------------------------------------------------

def test_a_mined_salt_actually_produces_the_address_it_claims():
    salt, address, attempts = deploy_guard.mine_vanity_salt(
        "0x6080", prefix="ab", max_attempts=200_000)
    assert salt is not None, f"no match in {attempts} attempts"
    assert address.lower()[2:].startswith("ab")
    assert deploy_guard.predict_create2_address(salt, "0x6080") == address


def test_a_suffix_works_too():
    salt, address, _n = deploy_guard.mine_vanity_salt(
        "0x6080", suffix="cd", max_attempts=200_000)
    assert salt is not None
    assert address.lower().endswith("cd")


def test_exhaustion_returns_an_answer_rather_than_raising():
    """Not finding one is an answer. Raising would make a bounded search look
    like a failure."""
    salt, address, attempts = deploy_guard.mine_vanity_salt(
        "0x6080", prefix="abcdef", max_attempts=5)
    assert (salt, address) == (None, None)
    assert attempts == 5


@pytest.mark.parametrize("pattern", ["r0b", "xyz", "hello"])
def test_a_non_hex_pattern_refuses_rather_than_searching_forever(pattern):
    """An address has only 0-9a-f. Searching for 'r' would never terminate."""
    with pytest.raises(ValueError) as exc:
        deploy_guard.mine_vanity_salt("0x6080", prefix=pattern)
    assert "only 0-9a-f" in str(exc.value)


def test_an_absurd_pattern_is_refused_up_front():
    with pytest.raises(ValueError) as exc:
        deploy_guard.mine_vanity_salt("0x6080", prefix="deadbeef1")
    assert "4 billion" in str(exc.value)


# -- the assertion ---------------------------------------------------------

class _C2Intent(_Intent):
    def __init__(self, **kw):
        self.create2_salt = kw.pop("create2_salt", "0x" + "11" * 32)
        super().__init__(**kw)


def _c2_deltas(address, **kw):
    base = dict(ok=True, native_delta=0, token_deltas={}, allowance_deltas={},
                holder_transfers=(), holder_approvals=(), gas_used=500_000,
                return_data="0x" + "00" * 12 + address[2:].lower())
    base.update(kw)
    return Deltas(**base)


def test_a_create2_deploy_to_the_committed_address_is_authorized():
    intent = _C2Intent(init_code="0x6080")
    expected = deploy_guard.predict_create2_address(intent.create2_salt, "0x6080")
    facts, why = deploy_guard.assert_deploy(
        intent, _c2_deltas(expected), holder=HOLDER, nonce=None)
    assert why is None, why
    assert facts.predicted_address == expected


def test_a_create2_deploy_to_a_DIFFERENT_address_refuses():
    """The bytes being deployed are not the bytes that were declared — that is
    what a mismatch means, and it is the only thing this path has to check."""
    intent = _C2Intent(init_code="0x6080")
    facts, why = deploy_guard.assert_deploy(
        intent, _c2_deltas("0x" + "99" * 20), holder=HOLDER, nonce=None)
    assert facts is None
    assert "The address is a hash OF the code" in why


def test_a_create2_deploy_needs_no_nonce():
    """The address does not come from the nonce, so requiring one would refuse
    a perfectly provable deployment."""
    intent = _C2Intent(init_code="0x6080")
    expected = deploy_guard.predict_create2_address(intent.create2_salt, "0x6080")
    _facts, why = deploy_guard.assert_deploy(
        intent, _c2_deltas(expected), holder=HOLDER, nonce=None)
    assert why is None


def test_an_empty_factory_return_reads_as_an_occupied_address():
    intent = _C2Intent(init_code="0x6080")
    _facts, why = deploy_guard.assert_deploy(
        intent, _c2_deltas("0x" + "00" * 20, return_data="0x"),
        holder=HOLDER, nonce=None)
    assert why and "already occupied" in why


def test_the_zero_address_is_a_failed_deployment_not_a_success():
    intent = _C2Intent(init_code="0x6080")
    _facts, why = deploy_guard.assert_deploy(
        intent, _c2_deltas("0x" + "00" * 20), holder=HOLDER, nonce=None)
    assert why and "zero address" in why


def test_a_create2_constructor_that_moves_a_token_still_refuses():
    """The movement checks are shared — proving WHAT is deployed says nothing
    about what the constructor did on the way."""
    intent = _C2Intent(init_code="0x6080")
    expected = deploy_guard.predict_create2_address(intent.create2_salt, "0x6080")
    _facts, why = deploy_guard.assert_deploy(
        intent, _c2_deltas(expected, token_deltas={"0xabc": -5}),
        holder=HOLDER, nonce=None)
    assert why and "must not spend" in why
