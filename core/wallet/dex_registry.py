"""Uniswap deployments — PINNED in code, hash-verified per call, never configured.

Same rule as `core/wallet/erc8004.py`: an env var may CONFIRM a pinned address and
may never introduce one, because a position manager is where the treasury's two
tokens are sent. Same rule as `tools/launchpad/pons.py::verify_pins`: the code at
the pin is re-hashed before every write and a mismatch REFUSES, it does not warn.

Addresses verified 2026-09-16 from the Uniswap deployments feed and re-read via
`eth_getCode` when the hashes below were measured. `polygon`/`arbitrum` were
pinned 2026-11-09 (order 0013): addresses from the Uniswap deployments feed, code
hashes measured by `eth_getCode` on two public RPCs and cross-linked on-chain
(NPM.factory, PosM/StateView/V4Quoter.poolManager). Chains with no measured hash
(e.g. optimism) are absent on purpose; `row_for` returns None.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

PROTOCOLS = ("v3", "v4")


class DexPinError(RuntimeError):
    """A pinned deployment is not what was reviewed. Refuse, do not proceed."""


@dataclass(frozen=True)
class DexRow:
    protocol: str
    chain: str
    chain_id: int
    factory: Optional[str] = None            # v3
    position_manager: Optional[str] = None   # v3 NPM / v4 PosM
    quoter: Optional[str] = None
    pool_manager: Optional[str] = None       # v4
    state_view: Optional[str] = None         # v4
    permit2: Optional[str] = None            # v4


_PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

_ROWS: Dict[Tuple[str, str], DexRow] = {
    ("robinhood", "v3"): DexRow("v3", "robinhood", 4663,
        factory="0x1f7d7550b1b028f7571e69a784071f0205fd2efa",
        position_manager="0x73991a25c818bf1f1128deaab1492d45638de0d3",
        quoter="0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7"),
    ("robinhood", "v4"): DexRow("v4", "robinhood", 4663,
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        position_manager="0x58daec3116aae6d93017baaea7749052e8a04fa7",
        state_view="0xf3334192d15450cdd385c8b70e03f9a6bd9e673b",
        quoter="0x8dc178efb8111bb0973dd9d722ebeff267c98f94", permit2=_PERMIT2),
    ("base", "v3"): DexRow("v3", "base", 8453,
        factory="0x33128a8fC17869897dcE68Ed026d694621f6FDfD",
        position_manager="0x03a520b32C04BF3bEEf7BEb72E919cf822Ed34f1",
        quoter="0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"),
    ("base", "v4"): DexRow("v4", "base", 8453,
        pool_manager="0x498581ff718922c3f8e6a244956af099b2652b2b",
        position_manager="0x7c5f5a4bbd8fd63184577525326123b519429bdc",
        state_view="0xa3c0c9b65bad0b08107aa264b0f3db444b867a71",
        quoter="0x0d5e0f971ed27fbff6c2837bf31316121532048d", permit2=_PERMIT2),
    ("ethereum", "v3"): DexRow("v3", "ethereum", 1,
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        position_manager="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    ("ethereum", "v4"): DexRow("v4", "ethereum", 1,
        pool_manager="0x000000000004444c5dc75cB358380D2e3dE08A90",
        position_manager="0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e",
        state_view="0x7ffe42c4a5deea5b0fec41c94c136cf115597227",
        quoter="0x52f0e24d1c21c8a0cb1e5a5dd6198556bd9e1203", permit2=_PERMIT2),
    ("arbitrum", "v3"): DexRow("v3", "arbitrum", 42161,
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        position_manager="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    ("arbitrum", "v4"): DexRow("v4", "arbitrum", 42161,
        pool_manager="0x360e68faccca8ca495c1b759fd9eee466db9fb32",
        position_manager="0xd88f38f930b7952f2db2432cb002e7abbf3dd869",
        state_view="0x76fd297e2d437cd7f76d50f01afe6160f86e9990",
        quoter="0x8928074CA1b241D8Ec02815881c1Af11E8bC5219", permit2=_PERMIT2),
    ("polygon", "v3"): DexRow("v3", "polygon", 137,
        factory="0x1F98431c8aD98523631AE4a59f267346ea31F984",
        position_manager="0xC36442b4a4522E871399CD717aBDD847Ab11FE88",
        quoter="0x61fFE014bA17989E743c5F6cB21bF9697530B21e"),
    ("polygon", "v4"): DexRow("v4", "polygon", 137,
        pool_manager="0x67366782805870060151383f4bbff9dab53e5cd6",
        position_manager="0x1ec2ebf4f37e7363fdfe3551602425af0b3ceef9",
        state_view="0x5ea1bd7974c8a611cbab0bdcafcb1d9cc9b3ba5a",
        quoter="0x31AB0Dea31FC0ad5A54F03cD59C896eCD067447B", permit2=_PERMIT2),
}

#: keccak(eth_getCode) per (chain, lowercase address) — MEASURED, pasted from the
#: step-3 script, never typed from a document.
CODE_HASHES: Dict[Tuple[str, str], str] = {
    ("robinhood", "0x1f7d7550b1b028f7571e69a784071f0205fd2efa"): "0xec72b1abd1f2faee020cfea9c646bd8994f9fb389054f6e574f103a895091739",
    ("robinhood", "0x73991a25c818bf1f1128deaab1492d45638de0d3"): "0x0a493d1af3d0f25fed8efa205244ebee14114267a08647fc38c515c7cd6ead4f",
    ("robinhood", "0x8366a39cc670b4001a1121b8f6a443a643e40951"): "0xbd3881180b547f5fe817545743cfb4343e96b1bc6640dcd70c106b0066e95626",
    ("robinhood", "0x58daec3116aae6d93017baaea7749052e8a04fa7"): "0xc873e135dc9aaec88489cfbad146b4cb49d6a32e0d80326377784b7ba17670b2",
    # v4 StateView + Permit2 on 4663, measured 2026-09-29 (anvil fork of 4663,
    # `cast keccak $(cast code …)`); core handoff W6, 090 R0.4.
    ("robinhood", "0xf3334192d15450cdd385c8b70e03f9a6bd9e673b"): "0x7d9c591e0956fd89d98feb4ffcfe8bf1f7a62bd485edd979fa21d104b49878a6",
    ("robinhood", "0x000000000022d473030f116ddee9f6b43ac78ba3"): "0x5208783f52488f7d3493e5e38311ab707c1d75457fe472a19b0b4d57d66a7fca",
    ("base", "0x33128a8fc17869897dce68ed026d694621f6fdfd"): "0x95707a4ac71f20181a63ef7d180e3c625be5d20fc8f6f980befa966bad568132",
    ("base", "0x03a520b32c04bf3beef7beb72e919cf822ed34f1"): "0x9177a11768996e8f951e0f0013d7165134178b15b21fb9916108f995e6c564bf",
    ("base", "0x498581ff718922c3f8e6a244956af099b2652b2b"): "0x83b2af6e9f3158defc2811cbcb0db71ecf8b2ba2abea39c39e370ac5c6f43eb6",
    ("base", "0x7c5f5a4bbd8fd63184577525326123b519429bdc"): "0x243f9e091ddf11c7c04e28059fdbbf1bab82b72d414fafb8e096c097aaeb622a",
    ("ethereum", "0x1f98431c8ad98523631ae4a59f267346ea31f984"): "0x4d7b8525cd5d14343fa67a732fba5b24cddba11620ca88392f4ec6c52f91fd69",
    ("ethereum", "0xc36442b4a4522e871399cd717abdd847ab11fe88"): "0x692e658b31cbe3407682854806658d315d61a58c7e4933a2f91d383dc00736c6",
    ("ethereum", "0x000000000004444c5dc75cb358380d2e3de08a90"): "0x785f1014552b7ce7d5fb7d0c970ca60edee94fd00425d7ca21609acac7ce1293",
    ("ethereum", "0xbd216513d74c8cf14cf4747e6aaa6420ff64ee9e"): "0x77e36c08b19959a30dde46dec9abe6208e371ff2f56884a56fe1e1a53615528b",
    ("arbitrum", "0x1f98431c8ad98523631ae4a59f267346ea31f984"): "0x4d7b8525cd5d14343fa67a732fba5b24cddba11620ca88392f4ec6c52f91fd69",
    ("arbitrum", "0xc36442b4a4522e871399cd717abdd847ab11fe88"): "0x45b4bff8136324d175c01151fd0fb715c82bcc8923019a4faba3eafb0cc3a20b",
    ("arbitrum", "0x360e68faccca8ca495c1b759fd9eee466db9fb32"): "0xe4b2759e456c9c4ef763e3b4e257c5105e1ba283d7de8b131dd321197de794a4",
    ("arbitrum", "0xd88f38f930b7952f2db2432cb002e7abbf3dd869"): "0x6156ddaa1c8cd2c26d37455a5dc57b1761dc2848856426c0ac261ae0c7fecd68",
    ("arbitrum", "0x76fd297e2d437cd7f76d50f01afe6160f86e9990"): "0x4c0e823a0cd44b6b2d9485e774c421cf929db3996096d9b84ee6b23525184b9e",
    ("arbitrum", "0x000000000022d473030f116ddee9f6b43ac78ba3"): "0x9e51dcb64cf56fc09a82cb41edbc17c6a2250f18dbd1b91e884c0aca02acd57c",
    ("polygon", "0x1f98431c8ad98523631ae4a59f267346ea31f984"): "0x4d7b8525cd5d14343fa67a732fba5b24cddba11620ca88392f4ec6c52f91fd69",
    ("polygon", "0xc36442b4a4522e871399cd717abdd847ab11fe88"): "0xdcffe49a687c3ea536e42193f2e0bc89c20282c7bd6106eddc2bfc19a091114e",
    ("polygon", "0x67366782805870060151383f4bbff9dab53e5cd6"): "0xcfd0bc71e4f75b0c3e078e53baad950360d3e9f62b55c48b556f9de967aa80f7",
    ("polygon", "0x1ec2ebf4f37e7363fdfe3551602425af0b3ceef9"): "0x876d8664907d1fa2b3f00e6f49671585abd614c836f110b36c1e76fd407e67ba",
    ("polygon", "0x5ea1bd7974c8a611cbab0bdcafcb1d9cc9b3ba5a"): "0x7dc64bb4b52657a626a2cc9589d5db335ac6e51eea0d15fef3822d0293c21098",
    ("polygon", "0x000000000022d473030f116ddee9f6b43ac78ba3"): "0xdb0dd198c1767513d04589d8b3da05423ddc97e543440f84afe0e216693af9bb",
}


#: v4 HOOKS a liquidity write may touch, keyed ``(chain, lowercase hook)`` →
#: keccak(runtime). A hook runs inside every add/remove, so an unpinned hook is
#: an unreviewed contract on the money path: refuse it. ``address(0)`` (no hook)
#: needs no pin. Pons MEME_HOOK measured 2026-09-29 on a 4663 fork (090 R0.4).
HOOK_HASHES: Dict[Tuple[str, str], str] = {
    ("robinhood", "0xe5e702641ea86f4ae6cc3cdaed2b886f976be044"): "0xc21b1e6c1b45403e81a581f22ed6d9c747997af1cfdac1b1dc9f4b1d346a10db",
}

_ZERO = "0x" + "0" * 40


def all_rows() -> Tuple[DexRow, ...]:
    return tuple(_ROWS.values())


def supported(protocol: str) -> Tuple[str, ...]:
    return tuple(c for (c, p) in _ROWS if p == protocol)


def row_for(chain: Optional[str], protocol: Optional[str]) -> Optional[DexRow]:
    if not chain or not protocol:
        return None
    return _ROWS.get((str(chain).strip().lower(), str(protocol).strip().lower()))


def position_managers(chain: Optional[str]) -> frozenset:
    """Every pinned position manager on *chain*, lowercase. Empty = none pinned,
    which is a refusal to treat ANY spender as one — never 'then everybody'."""
    return frozenset(r.position_manager.lower() for (c, _p), r in _ROWS.items()
                     if c == (chain or "").lower() and r.position_manager)


def resolve_position_manager(chain: Optional[str], protocol: str) -> str:
    row = row_for(chain, protocol)
    if row is None or not row.position_manager:
        raise ValueError(
            f"chain {chain!r} has no pinned Uniswap {protocol} position manager. "
            f"Supported for {protocol}: {', '.join(supported(protocol))}")
    env_name = f"UNISWAP_{protocol.upper()}_POSITION_MANAGER_{str(chain).upper()}"
    override = (os.getenv(env_name) or "").strip()
    if override and override.lower() != row.position_manager.lower():
        raise ValueError(
            f"{env_name}={override} does not match the pinned position manager "
            f"for {chain} ({row.position_manager}). This value may only CONFIRM "
            f"the pinned address, never replace it — a liquidity add sends two "
            f"tokens from the treasury wallet to whatever this resolves to.")
    return row.position_manager


def verify_pins(rpc: Callable, chain: str, protocol: str) -> None:
    """Re-hash the code at every write target for (chain, protocol). Refuses on
    any mismatch or on missing code. `rpc(method, params)`."""
    row = row_for(chain, protocol)
    if row is None:
        raise DexPinError(f"no pinned Uniswap {protocol} deployment on {chain}")
    # v4 also re-hashes StateView (the reads a write is sized from) and Permit2
    # (which moves the ERC-20 leg) — 090 R0.4.
    extra = (row.state_view, row.permit2) if protocol == "v4" else ()
    for addr in (row.factory, row.position_manager, row.pool_manager, *extra):
        if not addr:
            continue
        _check_code(rpc, row.chain, addr, CODE_HASHES.get((row.chain, addr.lower())),
                    f"the pinned Uniswap {protocol} deployment")


def hook_pinned(chain: Optional[str], hook: Optional[str]) -> bool:
    """True for ``address(0)`` or a hook with a measured hash on *chain*."""
    h = str(hook or "").lower()
    return h == _ZERO or (str(chain or "").lower(), h) in HOOK_HASHES


def verify_hook(rpc: Callable, chain: str, hook: str) -> None:
    """Re-hash a v4 hook before a write that runs it. ``address(0)`` passes
    (no hook runs); an unpinned hook or a changed runtime REFUSES."""
    h = str(hook or "").lower()
    if h == _ZERO:
        return
    _check_code(rpc, str(chain).lower(), h, HOOK_HASHES.get((str(chain).lower(), h)),
                "the pinned v4 hook")


def _check_code(rpc: Callable, chain: str, addr: str, expected: Optional[str],
                what: str) -> None:
    from eth_utils import keccak
    if not expected:
        raise DexPinError(f"{addr} on {chain} has no measured code hash — refusing")
    code = rpc("eth_getCode", [addr, "latest"])
    if not isinstance(code, str) or len(code) <= 2:
        raise DexPinError(f"{addr} has NO code on {chain} — {what} is not there. "
                          f"Refusing.")
    got = "0x" + keccak(bytes.fromhex(code[2:])).hex()
    if got != expected:
        raise DexPinError(
            f"the code at {addr} hashes to {got}, not the pinned {expected}. The "
            f"deployment has changed since it was reviewed — refusing.")
