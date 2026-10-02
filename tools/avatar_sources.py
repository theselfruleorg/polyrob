"""Where an avatar image comes from: a URL, or the image of an NFT.

The slot itself is ``core.avatar``; this module only turns a URL or an NFT into
image bytes for it. Every network read goes through ``tools.web_fetch.safe_fetch``
(per-hop SSRF validation, IP pinning, size cap) — a URL the owner pastes is still
an outbound request from the box.

``data:`` URIs (an on-chain ``tokenURI`` returns these) are decoded locally, and
``ipfs://`` is read through one public gateway.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
from typing import Optional, Tuple

from core.avatar import MAX_BYTES, AvatarError

_DEFAULT_IPFS_GATEWAY = "https://ipfs.io/ipfs/"


def _gateway(url: str) -> str:
    if url.startswith("ipfs://"):
        gw = _DEFAULT_IPFS_GATEWAY
        rest = url[len("ipfs://"):]
        if rest.startswith("ipfs/"):
            rest = rest[len("ipfs/"):]
        return gw + rest
    return url


def _decode_data_uri(uri: str) -> bytes:
    head, _, payload = uri.partition(",")
    if not _:
        raise AvatarError("a malformed data: URI")
    if head.endswith(";base64"):
        try:
            return base64.b64decode(payload, validate=False)
        except Exception as e:
            raise AvatarError(f"the data: URI does not decode ({e})") from e
    return urllib.parse.unquote_to_bytes(payload)


async def fetch_bytes(url: str, *, max_bytes: int = MAX_BYTES) -> bytes:
    """The bytes at ``url`` (``https``/``http``/``ipfs``/``data``). Raises :class:`AvatarError`."""
    url = (url or "").strip()
    if url.startswith("data:"):
        data = _decode_data_uri(url)
        if len(data) > max_bytes:
            raise AvatarError(f"the data: URI holds {len(data)} bytes; the limit is {max_bytes}")
        return data
    url = _gateway(url)
    if not url.startswith(("https://", "http://")):
        raise AvatarError("only https, http, ipfs and data URLs can be read")
    from tools.web_fetch.fetcher import WebFetchError, safe_fetch
    try:
        res = await safe_fetch(url, max_bytes=max_bytes, timeout_sec=20.0)
    except WebFetchError as e:
        raise AvatarError(f"the fetch was refused: {e}") from e
    except Exception as e:
        raise AvatarError(f"the fetch failed: {type(e).__name__}: {e}") from e
    if res.status != 200:
        raise AvatarError(f"the server answered HTTP {res.status}")
    return res.body


async def image_from_url(url: str) -> Tuple[bytes, str]:
    """``(bytes, source)`` for an image URL."""
    data = await fetch_bytes(url)
    shown = url if not url.startswith("data:") else url[:40] + "…"
    return data, f"url:{shown}"


def _token_uri(chain: str, contract: str, token_id: int) -> str:
    from core.wallet.onchain import _rpc, rpc_url_for_chain
    from tools.defi import nft_verbs

    def _call(method, args, timeout=8.0):
        return _rpc(rpc_url_for_chain(chain), method, args, timeout)

    facts = nft_verbs.read_token_facts(_call, chain=chain, contract=contract,
                                       token_id=int(token_id))
    uri = facts.get("uri")
    if not uri:
        raise AvatarError(f"{contract} #{token_id} on {chain} returned no metadata URI "
                          f"(the node did not answer, or the token has none)")
    # ERC-1155 `{id}` substitution: 64 hex chars, lower case, no 0x.
    return uri.replace("{id}", format(int(token_id), "064x"))


async def image_from_nft(chain: str, contract: str, token_id: int) -> Tuple[bytes, str]:
    """``(bytes, source)`` for the image an NFT's metadata names (``image`` field)."""
    import asyncio
    uri = await asyncio.to_thread(_token_uri, chain, contract, token_id)
    raw = await fetch_bytes(uri, max_bytes=1024 * 1024)
    try:
        meta = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise AvatarError(f"the token metadata at {uri[:80]} is not JSON") from e
    image: Optional[str] = None
    if isinstance(meta, dict):
        image = meta.get("image") or meta.get("image_url") or meta.get("image_data")
    if not image or not isinstance(image, str):
        raise AvatarError("the token metadata names no image")
    if image.lstrip().startswith("<svg"):  # `image_data` carries raw SVG
        data = image.encode("utf-8")
    else:
        data = await fetch_bytes(image)
    return data, f"nft:{chain}:{contract.lower()}:{int(token_id)}"


def parse_nft_ref(ref: str) -> Tuple[str, str, int]:
    """``chain:contract:token_id`` -> its parts. Raises :class:`AvatarError`."""
    parts = (ref or "").strip().split(":")
    if len(parts) != 3:
        raise AvatarError("an NFT is named as chain:contract:token_id "
                          "(e.g. base:0xabc…:42)")
    chain, contract, tid = parts
    if not (contract.startswith("0x") and len(contract) == 42):
        raise AvatarError(f"{contract!r} is not a contract address")
    try:
        token_id = int(tid, 0)
    except ValueError as e:
        raise AvatarError(f"{tid!r} is not a token id") from e
    return chain.strip().lower(), contract, token_id


__all__ = ["fetch_bytes", "image_from_nft", "image_from_url", "parse_nft_ref"]
