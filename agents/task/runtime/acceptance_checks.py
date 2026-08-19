"""§4.4 typed acceptance checks — optional sharpener, never a gate.

Framework-executed probes a producer MAY attach to a goal
(``payload.acceptance_checks``): the operator (``seed_goal.py --check``), the
eval harness, or the agent authoring checks for its own goals. When present
they run **fail-CLOSED** (a failed or crashing check fails the run) and their
results join the evidence pack. **Nothing rejects a goal without them** — the
create-gate was explicitly dropped (owner direction, proposal §4.4).

Core ships only the use-case-agnostic types (``artifact_glob``, ``http_ok``,
``file_contains``); instance verticals (invoice rows, wallet deltas, tweets)
register their own via :func:`register_check_type` — the way tools are
registered, OUTSIDE core.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_CHECKS = 10
DEFAULT_TIMEOUT_SEC = 10.0
# Bounded read for file_contains — no shared read-cap constant exists nearby
# (proposal 016), so cap at 1 MB; an oversized file fails the check, never crashes.
FILE_CONTAINS_MAX_BYTES = 1024 * 1024

# check fn: async (check: dict, ctx: dict) -> (ok: bool, detail: str)
CheckFn = Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Tuple[bool, str]]]


def _http_status(url: str, timeout: float) -> int:
    """Blocking GET returning the HTTP status (module-level for test injection)."""
    import urllib.request
    req = urllib.request.Request(url, method="GET",
                                 headers={"User-Agent": "polyrob-acceptance-check"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - operator/agent
        return int(getattr(resp, "status", 0) or resp.getcode())


async def _check_artifact_glob(check: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[bool, str]:
    pattern = str(check.get("pattern") or check.get("arg") or "").strip()
    if not pattern:
        return False, "artifact_glob: no pattern"
    base = str(check.get("workspace_dir") or ctx.get("workspace_dir") or "")
    if not base or not os.path.isdir(base):
        return False, f"artifact_glob: workspace dir unavailable ({base or 'unset'})"
    matches = glob.glob(os.path.join(base, "**", pattern), recursive=True)
    if matches:
        return True, f"artifact_glob: {len(matches)} match(es), e.g. {os.path.relpath(matches[0], base)}"
    return False, f"artifact_glob: no file matching {pattern!r} under workspace"


async def _check_artifact(check: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[bool, str]:
    """{"type":"artifact","name":"report.md"|"id":"<artifact_id>","contains":[...]}

    Resolves through the artifact ledger instead of the filesystem layout, which
    is what ``file_contains`` gets wrong. Five goals failed last week with
    ``file_contains: file not found ('data/x402-round4-decode.md')`` for evidence
    the agent had really written: the relative path was resolved against a shared
    workspace that had since been wiped, so a check meant to VERIFY the work
    reported the work was never done.

    The ledger separates the three states that path check conflates:
      * no row            -> the agent never produced it
      * row, file gone    -> produced, then deleted (missing)
      * row, hash differs -> produced, then altered (changed)

    ``contains`` still applies, on the ledger's recorded absolute path.
    """
    user_id = str(ctx.get("user_id") or "")
    if not user_id:
        return False, "artifact: no tenant in context"
    artifact_id = str(check.get("id") or "").strip()
    name = str(check.get("name") or check.get("arg") or "").strip()
    if not (artifact_id or name):
        return False, "artifact: neither 'id' nor 'name' given"

    from core.artifacts import (VERIFY_CHANGED, VERIFY_MISSING, VERIFY_OK,
                                get_artifact_ledger)
    ledger = get_artifact_ledger()

    row = None
    if artifact_id:
        row = ledger.get(artifact_id, user_id)
        if row is None:
            return False, f"artifact: no artifact {artifact_id!r} for this tenant"
    else:
        goal_id = str(ctx.get("goal_id") or "")
        candidates = ledger.list_for_goal(user_id, goal_id) if goal_id else []
        if not candidates:
            session_id = str(ctx.get("session_id") or "")
            candidates = (ledger.list_for_session(user_id, session_id)
                          if session_id else [])
        for cand in candidates:
            if os.path.basename(cand.path) == name:
                row = cand
                break
        if row is None:
            return False, f"artifact: no artifact named {name!r} was produced by this run"

    verdict = ledger.verify(row.id, user_id)
    if verdict == VERIFY_MISSING:
        return False, f"artifact: {name or row.id} was produced but is now missing from disk"
    if verdict == VERIFY_CHANGED:
        return False, f"artifact: {name or row.id} changed since it was recorded"
    if verdict != VERIFY_OK:
        return False, f"artifact: {name or row.id} unknown to the ledger"

    needles = [str(x) for x in (check.get("contains") or []) if str(x)]
    if not needles:
        return True, f"artifact: {name or row.id} ok ({row.bytes} bytes)"
    try:
        with open(row.path, "r", encoding="utf-8", errors="replace") as f:
            body = f.read(FILE_CONTAINS_MAX_BYTES)
    except OSError as e:
        return False, f"artifact: unreadable ({str(e)[:100]})"
    mode = str(check.get("mode") or "all").lower()
    hits = [n for n in needles if n in body]
    ok = (len(hits) == len(needles)) if mode != "any" else bool(hits)
    return ok, (f"artifact: {name or row.id} contains {len(hits)}/{len(needles)} "
                f"(mode={mode})")


async def _check_http_ok(check: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[bool, str]:
    url = str(check.get("url") or check.get("arg") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False, f"http_ok: not an http(s) url ({url[:80]!r})"
    timeout = float(ctx.get("timeout_sec") or DEFAULT_TIMEOUT_SEC)
    try:
        status = await asyncio.to_thread(_http_status, url, timeout)
    except Exception as e:
        return False, f"http_ok: request failed ({str(e)[:120]})"
    ok = 200 <= status < 400
    return ok, f"http_ok: {url[:120]} -> {status}"


async def _check_file_contains(check: Dict[str, Any], ctx: Dict[str, Any]) -> Tuple[bool, str]:
    """{"type":"file_contains","path":"...","contains":["A","B"],"mode":"all"|"any"}

    Workspace-relative path resolution mirrors ``artifact_glob`` (joined under
    the check/ctx ``workspace_dir``); bounded read; substring match. Missing
    file / missing substring / oversized file → ok=False with a clear detail.
    """
    path = str(check.get("path") or check.get("arg") or "").strip()
    if not path:
        return False, "file_contains: no path"
    contains = check.get("contains")
    if isinstance(contains, str):
        contains = [contains]
    needles = [str(s) for s in (contains or []) if str(s)]
    if not needles:
        return False, "file_contains: no substrings given ('contains' empty)"
    base = str(check.get("workspace_dir") or ctx.get("workspace_dir") or "")
    if not base or not os.path.isdir(base):
        return False, f"file_contains: workspace dir unavailable ({base or 'unset'})"
    full = os.path.join(base, path)  # same workspace-relative semantics as artifact_glob
    if not os.path.isfile(full):
        return False, f"file_contains: file not found ({path!r} under workspace)"
    try:
        size = os.path.getsize(full)
    except OSError as e:
        return False, f"file_contains: cannot stat {path!r} ({str(e)[:80]})"
    if size > FILE_CONTAINS_MAX_BYTES:
        return False, (f"file_contains: file too large ({size} bytes > "
                       f"{FILE_CONTAINS_MAX_BYTES} byte cap)")

    def _read() -> str:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read(FILE_CONTAINS_MAX_BYTES)

    try:
        text = await asyncio.to_thread(_read)
    except OSError as e:
        return False, f"file_contains: read failed ({str(e)[:80]})"
    mode = str(check.get("mode") or "all").strip().lower()
    missing = [s for s in needles if s not in text]
    found = len(needles) - len(missing)
    # "any" = at least one present; anything else uses the stricter default "all"
    ok = found > 0 if mode == "any" else not missing
    if ok:
        return True, (f"file_contains: {path} contains {found}/{len(needles)} "
                      f"substring(s) (mode={'any' if mode == 'any' else 'all'})")
    return False, (f"file_contains: {path} missing substring(s): "
                   + ", ".join(repr(s[:40]) for s in missing[:5]))


_CHECK_TYPES: Dict[str, CheckFn] = {
    "artifact_glob": _check_artifact_glob,
    "http_ok": _check_http_ok,
    "file_contains": _check_file_contains,
    # Prefer `artifact` over `file_contains` for a goal's OWN output: it resolves
    # through the ledger, so a wipe or a relative-path mismatch cannot masquerade
    # as "the agent never produced it".
    "artifact": _check_artifact,
}


def register_check_type(name: str, fn: CheckFn) -> None:
    """Instance-extension seam: register a check type OUTSIDE core (invoice
    rows, wallet deltas, …) the way tools are registered."""
    _CHECK_TYPES[str(name)] = fn


async def run_acceptance_checks(checks: List[Dict[str, Any]], *,
                                workspace_dir: Optional[str] = None,
                                timeout_sec: float = DEFAULT_TIMEOUT_SEC,
                                user_id: Optional[str] = None,
                                goal_id: Optional[str] = None,
                                session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Execute the typed checks; each result is ``{type, ok, detail, ...}``.

    Fail-CLOSED per check (unknown type / crash / timeout → ok=False) but the
    RUNNER never raises — the caller reads the results.
    """
    results: List[Dict[str, Any]] = []
    ctx = {"workspace_dir": workspace_dir, "timeout_sec": timeout_sec,
           "user_id": user_id, "goal_id": goal_id, "session_id": session_id}
    for check in list(checks or [])[:MAX_CHECKS]:
        if not isinstance(check, dict):
            results.append({"type": "?", "ok": False, "detail": "malformed check (not a dict)"})
            continue
        ctype = str(check.get("type") or "").strip()
        fn = _CHECK_TYPES.get(ctype)
        if fn is None:
            results.append({"type": ctype or "?", "ok": False,
                            "detail": f"unknown check type {ctype!r} (fail-closed)"})
            continue
        try:
            ok, detail = await asyncio.wait_for(fn(check, ctx), timeout=timeout_sec * 2)
        except Exception as e:
            ok, detail = False, f"check crashed (fail-closed): {str(e)[:160]}"
        results.append({**{k: v for k, v in check.items() if k != "type"},
                        "type": ctype, "ok": bool(ok), "detail": str(detail)[:300]})
    return results


def failed_checks(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in (results or []) if not r.get("ok")]
