"""The Solana pinned-USDC bridge origin: exactly the declared USDC leaves,
nothing else does, and SOL beyond fee + kept rent is charged, never waved."""
from types import SimpleNamespace

from tools.defi import bridge_token_origin as bto

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _sol(tool, sol):
    return sol * 150.0


class _Tool:
    def _price(self, chain, addr):
        return 1.0 if addr == USDC else 150.0


def _deltas(**over):
    d = dict(token_deltas={USDC: -400_000}, native_excess=0, fee_lamports=5000)
    d.update(over)
    return SimpleNamespace(**d)


def test_the_exact_outflow_is_valued_at_the_usdc_price():
    usd, why, _ = bto.svm_token_origin_value(_Tool(), _deltas(), USDC, 400_000, 6, price_sol=_sol)
    assert why is None and usd == 0.4


def test_more_or_less_usdc_than_declared_refuses():
    for moved in (-400_001, -399_999):
        _, why, _ = bto.svm_token_origin_value(
            _Tool(), _deltas(token_deltas={USDC: moved}), USDC, 400_000, 6, price_sol=_sol)
        assert "not the declared" in why


def test_another_token_leaving_refuses():
    _, why, _ = bto.svm_token_origin_value(
        _Tool(), _deltas(token_deltas={USDC: -400_000, "OtherMint": -1}), USDC, 400_000, 6,
        price_sol=_sol)
    assert "undeclared token outflow" in why


def test_an_unknown_fee_refuses_and_excess_sol_is_charged():
    _, why, _ = bto.svm_token_origin_value(_Tool(), _deltas(native_excess=None), USDC,
                                           400_000, 6, price_sol=_sol)
    assert "fee could not be computed" in why
    usd, why, _ = bto.svm_token_origin_value(
        _Tool(), _deltas(native_excess=10_000_000), USDC, 400_000, 6, price_sol=_sol)
    assert why is None and abs(usd - (0.4 + 1.5)) < 1e-6


def test_an_unmeasured_outflow_refuses():
    _, why, _ = bto.svm_token_origin_value(_Tool(), _deltas(token_deltas={}), USDC, 400_000, 6,
                                           price_sol=_sol)
    assert "did not measure" in why
