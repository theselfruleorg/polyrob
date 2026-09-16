"""Raw RPC limits apply before JSON parsing, including the broadcast transport."""
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.wallet import onchain, rpc_response, solana_onchain
from core.wallet.solana_rail import SolanaBroadcastError, SolanaRail


class Response(io.BytesIO):
    def __init__(self, body=b'{"result": 1}', headers=None):
        super().__init__(body)
        self.headers = headers or {}
        self.read_sizes = []

    def read1(self, size):
        assert size > 0
        self.read_sizes.append(size)
        return super().read(size)


def read(response):
    return rpc_response.read_response(response, deadline=float('inf'))


@pytest.mark.parametrize('encoding', ['gzip', 'br', 'deflate', 'identity, gzip'])
def test_compression_is_refused_before_read(encoding):
    response = Response(headers={'Content-Encoding': encoding})
    with pytest.raises(ValueError, match='encoded'):
        read(response)
    assert response.read_sizes == []


@pytest.mark.parametrize('length', ['-1', '12.0', ' 12', '9' * 5000])
def test_invalid_or_unbounded_content_length_never_reads(length):
    response = Response(headers={'Content-Length': length})
    with pytest.raises(ValueError):
        read(response)
    assert not response.read_sizes


def test_stream_is_bounded_before_json_parser(monkeypatch):
    monkeypatch.setattr(rpc_response, 'MAX_RESPONSE_BYTES', 16)
    parser = Mock(side_effect=AssertionError('oversized JSON must never be parsed'))
    monkeypatch.setattr(rpc_response.json, 'loads', parser)
    response = Response(b' ' * 1000)
    with pytest.raises(ValueError, match='byte budget'):
        read(response)
    assert response.tell() == 17
    assert response.read_sizes == [17]
    parser.assert_not_called()


def test_valid_exact_limit_and_declared_length(monkeypatch):
    body = b'{"result": 1}'
    monkeypatch.setattr(rpc_response, 'MAX_RESPONSE_BYTES', len(body))
    assert read(Response(body, {'Content-Length': str(len(body)), 'Content-Encoding': 'identity'})) == {'result': 1}


@pytest.mark.parametrize('length', ['1', '100'])
def test_incomplete_or_misdeclared_body_refuses(length):
    with pytest.raises(ValueError, match='length mismatch'):
        read(Response(headers={'Content-Length': length}))


def test_deadline_expires_between_incremental_reads(monkeypatch):
    ticks = iter([0, 2])
    monkeypatch.setattr(rpc_response.time, 'monotonic', lambda: next(ticks))
    response = Response()
    with pytest.raises(TimeoutError):
        rpc_response.read_response(response, deadline=1)
    assert len(response.read_sizes) == 1


@pytest.mark.parametrize('transport', ['evm', 'solana', 'solana_rail'])
def test_all_shared_rpc_transports_request_identity_close_and_refuse_compression(monkeypatch, transport):
    response = Response(headers={'Content-Encoding': 'gzip'})
    requests = []
    def open_response(req, **kwargs):
        requests.append(req)
        return response
    monkeypatch.setattr(onchain.urllib.request, 'urlopen', open_response)
    with pytest.raises((ValueError, SolanaBroadcastError)):
        if transport == 'evm':
            onchain._rpc('https://example.invalid', 'eth_chainId', [])
        elif transport == 'solana':
            solana_onchain._rpc('getSlot', [])
        else:
            SolanaRail(signer=SimpleNamespace())._rpc('getSlot', [])
    assert response.closed
    assert not response.read_sizes
    assert requests[0].get_header('Accept-encoding') == 'identity'


def test_solana_rail_uses_shared_rpc_successfully(monkeypatch):
    response = Response(json.dumps({'result': {'value': 8}}).encode())
    monkeypatch.setattr(onchain.urllib.request, 'urlopen', lambda *a, **kw: response)
    assert SolanaRail(signer=SimpleNamespace())._rpc('getBalance', ['test']) == {'value': 8}
    assert response.closed
