"""Checksum-aware detection of unlabelled wallet secrets.

The English wordlist and checksum format are BIP-39 (MIT),
https://github.com/bitcoin/bips/blob/master/bip-0039.mediawiki.
The shipped wordlist attribution is in THIRD-PARTY-NOTICES.md.
"""
import functools
import hashlib
import json
import re
from collections import deque
from pathlib import Path

_WORDS = re.compile(r"[A-Za-z]+")
#: Between two phrase words: whitespace, commas, quotes, and list numbering
#: (``1. abandon 2. ability``, ``1) …``) so a numbered phrase is still a phrase.
_SEPARATOR = re.compile(r"(?:[\s,'\"]|\d{1,2}[.):])+")
_KEYPAIR = re.compile(r"\[(?:\s*\d{1,3}\s*,){63}\s*\d{1,3}\s*\]")


@functools.lru_cache(maxsize=1)
def _word_indices():
    words = Path(__file__).with_name("bip39_english.txt").read_text(encoding="ascii").splitlines()
    if len(words) != 2048 or len(set(words)) != 2048:
        raise RuntimeError("BIP-39 credential scrubber wordlist is incomplete")
    return {word: i for i, word in enumerate(words)}


def _valid_mnemonic(indices):
    combined = 0
    for value in indices:
        combined = (combined << 11) | value
    checksum_bits = len(indices) // 3
    entropy_bits = len(indices) * 11 - checksum_bits
    entropy = (combined >> checksum_bits).to_bytes(entropy_bits // 8, "big")
    expected = hashlib.sha256(entropy).digest()[0] >> (8 - checksum_bits)
    return combined & ((1 << checksum_bits) - 1) == expected


def scrub_wallet_material(text: str, redacted: str) -> str:
    """Keep random byte arrays and prose; redact validated key material."""
    def keypair(match):
        values = json.loads(match.group())
        if any(value > 255 for value in values):
            return match.group()
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        raw = bytes(values)
        public = Ed25519PrivateKey.from_private_bytes(raw[:32]).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw)
        return redacted if public == raw[32:] else match.group()

    text = _KEYPAIR.sub(keypair, text)
    words = _word_indices()
    recent = deque(maxlen=24)
    intervals = []
    previous_end = None
    for token in _WORDS.finditer(text):
        value = words.get(token.group().lower())
        if value is None:
            recent.clear()
            previous_end = None
            continue
        if previous_end is not None and not _SEPARATOR.fullmatch(text[previous_end:token.start()]):
            recent.clear()
        recent.append((token.start(), value))
        previous_end = token.end()
        window = list(recent)
        for size in (24, 21, 18, 15, 12):
            if len(window) < size or not _valid_mnemonic([v for _, v in window[-size:]]):
                continue
            start, end = window[-size][0], token.end()
            while intervals and start <= intervals[-1][1]:
                start = min(start, intervals.pop()[0])
            intervals.append((start, end))
            break
    if not intervals:
        return text
    out, end = [], 0
    for start, stop in intervals:
        out.extend((text[end:start], redacted))
        end = stop
    out.append(text[end:])
    return "".join(out)
