"""Bound raw JSON-RPC responses before JSON parsing; do not decompress them."""
import json
import time

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def read_response(response, *, deadline):
    headers = getattr(response, 'headers', {})
    encoding = headers.get('Content-Encoding', '').strip().lower()
    if encoding not in ('', 'identity'):
        raise ValueError('encoded RPC responses are not supported')
    length = headers.get('Content-Length')
    if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
        raise ValueError('RPC response exceeds byte budget or has an invalid length')
    result = bytearray()
    # HTTPResponse.read1 returns available data rather than waiting to fill an
    # entire large read from a peer that trickles bytes. Network socket timeouts
    # remain in force too; DNS/connection setup are not a hard wall-time sandbox.
    read = getattr(response, 'read1', response.read)
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError('RPC response deadline exceeded')
        data = read(min(65536, MAX_RESPONSE_BYTES + 1 - len(result)))
        if not data:
            break
        if len(data) > MAX_RESPONSE_BYTES - len(result):
            raise ValueError('RPC response exceeds byte budget')
        result.extend(data)
    if time.monotonic() >= deadline:
        raise TimeoutError('RPC response deadline exceeded')
    if length is not None and len(result) != int(length):
        raise ValueError('RPC response length mismatch')
    return json.loads(result)
