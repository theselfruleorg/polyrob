"""Lazy context references (`@file` / `@folder` / `@diff` / `@url`) — Tier C1.

A scoped-down port of Reference's ``agent/context_references.py`` (see
docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9 C1). Users / callers can embed a
reference token in their input and have it expanded inline at prompt-assembly time,
with **soft (25%) and hard (50%) injection caps** against the model context length so
a single reference can never blow the window.

Syntax:
    @file:<path>      inline a file's contents
    @folder:<path>    inline a directory listing
    @url:<url>        inline the text of a URL (best-effort, short timeout)
    @diff             inline `git diff` for the repo root
    @diff:<path>      inline `git diff` for a path

This is opt-in: call ``preprocess_context_references`` on text that may contain refs.
Honors POLYROB's principle of failing soft — an unresolvable/oversized ref becomes an
inline note, never an exception.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Caps as a fraction of the model context length (mirrors Reference 25% soft / 50% hard).
_SOFT_INJECT_RATIO = 0.25
_HARD_INJECT_RATIO = 0.50

_REF_RE = re.compile(r"@(file|folder|url|diff)(?::([^\s]+))?")


def _is_within_root(path: str, root: str) -> bool:
    """True iff ``path`` resolves to a location inside ``root`` (no `..`/symlink escape).

    Thin alias over the shared core.path_safety.is_within_root (single source of
    truth — the coding/filesystem tools confine the same way).
    """
    from core.path_safety import is_within_root
    return is_within_root(path, root)


def _is_safe_url(url: str) -> bool:
    """Reject SSRF-prone URLs: non-http(s) schemes and private/loopback/link-local hosts.

    IP literals are range-checked directly; hostnames are best-effort resolved and any
    resolved private/loopback address blocks the URL. Unresolvable hostnames are left
    permitted (the fetch fails soft later) — this guard targets the obvious internal
    targets (127/8, 10/8, 169.254/16 metadata, ::1, localhost).
    """
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    low = host.lower()
    if low == "localhost" or low.endswith(".localhost"):
        return False

    def _blocked(ip: "ipaddress._BaseAddress") -> bool:
        return bool(
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        )

    try:
        return not _blocked(ipaddress.ip_address(host))
    except ValueError:
        pass  # not an IP literal -> hostname

    try:
        import socket

        for info in socket.getaddrinfo(host, None):
            if _blocked(ipaddress.ip_address(info[4][0])):
                return False
    except Exception:
        return True  # unresolvable -> let the fetch fail soft
    return True


def _load_file(path: str) -> Optional[str]:
	if not os.path.isfile(path):
		return None
	with open(path, "r", encoding="utf-8", errors="replace") as fh:
		return fh.read()


def _load_folder(path: str, root: Optional[str] = None) -> Optional[str]:
	if not os.path.isdir(path):
		return None
	entries = []
	for name in sorted(os.listdir(path)):
		full = os.path.join(path, name)
		# Secret guard: omit (or redact) entries that look like credential files.
		try:
			from agents.task.agent.core.secret_guard import is_secret_path as _isp
			_root = Path(root or os.getcwd())
			if not os.path.isdir(full) and _isp(Path(full), root=_root):
				entries.append(f"{name} <redacted secret>")
				continue
		except Exception:
			pass  # fail-soft: keep entry if guard raises
		entries.append(f"{name}/" if os.path.isdir(full) else name)
	return "\n".join(entries)


def _load_diff(path: Optional[str], root: Optional[str]) -> Optional[str]:
	import subprocess

	cmd = ["git", "diff"]
	if path:
		cmd.append(path)
	try:
		result = subprocess.run(
			cmd,
			cwd=root or None,
			capture_output=True,
			text=True,
			timeout=10,
		)
		return result.stdout or "(no diff)"
	except Exception as exc:
		logger.debug(f"@diff unavailable: {exc}")
		return None


def _load_url(url: str) -> Optional[str]:
	# Route through the SSRF-safe fetcher (per-hop re-validation, IP pinning, NO
	# auto-redirects, byte cap) instead of raw urlopen. urlopen FOLLOWS redirects and
	# re-resolves DNS at connect time, so the one-shot `_is_safe_url` pre-check is
	# bypassed by a 302 -> 169.254.169.254 (cloud metadata) or a DNS-rebinding host.
	# safe_fetch validates and pins EVERY hop, closing that gap. Bridge the async
	# fetcher into this sync callback via the shared background loop.
	try:
		from tools.web_fetch.fetcher import safe_fetch
		from core.async_bridge import run_coroutine_sync

		result = run_coroutine_sync(
			safe_fetch(url, max_bytes=2_000_000, timeout_sec=10.0),
			timeout=20.0,
		)
		return result.body.decode("utf-8", errors="replace")
	except Exception as exc:
		logger.debug(f"@url unavailable: {exc}")
		return None


def _load(kind: str, arg: Optional[str], root: Optional[str]) -> Optional[str]:
	if kind == "diff":
		return _load_diff(arg, root)
	if kind == "url":
		return _load_url(arg) if arg else None
	if not arg:
		return None
	path = arg
	if root and not os.path.isabs(path):
		path = os.path.join(root, path)
	if kind == "file":
		return _load_file(path)
	if kind == "folder":
		return _load_folder(path, root=root)
	return None


def preprocess_context_references(
	text: str,
	root: Optional[str] = None,
	context_length: int = 128_000,
	confine_to_root: bool = False,
	allow_filesystem: bool = True,
) -> str:
	"""Expand `@file`/`@folder`/`@diff`/`@url` references in ``text`` inline.

	Args:
		text: the input possibly containing reference tokens.
		root: base directory for relative paths / the git repo for `@diff`.
		context_length: model context window (chars proxy) used to size the caps.
		confine_to_root: when True, refuse `@file`/`@folder` paths that escape ``root``
			and `@url` targets on private/loopback hosts. Use on untrusted intake
			(CLI / A2A). Default False preserves the unconfined behaviour exactly.
		allow_filesystem: when False, refuse ALL filesystem refs (`@file`/`@folder`/
			`@diff`) outright — only `@url` expands. Use for fully remote intake (A2A)
			where there is no trusted session workspace to confine to.

	Returns:
		``text`` with each resolvable reference replaced by an enveloped block, each
		oversized/over-budget reference replaced by an inline note, and unresolvable
		references left untouched.
	"""
	if not text or "@" not in text:
		return text

	hard_cap = int(context_length * _HARD_INJECT_RATIO)
	soft_cap = int(context_length * _SOFT_INJECT_RATIO)
	injected = {"total": 0}

	def _expand(match: re.Match) -> str:
		kind = match.group(1)
		arg = match.group(2)
		# strip trailing sentence punctuation that isn't part of a path/url
		if arg:
			arg = arg.rstrip(".,);:")

		if kind in ("file", "folder", "url") and not arg:
			return match.group(0)  # nothing to resolve

		label = f"{kind}:{arg}" if arg else kind

		# Remote intake: refuse all server-side filesystem refs outright.
		if not allow_filesystem and kind in ("file", "folder", "diff"):
			return f"[{label}: filesystem references disabled; refused]"

		# Confinement guard (untrusted intake): refuse escapes / SSRF targets.
		if confine_to_root:
			if kind in ("file", "folder") and arg:
				candidate = arg if os.path.isabs(arg) else os.path.join(root or "", arg)
				if not (root and _is_within_root(candidate, root)):
					return f"[{label}: path outside allowed root; refused]"
			elif kind == "url" and arg and not _is_safe_url(arg):
				return f"[{label}: url blocked (private/loopback or non-http); refused]"

		# Secret / binary guard — runs for @file regardless of confine_to_root.
		if kind == "file" and arg:
			try:
				from agents.task.agent.core.secret_guard import (
					is_binary_file as _ibf,
					is_secret_path as _isp,
				)
				_resolved = arg if os.path.isabs(arg) else os.path.join(root or os.getcwd(), arg)
				_p = Path(_resolved)
				_r = Path(root or os.getcwd())
				if _isp(_p, root=_r):
					return f"[{label}: sensitive credential file; refused]"
				if _ibf(_p):
					try:
						size = _p.stat().st_size
						return f"[{label}: binary file, {size} bytes; not inlined]"
					except OSError:
						return f"[{label}: binary file; not inlined]"
			except Exception:
				pass  # fail-soft: fall through to normal load on guard error

		content = _load(kind, arg, root)
		if content is None:
			return match.group(0)  # unresolvable -> leave the token as-is

		if len(content) > hard_cap:
			return f"[{label}: {len(content)} chars exceeds hard inject cap ({hard_cap}); not inlined]"

		remaining = soft_cap - injected["total"]
		if len(content) > remaining:
			if remaining < 200:
				return f"[{label}: {len(content)} chars skipped (soft inject cap reached)]"
			content = content[:remaining] + "\n…[truncated at soft inject cap]"

		injected["total"] += len(content)
		return f'\n<context-ref kind="{kind}" src="{arg or ""}">\n{content}\n</context-ref>\n'

	return _REF_RE.sub(_expand, text)
