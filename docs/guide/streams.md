# Streams

A **stream** is one standing objective plus the recurring cycle of goals that
serves it. "Grow the treasury by trading memecoins" is a stream: an
objective row on the goal board, and a chain of goals (research → act →
report) that gets re-seeded every few hours as long as the objective is
active. As the number of standing objectives grows past a handful, two
things must keep working: the ready queue must not let one objective's
backlog starve the others, and adding a new stream must not mean writing a
new script and a new systemd timer.

This page covers both. See `docs/guide/architecture.md` ("W4 durable goal
board") for how the board itself works; this page is about running several
objectives side by side.

## Two ways to create a stream

There are two, because they need different levels of trust.

### 1. A board objective — the planner can serve it

Most streams don't need anything the agent can't already reach: research,
posting, writing to the knowledge base, filesystem work. For these, create a
standing objective and let the autonomous goal planner keep it fed:

```bash
polyrob goals objective add "Grow the newsletter" \
  --body "Publish one issue a week; grow list size and open rate." \
  --priority 5 \
  --success-criteria "list size and open rate trending up, judged monthly" \
  --goal-budget 20
```

- `--success-criteria` is shown verbatim in the planner's prompt — it is
  what the model is told to measure progress against.
- `--goal-budget` caps how many LIVE child goals this objective may have at
  once; past the cap, `board.create` refuses new children outright until
  some finish. Omit it (or pass `0`) for no cap.
- `--stream-id` ties this objective to a `data/streams/streams.yaml` entry
  (see below) — only needed if a manifest stream is adopting an
  operator-created objective rather than creating its own. ⚠️ Setting it also
  makes `--goal-budget` unenforced on that objective: a stream is standing work
  and is throttled by `max_live_goals` + `cadence_hours` instead (see "Why a
  stream objective has no goal budget").

Inspect one with `polyrob goals objective show <id>` (criteria, budget
usage, live children). From chat, a phone-only owner gets the same steering
without the CLI: `/goal objective list`, `/goal objective pause <id>`,
`/goal objective activate <id>`, `/goal objective drop <id>`. Pausing an
objective does not touch its already-running goals — it only stops the
planner from adding new ones.

The planner decides WHAT to write for this kind of stream; it never decides
which tools a goal gets beyond the safe self-goal allowlist (research/social/
knowledge/read-only DeFi data — see "Money tools are manifest-only" below).

### 2. A manifest entry — for work that needs an operator tool grant

Some streams need a tool no autonomous goal may grant itself — trading is
the current example (`defi_trade`). For these, add an entry to
`data/streams/streams.yaml` instead of a bare objective. The manifest is
operator-authored, git-tracked, and deployed with the code; it is the ONLY
place an autonomous goal is granted a money verb.
`agents/task/goals/streams.py` reads it and:

- creates (or adopts) the objective, keyed by `payload.stream_id` — not by
  title, so renaming the mission doesn't fork a duplicate objective;
- seeds the stream's goal chain when it's due (see throttles below), writing
  each goal's `payload.tools` **verbatim** — no inference, no widening, no
  filtering. Whatever tools you list is exactly what the dispatched session
  gets.

Run it by hand to try a change before it goes on the timer:

```bash
python scripts/seed_streams.py --dry-run                     # all streams, report only
python scripts/seed_streams.py --stream treasury-trading      # just one
python scripts/seed_streams.py --force                        # ignore both throttles
```

In production this runs from `polyrob-streams.timer`, hourly, against every
declared stream — see "Rolling this out" below.

## The manifest schema

`data/streams/streams.yaml`, one `streams:` list, each entry:

```yaml
version: 1
streams:
- id: treasury-trading          # stable identity — see ensure_objective below
  cadence_hours: 4              # hours between two seeds of this stream (default 4)
  max_live_goals: 3             # ceiling on goals of this stream in flight (0 = disabled)
  objective:
    title: ...                  # required
    body: ...
    priority: 1
    goal_budget: 12             # optional; NOT enforced on a stream — see below
    success_criteria: ...       # optional; shown in the planner's prompt
  goals:                        # required, non-empty; one manifest entry, N goals
  - title: ...                  # required
    body: ...                   # required
    priority: 2
    tools: [defi_data, twitter, web_fetch, ...]   # required, non-empty — GRANTED VERBATIM
    max_steps: 30                # default 30
    acceptance: ...              # optional
```

`load_manifest` validates all of this up front and raises on any bad entry
— a silently-skipped stream is a stream that goes dark for days before
anyone notices. In particular: `id` must be non-empty and unique, `goals`
must be a non-empty list, and every goal needs a non-empty `title`, `body`,
and `tools`. The manifest also cannot live under a session workspace path
(anything containing `/sessions/` or `/workspace/`) — that check exists so
the agent can never point the seeder at a file it wrote itself; the
manifest must be an operator grant, never a self-grant.

Every field under `objective:` that lands in the payload — `stream_id`,
`success_criteria`, `goal_budget` — is re-applied on **every** run, not only
when the objective is first created. The manifest is the declarative source for
its own streams, so editing `success_criteria` there takes effect on the next
hourly tick. Only those declared keys are written (`merge_payload` preserves
everything else on the row), and `title`/`body`/`priority` are deliberately NOT
re-applied: identity is `stream_id`, and an objective's prose stays editable
from the CLI and chat without the manifest silently overwriting it.

### Why a stream objective has no goal budget

`OBJECTIVE_GOAL_BUDGET` (and a per-objective `goal_budget`) is a **lifetime**
tally: it counts every child that is not `cancelled`/`dropped`, so `done`
children count forever and nothing sweeps them. That is exactly what you want
for a bounded project — it is the rail that stops the planner opening "Round 9"
on an objective that never finishes. It is exactly wrong for a stream, whose
contract is to re-seed the same cycle for as long as it stands: a 3-leg cycle
against a budget of 12 jams permanently after four cycles, and every later seed
is refused with "objective has spent its goal budget".

So `GoalBoard.objective_budget` returns 0 (uncapped) for any objective carrying
`payload.stream_id`. A manifest `goal_budget` is still recorded on the row — it
takes effect again if the stream identity is ever removed — it is simply not
enforced while the objective IS a stream. The throttle that bounds a stream is
`max_live_goals` + `cadence_hours`, which bound CONCURRENT work rather than
lifetime work.

Multiple goals in one stream's `goals:` list are chained head-to-tail
(`depends_on`) so the second only starts once the first's artifact exists —
seeding them all at once, unchained, would let `GOAL_MAX_CONCURRENT`
dispatch two legs in parallel with the second reading an empty result from
the first.

## Cadence and live-goal throttles

Two independent gates, both required, computed in `stream_is_due`:

1. **Live-goal ceiling.** `max_live_goals` (or, if omitted, the length of
   the stream's `goals` list) is the most goals of this stream allowed
   in flight at once, counting anything in `LIVE_STATUSES` — which
   includes `triage`, `waiting`, `ready`, `running`, **and `blocked`**.
   Blocked is deliberately counted as live: `blocked` is not a terminal
   status in POLYROB. A `provider_outage` block self-heals — it
   auto-requeues to `ready` after `GOAL_BLOCKED_PROVIDER_RETRY_MIN`
   (default 30 minutes) — and any other block kind eventually ages out to
   `cancelled` after `GOAL_BLOCKED_MAX_AGE_DAYS` (default 14). If a stalled
   leg dropped out of "live" the moment it blocked, a lapsed cadence window
   would green-light a second, overlapping seed of the same stream while
   the first cycle's blocked leg is still sitting there — for the trading
   stream that means two cycles both able to spend at once. So the correct,
   conservative failure is: **the whole stream pauses behind a permanently
   blocked leg**, not a second concurrent cycle.

   **How long that pause lasts, precisely.** Only `provider_outage` heals by
   itself (~30 minutes). For every other block kind on a CHAINED cycle the
   automatic thaw is NOT 14 days — it is roughly
   `GOAL_BLOCKED_MAX_AGE_DAYS x (legs - 1)`, so **about 28 days** for the
   shipped 3-leg trading cycle. Trace it: leg 1 blocks at T, and
   `_cascade_dep_failed` flips leg 2 `waiting -> blocked` with a *fresh*
   `completed_at`; leg 3 stays `waiting`, because the cascade does not recurse.
   At T+14d `age_out_blocked` cancels legs 1-2, which cascades leg 3 to
   `blocked` — again with a fresh `completed_at` — and leg 3 ages out at
   T+28d. Only then does `stream_live_goals` reach 0 and the stream resume.

   So a stuck money stream is an **owner action, not a wait**: run
   `goal_unblock` on **each** blocked leg (`polyrob goals list --status blocked`
   to find them). Unblocking only the head leaves the rest blocked behind it.
2. **Cadence window.** `cadence_hours` (default 4) since the newest goal
   this stream produced, derived from the board's own `created_at` rather
   than a side table — the cadence cannot drift out of step with what was
   actually written.

Both must pass for a re-seed to happen. `--force` on the seeder script
bypasses both, for a manual top-up.

## Fair dispatch across objectives

Once goals exist, `GoalDispatcher` decides which `ready` ones to claim each
tick. The legacy order (`board.ready()`) sorts the WHOLE board by
`priority DESC, created_at` — fine with a handful of standing objectives,
but at a dozen or more it means one objective's backlog can take every
concurrency slot while its neighbours never get picked.

`GOAL_FAIR_DISPATCH` (default **ON**) switches dispatch to
`board.ready_fair`: it walks the same priority-ordered window
(`READY_SCAN_LIMIT` = 200 rows) but takes **at most one ready goal per
objective per pass**, round-robining across objectives until the tick's
concurrency slots are filled. Priority still decides who is served FIRST
within a pass; it no longer decides who is served AT ALL. Throughput is
unchanged when only one objective has ready work — every pass still draws
from it, so all available slots still fill.

`GOAL_PER_OBJECTIVE_CAP` (default **0**, disabled) is an optional extra
ceiling: the most concurrent RUNNING goals one objective may hold, checked
against `board.count_running_by_objective()`. It defaults to 0 — not 1 —
on purpose: the round-robin alone already delivers fairness with no
throughput cost, while a default cap of 1 would idle a concurrency slot
any time only one stream happens to have ready work. Set it to `1` only if
you specifically need to guarantee no single stream can ever hold two
slots, accepting that this can idle capacity.

Fair dispatch fails open: any error in `ready_fair` falls back to the
legacy global `board.ready()` order rather than stalling the tick.

The planner side of fairness is separate:
`planner._starved_order_with_stats` (sorted fewest-IN-FLIGHT-children first,
then oldest-activity, then priority — `done` children do not make a stream look
saturated) tells the planner which objectives most need new work, via a "SERVE
THESE OBJECTIVES FIRST" section built into its prompt. Code now picks WHICH
objective the planner serves next; the model still writes the goals.

`GOAL_PLANNER_SCALING` (default **ON**) is the single revert for that whole
planner half, the way `GOAL_FAIR_DISPATCH` is for dispatch. Set it to `false`
and the planner goes back to its pre-scaling shape: a fixed ready ceiling of 5,
a flat `GOAL_PLANNER_MIN_READY` thinness gate, and no starvation block in the
prompt. Pinning `GOAL_PLANNER_READY_CEILING` cannot do this — the scaled value
is a `max()` of it, and pinning it to `1` corrupts the prompt text itself.

## Knobs to turn as the stream count grows

As you add streams, three things need to widen together, or one becomes the
new bottleneck:

- **`GOAL_MAX_CONCURRENT`** (default 2) — the number of goal runs that can
  be in flight at once, globally. Fair dispatch spreads whatever slots
  exist across objectives; it cannot create slots that don't exist. Measured
  prod baseline (14 days to 2026-08-25, 6 active objectives): 19–27 goal
  runs/day, run duration p50 3 minutes / p90 20 minutes, against a cap of 2
  — the two slots sit idle most of the day, so compute was never the
  constraint here, fairness was. Raise this only as an owner env edit (see
  below); it cannot be raised from chat or by the agent.
- **`GOAL_PLANNER_READY_CEILING`** (default `0` = derive
  `max(5, active_objective_count)`) — how many ready goals the planner is
  told to keep the board stocked to, and the threshold `_maybe_plan` uses to
  decide the queue is thin enough to refill. The default derivation already
  scales with stream count; pin a number only if you want a fixed ceiling
  regardless of how many streams exist.
- **`GOAL_PLANNER_MAX_SOCIAL`** (default 1) — how many goals in one planner
  run may carry the `twitter` tool. Deliberately does NOT scale with stream
  count — raising it raises posting volume, not stream coverage. Leave it
  alone unless you specifically want more posts per planner run.

`GOAL_PLANNER_GOALS_PER_RUN` (default 3) is the third derived-ceiling input
and behaves like `GOAL_PLANNER_MAX_SOCIAL` — it bounds spend per planner
run, not coverage, so it's not a "turn this as streams grow" knob.

## Money tools are manifest-only

`allowed_self_goal_tools()` (`tools/goal_tools.py`) is the complete set of
tools an autonomous goal may request for itself — directly, or under
`AUTONOMY_MODE=autonomous`'s wider grant. It never contains `defi_trade` or
any other money-spend tool, in either case. Concretely: neither the
planner, nor the agent writing its own goal, nor any chat verb, can put a
money tool into a goal's `payload.tools`. The manifest's `goals[].tools`
list, written verbatim by `seed_stream`, is the **only** path by which an
autonomous goal run gets one. That is what makes granting `defi_trade` to
the `treasury-trading` stream safe: the grant lives in a file only an
operator with repo write access can change, it is reviewed like any other
code change, and it ships with a deploy — not with a chat message.

## Rolling this out

### Where the manifest lives (and what if there isn't one yet)

`streams.py::default_manifest_path()` resolves, in order:

1. `$POLYROB_STREAMS_MANIFEST`, if set — an absolute path wins over everything.
2. Otherwise `data/streams/streams.yaml` **inside the install tree** (the repo
   root in a dev checkout, `/opt/polyrob/data/streams/streams.yaml` on the box,
   the installed package's `data/streams/` in a pip install — it ships in the
   wheel via `MANIFEST.in` + `[tool.setuptools.package-data]`).

It deliberately does NOT live under `POLYROB_DATA_DIR` or in a profile home:
those trees are runtime state the agent can reach, and this file is an operator
grant. It also cannot live under a session workspace at all — `load_manifest`
refuses any path containing a `sessions/` or `workspace/` segment — and it is
hard-denied to every agent-writable file surface by
`core/security/secret_guard.py::is_protected_config_path`.

This repo ships a manifest, so there is normally nothing to bootstrap. If yours
is missing (an install that predates it, or a deploy path that did not sync
bundled content — see the trap below), `seed_streams.py` exits `2` with
`manifest error: ... No such file`. Create it rather than pointing the seeder
elsewhere:

```bash
mkdir -p /opt/polyrob/data/streams
cat > /opt/polyrob/data/streams/streams.yaml <<'YAML'
version: 1
streams:
- id: my-first-stream
  cadence_hours: 24
  objective:
    title: What this stream is for
  goals:
  - title: The one thing this stream does each cycle
    body: Concrete instructions for the run.
    tools: [web_fetch, filesystem, task]
YAML
```

An empty file is not valid — `streams:` must be a non-empty list, because a
manifest that parses to nothing is indistinguishable from a manifest that never
deployed. Track the file in git wherever your install tree is versioned; that
tracking is what makes the tool grant reviewable.

### ⚠️ The dev-machine deploy trap

**`scripts/deploy_from_local.sh` does not ship `data/streams` at all.**
Only `scripts/deploy_prod.sh` (run from the on-box maintenance clone) syncs
bundled data content — it has a `BUNDLED_DIRS` list
(`data/prompts data/characters data/streams`) that `deploy_from_local.sh`
does not have and never had; that script only rsyncs its own `CODE_DIRS`
copy, so it has never shipped `data/prompts` or `data/characters` either.

If you deploy from a dev machine with `deploy_from_local.sh`, the CODE for
`agents/task/goals/streams.py` and `scripts/seed_streams.py` lands on the
box, but `data/streams/streams.yaml` does not. The result: `load_manifest`
raises on the missing file, and the hourly `polyrob-streams.timer` fails
**every hour** with exit code 2. This is visible in `journalctl` and
`systemctl list-timers` — but nothing pushes that failure to the owner
proactively. This was a deliberate choice, not an oversight: what a
secondary deploy path pushes into prod's `data/` tree is an owner decision,
not something an autonomous session should silently patch up.

**Always deploy this change via `scripts/deploy_prod.sh`** (the on-box
maintenance clone), or manually copy `data/streams/streams.yaml` to the box
before relying on `deploy_from_local.sh`.

### When the seeder fails, the owner hears about it

An hourly systemd oneshot that starts failing is otherwise invisible outside
`journalctl`, which nothing pushes anywhere. `seed_streams.py` therefore:

- **isolates each stream.** One stream raising no longer aborts the run — it is
  reported, skipped, and every LATER stream in the manifest is still seeded.
- **exits non-zero** (`1` if any stream failed, `2` on a manifest error), so
  `systemctl status polyrob-streams` and `systemctl list-timers` show a failed
  unit instead of a clean one.
- **files a durable owner ask** on the existing goal-board ask rail — the same
  one a blocked goal uses. It shows up in `polyrob owner asks` / Telegram
  `/asks` (NOT `polyrob owner pending` — that surface only aggregates
  self-evolution proposals, queued tool approvals, and correspondent
  bindings, and this ask matches none of those). It is deduped EXACTLY per
  stream (one standing ask per failing stream, its `failures` counter and
  `last_error` refreshed each tick, never 24 asks a day), and it is closed
  automatically as `obsolete` the moment that stream seeds or skips cleanly
  again — so a transient failure does not leave a permanent chore. A missing
  manifest files one ask against the whole run under the pseudo-stream id
  `__manifest__`. The ask is durable and deduplicated, but PULL-ONLY: nothing
  pushes it to the owner, so a dead stream stays silent until the owner runs
  `owner asks` or `/asks`.

### Deploy, then verify by content

```bash
cd ~/rob_dev && git pull --rebase origin main && bash scripts/deploy_prod.sh
```

Verify by content, never by `.deployed_sha` alone:

```bash
ssh <host> 'ls -l /opt/polyrob/data/streams/streams.yaml && \
  /opt/polyrob/venv/bin/python -c "from agents.task.goals import streams as S; print([s[\"id\"] for s in S.load_manifest(S.default_manifest_path())])"'
```

### Install the new timer — old timer OFF first

**Disable the old timer BEFORE enabling the new one.** `enable --now` on an
`OnCalendar=hourly` + `Persistent=true` timer fires almost immediately (only
`RandomizedDelaySec=300` stands between the enable and the first run), so
enabling the new timer first can seed a cycle before you get to type the next
command.

```bash
sudo systemctl disable --now polyrob-trading-cycle.timer   # FIRST
sudo cp /opt/polyrob/deployment/polyrob-streams.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
/opt/polyrob/venv/bin/python /opt/polyrob/scripts/seed_streams.py \
  --db /var/lib/polyrob/goals.db --user-id rob --dry-run
sudo systemctl enable --now polyrob-streams.timer
systemctl list-timers polyrob-streams.timer --no-pager
```

What the dry run reports depends on the board, and both answers are correct:

- **`skip — N goal(s) live (ceiling 3)`** while the cycle the old timer seeded
  is still in flight. `stream_live_goals` counts the legacy `payload.cycle` tag
  as well as the new `payload.stream` one — the legacy `CYCLE_TAG` is literally
  `treasury-trading`, the same string as the manifest stream id — so the two
  seeders are not blind to each other and the new one will not open a second,
  overlapping trading cycle on top of the old one's.
- **`WOULD SEED — due`** once that cycle has finished (or if it never ran). That
  is the normal steady state, not a warning.

`scripts/seed_trading_cycle.py` stays in the tree for one release as the
rollback path, marked deprecated by a banner only. When it is finally deleted,
the `payload.cycle` half of the throttle simply stops matching anything.

### Raise the concurrency cap (owner env edit)

```bash
sudo sed -i 's/^GOAL_MAX_CONCURRENT=2$/GOAL_MAX_CONCURRENT=4/' /etc/polyrob/polyrob.env
sudo systemctl restart polyrob.service
```

`/etc/polyrob/polyrob.env` is hard-denied to every agent-writable surface,
and flags freeze at import — this is deliberately an owner action requiring
a restart. No agent, chat verb, or preference can perform it: an owner
preference set via `/config set` can only ever LOWER the cap
(`core/prefs.py` merges `goals.max_concurrent` with `min`, never raises it).

### Watch a day before adding more streams

```bash
sqlite3 /var/lib/polyrob/goals.db \
  "select coalesce(substr(parent_id,1,8),'(none)') o, count(*) n
     from goals where kind='goal' and started_at > strftime('%s','now')-86400
    group by o order by n desc;"
```

Success looks like runs spread across objectives instead of concentrated in
two. Baseline for comparison (14 days to 2026-08-25): `(none)` 110, then
28 / 23 / 19 / 11 / 11 / 8 / 4 / 2. Only add new streams once the spread is
real.

## Known limits

- **Objective count plateaus at 50.** The planner's thinness gate
  (`GoalDispatcher._active_objective_owners`) reads
  `board.list(status="active", limit=50)`. Past 50 standing objectives, the
  derived `GOAL_PLANNER_READY_CEILING` (`max(5, active_objectives)`) stops
  growing with the true count — pin the ceiling by env if you cross this.
- **Stream throttles no longer scan a window.** `stream_live_goals`,
  `stream_last_seeded_at` and `ensure_objective` used to derive their answers
  from `board.list(user_id=..., limit=1000)`, which ends
  `ORDER BY priority DESC, created_at LIMIT ?`. That was unsafe rather than
  merely limited: a manifest stream's legs and objective sit BELOW the board
  default priority of 5 by design, so they were the FIRST rows unrelated
  traffic evicted — inverting both throttles into "seed another cycle" and
  making `ensure_objective` mint a duplicate objective on every run. They now
  use `board.stream_goals` (tag-filtered in SQL, tenant-scoped) and
  `board.objectives` (kind-filtered, unbounded), which nothing can evict.
