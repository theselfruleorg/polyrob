"""Jupiter v6 route-plan ``Swap`` enum: the byte layout of every variant.

GENERATED from Jupiter v6's on-chain Anchor IDL — program
JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4, IDL account
C88XWfp26heEmDkmfSzeXP7Fd7GQJ2j9dDTUsyiZbUTa, fetched 2026-10-08, decompressed
JSON sha256 82a3907455712fa9c2104d1b8fb8a2649cc4d96789d4c16a572ed343981b9f4e
(197 Swap variants). Variants 0..38 match the earlier reviewed jupiter-cpi
idl.json byte for byte. Jupiter only APPENDS variants, so a newer route that
uses a tag past the end refuses by name until this table is regenerated.

Why the whole enum: Anchor permits trailing instruction bytes, so the swap
bounds (in amount, quoted out, slippage) can only be trusted when the route
vector before them is parsed exactly. A variant whose width is guessed would
let a forged suffix pose as the executed floor.

Type grammar: a primitive name, ``("defined", Name)``, ``("vec", T)``,
``("option", T)``, ``("array", T, n)`` or ``"bytes"`` (u32 length + bytes).
"""
import struct

_PRIMITIVE = {"u8": 1, "u16": 2, "u32": 4, "u64": 8, "u128": 16}

STRUCTS = {
    "RemainingAccountsInfo": (("vec", ("defined", "RemainingAccountsSlice")),),
    "RemainingAccountsSlice": ("u8", "u8"),
    "CandidateSwapWithBps": (("defined", "CandidateSwap"), "u32"),
}

UNIT_ENUMS = {'Side': 2, 'BisonFiPredictSide': 2, 'HyloSwapType': 8, 'SanctumSolsSwapType': 3, 'PerenaTrancheKind': 2}

SWAP_VARIANTS = (
    ('Saber', ()),  # 0
    ('SaberAddDecimalsDeposit', ()),  # 1
    ('SaberAddDecimalsWithdraw', ()),  # 2
    ('TokenSwap', ()),  # 3
    ('Sencha', ()),  # 4
    ('Step', ()),  # 5
    ('Cropper', ()),  # 6
    ('Raydium', ()),  # 7
    ('Crema', ('bool',)),  # 8
    ('Lifinity', ()),  # 9
    ('Mercurial', ()),  # 10
    ('Cykura', ()),  # 11
    ('Serum', (('defined', 'Side'),)),  # 12
    ('MarinadeDeposit', ()),  # 13
    ('MarinadeUnstake', ()),  # 14
    ('Aldrin', (('defined', 'Side'),)),  # 15
    ('AldrinV2', (('defined', 'Side'),)),  # 16
    ('Whirlpool', ('bool',)),  # 17
    ('Invariant', ('bool',)),  # 18
    ('Meteora', ()),  # 19
    ('GooseFX', ()),  # 20
    ('DeltaFi', ('bool',)),  # 21
    ('Balansol', ()),  # 22
    ('MarcoPolo', ('bool',)),  # 23
    ('Dradex', (('defined', 'Side'),)),  # 24
    ('LifinityV2', ()),  # 25
    ('RaydiumClmm', ()),  # 26
    ('Openbook', (('defined', 'Side'),)),  # 27
    ('Phoenix', (('defined', 'Side'),)),  # 28
    ('Symmetry', ('u64', 'u64')),  # 29
    ('TokenSwapV2', ()),  # 30
    ('HeliumTreasuryManagementRedeemV0', ()),  # 31
    ('StakeDexStakeWrappedSol', ()),  # 32
    ('StakeDexSwapViaStake', ('u32',)),  # 33
    ('GooseFXV2', ()),  # 34
    ('Perps', ()),  # 35
    ('PerpsAddLiquidity', ()),  # 36
    ('PerpsRemoveLiquidity', ()),  # 37
    ('MeteoraDlmm', ()),  # 38
    ('OpenBookV2', (('defined', 'Side'),)),  # 39
    ('RaydiumClmmV2', ()),  # 40
    ('StakeDexPrefundWithdrawStakeAndDepositStake', ('u32',)),  # 41
    ('Clone', ('u8', 'bool', 'bool')),  # 42
    ('SanctumS', ('u8', 'u8', 'u32', 'u32')),  # 43
    ('SanctumSAddLiquidity', ('u8', 'u32')),  # 44
    ('SanctumSRemoveLiquidity', ('u8', 'u32')),  # 45
    ('RaydiumCP', ()),  # 46
    ('WhirlpoolSwapV2', ('bool', ('option', ('defined', 'RemainingAccountsInfo')))),  # 47
    ('OneIntro', ()),  # 48
    ('PumpWrappedBuy', ()),  # 49
    ('PumpWrappedSell', ()),  # 50
    ('PerpsV2', ()),  # 51
    ('PerpsV2AddLiquidity', ()),  # 52
    ('PerpsV2RemoveLiquidity', ()),  # 53
    ('MoonshotWrappedBuy', ()),  # 54
    ('MoonshotWrappedSell', ()),  # 55
    ('StabbleStableSwap', ()),  # 56
    ('StabbleWeightedSwap', ()),  # 57
    ('Obric', ('bool',)),  # 58
    ('FoxBuyFromEstimatedCost', ()),  # 59
    ('FoxClaimPartial', ('bool',)),  # 60
    ('SolFi', ('bool',)),  # 61
    ('SolayerDelegateNoInit', ()),  # 62
    ('SolayerUndelegateNoInit', ()),  # 63
    ('TokenMill', (('defined', 'Side'),)),  # 64
    ('DaosFunBuy', ()),  # 65
    ('DaosFunSell', ()),  # 66
    ('ZeroFi', ()),  # 67
    ('StakeDexWithdrawWrappedSol', ()),  # 68
    ('VirtualsBuy', ()),  # 69
    ('VirtualsSell', ()),  # 70
    ('Perena', ('u8', 'u8')),  # 71
    ('PumpSwapBuy', ()),  # 72
    ('PumpSwapSell', ()),  # 73
    ('Gamma', ()),  # 74
    ('MeteoraDlmmSwapV2', (('defined', 'RemainingAccountsInfo'),)),  # 75
    ('Woofi', ()),  # 76
    ('MeteoraDammV2', ()),  # 77
    ('MeteoraDynamicBondingCurveSwap', ()),  # 78
    ('StabbleStableSwapV2', ()),  # 79
    ('StabbleWeightedSwapV2', ()),  # 80
    ('RaydiumLaunchlabBuy', ('u64',)),  # 81
    ('RaydiumLaunchlabSell', ('u64',)),  # 82
    ('BoopdotfunWrappedBuy', ()),  # 83
    ('BoopdotfunWrappedSell', ()),  # 84
    ('Plasma', (('defined', 'Side'),)),  # 85
    ('GoonFi', ('bool', 'u8')),  # 86
    ('HumidiFi', ('u64', 'bool')),  # 87
    ('MeteoraDynamicBondingCurveSwapWithRemainingAccounts', ()),  # 88
    ('TesseraV', (('defined', 'Side'),)),  # 89
    ('PumpWrappedBuyV2', ()),  # 90
    ('PumpWrappedSellV2', ()),  # 91
    ('PumpSwapBuyV2', ()),  # 92
    ('PumpSwapSellV2', ()),  # 93
    ('Heaven', ('bool',)),  # 94
    ('SolFiV2', ('bool',)),  # 95
    ('Aquifer', ()),  # 96
    ('PumpWrappedBuyV3', ()),  # 97
    ('PumpWrappedSellV3', ()),  # 98
    ('PumpSwapBuyV3', ()),  # 99
    ('PumpSwapSellV3', ()),  # 100
    ('JupiterLendDeposit', ()),  # 101
    ('JupiterLendRedeem', ()),  # 102
    ('DefiTuna', ('bool', ('option', ('defined', 'RemainingAccountsInfo')))),  # 103
    ('AlphaQ', ('bool',)),  # 104
    ('RaydiumV2', ()),  # 105
    ('SarosDlmm', ('bool',)),  # 106
    ('Futarchy', (('defined', 'Side'),)),  # 107
    ('MeteoraDammV2WithRemainingAccounts', ()),  # 108
    ('Obsidian', ()),  # 109
    ('WhaleStreet', (('defined', 'Side'),)),  # 110
    ('DynamicV1', (('vec', ('defined', 'CandidateSwap')), ('option', 'u8'))),  # 111
    ('PumpWrappedBuyV4', ()),  # 112
    ('PumpWrappedSellV4', ()),  # 113
    ('CarrotIssue', ()),  # 114
    ('CarrotRedeem', ()),  # 115
    ('Manifest', (('defined', 'Side'),)),  # 116
    ('BisonFi', ('bool',)),  # 117
    ('HumidiFiV2', ('u64', 'bool')),  # 118
    ('PerenaStar', ('bool',)),  # 119
    ('JupiterRfqV2', (('defined', 'Side'), 'bytes')),  # 120
    ('GoonFiV2', ('bool',)),  # 121
    ('Scorch', ('u128',)),  # 122
    ('VaultLiquidUnstake', (('array', 'u64', 5), 'u64')),  # 123
    ('XOrca', ()),  # 124
    ('Quantum', (('defined', 'Side'),)),  # 125
    ('WhaleStreetV2', (('defined', 'Side'), 'u64', 'u64')),  # 126
    ('Riptide', ('bool',)),  # 127
    ('RunnerRodeo', ()),  # 128
    ('TaurusFi', ('bool',)),  # 129
    ('Omnipair', ()),  # 130
    ('MSwap', ()),  # 131
    ('Hylo', (('defined', 'HyloSwapType'),)),  # 132
    ('VoltrDeposit', ()),  # 133
    ('VoltrWithdraw', ()),  # 134
    ('SanctumSV2', ('u8', 'u8', 'u32', 'u32')),  # 135
    ('LemmingsFi', ('bool',)),  # 136
    ('ScaleVmmBuy', ()),  # 137
    ('ScaleVmmSell', ()),  # 138
    ('ScaleAmmBuy', ()),  # 139
    ('ScaleAmmSell', ()),  # 140
    ('BisonFiV2', ('bool',)),  # 141
    ('Trends', ()),  # 142
    ('HumaDeposit', ()),  # 143
    ('HumaInstantWithdraw', ()),  # 144
    ('Kipseli', ('bool',)),  # 145
    ('DynamicV2', (('vec', ('defined', 'CandidateSwapWithBps')), 'u8', 'u8')),  # 146
    ('PumpSwapBuyV3WithCashbackClaim', ()),  # 147
    ('PumpSwapSellV3WithCashbackClaim', ()),  # 148
    ('PumpWrappedBuyV4WithCashbackClaim', ()),  # 149
    ('PumpWrappedSellV4WithCashbackClaim', ()),  # 150
    ('GoonFiV3', ('bool',)),  # 151
    ('PumpWrappedBuyV5', ('bool',)),  # 152
    ('PumpWrappedSellV5', ('bool',)),  # 153
    ('ZeroFiSwapV2', ()),  # 154
    ('BisonFiPredict', (('defined', 'BisonFiPredictSide'), 'bool')),  # 155
    ('ByrealDynamicV3', ()),  # 156
    ('Flux', ('u64', 'bool')),  # 157
    ('VaultLiquidSellLst', ()),  # 158
    ('VaultLiquidBuyLst', ('u64',)),  # 159
    ('KipseliV2', ('bool',)),  # 160
    ('Deriverse', (('defined', 'Side'), 'u32')),  # 161
    ('Hadron', ('bool',)),  # 162
    ('BinaryFi', ()),  # 163
    ('Metric', ('bool',)),  # 164
    ('JupiterLendDexSwap', ('bool',)),  # 165
    ('Gatorswap', ('bool',)),  # 166
    ('Flint', ('bool', 'bool')),  # 167
    ('Denali', ('bool',)),  # 168
    ('PerenaStarV2Deposit', ()),  # 169
    ('PerenaStarV2WithdrawFromExternal', ('u8',)),  # 170
    ('SanctumSols', (('defined', 'SanctumSolsSwapType'),)),  # 171
    ('HyloV2', (('defined', 'HyloSwapType'),)),  # 172
    ('SanctumPamm', ()),  # 173
    ('Archer', (('defined', 'Side'),)),  # 174
    ('TrenchWrappedBuy', ()),  # 175
    ('TrenchWrappedSell', ()),  # 176
    ('BisonFiMarketBacked', ('bool',)),  # 177
    ('TesseraVV2', (('defined', 'Side'),)),  # 178
    ('Stableswap', ()),  # 179
    ('BinaryFiV2', ()),  # 180
    ('KipseliV3', ('bool',)),  # 181
    ('PerenaStarV2TrancheDeposit', (('defined', 'PerenaTrancheKind'),)),  # 182
    ('PerenaStarV2TrancheWithdrawFromExternal', (('defined', 'PerenaTrancheKind'), ('option', 'u8'))),  # 183
    ('Quay', ('bool',)),  # 184
    ('HumidiFiRouter', ('u64', ('array', 'u8', 32), ('array', 'u8', 16), 'bool')),  # 185
    ('HumidiFiRouterV2', ('u64', 'u64', ('array', 'u8', 32), ('array', 'u8', 16), 'bool')),  # 186
    ('PumpWrappedBuyV6', ('bool', 'bool')),  # 187
    ('HyloRouter', ()),  # 188
    ('Memefun', ('bool',)),  # 189
    ('HumidiFiRouterV3', ('u64', 'u64', 'u64', 'u64', ('array', 'u8', 32), ('array', 'u8', 16), 'bool')),  # 190
    ('PumpWrappedBuyV7', ('bool', 'bool')),  # 191
    ('PumpWrappedSellV6', ('bool',)),  # 192
    ('PumpSwapBuyV4', ('bool',)),  # 193
    ('PumpSwapSellV4', ('bool',)),  # 194
    ('ZeroFiSwapV3', ()),  # 195
    ('DenaliV2', ('bool',)),  # 196
)

CANDIDATE_SWAP_VARIANTS = (
    ('HumidiFi', ('u64', 'bool')),  # 0
    ('TesseraV', (('defined', 'Side'),)),  # 1
    ('HumidiFiV2', ('u64', 'bool')),  # 2
    ('RaydiumV2', ()),  # 3
    ('RaydiumClmm', ()),  # 4
    ('Whirlpool', ('bool',)),  # 5
    ('ZeroFi', ()),  # 6
    ('BisonFiV2', ('bool',)),  # 7
    ('GoonFiV2', ('bool',)),  # 8
    ('GoonFiV3', ('bool',)),  # 9
    ('WhirlpoolV2', ('bool', ('option', ('defined', 'RemainingAccountsInfo')))),  # 10
    ('ZeroFiSwapV2', ()),  # 11
    ('BisonFiMarketBacked', ('bool',)),  # 12
    ('RaydiumClmmV2', ()),  # 13
    ('TesseraVV2', (('defined', 'Side'),)),  # 14
    ('HumidiFiRouter', ('u64', ('array', 'u8', 32), ('array', 'u8', 16), 'bool')),  # 15
    ('HumidiFiRouterV2', ('u64', 'u64', ('array', 'u8', 32), ('array', 'u8', 16), 'bool')),  # 16
    ('HumidiFiRouterV3', ('u64', 'u64', 'u64', 'u64', ('array', 'u8', 32), ('array', 'u8', 16), 'bool')),  # 17
    ('ZeroFiSwapV3', ()),  # 18
)

_ENUMS = {"Swap": SWAP_VARIANTS, "CandidateSwap": CANDIDATE_SWAP_VARIANTS}


class UnknownSwapVariant(ValueError):
    """The route uses a swap variant newer than this pinned table."""

    def __init__(self, tag: int):
        super().__init__(tag)
        self.tag = tag


def _skip(data: bytes, offset: int, kind) -> int:
    """Return the offset after one value of ``kind``; ValueError on bad bytes."""
    if isinstance(kind, str):
        if kind == "bool":
            if data[offset] not in (0, 1):
                raise ValueError("invalid bool")
            return offset + 1
        if kind == "bytes":
            size = struct.unpack_from("<I", data, offset)[0]
            end = offset + 4 + size
            if end > len(data):
                raise ValueError("bytes run past the instruction")
            return end
        width = _PRIMITIVE[kind]
        if offset + width > len(data):
            raise ValueError("value runs past the instruction")
        return offset + width
    head = kind[0]
    if head == "vec":
        count = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        if count > len(data) - offset:   # every element takes at least one byte
            raise ValueError("vector longer than the instruction")
        for _ in range(count):
            offset = _skip(data, offset, kind[1])
        return offset
    if head == "option":
        flag = data[offset]
        if flag not in (0, 1):
            raise ValueError("invalid option tag")
        return _skip(data, offset + 1, kind[1]) if flag else offset + 1
    if head == "array":
        for _ in range(kind[2]):
            offset = _skip(data, offset, kind[1])
        return offset
    name = kind[1]
    if name in UNIT_ENUMS:
        if data[offset] >= UNIT_ENUMS[name]:
            raise ValueError(f"invalid {name} value")
        return offset + 1
    if name in STRUCTS:
        for field in STRUCTS[name]:
            offset = _skip(data, offset, field)
        return offset
    return _skip_enum(data, offset, name)


def _skip_enum(data: bytes, offset: int, name: str) -> int:
    variants = _ENUMS[name]
    tag = data[offset]
    if tag >= len(variants):
        if name == "Swap":
            raise UnknownSwapVariant(tag)
        raise ValueError(f"invalid {name} variant")
    offset += 1
    for field in variants[tag][1]:
        offset = _skip(data, offset, field)
    return offset


def skip_swap(data: bytes, offset: int) -> int:
    """Return the offset after the ``Swap`` value at ``offset``.

    Raises ``UnknownSwapVariant`` for a tag past this table, ``ValueError`` for
    invalid field bytes, ``IndexError``/``struct.error`` for truncated data.
    """
    return _skip_enum(data, offset, "Swap")


def swap_name(tag: int) -> str:
    return SWAP_VARIANTS[tag][0] if 0 <= tag < len(SWAP_VARIANTS) else f"unknown({tag})"
