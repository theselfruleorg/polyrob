"""Objective-driven goal planner (replaces scripts/seed_goal_seeder.py).

The PROMPT IS BUILT BY CODE from live board + deliverables state — the agent
never re-derives its mission from a hardcoded theme list. Pure functions here;
the dispatcher owns triggering (flags, cooldown, quota) and session dispatch.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PLANNER_TOOLS = ["goal", "task"]
PLANNER_MAX_STEPS = 8

#: Floor for the derived ready-goal ceiling. Below this the planner cannot keep a
#: board of any size supplied, however few objectives are active.
PLANNER_READY_FLOOR = 5

#: Cap on the planner cooldown multiplier after consecutive empty runs.
PLANNER_BACKOFF_MAX_MULT = 4


def planner_backoff_multiplier(consecutive_empty_runs: int) -> int:
    """How many cooldowns to wait before the next planner run.

    Prod 2026-08-29: with two objectives at their lifetime budget and the rest
    covered, the planner ran every cooldown (hourly) for 24 h, ~55k input tokens a
    run, and reached the same "REAL BLOCKER" paragraph each time. A run that
    queues nothing is evidence the next one will too: 1x for the first empty
    run, then 2x, 4x, capped at :data:`PLANNER_BACKOFF_MAX_MULT`. A run that
    queues anything resets the streak; a stream refill that lifts the ready count
    above the thinness gate skips the planner regardless.
    """
    n = int(max(0, consecutive_empty_runs))
    if n < 2:
        return 1
    return int(min(PLANNER_BACKOFF_MAX_MULT, 2 ** (n - 1)))


def planner_ceilings(active_objectives: int) -> Dict[str, int]:
    """The planner's numeric limits, derived from how many streams stand.

    These three numbers used to be literals in the prompt prose ("Create 1-3
    goals", "Never exceed 5 ready goals total", "At most ONE goal may include
    'twitter'"), tuned when the instance ran 6 objectives. At 16 objectives the
    same literals starve most streams: five ready goals cannot cover sixteen
    streams, and a one-post ceiling silences fifteen of them.

    Only the ready ceiling scales; per-run and social stay owner-set, because
    scaling THEM would raise spend and posting volume rather than coverage. Each
    is pinnable by env — a pinned value always wins over the derivation.

    ``GOAL_PLANNER_SCALING=false`` reverts the DERIVATION (not the flags): the
    ceiling falls back to :data:`PLANNER_READY_FLOOR`, which is the value the old
    prompt literal carried, so the three numbers become 3 / 5 / 1 — byte-identical
    to the pre-scaling prompt. Pinning ``GOAL_PLANNER_READY_CEILING`` can no
    longer serve as that revert, because the derived value is a ``max()`` of it.
    """
    from agents.task.constants import AutonomyConfig
    ceiling = AutonomyConfig.goal_planner_ready_ceiling()
    if ceiling <= 0:
        ceiling = (max(PLANNER_READY_FLOOR, int(active_objectives))
                   if AutonomyConfig.goal_planner_scaling() else PLANNER_READY_FLOOR)
    return {
        "per_run": max(1, AutonomyConfig.goal_planner_goals_per_run()),
        "ready_ceiling": ceiling,
        "social": max(0, AutonomyConfig.goal_planner_max_social()),
    }


def planner_session_tools() -> list:
    """Planner session toolset. Under autonomous mode the planner also gets read-only
    web_fetch so 'is this duplicate / still true?' checks are grounded, not guessed."""
    from agents.task.constants import full_autonomy_enabled
    tools = list(PLANNER_TOOLS)
    if full_autonomy_enabled() and "web_fetch" not in tools:
        tools.append("web_fetch")
    return tools


def _is_live_waiting_goal(board, goal_id: str, _visited: Optional[set] = None) -> bool:
    """True iff every currently-unsatisfied prerequisite of a ``waiting`` goal is
    itself still in flight (``ready``/``running``) or ``waiting`` on a chain that
    is ITSELF live — recursive, since a waiting-on-waiting chain is fine as long
    as it bottoms out on live work rather than dead work.

    T2.1 review (Critical): board.py's ``_cascade_dep_failed`` flips a dependent
    straight to ``blocked`` when a prerequisite is cancelled or the circuit
    breaker trips it to ``blocked`` — but NOT when the prerequisite is flipped to
    ``blocked`` via ``block_from_ready`` (agent-declared ``OUTCOME: BLOCKED``,
    dispatcher's ``_fail_run(block=True)``), which is deliberately NOT cascaded
    (agent-declared blocks are owner-recoverable; cascading would force a double
    owner-unblock). So a dependent can sit in ``waiting`` on a dead prerequisite
    forever with no board-side signal. Treating "any waiting goal exists" as
    "board is fine" (the pre-fix suppression) silently reproduces the documented
    14h-idle stall shape. A ``blocked``/``cancelled`` prerequisite anywhere in the
    chain makes the whole chain dead.
    """
    if _visited is None:
        _visited = set()
    if goal_id in _visited:
        # T2.1 final-review Fix 4: a revisit is NOT a cycle — cycles are
        # impossible at write time (add_dependencies rejects them), so a
        # revisit means a diamond (two branches share a prerequisite) and
        # this node was already found live (or is still being explored,
        # which — absent an actual cycle — only happens on a shared
        # ancestor already proven live by the frame that visited it first).
        # Returning False here was a false negative that could sink an
        # otherwise fully-live diamond's STALLED-suppression check.
        return True
    _visited.add(goal_id)
    for dep_id in board.dependencies(goal_id):
        dep = board.get(dep_id)
        if dep is None:
            return False
        if dep.status == "done":
            continue
        if dep.status in ("ready", "running"):
            continue
        if dep.status == "waiting":
            if not _is_live_waiting_goal(board, dep_id, _visited):
                return False
            continue
        return False  # blocked / cancelled / anything else terminal-bad
    return True


def list_deliverables(root: Path, max_files: int = 40) -> List[Dict[str, Any]]:
    """name (relative), mtime iso, first markdown heading — depth <=2, dotfiles skipped."""
    out: List[Dict[str, Any]] = []
    if not root or not Path(root).is_dir():
        return out
    root = Path(root)
    candidates = sorted(
        (p for pattern in ("*", "*/*") for p in root.glob(pattern)
         if p.is_file() and not any(part.startswith(".") for part in p.relative_to(root).parts)),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for p in candidates[:max_files]:
        heading = ""
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh):
                    if i > 40:
                        break
                    if line.startswith("# "):
                        heading = line[2:].strip()
                        break
        except OSError:
            pass
        out.append({
            "name": str(p.relative_to(root)),
            "mtime_iso": time.strftime("%Y-%m-%d %H:%M", time.gmtime(p.stat().st_mtime)),
            "heading": heading,
        })
    return out


def _starved_order_with_stats(board, user_id: str,
                               objectives: List[Any]) -> List[Tuple[Any, int, Optional[float]]]:
    """One pass over the board: for each objective, its live-child count and
    last-activity timestamp, sorted hungriest first.

    The "SERVE THESE OBJECTIVES FIRST" prompt block needs the ORDER and the
    per-objective live-count/last-activity numbers to render, and derives both
    from this single board read — so a 16-objective prompt build costs one
    `objective_last_activity` call and one `children_of` call per objective, not
    two of each.

    LIVE here means IN FLIGHT — `done` children are excluded. `children_of`
    itself drops only `cancelled`/`dropped`, so counting its result would make a
    busy, long-running stream look permanently saturated and push it to the
    BOTTOM of an order whose whole purpose is to find the stream that needs work.
    (The separate "goal budget: N/M live" line stays `children_of`-shaped: that
    number must match what `_check_objective_budget` actually enforces, which IS
    a lifetime tally.)

    Sort key: fewest in-flight children first, then oldest activity, then highest
    priority. Fail-open on any board error — an unordered list is still a usable
    prompt, an exception is not.
    """
    try:
        activity = board.objective_last_activity(user_id)
    except Exception:
        activity = {}

    def _live(o) -> int:
        try:
            return sum(1 for c in board.children_of(user_id, o.id)
                       if getattr(c, "status", None) != "done")
        except Exception:
            return 0

    stats = [(o, _live(o), activity.get(o.id)) for o in objectives]
    stats.sort(key=lambda t: (t[1], t[2] or 0.0, -int(t[0].priority or 0)))
    return stats


def build_planner_prompt(board, user_id: str, deliverables_root: Optional[Path],
                         *, history_n: int = 10) -> str:
    from agents.task.goals.board import OBJ_ACTIVE

    objectives = board.objectives(user_id=user_id, status=OBJ_ACTIVE)
    done = [g for g in board.list(user_id=user_id, status="done", limit=history_n * 3)
            if g.kind == "goal"]
    done = sorted(done, key=lambda g: g.completed_at or 0, reverse=True)[:history_n]
    blocked = [g for g in board.list(user_id=user_id, status="blocked", limit=20)
               if g.kind == "goal"]
    ready = [g for g in board.list(user_id=user_id, status="ready", limit=20)
             if g.kind == "goal"]
    # T2.1 Task 4 (review-fixed, Critical): a goal in 'waiting' status does NOT
    # always have a live prerequisite — `block_from_ready` (agent-declared
    # OUTCOME: BLOCKED) is deliberately NOT cascaded, so a waiting goal can sit
    # behind a dead prerequisite indefinitely. `_is_live_waiting_goal` re-checks
    # the chain fresh every prompt build (never cached) to tell "will self-heal"
    # apart from "silently dead" — only the former should suppress STALLED.
    waiting = [g for g in board.list(user_id=user_id, status="waiting", limit=20)
               if g.kind == "goal"]
    live_waiting = [g for g in waiting if _is_live_waiting_goal(board, g.id)]

    sections = ["You are planning your own work queue. Create goals that genuinely "
                "advance a standing objective below. If you create nothing, there are "
                "TWO valid, DIFFERENT outcomes — pick the true one:\n"
                "  (a) REAL BLOCKER: progress needs something only the owner can provide "
                "(a credential, a decision, access) — state that specific blocker as your "
                "summary so it reaches the owner.\n"
                "  (b) QUEUE HEALTHY: the objective is already well-covered and there is "
                "no non-duplicate work worth adding right now — say exactly \"queue "
                "healthy, nothing to add\". This is NORMAL and is NOT a blocker.\n"
                "Do NOT invent busywork, and do NOT report a routine empty/covered queue "
                "as a blocker."]

    def _obj_line(o) -> str:
        crit = (o.payload or {}).get("success_criteria")
        base = f"- id={o.id} [{o.title}] {o.body}".rstrip()
        if crit:
            base += f"\n    success criteria: {crit}"
        # Show the objective's SPEND, not just its description. Without this the
        # planner cannot tell a fresh objective from one that has already spawned
        # 32 children over a month, so it opens the next round either way — which
        # is how "x402 Round 9" happened. Past the cap board.create refuses the
        # child outright, so the number here is a warning, not a surprise.
        try:
            budget = board.objective_budget(o)
            if budget > 0:
                live = len(board.children_of(user_id, o.id))
                base += f"\n    goal budget: {live}/{budget} live"
                if live >= budget:
                    base += (" — SPENT. Do not open another round on this objective; "
                             "finish or cancel its open goals, or raise an ask naming "
                             "the decision you need from the owner.")
                elif live >= max(1, int(budget * 0.8)):
                    base += " — nearly spent; converge rather than broaden."
        except Exception:
            pass
        return base

    sections.append("STANDING OBJECTIVES (active):\n" + "\n".join(
        _obj_line(o) for o in objectives))

    # Code owns which stream is served next. Without this the model picks, and at
    # a dozen-plus standing objectives it picks the same two or three every run.
    # Rides GOAL_PLANNER_SCALING so the whole planner half has ONE revert: with it
    # off the prompt has no starvation block at all, which is the pre-scaling shape.
    from agents.task.constants import AutonomyConfig as _AC
    if len(objectives) > 1 and _AC.goal_planner_scaling():
        _now = time.time()
        _lines = []
        for i, (o, live, last) in enumerate(
                _starved_order_with_stats(board, user_id, objectives), start=1):
            age = "never" if not last else f"{int((_now - last) // 3600)}h ago"
            _lines.append(f"{i}. id={o.id} [{o.title}] — {live} live goal(s), "
                          f"last activity {age}")
        sections.append(
            "SERVE THESE OBJECTIVES FIRST (hungriest at the top):\n"
            + "\n".join(_lines)
            + "\n\nWork DOWN this list. Do NOT create a goal for an objective lower "
              "in the list while one above it still has 0 live goals. This order is "
              "computed from the board, not a suggestion.")

    if done:
        # 2026-08-28 forensics: 63 of 94 planner runs in four days ended in
        # `dedup_rejected` — the board compares a new title against EVERY goal of
        # the last 7 days including done ones, and the planner kept re-proposing
        # yesterday's work under a new date/letter suffix. Say so explicitly.
        try:
            from agents.task.constants import AutonomyConfig as _DAC
            _thr = f"{int(round(_DAC.goal_dedup_threshold() * 100))}%"
        except Exception:
            _thr = "60%"
        sections.append(
            "RECENTLY DONE (title -> outcome) — DEDUP-PROTECTED for 7 days: a new "
            f"title >= {_thr} similar to any of these is REJECTED by goal_create, and a "
            "changed date, letter suffix or run number does NOT make it new. Propose "
            "genuinely different work, or extend the recorded deliverable under a "
            "distinct title:\n" + "\n".join(
            f"- {g.title} -> {(g.payload or {}).get('outcome') or '[no outcome recorded]'}"
            for g in done))
    if blocked:
        # T9: recall-vs-filesystem honesty — a blocked goal's recorded failure
        # text is stale memory, not ground truth. Stamp any workspace-relative
        # path it references with what's ACTUALLY on disk (fail-open, never
        # raises), so the planner can tell "still missing" from "owner already
        # fixed it" instead of re-deriving that from the error prose alone.
        try:
            from agents.task.goals.context import stamp_artifact_references
        except Exception:
            stamp_artifact_references = lambda text, root=None: text  # noqa: E731

        def _blocked_line(g) -> str:
            err = (g.last_failure_error or '?')[:120]
            if deliverables_root is not None:
                err = stamp_artifact_references(err, deliverables_root)
            return f"- {g.title} (error: {err})"

        sections.append("BLOCKED (do NOT recreate; fix or avoid):\n" + "\n".join(
            _blocked_line(g) for g in blocked))
    if ready:
        sections.append("ALREADY QUEUED (ready):\n" + "\n".join(f"- {g.title}" for g in ready))

    # The board's OPEN asks are the ONLY live owner-blockers. Prod 2026-08-29: the
    # planner cited an "entry pause" from an old report file + memory recall as a
    # REAL BLOCKER for 10 straight runs after the pause had been lifted — nothing
    # in the prompt said which asks were still open. Stamp the board's truth so a
    # file or a memory cannot outvote it (same shape as the entry-pause stamp).
    try:
        from agents.task.goals.board import ASK_OPEN as _ASK_OPEN
        open_asks = board.asks(user_id=user_id, status=_ASK_OPEN) or []
    except Exception:
        open_asks = None
    if open_asks is not None:
        if open_asks:
            ask_lines = "\n".join(f"- {a.id} {a.title}" for a in open_asks[:20])
        else:
            ask_lines = "- none"
        sections.append(
            "OPEN ASKS (board ground truth — the ONLY owner decisions still pending):\n"
            + ask_lines +
            "\nAny ask, pause or blocker you remember or find in a report file that is "
            "NOT listed here is RESOLVED. Do not cite it as a blocker, and do not "
            "re-file it. An objective whose only blocker is listed here is COVERED: "
            "say \"queue healthy, nothing to add\" for it rather than REAL BLOCKER.")

    if waiting:
        sections.append("WAITING ON DEPENDENCIES (will auto-ready when their "
                        "prerequisite completes; do NOT recreate):\n" + "\n".join(
            f"- {g.title} (waiting on: {', '.join(board.dependencies(g.id))})"
            for g in waiting))

    # Board-stall guard: 0 ready + blocked goals means the instance goes IDLE. "queue
    # healthy" is NOT valid here — it was the observed 14h-quiet stall (blocked -> ask ->
    # "queue healthy" -> idle). Force NEW achievable work or a single concrete owner-blocker.
    # T2.1 Task 4 (review-fixed): only a LIVE waiting goal (see `_is_live_waiting_goal`)
    # suppresses this — a waiting goal stuck behind a block_from_ready'd (agent-declared
    # BLOCKED) prerequisite is just as dead-in-the-water as the classic 0-ready+blocked
    # shape, so it must NOT silently suppress the stall banner.
    if not ready and blocked and not live_waiting:
        sections.append(
            f"⚠️ STALLED BOARD: 0 ready goals, {len(blocked)} blocked. If you add nothing the "
            "instance goes idle — that is a FAILURE, not 'queue healthy'. You MUST create 1-3 "
            "NEW, DIFFERENT, achievable goals that AVOID the blocked goals' failure modes "
            "(smaller scope, a different approach, no dependency on an unmet owner-blocker), OR "
            "— only if EVERY path genuinely needs the owner — state the ONE specific blocker. "
            "Do NOT recreate a blocked goal and do NOT say 'queue healthy, nothing to add'.")

    dels = list_deliverables(deliverables_root) if deliverables_root else []
    if dels:
        sections.append("EXISTING DELIVERABLES (extend these; do NOT create overlapping docs):\n"
                        + "\n".join(f"- {d['name']} ({d['mtime_iso']})"
                                    + (f": {d['heading']}" if d['heading'] else "")
                                    for d in dels))

    # T8 (013 owner transparency directive): the planner's own session toolset is
    # NOT the ceiling for goals it creates — goals carry their OWN tools. Fail-open:
    # on any error, omit only the dynamic grantable-list line, never the whole prompt.
    ground_truth_lines = [
        "TOOL GROUND TRUTH:",
        "- Your OWN session toolset is NEVER the ceiling — goals carry their OWN tools.",
    ]
    try:
        from agents.task.agent.core.tool_availability import grantable_autonomous_tools
        ground_truth_lines.append(
            "- Goals you create may be granted: "
            + ", ".join(grantable_autonomous_tools()) + ".")
    except Exception:
        pass
    ground_truth_lines.append(
        "- Declaring 'REAL BLOCKER: <tool> unavailable' about YOUR OWN session is a "
        "category error and is FORBIDDEN. A REAL BLOCKER is exclusively something only "
        "the owner can provide (a credential, a decision, access) that is NOT in the "
        "grantable list above.")
    # Live entry-pause status: recurring failure mode (3+ instances, 2026-08-29) was
    # this same "REAL BLOCKER: entry pause" line surviving in a new ask hours after
    # the owner actually lifted it — the planner was citing an old escalation report
    # from memory instead of the CURRENT flag. Stamp the live value directly so a
    # stale recall can't outlive the fix (mirrors the BLOCKED-goal stamping above).
    try:
        from agents.task.constants import AutonomyConfig as _EntryPauseCfg
        _paused = _EntryPauseCfg.entry_paused()
    except Exception:
        _paused = None
    if _paused is not None:
        ground_truth_lines.append(
            f"- TREASURY ENTRY-PAUSE right now: {'ACTIVE — no new entries' if _paused else 'NOT active — entries are open'}. "
            "This is the CURRENT value, not a memory of a past escalation — do not cite "
            "an old owner-ask doc's pause status if it contradicts this line.")
    sections.append("\n".join(ground_truth_lines))

    _c = planner_ceilings(len(objectives))
    sections.append(
        "INSTRUCTIONS:\n"
        f"- Create 1-{_c['per_run']} goals with goal_create; each MUST set objective_id, tools, and "
        "acceptance (what 'done' must prove: ids/paths/urls). Sequence dependent work "
        "with depends_on=[goal_id,...] instead of writing one mega-goal or "
        "duplicating steps.\n"
        "- When the outcome is mechanically checkable, ALSO set acceptance_checks "
        "(typed, framework-executed) — a passed check is proof, prose is not. The ONLY "
        "valid check types are 'artifact_glob' ({'type':'artifact_glob','pattern':'*.md'}), "
        "'http_ok' ({'type':'http_ok','url':'…'}) and 'file_contains' "
        "({'type':'file_contains','path':'report.md','contains':['A','B'],'mode':'all'}); "
        "do NOT invent other types — an unknown type fail-closes and can never pass. "
        "file_contains is an EXACT literal-substring match — use it only for known-exact "
        "strings (an id, a path, a specific number), never to assert a report 'discusses' "
        "a topic (e.g. contains=['PnL']): a semantically-complete report phrased "
        "differently will fail the check and force a wasted retry. Put that kind of "
        "completeness in plain-English acceptance text instead.\n"
        "- Each goal must EXTEND an existing deliverable or state why none applies.\n"
        "- Tools by shape: research -> ['web_fetch','anysite','filesystem','task']; "
        "drafting -> ['filesystem','task','web_fetch']; posting/engagement -> "
        f"['twitter','filesystem','task']. At most {_c['social']} goal may include 'twitter'.\n"
        f"- Never exceed {_c['ready_ceiling']} ready goals total (a goal waiting on depends_on does NOT "
        "count toward this ceiling — it isn't ready yet). A rejected duplicate means: "
        "extend the matched goal's work instead of retrying a rename.\n"
        "- If progress is blocked on something only the owner can provide (credentials, "
        "a decision, access), SAY SO explicitly and specifically — a concrete ask beats "
        "inventing busywork.\n"
        "- If ≥3 blocked/failed goals reference the SAME missing artifact, create ONE ask "
        "that names the artifact and STOP queuing goals that depend on it until the ask is "
        "fulfilled.\n"
        "- Finish with a one-line summary of what you queued, or — if you queued "
        "nothing — the specific blocker/ask standing between you and the objective."
    )
    return "\n\n".join(sections)
