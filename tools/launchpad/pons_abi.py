"""Pons V2 on Robinhood Chain — the pinned ABI and addresses (042).

⚠️ EVERY value here was read from chain 4663 on 2026-09-13 and cross-checked
four ways, because a launchpad pin that has quietly moved sends the owner's
money into a stranger's contract:

1. the factory address came from a LIVE pons-v2 pool's ``factory()`` view, not
   from a blog post — web search returns the **V1** generation
   (``0xA5aAb3F0…``), which is a different protocol;
2. every other address came from a factory view (``launchForwarder()``,
   ``memeHook()``, ``locker()``, …), so the set is self-consistent by
   construction;
3. the ABI is Sourcify ``exact_match`` verified source (solc 0.8.35, viaIR,
   optimizer 200, cancun) RECOMPILED to bytecode identical to ``eth_getCode``
   outside the immutable slots, with 60/60 and 8/8 dispatcher coverage;
4. the acceptance test in ``tests/unit/tools/launchpad/test_pons_abi.py``
   re-encodes the arguments of two LIVE mainnet transactions and asserts the
   calldata is byte-identical.

The runtime code hashes below are re-checked before every launch
(``pons.verify_pins``). A pin that no longer matches REFUSES — it does not warn.

⚠️ A CURVE is deliberately NOT pinned by code hash. Each curve bakes its
``creatorTaxBps`` and ``deployer`` into its runtime, so two honest curves have
different hashes. A curve is verified by provenance instead:
``factory.getLaunchedToken(token).curve == curve``.
"""
from __future__ import annotations

CHAIN = "robinhood"
CHAIN_ID = 4663

FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
ROUTER = "0xe33E9E479dF8802cb0866d5d05258bEc4cF62948"      # PonsV2LaunchAndBuy
DEPLOYER = "0x3711ceA4feaDE896C913C68F01Eda97Cb06D1A42"
MEME_HOOK = "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044"
LOCKER = "0x267444D099b10fB5Ed7c3Cc7B7c767AdcA574952"

#: ``keccak(eth_getCode(addr))``. Re-read and compared before every launch.
CODE_HASHES = {
    FACTORY: "0x89a27da6f703e0a7cdd4f233e7cb57604ff75b164530962d3ff7cf8483a67d84",
    ROUTER: "0xed9065184519eaa24a22c2556403d5d8bbb230ff94dbc5c414cf5028e20e52e7",
}

#: The ONE launch config that exists. ``launchConfigCount()`` is 1, and all
#: 84,875 ``TokenLaunched`` logs sampled across 4.4 days carry id 0.
LAUNCH_CONFIG_ID = 0

#: ``pairToken = address(0)`` is the NATIVE quote. 57% of live launches use it,
#: it needs no ERC-20 approval, and it carries the lowest graduation threshold
#: (4.2e18). An ERC-20 pair additionally needs ``approvedPairTokens()``.
NATIVE_PAIR = "0x0000000000000000000000000000000000000000"

#: Maximum creator tax, read live from ``maxCreatorTaxBps()``. Live launches use 100.
MAX_CREATOR_TAX_BPS = 1000

#: ``MAX_SNIPE_TAX_EXEMPTIONS`` in the factory source.
MAX_SNIPE_EXEMPTIONS = 32

SOCIALS = {
    "name": "socials", "type": "tuple",
    "components": [
        {"name": "twitter", "type": "string"},
        {"name": "telegram", "type": "string"},
        {"name": "discord", "type": "string"},
        {"name": "website", "type": "string"},
        {"name": "farcaster", "type": "string"},
    ],
}

TOKEN_PARAMS = {
    "name": "params", "type": "tuple",
    "components": [
        {"name": "name", "type": "string"},
        {"name": "symbol", "type": "string"},
        {"name": "logo", "type": "string"},
        {"name": "description", "type": "string"},
        SOCIALS,
        {"name": "creatorFeeRecipient", "type": "address"},
        {"name": "creatorTaxBps", "type": "uint16"},
        {"name": "buybackEnabled", "type": "bool"},
        # A commitment to the owner-settable launch terms. bytes32(0) WAIVES the
        # check; anything else must equal previewLaunchEconomics(id, pairToken)
        # or the factory reverts LaunchEconomicsMismatch. We always COMMIT (read
        # it live, pass it) — waiving means accepting whatever the terms became
        # between the quote and the broadcast, which is exactly the thing a
        # guard exists to prevent.
        {"name": "expectedEconomics", "type": "bytes32"},
        # CREATE2 salt, namespaced per deployer as keccak(abi.encode(deployer, salt)).
        {"name": "salt", "type": "bytes32"},
    ],
}

LAUNCH_TOKEN = {
    "name": "launchToken",
    "inputs": [
        TOKEN_PARAMS,
        {"name": "launchConfigId", "type": "uint256"},
        {"name": "pairToken", "type": "address"},
        {"name": "snipeTaxExemptions", "type": "address[]"},
    ],
    "outputs": [{"name": "token", "type": "address"},
                {"name": "curve", "type": "address"}],
}

LAUNCH_AND_BUY = {
    "name": "launchAndBuy",
    "inputs": [
        TOKEN_PARAMS,
        {"name": "launchConfigId", "type": "uint256"},
        {"name": "pairToken", "type": "address"},
        {"name": "quoteIn", "type": "uint256"},
        {"name": "minTokensOut", "type": "uint256"},
        {"name": "recipient", "type": "address"},
        {"name": "snipeTaxExemptions", "type": "address[]"},
    ],
    "outputs": [{"name": "token", "type": "address"},
                {"name": "curve", "type": "address"},
                {"name": "tokensOut", "type": "uint256"}],
}

#: Factory views. Selectors are DERIVED from these shapes, never pasted.
GET_LAUNCHED_TOKEN = {
    "name": "getLaunchedToken",
    "inputs": [{"name": "token", "type": "address"}],
    "outputs": [{"name": "", "type": "tuple", "components": [
        {"name": "token", "type": "address"},
        {"name": "curve", "type": "address"},
        {"name": "deployer", "type": "address"},
        {"name": "creatorFeeRecipient", "type": "address"},
        {"name": "pairToken", "type": "address"},
        {"name": "graduationThreshold", "type": "uint256"},
        {"name": "poolFee", "type": "uint24"},
        {"name": "tickSpacing", "type": "int24"},
        {"name": "creatorTaxBps", "type": "uint16"},
        {"name": "buybackEnabled", "type": "bool"},
        {"name": "phase", "type": "uint8"},
        {"name": "sweptQuote", "type": "uint256"},
        {"name": "sweptTokens", "type": "uint256"},
        {"name": "sweptAt", "type": "uint256"},
        {"name": "exists", "type": "bool"},
    ]}],
}

GET_LAUNCH_CONFIG = {
    "name": "getLaunchConfig",
    "inputs": [{"name": "id", "type": "uint256"}],
    "outputs": [{"name": "", "type": "tuple", "components": [
        {"name": "supply", "type": "uint256"},
        {"name": "curveFeeBps", "type": "uint256"},
        {"name": "phantomQuote", "type": "uint256"},
        {"name": "graduationThreshold", "type": "uint256"},
        {"name": "poolFee", "type": "uint24"},
        {"name": "tickSpacing", "type": "int24"},
        {"name": "enabled", "type": "bool"},
    ]}],
}

PREVIEW_ECONOMICS = {
    "name": "previewLaunchEconomics",
    "inputs": [{"name": "launchConfigId", "type": "uint256"},
               {"name": "pairToken", "type": "address"}],
    "outputs": [{"name": "", "type": "bytes32"}],
}

PAIR_TOKEN_ECONOMICS = {
    "name": "pairTokenEconomics",
    "inputs": [{"name": "pairToken", "type": "address"}],
    "outputs": [{"name": "phantomQuote", "type": "uint256"},
                {"name": "graduationThreshold", "type": "uint256"},
                {"name": "decimals", "type": "uint8"}],
}

LAUNCH_FEE = {"name": "launchFee", "inputs": [],
              "outputs": [{"name": "", "type": "uint256"}]}
LAUNCH_ENABLED = {"name": "launchEnabled", "inputs": [],
                  "outputs": [{"name": "", "type": "bool"}]}
CAN_LAUNCH = {"name": "canLaunch",
              "inputs": [{"name": "who", "type": "address"}],
              "outputs": [{"name": "", "type": "bool"}]}
APPROVED_PAIR = {"name": "approvedPairTokens",
                 "inputs": [{"name": "token", "type": "address"}],
                 "outputs": [{"name": "", "type": "bool"}]}

#: Bonding-curve interface.
CURVE_BUY = {
    "name": "buy",
    "inputs": [{"name": "quoteIn", "type": "uint256"},
               {"name": "minTokensOut", "type": "uint256"},
               {"name": "recipient", "type": "address"}],
    "outputs": [{"name": "tokensOut", "type": "uint256"}],
}
CURVE_SELL = {
    "name": "sell",
    "inputs": [{"name": "tokensIn", "type": "uint256"},
               {"name": "minQuoteOut", "type": "uint256"},
               {"name": "recipient", "type": "address"}],
    "outputs": [{"name": "quoteOut", "type": "uint256"}],
}
CURVE_GET_RESERVES = {
    "name": "getReserves", "inputs": [],
    #: QUOTE FIRST. Reading these the other way round prices every trade
    #: backwards, and both are uint256 so nothing would complain.
    "outputs": [{"name": "quoteReserve", "type": "uint256"},
                {"name": "tokenReserve", "type": "uint256"}],
}
CURVE_FEE_BPS = {"name": "feeBps", "inputs": [],
                 "outputs": [{"name": "", "type": "uint256"}]}
CURVE_CREATOR_TAX_BPS = {"name": "creatorTaxBps", "inputs": [],
                         "outputs": [{"name": "", "type": "uint256"}]}
CURVE_SNIPE_TAX_BPS = {
    "name": "currentSnipeTaxBps",
    "inputs": [{"name": "recipient", "type": "address"}],
    "outputs": [{"name": "", "type": "uint256"}],
}
CURVE_GRADUATED = {"name": "graduated", "inputs": [],
                   "outputs": [{"name": "", "type": "bool"}]}
CURVE_READY = {"name": "readyToGraduate", "inputs": [],
               "outputs": [{"name": "", "type": "bool"}]}
CURVE_FACTORY = {"name": "factory", "inputs": [],
                 "outputs": [{"name": "", "type": "address"}]}
CURVE_TOKEN = {"name": "token", "inputs": [],
               "outputs": [{"name": "", "type": "address"}]}
CURVE_RESERVED_TOKENS = {"name": "reservedTokens", "inputs": [],
                         "outputs": [{"name": "", "type": "uint256"}]}
CURVE_GRADUATION_THRESHOLD = {"name": "graduationThreshold", "inputs": [],
                              "outputs": [{"name": "", "type": "uint256"}]}

#: The fee ESCROW. Creator tax does not sit on the curve — the curve credits it
#: to a shared escrow and the deployer claims from there. Reading the curve for a
#: fee balance returns 0 forever, which is exactly what happened in production on
#: 2026-09-14: the agent probed `creatorFees()`/`pendingFees()`/`claimable()` on
#: the curve, got reverts, and reported the fees unproven while 13.613 ETH sat in
#: the escrow. `creatorTaxBalance()` on the curve is the UNSWEPT remainder, not
#: the claimable total.
CURVE_FEE_ESCROW = {"name": "feeEscrow", "inputs": [],
                    "outputs": [{"name": "", "type": "address"}]}
CURVE_CREATOR_TAX_BALANCE = {"name": "creatorTaxBalance", "inputs": [],
                             "outputs": [{"name": "", "type": "uint256"}]}

#: Escrow interface. `balanceOf` is the NATIVE credit owed to an address — the
#: name is ERC-20-shaped but the asset is the chain's native token.
ESCROW_BALANCE_OF = {"name": "balanceOf",
                     "inputs": [{"name": "who", "type": "address"}],
                     "outputs": [{"name": "", "type": "uint256"}]}
ESCROW_CLAIM = {"name": "claim", "inputs": [], "outputs": []}
ESCROW_CLAIM_TOKEN = {"name": "claimToken",
                      "inputs": [{"name": "token", "type": "address"}],
                      "outputs": []}

#: Event topic0s, for reading a receipt back.
TOPIC_TOKEN_LAUNCHED = (
    "0x8d4aad4953d0ca700d468f3753aa14432d1b35b43ec6409f051fb6aa43a89607")
TOPIC_CURVE_BUY = (
    "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455")
TOPIC_CURVE_SELL = (
    "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df")
