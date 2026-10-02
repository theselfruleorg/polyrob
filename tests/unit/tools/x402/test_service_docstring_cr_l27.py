"""CR-L27: the x402 pay tool must not claim a payTo binding it does not have."""
import tools.x402.service as service


def test_module_doc_does_not_claim_a_paytto_binding():
    doc = service.__doc__ or ""
    assert "payTo-binding (pays only the resource it called)" not in doc
    assert "NO payTo binding" in doc
