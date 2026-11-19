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

def _host(url: str) -> str:
    """The host a refusal names (never the full URL: a path can be long or private)."""
    return urllib.parse.urlsplit(url).hostname or "that host"


def _gateway(url: str) -> str:
    if url.startswith("ipfs://"):
        from modules.eip8004.registration import ipfs_gateway
        gw = ipfs_gateway()
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


async def fetch_bytes(url: str, *, max_bytes: int = MAX_BYTES, what: str = "the image") -> bytes:
    """The bytes at ``url`` (``https``/``http``/``ipfs``/``data``). Raises :class:`AvatarError`.

    ``what`` names the step in a refusal ("the image", "the token metadata"), and
    the refusal names the host that answered: an NFT read makes two fetches on two
    hosts, and "HTTP 403" alone says neither which one nor who refused."""
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
        raise AvatarError(f"the fetch of {what} from {_host(url)} was refused: {e}") from e
    except Exception as e:
        raise AvatarError(f"the fetch of {what} from {_host(url)} failed: "
                          f"{type(e).__name__}: {e}") from e
    if res.status != 200:
        raise AvatarError(f"{_host(res.final_url or url)} answered HTTP {res.status} "
                          f"for {what}")
    return res.body


async def image_from_url(url: str) -> Tuple[bytes, str]:
    """``(bytes, source)`` for an image URL."""
    data = await fetch_bytes(url)
    shown = url if not url.startswith("data:") else url[:40] + "…"
    return data, f"url:{shown}"


def _token_uri(chain: str, contract: str, token_id: int) -> str:
    from core.wallet.onchain import _rpc, rpc_url_for_chain
    from tools.defi import nft_verbs

    rpc_url = rpc_url_for_chain(chain)
    faults: list = []

    def _call(method, args, timeout=8.0):
        # `read_token_facts` turns a failed call into "no answer" (never a fake
        # zero). Keep the fault, so a node that refused is NAMED and does not read
        # as a token with no metadata.
        try:
            return _rpc(rpc_url, method, args, timeout)
        except Exception as e:
            faults.append(e)
            raise

    facts = nft_verbs.read_token_facts(_call, chain=chain, contract=contract,
                                       token_id=int(token_id))
    uri = facts.get("uri")
    if not uri and faults:
        e = faults[-1]
        raise AvatarError(f"the {chain} node at {_host(rpc_url or '')} could not be read "
                          f"({type(e).__name__}: {str(e)[:160]}); the metadata URI of "
                          f"{contract} #{token_id} is unknown")
    if not uri:
        raise AvatarError(f"{contract} #{token_id} on {chain} has no metadata URI "
                          f"(the node answered, and the token names none)")
    # ERC-1155 `{id}` substitution: 64 hex chars, lower case, no 0x.
    return uri.replace("{id}", format(int(token_id), "064x"))


async def image_from_nft(chain: str, contract: str, token_id: int) -> Tuple[bytes, str]:
    """``(bytes, source)`` for the image an NFT's metadata names (``image`` field)."""
    import asyncio
    uri = await asyncio.to_thread(_token_uri, chain, contract, token_id)
    raw = await fetch_bytes(uri, max_bytes=1024 * 1024, what="the token metadata")
    try:
        meta = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise AvatarError(f"the token metadata at {uri[:80]} is not JSON") from e
    image: Optional[str] = None
    if isinstance(meta, dict):
        image = meta.get("image") or meta.get("image_url") or meta.get("image_data")
    if not image or not isinstance(image, str):
        raise AvatarError("the token metadata names no image")
    if _own_avatar_url(image):
        raise AvatarError("that token shows this instance's own avatar slot, so it cannot "
                          "be the source of it; name the image itself")
    if image.lstrip().startswith("<svg"):  # `image_data` carries raw SVG
        data = image.encode("utf-8")
    else:
        data = await fetch_bytes(image, what="the token image")
    return data, f"nft:{chain}:{contract.lower()}:{int(token_id)}"


def _own_avatar_url(url: str) -> bool:
    """True when ``url`` is THIS instance's own ``/avatar.png`` (the image its
    ERC-8004 card publishes). Such a token cannot be the slot's source: the image
    it names IS the slot."""
    import os
    base = (os.environ.get("A2A_BASE_URL") or "").strip()
    if not base:
        return False
    mine, theirs = urllib.parse.urlsplit(base), urllib.parse.urlsplit(url)
    return (bool(mine.hostname) and theirs.hostname == mine.hostname
            and theirs.path.rstrip("/") == "/avatar.png")


async def image_from_ref(ref: str, *, workspace: Optional[str] = None,
                         local_files: bool = False) -> Tuple[bytes, str]:
    """``(bytes, source)`` for any avatar reference. Raises :class:`AvatarError`.

    THE one branch every seat and the agent call (120 W1): ``nft:chain:contract:id``,
    an ``https``/``http``/``ipfs``/``data`` URL, or a FILE. A file is read only
    when the caller says where it may come from:

    - ``workspace`` — the agent: the path must resolve INSIDE that session
      workspace (``core.surfaces.attachments.validate_media_paths``, the same
      confinement every media path has);
    - ``local_files=True`` — an owner at the terminal: any file that user can read.

    A chat seat passes neither, so a file path from a chat is refused.
    """
    ref = (ref or "").strip()
    if not ref:
        raise AvatarError("name an image: a URL, nft:chain:contract:token_id, or a file")
    if ref.lower().startswith("nft:"):
        return await image_from_nft(*parse_nft_ref(ref[len("nft:"):]))
    if "://" in ref or ref.startswith("data:"):
        return await image_from_url(ref)
    from pathlib import Path
    if workspace:
        from core.surfaces.attachments import validate_media_paths
        validated, err = validate_media_paths([ref], workspace)
        if err:
            raise AvatarError(err)
        p = Path(validated[0])
        missing = f"no such file in the workspace: {ref}"
    elif local_files:
        p = Path(ref).expanduser()
        missing = f"no such file: {p}"
    else:
        raise AvatarError("a file cannot be read from here; give a URL or "
                          "nft:chain:contract:token_id")
    if not p.is_file():
        raise AvatarError(missing)
    if p.stat().st_size > MAX_BYTES:
        raise AvatarError(f"{p.name} is {p.stat().st_size} bytes; the limit is {MAX_BYTES}")
    return p.read_bytes(), f"file:{p.name}"


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
