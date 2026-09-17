"""Declarative multi-stream seeding.

A "stream" is one standing objective plus the recurring cycle of goals that
serves it. ``scripts/seed_trading_cycle.py`` proved the pattern and hardcoded it:
one objective, three bodies, one toolset, one cadence carried by its own systemd
timer. Ten streams under that pattern means ten scripts and ten timers.

This module is the pattern with the content lifted out into a YAML manifest, so a
new stream is a manifest entry rather than a new script.

SAFETY. The manifest is the ONE place an autonomous goal may be granted a money
verb such as ``defi_trade`` — ``allowed_self_goal_tools()`` never contains one, so
the planner and the agent cannot self-grant. That stays safe only because the
manifest is operator-authored, git-tracked and deployed with the code. This module
therefore writes ``payload.tools`` VERBATIM: no inference, no widening, no
filtering. ``load_manifest`` refuses a path under a session workspace so the agent
can never aim the seeder at a file it wrote itself.
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Statuses that mean "this stream still has work in flight". Includes
#: "blocked": blocked is NOT a terminal status in POLYROB — a
#: ``provider_outage`` block self-heals (auto-requeued blocked -> ready on a
#: short window, see ``GoalBoard.age_out_blocked``), and any other block kind
#: eventually ages out to ``cancelled``. A stalled leg must therefore keep
#: holding the stream's ceiling: if it dropped out of "live" the moment it
#: blocked, a lapsed cadence window would green-light a second, overlapping
#: seed of the same stream while the first cycle's blocked leg is still
#: sitting there — two concurrent copies of whatever money verb the stream
#: grants (e.g. ``defi_trade``) is exactly what this throttle exists to
#: prevent. The conservative failure is a paused stream, not a double-spend.
#: Recovery is automatic ONLY for ``provider_outage`` (requeued in ~30 minutes).
#: Every other block kind waits for ``age_out_blocked``, and for a CHAINED cycle
#: that is NOT ``GOAL_BLOCKED_MAX_AGE_DAYS``: cancelling leg 1 cascades leg 2 to
#: ``blocked`` with a FRESH ``completed_at``, so an N-leg chain thaws only after
#: roughly ``GOAL_BLOCKED_MAX_AGE_DAYS x (N - 1)`` — ~28 days for the shipped
#: 3-leg cycle. A stuck money stream is therefore an owner action, not a wait:
#: ``goal_unblock`` EACH blocked leg (unblocking only the head leaves the rest
#: blocked behind it).
from core.goal_vocab import LIVE_STATUSES  # noqa: E402 — the ONE spelling

#: The only objective status under which a stream seeds. A literal, not an
#: import, because `board` imports are lazy everywhere else in this module;
#: `tests/unit/agents/task/goals/test_streams_pause.py` pins it to
#: `board.OBJ_ACTIVE` so the two cannot drift apart.
_OBJ_ACTIVE = "active"

#: Default hours between two seeds of one stream, when the manifest omits it.
DEFAULT_CADENCE_HOURS = 4

#: Slack, in seconds, that :func:`stream_is_due` grants a cadence window. The
#: seeder runs from an HOURLY systemd timer with ``RandomizedDelaySec=300``, so
#: a tick can land seconds BEFORE ``last_seed + cadence`` — a strict comparison
#: then skips it ("4.0h of 4.0h") and the stream waits a whole extra hour. On
#: prod a 4 h cadence ran at 5 h in 7 of 9 measured gaps (2026-08-29). The
#: tolerance is the timer jitter plus a margin, and never more than a quarter of
#: the cadence, so a short cadence cannot be hollowed out by it.
CADENCE_TOLERANCE_SEC = 15 * 60

#: How long a stream may sit "due" before that counts as the seeder having
#: MISSED it: the timer is hourly (+ up to 5 min random delay), so anything past
#: this is a stall, not the normal wait for the next tick.
SEEDER_GRACE_SEC = 75 * 60

#: Path fragments that mark an agent-writable session tree. A manifest there is
#: refused: it is the difference between an operator grant and a self-grant.
_FORBIDDEN_FRAGMENTS = (os.sep + "sessions" + os.sep, os.sep + "workspace" + os.sep)


def shipped_manifest_path() -> str:
    """The manifest that SHIPS with the code: ``<install>/data/streams/streams.yaml``.

    Lives under ``data/`` rather than ``config/`` because ``scripts/deploy_prod.sh``
    syncs ``data/prompts`` and ``data/characters`` as bundled content and does NOT
    sync ``config/`` at all — a manifest under ``config/`` would never reach the box.

    This is the DEFAULT a fresh install starts from, not the file that is read at
    runtime. See :func:`default_manifest_path`.
    """
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    return os.path.join(here, "data", "streams", "streams.yaml")


def home_manifest_path() -> Optional[str]:
    """``<data_home>/streams/streams.yaml`` — the OWNER-editable copy, or ``None``
    when the data home cannot be resolved. Does not check that it exists.

    Never binds the data home at import time (``tests/test_home_binding_ratchet.py``).
    """
    try:
        from core.runtime_paths import resolve_data_home
        return str(Path(resolve_data_home()) / "streams" / "streams.yaml")
    except Exception:
        logger.debug("stream manifest: data home unresolvable (fail-open)", exc_info=True)
        return None


def default_manifest_path() -> str:
    """The manifest actually read at runtime. **Pure — never writes.**

    034 §12.3. This used to resolve inside the install tree, and
    ``scripts/deploy_prod.sh`` rsyncs ``data/streams`` from ``git archive HEAD`` —
    so an owner edit made on the box was silently reverted by the next deploy.
    That makes any "edit the manifest from chat" verb a lie that takes hours to
    notice, which is why the owner's seat moves the file rather than the file
    moving the owner.

    Resolution order:

    1. ``POLYROB_STREAMS_MANIFEST`` — an explicit operator override always wins.
    2. ``<data_home>/streams/streams.yaml`` **if it exists** — a deploy cannot
       reach it, so an owner edit survives.
    3. The shipped copy — byte-identical to the pre-034 behaviour.

    Purity is load-bearing: the first cut seeded from inside this function, and
    three existing tests that call it without isolation immediately wrote into the
    developer's real data home. :func:`ensure_manifest_seeded` is the one writer.

    The ``streams/`` path segment is preserved deliberately: it is what
    ``secret_guard.is_protected_config_path`` matches on, so the manifest stays
    unreachable from every AGENT-writable file surface at its new location. The
    guarantee that protects is "never a SELF-grant", not "never editable" — an
    owner editing it from an authenticated seat IS the operator grant.
    """
    env = os.environ.get("POLYROB_STREAMS_MANIFEST")
    if env:
        return env
    home = home_manifest_path()
    if home and os.path.isfile(home):
        return home
    return shipped_manifest_path()


def ensure_manifest_seeded() -> str:
    """Seed the owner-editable copy from the shipped one ONCE; return the effective
    path. The only writer.

    Idempotent by existence, never by content: a later call — a reboot, an hourly
    seeder tick — must never clobber an owner edit, which is the entire point of
    moving the file out of the deploy's reach. Fail-open: an unresolvable or
    unwritable data home returns the shipped path, i.e. pre-034 behaviour, rather
    than raising.

    Called from the hourly stream seeder, so the data-home copy appears on the
    first tick of a fresh install and :func:`default_manifest_path` prefers it
    from then on.
    """
    env = os.environ.get("POLYROB_STREAMS_MANIFEST")
    if env:
        return env
    shipped = shipped_manifest_path()
    home = home_manifest_path()
    if not home:
        return shipped
    if os.path.isfile(home):
        return home
    if not os.path.isfile(shipped):
        return shipped  # nothing to seed from; keep legacy behaviour
    try:
        Path(home).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shipped, home)
    except Exception:
        logger.warning("stream manifest: could not seed the data-home copy, using the "
                       "shipped one (an owner edit there will NOT survive a deploy)",
                       exc_info=True)
        return shipped
    logger.info("stream manifest seeded into the data home: %s", home)
    return home


def load_manifest(path: str) -> List[Dict[str, Any]]:
    """Parse and validate the manifest. Raises ``ValueError`` on any bad entry.

    Validation is strict and up-front on purpose: a silently-skipped stream is a
    stream that goes dark for days before anyone notices.
    """
    # realpath (not abspath): abspath only normalizes a relative path, it does
    # NOT resolve symlinks, while open() below DOES follow them. A symlink
    # whose own path names neither "sessions" nor "workspace" but that POINTS
    # AT a file inside one would sail past an abspath-only check and then get
    # read anyway — a guard that looks like it works but doesn't is worse than
    # no guard. realpath also covers a relative path that resolves into a
    # session workspace, which abspath alone would likewise miss.
    resolved = os.path.realpath(path)
    for frag in _FORBIDDEN_FRAGMENTS:
        if frag in resolved + os.sep:
            raise ValueError(
                f"refusing a stream manifest inside a session workspace: {resolved}. "
                "The manifest grants money tools and must be operator-authored.")
    import yaml
    with open(resolved, "r", encoding="utf-8") as fh:
        try:
            doc = yaml.safe_load(fh) or {}
        except yaml.YAMLError as e:
            # Re-raised as ValueError (not left as yaml.YAMLError) so every
            # caller — the runner's `except (OSError, ValueError)`, or any
            # future caller — gets ONE exception vocabulary for "this manifest
            # is unusable", and YAML stays an implementation detail of this
            # module rather than leaking into callers' except clauses.
            raise ValueError(f"{resolved}: invalid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"{resolved}: top level must be a mapping")
    raw = doc.get("streams")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{resolved}: 'streams' must be a non-empty list")
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict):
            raise ValueError(f"{resolved}: stream #{i} is not a mapping")
        sid = str(s.get("id") or "").strip()
        if not sid:
            raise ValueError(f"{resolved}: stream #{i} has no 'id'")
        if sid in seen:
            raise ValueError(f"{resolved}: duplicate stream id {sid!r}")
        seen.add(sid)
        obj = s.get("objective")
        if not isinstance(obj, dict) or not str(obj.get("title") or "").strip():
            raise ValueError(f"{resolved}: stream {sid!r} needs objective.title")
        goals = s.get("goals")
        if not isinstance(goals, list) or not goals:
            raise ValueError(f"{resolved}: stream {sid!r} needs a non-empty 'goals' list")
        for j, g in enumerate(goals):
            if not isinstance(g, dict):
                raise ValueError(f"{resolved}: {sid} goal #{j} is not a mapping")
            for key in ("title", "body"):
                if not str(g.get(key) or "").strip():
                    raise ValueError(f"{resolved}: {sid} goal #{j} has no {key!r}")
            tools = g.get("tools")
            if not isinstance(tools, list) or not tools:
                raise ValueError(f"{resolved}: {sid} goal #{j} needs a non-empty 'tools'")
        out.append(s)
    return out


def _payload_of(row) -> Dict[str, Any]:
    import json
    raw = getattr(row, "payload", None) or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def stream_live_goals(board, user_id: str, stream_id: str) -> int:
    """Goals of this stream still in flight (see :data:`LIVE_STATUSES`).

    Reads ``board.stream_goals`` — a tag-filtered, tenant-scoped SQL query — NOT
    a bounded ``board.list`` window. A window ordered ``priority DESC`` evicts a
    manifest stream's own legs first (they are deliberately below the board
    default priority), which would invert this throttle from "wait" into "seed a
    second cycle". ``stream_goals`` also counts the LEGACY ``payload.cycle`` tag,
    so an armed old per-stream seeder and this one cannot be blind to each other
    during a cutover.
    """
    return sum(1 for row in board.stream_goals(user_id, stream_id)
               if getattr(row, "status", None) in LIVE_STATUSES)


def stream_objective_status(board, user_id: str, stream: Dict[str, Any]) -> Optional[str]:
    """This stream's objective status, or ``None`` when it has no objective yet.

    Identity matches :func:`ensure_objective` exactly — ``payload.stream_id`` first,
    then an exact title — so the "may I seed?" throttle and the "which objective is
    mine?" lookup can never disagree about which row a stream owns. Reads
    ``board.objectives`` (kind-filtered, tenant-scoped, unbounded), never a
    ``board.list`` window. Fail-open: an unreadable board returns ``None`` and the
    other throttles still apply.
    """
    sid = str(stream["id"])
    title = str((stream.get("objective") or {}).get("title") or "").strip()
    try:
        rows = board.objectives(user_id=user_id) or []
    except Exception:
        logger.debug("stream objective status read failed (fail-open)", exc_info=True)
        return None
    for r in rows:
        if _payload_of(r).get("stream_id") == sid:
            return getattr(r, "status", None)
    if title:
        for r in rows:
            if (getattr(r, "title", "") or "").strip() == title:
                return getattr(r, "status", None)
    return None


def stream_last_seeded_at(board, user_id: str, stream_id: str) -> Optional[float]:
    """Newest ``created_at`` among this stream's goals; ``None`` if never seeded.

    Derived from the board rather than a side table, so the cadence cannot drift
    out of step with what was actually written — and via the same tag-filtered
    query ``stream_live_goals`` uses, so neither throttle can be silently
    hollowed out by unrelated rows crowding a scan window.
    """
    newest: Optional[float] = None
    for row in board.stream_goals(user_id, stream_id):
        t = getattr(row, "created_at", None)
        if t is not None and (newest is None or float(t) > newest):
            newest = float(t)
    return newest


def declared_objective_payload(stream: Dict[str, Any]) -> Dict[str, Any]:
    """The objective payload keys this manifest entry DECLARES.

    One place builds them, so create and adopt cannot drift apart — the bug that
    made adoption a silent downgrade: it stamped ``stream_id`` alone, so a legacy
    objective adopted on prod kept the deployment-default budget and no success
    criteria, permanently, and a later manifest edit to either field was a no-op.
    """
    payload: Dict[str, Any] = {"stream_id": str(stream["id"])}
    spec = stream["objective"]
    # B26 (S9, 2026-08-29): the manifest prose version this row last followed.
    # ⚠️ Correct ONLY on CREATE, where the row is written FROM this prose. On an
    # existing row the caller must go through `_prose_stamp_for_sync` instead —
    # see the freeze bug documented there.
    payload["manifest_prose_hash"] = _prose_hash(spec.get("title"), spec.get("body"))
    if spec.get("success_criteria"):
        payload["success_criteria"] = str(spec["success_criteria"])
    if spec.get("goal_budget") is not None:
        payload["goal_budget"] = int(spec["goal_budget"])
    return payload


def _prose_hash(title, body) -> str:
    import hashlib
    raw = f"{str(title or '').strip()}\n{str(body or '')}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def _sync_prose(board, row, spec: Dict[str, Any]) -> bool:
    """B26 (S9, 2026-08-29): re-apply the manifest's title/body to an existing
    objective ONLY when the row still carries the prose of the manifest version it
    last followed (``payload.manifest_prose_hash``). An owner edit in the CLI/chat
    makes the row's prose diverge from that stamp, and from then on the manifest
    never overwrites it — the owner's words win. A row with no stamp (seeded before
    this existed) is never rewritten; it gets stamped by the declared-payload sync
    and is tracked from the next run. True when the prose was re-synced."""
    stored = _payload_of(row).get("manifest_prose_hash")
    if not stored:
        return False
    new_hash = _prose_hash(spec.get("title"), spec.get("body"))
    if new_hash == stored:
        return False
    row_hash = _prose_hash(getattr(row, "title", ""), getattr(row, "body", ""))
    if row_hash != stored:
        return False  # owner-edited prose — leave it alone
    return bool(board.update_fields(
        row.id, title=str(spec["title"]).strip(), body=str(spec.get("body") or "")))


def _declared_for_existing_row(row, spec: Dict[str, Any], declared: Dict[str, Any],
                               *, adopting: bool = False) -> Dict[str, Any]:
    """*declared*, with the prose stamp corrected for an EXISTING row.

    The stamp means "the manifest version whose prose this row currently holds",
    and `_sync_prose` reads it to tell a manifest edit (apply it) from an owner
    edit (never overwrite it). That only works while the stamp and the prose
    move together.

    They did not. `declared_objective_payload` always carried the CURRENT
    manifest's hash and `_sync_declared_payload` wrote it on every tick — even
    on the adopt-by-title path, which never calls `_sync_prose` at all. So a
    legacy objective adopted by title kept its old body under the new manifest's
    stamp; from the next tick `row_hash != stored` read as "the owner edited
    this", and the manifest could never reach the row again. Nobody had edited
    anything.

    Reproduced by the tests, not by an incident: prod's live streams were
    checked on 2026-09-10 and were consistent. The exposure is any objective
    that enters through adoption rather than creation, which is the documented
    migration path for a stream whose objective predates its manifest entry.

    Two rules, and the difference between them is the whole fix:

    * **Ordinary tick** — advance the stamp only when the row's prose IS the
      manifest's prose. Otherwise drop the key so ``merge_payload`` preserves
      what was there, which keeps a genuine owner edit frozen against the
      manifest, permanently and by design.
    * **Adoption, or a row with no stamp at all** — stamp with the row's OWN
      prose. Both are the moment a row comes under manifest control, so it is
      tracked from what it currently holds; the next tick then reads
      ``row_hash == stored``, recognises a manifest edit rather than an owner
      one, and syncs. Deliberately NOT a rewrite in the same breath: taking
      ownership and replacing the owner's words at once is the failure this
      stamp exists to prevent. Stamping from the MANIFEST here instead is what
      froze prod — it claims the row followed prose it never held.
    """
    row_hash = _prose_hash(getattr(row, "title", ""), getattr(row, "body", ""))
    if adopting or not _payload_of(row).get("manifest_prose_hash"):
        return {**declared, "manifest_prose_hash": row_hash}
    if row_hash == _prose_hash(spec.get("title"), spec.get("body")):
        return declared
    out = dict(declared)
    out.pop("manifest_prose_hash", None)
    return out


def _sync_declared_payload(board, row, declared: Dict[str, Any]) -> bool:
    """Re-apply *declared* onto an existing objective row. True if it changed.

    Applied on EVERY run, not once at adoption: the manifest is documented as the
    declarative source for its streams, so editing ``success_criteria`` or
    ``goal_budget`` there must actually take effect on the next tick rather than
    being frozen at whatever the row happened to hold when it was first adopted.
    Scoped to the keys the manifest DECLARES — ``merge_payload`` preserves every
    other key, so an operator's out-of-band payload additions survive, and a key
    the manifest omits is never blanked. Priority is deliberately NOT re-applied;
    title/body follow the manifest through :func:`_sync_prose`, which yields to an
    owner edit (identity is ``stream_id``, never the title).
    """
    current = _payload_of(row)
    if all(current.get(k) == v for k, v in declared.items()):
        return False
    board.merge_payload(row.id, declared)
    return True


def ensure_objective(board, user_id: str, stream: Dict[str, Any]) -> Tuple[str, str]:
    """Return ``(objective_id, note)``, creating or adopting as needed.

    Identity is ``payload.stream_id``, NOT the title. Title matching is what makes
    the legacy seeder fragile: reword the mission and it collides with the version
    it replaces. An exact-title match is accepted ONCE, to adopt an objective that
    predates the manifest; the adoption stamps the id so the next run matches
    cleanly.

    Reads ``board.objectives`` (kind-filtered, tenant-scoped, unbounded) rather
    than a ``board.list`` window: an objective declared at manifest priority 1
    sits below the board default of 5 and is among the FIRST rows a
    ``priority DESC`` window evicts, so a crowded board made this function stop
    seeing its own objective and mint a duplicate on every hourly run — it passes
    ``force=True``, so the near-duplicate guard never caught it.
    """
    sid = str(stream["id"])
    spec = stream["objective"]
    title = str(spec["title"]).strip()
    declared = declared_objective_payload(stream)

    rows = board.objectives(user_id=user_id, status="active")
    # 031: an objective the owner PAUSED (/goal objective pause) is honoured, not
    # re-created — a second active twin would quietly restart the stream.
    for r in board.objectives(user_id=user_id, status="paused"):
        if (_payload_of(r).get("stream_id") == sid
                or (getattr(r, "title", "") or "").strip() == title):
            return r.id, f"objective is paused by the owner, not re-created: {r.id}"
    for r in rows:
        if _payload_of(r).get("stream_id") == sid:
            prose = _sync_prose(board, r, spec)
            if prose:
                # _sync_prose wrote through the board, so the in-memory row is
                # stale — and it is exactly the prose the stamp decision reads.
                r = board.get(r.id) or r
            fields = _sync_declared_payload(
                board, r, _declared_for_existing_row(r, spec, declared))
            if prose:
                return r.id, f"objective already active, manifest prose re-synced: {r.id}"
            if fields:
                return r.id, f"objective already active, manifest fields re-applied: {r.id}"
            return r.id, f"objective already active: {r.id}"
    for r in rows:
        if (getattr(r, "title", "") or "").strip() == title:
            _sync_declared_payload(
                board, r, _declared_for_existing_row(r, spec, declared, adopting=True))
            return r.id, f"objective adopted by title and stamped from the manifest: {r.id}"

    payload: Dict[str, Any] = dict(declared)
    obj = board.create_objective(
        user_id=user_id, title=title, body=str(spec.get("body") or ""),
        priority=int(spec.get("priority") or 5), payload=payload,
        # force=True: the near-duplicate guard also matches RETIRED objectives, so
        # a reworded mission collides with the version it replaces and the board is
        # left with no active objective — which silently stops the planner. The
        # operator naming this stream in the manifest IS the disambiguation.
        force=True)
    return obj.id, f"objective created: {obj.id}"


#: Marks an ask this module files about its OWN health, so the "is the seeder
#: failing?" ask can be matched EXACTLY (by stream id) rather than by the fuzzy
#: title similarity ``create_ask`` uses by default — two different streams
#: failing must produce two different asks, not one fuzzily-merged one.
SEED_FAILURE_ASK_KIND = "stream_seed_failure"


def _open_failure_ask(board, user_id: str, stream_id: str):
    """This tenant's OPEN seeder-failure ask for *stream_id*, or ``None``."""
    try:
        rows = board.asks(user_id=user_id, status="open") or []
    except Exception:
        return None
    for a in rows:
        p = _payload_of(a)
        if p.get("kind") == SEED_FAILURE_ASK_KIND and p.get("stream") == stream_id:
            return a
    return None


def record_stream_failure(board, user_id: str, stream_id: str, error: Any) -> None:
    """File (or refresh) a durable owner-facing ask about a failing stream.

    The seeder runs from an hourly systemd timer, so every failure mode it has —
    a manifest that did not deploy, a board error, a stream that raises mid-cycle
    — otherwise surfaces ONLY in ``journalctl``, which nothing pushes to the
    owner. A money stream can therefore go dark indefinitely with no signal. This
    reuses the SAME rail a blocked goal already uses
    (``GoalDispatcher._maybe_escalate_blocked`` -> ``board.create_ask``): durable,
    tenant-scoped, and visible in ``polyrob owner pending`` / ``owner asks``.

    Deliberately exact-deduped rather than fuzzy: one ask per failing stream, its
    ``failures`` counter and ``last_error`` refreshed on every subsequent tick, so
    an hourly failure is one standing ask that shows how long it has been failing
    rather than 24 asks a day. Fail-open — an alerting problem must never be the
    reason the seeder stops seeding the OTHER streams.
    """
    what = f"Stream seeding is failing: {stream_id}"
    why = f"{type(error).__name__}: {error}"[:1000]
    try:
        existing = _open_failure_ask(board, user_id, stream_id)
        if existing is not None:
            payload = _payload_of(existing)
            board.merge_payload(existing.id, {
                "failures": int(payload.get("failures") or 1) + 1,
                "last_error": why,
                "last_failed_at": time.time(),
            })
            return
        board.create_ask(
            user_id=user_id, what=what, why=why,
            # force=True: create_ask's default dedup is fuzzy TITLE similarity, and
            # "Stream seeding is failing: <a>" scores well over the threshold against
            # "... <b>" — two dead streams would collapse into one ask naming only the
            # first. The exact per-stream match above IS the dedup.
            force=True,
            extra_payload={"kind": SEED_FAILURE_ASK_KIND, "stream": stream_id,
                           "failures": 1, "last_error": why,
                           "last_failed_at": time.time()})
    except Exception:
        pass


def clear_stream_failure(board, user_id: str, stream_id: str) -> None:
    """Close a standing seeder-failure ask once the stream runs clean again.

    A transient failure must not leave a permanent ask the owner has to dismiss
    by hand. Closed as ``obsolete`` (the need went away), never as
    ``fulfilled`` — nobody decided anything. Fail-open.
    """
    try:
        existing = _open_failure_ask(board, user_id, stream_id)
        if existing is not None:
            board.obsolete_ask(existing.id, user_id=user_id,
                               reason="stream_seeded_cleanly")
    except Exception:
        pass


def stream_is_due(board, user_id: str, stream: Dict[str, Any],
                  now: Optional[float] = None) -> Tuple[bool, str]:
    """``(due, reason)`` — may this stream be seeded right now?

    Two independent throttles, both required. A stream with work in flight is never
    re-seeded (that is what stops a slow run piling up duplicates), and a stream
    seeded inside its cadence window is never re-seeded (that is what stops a fast
    timer outrunning the board).
    """
    now = time.time() if now is None else now
    sid = str(stream["id"])
    # 031 owner pause: the `streams` scope (or everything). ONE predicate,
    # fail-closed — an unreadable record never resolves to "seed anyway".
    from core.autonomy_control import allows
    _dec = allows("seed_stream")
    if not _dec.allowed:
        return False, f"stream seeding {_dec.reason}"
    # 034 §11.4: an objective the owner switched OFF stops its stream. This was the
    # gap that made `/goal objective pause` a no-op: `ensure_objective` refused to
    # create a DUPLICATE objective but returned the paused row's id, and
    # `scripts/seed_streams.py` seeded under it because nothing here read the
    # objective's status. The owner's only per-stream off switch did nothing.
    _obj_status = stream_objective_status(board, user_id, stream)
    if _obj_status is not None and _obj_status != _OBJ_ACTIVE:
        return False, f"objective is {_obj_status} by the owner (/goal objective activate to re-arm)"
    # `is None`, not `or`: an explicit `max_live_goals: 0` means the operator
    # wants the stream disabled (0 ever live), which is a real, meaningful
    # value — `or len(...)` would treat it as falsy/"unset" and silently
    # revert to the default ceiling instead of honoring the 0.
    raw_ceiling = stream.get("max_live_goals")
    ceiling = int(raw_ceiling) if raw_ceiling is not None else len(stream["goals"])
    live = stream_live_goals(board, user_id, sid)
    # max(0, ...), not max(1, ...): a floor of 1 would silently re-widen an
    # intentional ceiling of 0 back to 1, defeating the "0 disables" contract
    # above. 0 still guards a nonsensical negative manifest value.
    if live >= max(0, ceiling):
        return False, f"{live} goal(s) live (ceiling {ceiling})"
    last = stream_last_seeded_at(board, user_id, sid)
    cadence_h = float(stream.get("cadence_hours") or DEFAULT_CADENCE_HOURS)
    if last is not None and (now - last) < _effective_window_sec(cadence_h):
        waited_h = (now - last) / 3600.0
        return False, f"inside the cadence window ({waited_h:.1f}h of {cadence_h}h)"
    return True, "due"


def _effective_window_sec(cadence_h: float) -> float:
    """The cadence window minus the timer-jitter tolerance (see
    :data:`CADENCE_TOLERANCE_SEC`), never shorter than 3/4 of the cadence."""
    window = cadence_h * 3600.0
    return window - min(float(CADENCE_TOLERANCE_SEC), window / 4.0)


def stream_overdue_by(board, user_id: str, stream: Dict[str, Any],
                      now: Optional[float] = None) -> float:
    """Seconds this stream has been due WITHOUT being re-seeded; ``0.0`` when it
    is busy (held by live goals), inside its window, or never seeded. More than
    :data:`SEEDER_GRACE_SEC` means the hourly seeder missed it — a real stall."""
    now = time.time() if now is None else now
    sid = str(stream["id"])
    raw_ceiling = stream.get("max_live_goals")
    ceiling = int(raw_ceiling) if raw_ceiling is not None else len(stream["goals"])
    if stream_live_goals(board, user_id, sid) >= max(0, ceiling):
        return 0.0
    last = stream_last_seeded_at(board, user_id, sid)
    if last is None:
        return 0.0
    cadence_h = float(stream.get("cadence_hours") or DEFAULT_CADENCE_HOURS)
    return max(0.0, now - (last + _effective_window_sec(cadence_h)))


def next_seed_at(board, user_id: str, streams: List[Dict[str, Any]],
                 now: Optional[float] = None) -> Optional[float]:
    """When the earliest IDLE stream's cadence window reopens, or ``None``.

    An idle stream is one with no live goals whose last seed is still inside its
    window — the board looks empty only because the cycle finished and the next
    one is not due yet. ``None`` means there is nothing to wait for: no stream
    has ever been seeded, a stream is due right now (the next tick will refill
    the board), or every stream is busy (held by live goals). The dispatcher's
    empty-pipeline escalation uses this to tell "idle between cycles" from a real
    stall; it must never turn a seedable board into a wait, so a due stream
    short-circuits to ``None``.
    """
    now = time.time() if now is None else now
    earliest: Optional[float] = None
    for stream in streams:
        sid = str(stream["id"])
        raw_ceiling = stream.get("max_live_goals")
        ceiling = int(raw_ceiling) if raw_ceiling is not None else len(stream["goals"])
        if stream_live_goals(board, user_id, sid) >= max(0, ceiling):
            continue  # busy, not idle
        last = stream_last_seeded_at(board, user_id, sid)
        if last is None:
            continue  # never seeded: nothing to wait for
        cadence_h = float(stream.get("cadence_hours") or DEFAULT_CADENCE_HOURS)
        reopens = last + _effective_window_sec(cadence_h)
        if reopens <= now:
            return None  # due now — the next tick refills the board
        if earliest is None or reopens < earliest:
            earliest = reopens
    return earliest


def seed_stream(board, user_id: str, stream: Dict[str, Any],
                objective_id: str) -> List[Any]:
    """Write this stream's cycle, chained head to tail. Returns the created goals.

    ``depends_on`` chains the legs because each consumes what the previous writes:
    seeded flat, ``GOAL_MAX_CONCURRENT`` dispatches leg 1 and leg 2 together and
    leg 2 reads an empty artifact. ``force=True`` on every create is deliberate — a
    recurring cycle reuses its titles by design, and the near-duplicate guard also
    matches COMPLETED rows, so the second cycle scores 1.00 against the first and
    every later top-up would be silently refused. ``stream_is_due`` is the correct,
    stricter throttle.

    ALL-OR-NOTHING. ``force=True`` skips the dedup guard, not every guard a create
    can fail on, so a refusal can still land MID-cycle. A half-written trading
    cycle is the worst outcome available: leg 1 (research) exists and holds a live
    slot against ``max_live_goals``, while leg 2 — the leg carrying ``defi_trade``
    — never exists, so the stream reads as busy and does nothing. On any failure
    every goal written by THIS call is cancelled and the error re-raised, leaving
    the stream in the state the next tick can retry cleanly from.
    """
    sid = str(stream["id"])
    created: List[Any] = []
    previous_id: Optional[str] = None
    try:
        for spec in stream["goals"]:
            payload = {
                # Verbatim operator grant — see the module docstring. Never inferred,
                # never filtered.
                "tools": list(spec["tools"]),
                "max_steps": int(spec.get("max_steps") or 30),
                "stream": sid,
            }
            if spec.get("acceptance"):
                payload["acceptance"] = str(spec["acceptance"])
            goal = board.create(
                user_id=user_id, title=str(spec["title"]), body=str(spec["body"]),
                priority=int(spec.get("priority") or 5), parent_id=objective_id,
                payload=payload, force=True,
                depends_on=[previous_id] if previous_id else None)
            previous_id = goal.id
            created.append(goal)
    except BaseException:
        # Roll back newest-first so a leg is never cancelled while a later leg
        # still depends on it. Cancellation is best-effort: a rollback that
        # itself fails must not replace the real error with its own.
        for g in reversed(created):
            try:
                board.cancel(g.id, user_id=user_id)
            except Exception:
                pass
        raise
    return created
