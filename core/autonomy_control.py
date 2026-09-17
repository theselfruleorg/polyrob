"""031 — the ONE runtime pause record every autonomous starter consults.

Capability ("what may this deployment do") stays in env flags. THIS is runtime
state ("is autonomous work allowed right now"): a JSON record at the data home,
written atomically to every base the runtime probes, read fail-closed by every
process (the telegram daemon, the email daemon, the webview, the API, the
stream-seeder timer, the on-box scripts).

Legacy sentinels (the ``AUTONOMY_HALT`` / ``TREASURY_ENTRY_PAUSE`` /
``STREAM_SEEDING_PAUSE`` files or env flags) are read as FACETS of this record
so a ``touch`` still works; nothing writes them any more, and ``resume`` clears
the files (an env facet needs an env edit + restart — the result says so).

core-tier: stdlib + core.sqlite_util + core.runtime_paths + core.env + core.event_log.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from core.env import bool_env

logger = logging.getLogger(__name__)

PAUSE_FILENAME = "AUTONOMY_PAUSE.json"
LEGACY_HALT_FILENAME = "AUTONOMY_HALT"
LEGACY_ENTRY_FILENAME = "TREASURY_ENTRY_PAUSE"
LEGACY_STREAM_FILENAME = "STREAM_SEEDING_PAUSE"

#: The ONE legacy sentinel table: file/env name -> the scope it stands for.
#: Every legacy read, resume-clear and env-name lookup derives from this — a new
#: facet is one row here, not three edits scattered through the module.
LEGACY_FACETS: Tuple[Tuple[str, str], ...] = (
    (LEGACY_HALT_FILENAME, "all"),
    (LEGACY_ENTRY_FILENAME, "trading"),
    (LEGACY_STREAM_FILENAME, "streams"),
)

SCOPES: Tuple[str, ...] = ("all", "trading", "streams", "planner", "cron",
                           "social", "oversight", "pings", "apps")

#: activity kind -> the scopes whose pause denies it ("all" always denies).
KIND_SCOPES: Dict[str, Tuple[str, ...]] = {
    "dispatch": ("all",),
    "plan": ("all", "planner"),
    "escalate": ("all", "pings"),
    "seed_stream": ("all", "streams"),
    "cron_run": ("all", "cron"),
    "digest": ("all",),
    "self_wake": ("all",),
    "resume_session": ("all",),
    "curator": ("all",),
    "background_review": ("all",),
    "settlement_scan": ("all",),
    "lifecycle_ping": ("all", "pings"),
    "trade_entry": ("all", "trading"),
    "trade_exit": ("all",),
    "spend": ("all",),
    "social_post": ("all", "social"),
    "oversight_seed": ("all", "oversight"),
    "oversight_dial": ("all", "oversight"),
    "oversight_deploy": ("all", "oversight"),
    "oversight_alert": ("all", "oversight"),
    # 032: the durable app service — the agent verb and the supervisor tick.
    "app_deploy": ("all", "apps"),
    "app_serve": ("all", "apps"),
    # 043 A8/A42: the ownership-keyed sandbox container sweep — the one
    # always-on autonomy_runtime loop that had neither a flag nor a pause kind.
    "sandbox_reap": ("all",),
    # 046: minting a PAID room-action offer. Riding `social` puts it under
    # `/pause social` with the rest of the room's outbound life.
    #
    # ⚠️ A row is REQUIRED, not optional: `allows()` denies an UNKNOWN kind
    # under EVERY scope, so without it a `trading` pause would also stop a room
    # action — a refusal the owner never asked for.
    #
    # ⚠️ There is deliberately NO kind for APPLYING one. We already hold the
    # payer's money at that point, and stranding the effect behind a pause is a
    # silent default on an obligation — the same reasoning that leaves the 031
    # cold-start requeue ungated.
    "room_action_offer": ("all", "social"),
}

#: 043 A8/A42: kinds declared above with no caller anywhere in the tree yet —
#: a plain-word must not be offered for a scope whose kinds are all dormant.
#: `tests/test_autonomy_control_ratchet.py::test_every_kind_has_a_caller_or_is_dormant`
#: keeps this set bidirectionally honest: a kind gains a caller, it comes out
#: of here the same day; a kind loses its last caller, it goes back in.
DORMANT_KINDS = frozenset({
    "trade_exit", "oversight_seed", "oversight_dial", "oversight_deploy",
    "oversight_alert",
})


@dataclass(frozen=True)
class PauseState:
    paused: bool
    scopes: Tuple[str, ...]
    since: Optional[float]
    until: Optional[float]
    set_by: str
    via: str
    reason: str
    source: str  # "record" | "legacy" | "env" | "unreadable" | "none"
    #: where each scope comes from: the JSON record, a touched legacy file, the env.
    #: ``scopes`` is their union; a resume can clear the first two, never the env.
    record_scopes: Tuple[str, ...] = ()
    file_scopes: Tuple[str, ...] = ()
    env_scopes: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        """The one JSON/dict shape every seat renders from (status snapshot,
        webview, CLI/REPL snapshot, telegram)."""
        return {"paused": self.paused, "scopes": list(self.scopes), "since": self.since,
                "until": self.until, "set_by": self.set_by, "via": self.via,
                "reason": self.reason, "source": self.source,
                "env_scopes": list(self.env_scopes)}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    state: PauseState


@dataclass
class PauseResult:
    state: PauseState
    written: List[str]
    removed: List[str]
    effective: bool
    #: optional one-line note a seat may attach (e.g. "held 2 in-flight run(s)")
    held: Optional[str] = None
    #: why a write was refused/ineffective, for the renderer (never silent)
    note: str = ""


_WARNED_PATHS: set = set()


_NOT_PAUSED = PauseState(False, (), None, None, "", "", "", "none")


def unreadable_state(reason: str) -> PauseState:
    """The fail-CLOSED state a seat renders when it cannot read the record at
    all (a probe that raised before ``read_state`` could): everything paused,
    with the reason. One shape, so no seat invents its own."""
    return PauseState(True, ("all",), None, None, "", "", reason, f"unreadable ({reason})",
                      record_scopes=("all",))
_TRANSITION_HOOKS: List[Callable[[PauseState, PauseState], None]] = []


def _now() -> float:
    return time.time()


def _resolved_home() -> Optional[str]:
    """The resolved data home, as ONE substitutable seam.

    Every base ``state_bases`` adds beyond the caller's own ``data_dir`` funnels
    through here, so this is the single place a test rig can redirect. That
    matters: ``state_bases`` ALWAYS appends the resolved home even when an
    explicit ``data_dir`` was passed, so a test calling ``pause()`` with a tmp
    dir would otherwise still write ``AUTONOMY_PAUSE.json`` into the developer's
    real ``cwd/.polyrob``. ``tests/conftest.py::_isolate_autonomy_pause_record``
    substitutes this function (mirroring ``_isolate_wallet_audit_sink``);
    ``core.surfaces.owner_admin`` binds ``state_bases`` itself at import, so
    patching that name is NOT a usable isolation point.
    """
    try:
        from core.runtime_paths import resolve_data_home
        return str(resolve_data_home())
    except Exception:
        logger.debug("autonomy_control: data-home resolution failed", exc_info=True)
        return None


def state_bases(data_dir: Optional[str]) -> List[str]:
    """Every directory the runtime probes for the record (the caller's home first).

    Mirrors what the legacy halt file already used: ``POLYROB_DATA_DIR``,
    ``DATA_ROOT`` and the resolved data home — a record written to only one of
    them is a pause the runtime may never see.
    """
    bases: List[str] = []
    for base in (data_dir, os.getenv("POLYROB_DATA_DIR"), os.getenv("DATA_ROOT")):
        if base and str(base) not in bases:
            bases.append(str(base))
    home = _resolved_home()
    if home and home not in bases:
        bases.append(home)
    return bases


def _record_dict(scopes, since: float, until: Optional[float], set_by: str, via: str,
                 reason: str) -> Dict[str, object]:
    """The ONE on-disk record shape. Both writers (``pause`` and the scoped-resume
    rewrite in ``resume``) build it here, so a field change is one edit."""
    return {"version": 1, "paused": True, "scopes": list(scopes), "since": since,
            "until": until, "set_by": set_by, "via": via, "reason": (reason or "")[:500]}


def _record_path(base: str) -> str:
    return os.path.join(base, PAUSE_FILENAME)


def _parse(raw: str) -> PauseState:
    rec = json.loads(raw)
    scopes = tuple(s for s in rec.get("scopes", []) if s in SCOPES) or ("all",)
    return PauseState(
        paused=bool(rec.get("paused", True)), scopes=scopes,
        since=rec.get("since"), until=rec.get("until"),
        set_by=str(rec.get("set_by") or ""), via=str(rec.get("via") or ""),
        reason=str(rec.get("reason") or ""), source="record")


def _legacy_scopes(base: str) -> List[str]:
    return [scope for name, scope in LEGACY_FACETS
            if os.path.exists(os.path.join(base, name))]


def _legacy_since(bases: List[str]) -> Optional[float]:
    """The OLDEST touch-file mtime across the bases — when a `touch`ed pause
    began. Without it every seat renders "since ?" for an owner pause set with
    `touch <data>/AUTONOMY_HALT`, and the violation detector has no window."""
    stamps = []
    for base in bases:
        for name, _scope in LEGACY_FACETS:
            path = os.path.join(base, name)
            try:
                if os.path.exists(path):
                    stamps.append(os.path.getmtime(path))
            except OSError:
                continue
    return min(stamps) if stamps else None


def _env_scopes() -> List[str]:
    return [scope for name, scope in LEGACY_FACETS if bool_env(name, False)]


def legacy_names_for(scopes) -> List[str]:
    """The legacy file/env NAMES that stand for *scopes* — for owner-facing text
    ("set in the ENVIRONMENT (AUTONOMY_HALT)") and for the resume-clear list."""
    return [name for name, scope in LEGACY_FACETS if scope in scopes]


def read_state(data_dir: Optional[str] = None) -> PauseState:
    """The live pause state. Fail CLOSED: an unreadable record is a pause.

    A read never creates anything — an expired record is the one exception
    (it is removed, so expiry is honoured by every process, not just the writer).
    """
    try:
        env_scopes = tuple(_env_scopes())
        file_scopes: List[str] = []
        newest: Optional[PauseState] = None
        bases = state_bases(data_dir)
        for base in bases:
            # legacy touch files FIRST — an expired record in the same base must
            # never hide them for a read
            file_scopes.extend(_legacy_scopes(base))
            path = _record_path(base)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    st = _parse(fh.read())
            except Exception:
                if path not in _WARNED_PATHS:
                    _WARNED_PATHS.add(path)
                    logger.warning("autonomy_control: unreadable %s — treating as PAUSED", path)
                return PauseState(True, ("all",), None, None, "", "", "unreadable record",
                                  "unreadable", record_scopes=("all",), env_scopes=env_scopes)
            _WARNED_PATHS.discard(path)
            if not st.paused:
                continue  # nothing writes paused:false; never let it mask another base
            if st.until is not None and st.until <= _now():
                _remove_quiet(path)
                _emit("autonomy_resumed", {"reason": "expired", "scopes": list(st.scopes)})
                continue
            if newest is None or (st.since or 0) > (newest.since or 0):
                newest = st
        file_t = tuple(dict.fromkeys(file_scopes))
        facets = list(file_t) + list(env_scopes)
        if newest is not None:
            merged = tuple(dict.fromkeys(list(newest.scopes) + facets))
            if "all" in merged:
                merged = ("all",)
            return PauseState(True, merged, newest.since, newest.until, newest.set_by,
                              newest.via, newest.reason, "record",
                              record_scopes=newest.scopes, file_scopes=file_t,
                              env_scopes=env_scopes)
        if facets:
            scopes = ("all",) if "all" in facets else tuple(dict.fromkeys(facets))
            return PauseState(True, scopes, _legacy_since(bases) if file_t else None, None,
                              "legacy", "file/env", "",
                              "env" if env_scopes else "legacy",
                              file_scopes=file_t, env_scopes=env_scopes)
        return _NOT_PAUSED
    except Exception:
        logger.warning("autonomy_control: state probe raised — treating as PAUSED", exc_info=True)
        return PauseState(True, ("all",), None, None, "", "", "probe error", "unreadable",
                          record_scopes=("all",))


def allows(kind: str, data_dir: Optional[str] = None) -> Decision:
    """May *kind* start right now? Unknown kinds are denied while any pause is on."""
    st = read_state(data_dir)
    if not st.paused:
        return Decision(True, "running", st)
    denying = KIND_SCOPES.get(kind, SCOPES)
    hit = [s for s in st.scopes if s in denying]
    if hit:
        who = f"{st.set_by} via {st.via}" if st.set_by else st.source
        return Decision(False, f"paused ({', '.join(hit)}) by {who}", st)
    return Decision(True, f"paused scopes {list(st.scopes)} do not cover {kind}", st)


def _atomic_write(path: str, obj: dict) -> None:
    """Unique temp name + fsync + os.replace: two seats writing at once can never
    truncate each other's temp file or publish a partial record."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=PAUSE_FILENAME + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _remove_quiet(tmp)
        raise


class _RecordLock:
    """Cross-process lock around a read-merge-write of the record (advisory
    flock on ``<resolved home>/AUTONOMY_PAUSE.lock``; every writer shares that
    base). Fail-open: a lock error never blocks a pause."""

    def __init__(self, data_dir: Optional[str]):
        bases = state_bases(data_dir)
        self._path = os.path.join(bases[-1] if bases else ".", PAUSE_FILENAME + ".lock")
        self._fh = None

    def __enter__(self):
        try:
            import fcntl
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            self._fh = open(self._path, "a+")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            logger.debug("autonomy_control: record lock unavailable (proceeding)", exc_info=True)
            self._fh = None
        return self

    def __exit__(self, *exc):
        if self._fh is not None:
            try:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            self._fh.close()
        return False


def _remove_quiet(path: str) -> bool:
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except OSError:
        logger.warning("autonomy_control: could not remove %s", path, exc_info=True)
    return False


def _emit(kind: str, attrs: dict) -> None:
    from core.event_log import emit
    emit(kind, source="autonomy_control", attrs=attrs)


def _audit(data_dir: Optional[str], action: str, st: PauseState) -> None:
    try:
        from core.runtime_paths import sidecar_db_path
        from core.sqlite_util import execute_retry
        bases = state_bases(data_dir)
        db = (os.path.join(bases[0], "autonomy_state.db") if bases
              else str(sidecar_db_path("autonomy_state.db")))
        execute_retry(db, """CREATE TABLE IF NOT EXISTS control_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, action TEXT NOT NULL,
            scopes TEXT NOT NULL, set_by TEXT, via TEXT, reason TEXT, until_ts REAL)""")
        execute_retry(db, "INSERT INTO control_events (ts, action, scopes, set_by, via, reason, until_ts) "
                          "VALUES (?,?,?,?,?,?,?)",
                      (_now(), action, json.dumps(list(st.scopes)), st.set_by, st.via,
                       st.reason, st.until))
    except Exception:
        logger.debug("autonomy_control: audit row failed (non-fatal)", exc_info=True)


def _fire(old: PauseState, new: PauseState) -> None:
    for fn in list(_TRANSITION_HOOKS):
        try:
            fn(old, new)
        except Exception:
            logger.warning("autonomy_control: transition hook failed", exc_info=True)


def register_transition_hook(fn: Callable[[PauseState, PauseState], None]) -> None:
    """Process-local: called synchronously after every state change (old, new)."""
    if fn not in _TRANSITION_HOOKS:
        _TRANSITION_HOOKS.append(fn)


def unregister_transition_hook(fn: Callable[[PauseState, PauseState], None]) -> None:
    try:
        _TRANSITION_HOOKS.remove(fn)
    except ValueError:
        pass


def pause(data_dir: Optional[str] = None, *, scopes: Tuple[str, ...] = ("all",),
          duration_minutes: Optional[int] = None, set_by: str = "owner",
          via: str = "cli", reason: str = "") -> PauseResult:
    """Write the record to every base. A scoped pause MERGES into an existing
    record; ``all`` replaces it. ``effective`` is the READ-BACK state.

    A19: an unknown scope REFUSES (``ValueError``) instead of being silently
    dropped — every real caller validates through
    ``core.surfaces.owner_intent.parse_pause_args`` first, so this is a
    backstop, not the primary gate. An EMPTY *scopes* still defaults to
    ``("all",)``.
    """
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope(s) {bad!r} (one of {', '.join(SCOPES)})")
    requested = tuple(dict.fromkeys(scopes)) or ("all",)
    with _RecordLock(data_dir):
        old = read_state(data_dir)
        scopes = requested
        now = _now()
        until = now + duration_minutes * 60 if duration_minutes and duration_minutes > 0 else None
        if "all" in scopes:
            scopes = ("all",)
        elif old.record_scopes:
            # Merge into the existing RECORD (facets stay facets); the wider/longer
            # pause wins — "pause cron for 90m" on top of an indefinite "pause
            # streams" must not turn the streams pause into a 90-minute one.
            scopes = tuple(dict.fromkeys(list(old.record_scopes) + list(scopes)))
            if "all" in scopes:
                scopes = ("all",)
            until = None if (old.until is None or until is None) else max(old.until, until)
        # A repeated "stop" on an unchanged record keeps the ORIGINAL `since`: the
        # violation detector's window must not shrink because the owner said it twice.
        since = now
        if old.record_scopes and set(old.record_scopes) == set(scopes) and old.since:
            since = old.since
        rec = _record_dict(scopes, since, until, set_by, via, reason)
        written: List[str] = []
        for base in state_bases(data_dir):
            try:
                os.makedirs(base, exist_ok=True)
                _atomic_write(_record_path(base), rec)
                written.append(_record_path(base))
            except OSError:
                logger.warning("autonomy_control: could not write record under %s", base,
                               exc_info=True)
        new = read_state(data_dir)
    # effective = the READ-BACK state covers what was asked (an `all` facet covers everything)
    effective = new.paused and ("all" in new.scopes or all(s in new.scopes for s in requested))
    _audit(data_dir, "pause", new)
    _emit("autonomy_paused", {"scopes": list(scopes), "set_by": set_by, "via": via,
                              "until": until, "reason": (reason or "")[:200]})
    _fire(old, new)
    return PauseResult(state=new, written=written, removed=[], effective=effective)


def resume(data_dir: Optional[str] = None, *, scopes: Optional[Tuple[str, ...]] = None,
           set_by: str = "owner", via: str = "cli") -> PauseResult:
    """Clear the record (``scopes=None``) or drop the given scopes from it.

    Also clears the matching legacy sentinel FILES so a ``touch``ed halt can be
    lifted from chat; an env facet cannot (``effective`` is False and
    ``state.env_scopes`` names it). A SCOPED resume never narrows a FULL pause —
    "resume trading" while everything is paused is refused (``effective=False``,
    ``note`` says why); only a full ``resume`` lifts an ``all`` pause.

    A19: an unknown scope REFUSES (``ValueError``) instead of being silently
    dropped — see :func:`pause`. ``scopes=None`` (or empty) still means
    "resume everything".
    """
    bad = [s for s in (scopes or ()) if s not in SCOPES]
    if bad:
        raise ValueError(f"unknown scope(s) {bad!r} (one of {', '.join(SCOPES)})")
    requested = tuple(dict.fromkeys(scopes or ())) or None
    if requested and "all" in requested:
        requested = None
    with _RecordLock(data_dir):
        old = read_state(data_dir)
        removed: List[str] = []
        written: List[str] = []
        if requested and old.paused and "all" in old.scopes:
            _audit(data_dir, "resume_refused", old)
            return PauseResult(state=old, written=[], removed=[], effective=False,
                               note="everything is paused, and a scoped resume does not narrow "
                                    "a full pause — resume with no scope to lift it all")
        keep: Tuple[str, ...] = ()
        if requested and old.record_scopes:
            keep = tuple(s for s in old.record_scopes if s not in requested)
        # a full resume clears every facet file; a scoped one only its own
        legacy_names = (legacy_names_for(requested) if requested
                        else [name for name, _ in LEGACY_FACETS])
        for base in state_bases(data_dir):
            if keep:
                rec = _record_dict(keep, old.since or _now(), old.until,
                                   old.set_by or set_by, old.via or via, old.reason)
                try:
                    os.makedirs(base, exist_ok=True)
                    _atomic_write(_record_path(base), rec)
                    written.append(_record_path(base))
                except OSError:
                    logger.warning("autonomy_control: could not rewrite record under %s", base,
                                   exc_info=True)
            elif _remove_quiet(_record_path(base)):
                removed.append(_record_path(base))
            for name in legacy_names:
                p = os.path.join(base, name)
                if _remove_quiet(p):
                    removed.append(p)
        new = read_state(data_dir)
    _audit(data_dir, "resume", new)
    _emit("autonomy_resumed", {"scopes": list(requested or ("all",)), "set_by": set_by, "via": via,
                               "still_paused": new.paused})
    _fire(old, new)
    if requested:
        effective = not any(s in new.scopes for s in requested)
    else:
        effective = not new.paused
    note = ""
    if not effective:
        stuck = [s for s in new.scopes if s in new.env_scopes]
        if stuck:
            note = (f"still paused: {', '.join(new.scopes)}; set in the ENVIRONMENT "
                    f"({', '.join(legacy_names_for(stuck))}) — needs an env-file edit + restart")
        else:
            note = (f"still paused: {', '.join(new.scopes) or 'unknown'}; the record could not "
                    f"be cleared (see the log)")
    return PauseResult(state=new, written=written, removed=removed, effective=effective, note=note)


#: The half of a pause refusal that is TRUE regardless of what the record says:
#: it is not a separate lever, it binds the owner's own typed command, here is the
#: remedy, and nothing happened. Kept beside the record so no caller can ship a
#: refusal that carries only some of these (census, 2026-09-12).
_PAUSE_REFUSAL_TAIL = (
    " This is the autonomy pause, NOT a separate kill-switch, and it binds YOUR"
    " OWN seat too: typing the command yourself hits this same check, because it"
    " runs before any seat distinction. Lift it with `/resume` (or `polyrob"
    " autonomy resume`), then run this again. Nothing was broadcast."
)


def pause_refusal_text(kind: str, *, what: str = "this action",
                       data_dir: Optional[str] = None,
                       force: bool = False) -> Optional[str]:
    """The ONE honest refusal sentence for a pause-blocked action, or None.

    ⚠️ There is no separate kill-switch to name. ``AutonomyConfig.autonomy_halted``
    is literally ``not allows("dispatch").allowed`` — a FACET of this same record —
    so the legacy "autonomy is HALTED (owner kill-switch)" text sent the owner
    looking for a lever that does not exist, and offered no remedy at all.

    Live, 2026-09-12: the owner's `/pause all` (reason "Stop all goals") denied
    ``spend`` and ``trade_exit`` too, because both map to ``("all",)``. He was then
    told a bridge was "a hard safety gate I can't self-grant". It was his own stop.
    A refusal on the money path must therefore name three things: WHAT refused, WHO
    set it, and a remedy that actually works — including that it binds the owner's
    OWN typed command, since these predicates run before any seat distinction.

    ``force=True`` renders the sentence even when this record reads ALLOWED. That
    is not a courtesy: ``autonomy_halted`` also answers to the legacy env/touch-file
    facets and to an injected probe, so a caller can know it was stopped while the
    record here is silent. Without ``force`` those callers fell back to a one-line
    refusal that dropped every honesty property the record-backed path carries —
    i.e. the exact defect, reintroduced on the rarer branch.

    Returns ``None`` when *kind* may run and ``force`` is not set, so the caller
    reads as a guard clause. Never raises: a probe fault is the CALLER's
    fail-closed decision to make, not this renderer's.
    """
    # Call shape matters: every caller in the tree (and every test stub) uses the
    # one-arg `allows(kind)`, so only pass `data_dir` when one was actually given.
    decision = allows(kind, data_dir) if data_dir is not None else allows(kind)
    allowed = bool(getattr(decision, "allowed", False))
    if allowed and not force:
        return None
    # `decision.reason` already reads "paused (<scopes>) by <who> via <surface>",
    # so WHAT and WHO are covered; only the owner's own stated reason and the
    # remedy need adding. Re-stating set_by/via here printed them twice.
    # Read through getattr throughout: this renderer's job is to make a refusal
    # clearer, so it must never be the thing that raises on an odd Decision.
    cause = (getattr(decision, "reason", None) or "paused") if not allowed \
        else "paused (autonomy halted)"
    stated = getattr(getattr(decision, "state", None), "reason", None)
    return (
        f"refused: {what} is {cause}"
        + (f" — the stated reason was {stated!r}" if stated else "")
        + "." + _PAUSE_REFUSAL_TAIL
    )
