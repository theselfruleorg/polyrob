"""The client binds an approved address, without inventing host identity proof."""
import tools.x402.service as service


def test_module_documents_recipient_binding():
    assert "expected_pay_to" in service.__doc__
    assert "not to a claimed identity" in service.__doc__
