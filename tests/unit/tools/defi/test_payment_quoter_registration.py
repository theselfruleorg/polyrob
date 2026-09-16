"""The `payment_quoter` service is actually REGISTERED.

⚠️ It was not. `core/surfaces/room_actions.py` and `modules/x402/invoicing.py`
both resolve it, `surfaces/telegram/group_ops.py` says in its own comment that
"NOTHING ever" registers it, and the consequence is that every non-stable
payable asset refuses to price — so an operator could pin a token with
`polyrob wallet asset add` and never mint an invoice in it.

These tests pin the join itself, not the quote maths (that is
`core/payments/quote.py`'s own suite).
"""
import pytest

from tools.defi.payment_quote import PaymentQuoter, register_payment_quoter


class _Container:
    def __init__(self):
        self._svc = {}

    def has_service(self, name):
        return name in self._svc

    def register_service(self, name, obj):
        self._svc[name] = obj

    def get_service(self, name):
        return self._svc[name]


def test_it_registers_a_working_quoter():
    c = _Container()
    assert register_payment_quoter(c) is True
    assert isinstance(c.get_service("payment_quoter"), PaymentQuoter)


def test_it_is_idempotent_and_never_replaces_an_existing_service():
    c = _Container()
    sentinel = object()
    c.register_service("payment_quoter", sentinel)
    assert register_payment_quoter(c) is True
    assert c.get_service("payment_quoter") is sentinel


def test_it_fails_open_rather_than_raising_into_startup():
    """⚠️ A registration error must leave the service ABSENT, which downstream
    reads as a refusal to price — never as a par-priced or free action."""
    class _Broken(_Container):
        def register_service(self, name, obj):
            raise RuntimeError("container closed")

    assert register_payment_quoter(_Broken()) is False


def test_construction_does_no_network_io(monkeypatch):
    """Safe to run unconditionally at startup: the indexer is hit per quote."""
    import tools.defi.providers.geckoterminal as gt

    def _boom(*a, **k):
        raise AssertionError("the quoter read an indexer at construction time")

    monkeypatch.setattr(gt, "_get", _boom)
    register_payment_quoter(_Container())


@pytest.mark.parametrize("module_path", [
    "tools.x402.invoice_tool",   # if it can MINT an invoice it must PRICE one
    "tools.defi.data_tool",      # the price IS a DeFi read
])
def test_the_tools_tier_entry_points_call_the_one_helper(module_path):
    """⚠️ The join must live ABOVE core. `core/` may not import `tools/`
    (tests/test_layering_ratchet.py), which is exactly why the quote arrives as
    a container service — so the registration is the tools tier's job. Both
    entry points go through the same idempotent helper, so neither can drift.
    """
    import importlib
    import inspect
    src = inspect.getsource(importlib.import_module(module_path))
    assert "register_payment_quoter" in src, (
        f"{module_path} does not register the payment quoter")


def test_core_never_imports_the_quoter_directly():
    """The seam exists because this import is forbidden; pin that it stays so."""
    import pathlib as _p
    for name in ("core/bootstrap.py", "core/initialization.py"):
        assert "payment_quote" not in _p.Path(name).read_text(), (
            f"{name} imports the tools-tier quoter — use the container service")
