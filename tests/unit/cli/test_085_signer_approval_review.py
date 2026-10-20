"""085 W8 (signer L-4): `owner promote signer_approval <id>` shows the signer's
own review of the request and asks for `y` before it grants."""
from click.testing import CliRunner

from cli.commands.owner import owner
from core.instance import DEFAULT_INSTANCE_ID


class _Client:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def call(self, op, body=None, *, timeout=None):
        self.calls.append((op, body))
        if op == "approvals.list":
            return {"pending": self.rows}
        if op == "approvals.decide":
            return {"id": body["id"], "state": "granted", "amount_usd": 50.0}
        raise AssertionError(op)


ROW = {"id": "sig-abc123", "op": "x402.authorize", "chain": "base", "amount_usd": 50.0,
       "summary": '{"decoded_payee": {"recipient": "0xcccc"}}', "created": 0, "expires": 1}


def _env(tmp_path, monkeypatch, rows):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", DEFAULT_INSTANCE_ID)
    client = _Client(rows)
    import core.signer.client as sc
    monkeypatch.setattr(sc, "SignerClient", lambda *a, **k: client)
    return client


def _decided(client):
    return [b for op, b in client.calls if op == "approvals.decide"]


def test_promote_prints_the_review_and_grants_on_y(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, [ROW])
    res = CliRunner().invoke(owner, ["promote", "signer_approval", "sig-abc123"], input="y\n")
    assert "0xcccc" in res.output and "x402.authorize" in res.output
    assert "$50.00" in res.output
    assert _decided(client) == [{"id": "sig-abc123", "grant": True}]
    assert res.exit_code == 0, res.output


def test_promote_without_y_grants_nothing(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, [ROW])
    for answer in ("n\n", "\n", ""):
        res = CliRunner().invoke(owner, ["promote", "signer_approval", "sig-abc123"],
                                 input=answer)
        assert res.exit_code != 0
        assert "NOT granted" in res.output
    assert _decided(client) == []


def test_promote_of_an_unknown_id_grants_nothing(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, [ROW])
    res = CliRunner().invoke(owner, ["promote", "signer_approval", "sig-other"], input="y\n")
    assert res.exit_code != 0
    assert "no pending signer approval" in res.output
    assert _decided(client) == []


def test_pending_items_name_the_real_op():
    from core.signer.approvals import pending_items
    items = pending_items(client=_Client([ROW]))
    assert items[0]["tool"] == "signer.x402_authorize"


def test_promote_all_never_grants_a_signer_approval(tmp_path, monkeypatch):
    client = _env(tmp_path, monkeypatch, [ROW])
    monkeypatch.setenv("WALLET_SIGNER", "shadow")
    res = CliRunner().invoke(owner, ["promote", "all"])
    assert _decided(client) == []
    assert "owner promote signer_approval sig-abc123" in res.output, res.output
