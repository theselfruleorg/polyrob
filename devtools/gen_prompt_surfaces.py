#!/usr/bin/env python
"""081 WS-0.1 — the prompt-surface golden: what the model receives, per surface.

For each surface the REAL production path builds a session and its agent — a
real ``TaskAgent`` over the CLI container, ``create_session`` →
``orchestrator.create_agent`` (stub LLM, never called) — then this reads:

* the system side: ``message_manager.get_messages_for_llm(consume_ephemeral=False)``
  after the step-1 blocks were queued (``_maybe_inject_*`` as ``step.py`` runs
  them). One ``===== <layer> =====`` header per ``FOUNDATION_LAYERS`` row, then
  one ``===== step1:<origin> =====`` header per queued step-1 block;
* ``tools[]``: ``controller.get_all_actions_for_provider(PROVIDER)``.

Output, ``tests/golden/prompt_surface/``:

* ``<surface>.system.txt`` — the text, normalised (paths, version, clock stamps);
* ``<surface>.tools.json`` — the tool names, each with a short schema hash;
* ``tools/<tool>.json`` — each schema ONCE (the most common variant), plus
  ``tools/<tool>.<surface>.json`` only where a surface serves another variant;
* ``sizes.json`` — chars and tokens per layer per surface, the tool count and
  the schema size. Tokens are ``chars // 4``: an ESTIMATE, not a tokenizer.

Surfaces: owner Telegram DM, owner REPL, cron, goal, room, correspondent, leaf.
Variant ``prod_owner_telegram_dm`` (081 WS-3.1): the owner DM again, rendered in
a second child under the prod posture (:data:`PROD_ENV` — full autonomy with the
owner's grants: the armed money rail, compute posture 1, the X rails), so the
owner's first call is measured at the size prod sends, not the bare install's.

Determinism: the render runs in a CHILD process with a scrubbed environment
(``HOME``/``POLYROB_DATA_DIR`` in a fresh temp dir, ``TZ=UTC``, a fixed hash
seed, cwd an empty project dir) and pins, inside the child, the posture
(``POLYROB_LOCAL`` + ``AUTONOMY_MODE=autonomous`` + a bound owner — the prod
shape), the model and provider, the platform line, the host-executable probe,
the install facts (version line, install method, missing extras), browser
custody, MCP and the network. Every installed first-party pack counts as
available. What is left is normalised: temp paths, and a clock stamp (a date
and time within a day of the render) becomes ``<NOW>``. Output
order is sorted; there is no timestamp, so a git conflict in the golden is
resolved by regenerating.

Usage::

    python devtools/gen_prompt_surfaces.py           # rewrite the golden
    python devtools/gen_prompt_surfaces.py --check   # exit 1 when stale

``tests/test_prompt_surface_golden.py`` renders in a temp dir and fails with the
per-layer size delta when the committed golden is stale.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "golden" / "prompt_surface"
REGEN = "python devtools/gen_prompt_surfaces.py"
CHARS_PER_TOKEN = 4

MODEL = "deepseek/deepseek-v3.2"
PROVIDER = "openrouter"
OWNER = "golden-owner"
SURFACES = ("owner_telegram_dm", "owner_repl", "cron", "goal", "room",
            "correspondent", "leaf")

#: 081 WS-3.1: the prod-like posture — the grants the owner turned on in prod
#: (the armed money rail, compute posture 1 with its tools, the X API and browser
#: rails, MCP). Keys are placeholders; the network stays off. Rendered for the
#: surfaces in :data:`PROD_SURFACES` only, as ``prod_<surface>``.
PROD_ENV = {
    "DEFI_AGENT_AUTONOMY": "true", "DEFI_TRADE_ENABLED": "true", "DEFI_DATA_ENABLED": "true",
    "AGENT_COMPUTE_POSTURE": "1", "CODE_EXEC_ENABLED": "true", "SHELL_TOOLS_ENABLED": "true",
    "TWITTER_ENABLED": "true", "TWITTER_OAUTH2_ACCESS_TOKEN": "golden-not-a-token",
    "X_BROWSER_ENABLED": "true", "MCP_ENABLED": "true",
    "EMAIL_PROVIDER": "agentmail", "AGENTMAIL_API_KEY": "golden-not-a-key",
}
PROD_SURFACES = ("owner_telegram_dm",)
PROD_PREFIX = "prod_"

#: The env the child starts from: process plumbing + the pinned posture only.
_PINNED_ENV = {
    "TZ": "UTC", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
    "POLYROB_LOCAL": "1", "AUTONOMY_MODE": "autonomous",
    "POLYROB_OWNER_USER_ID": OWNER, "POLYROB_INSTANCE_ID": "golden",
    "MEMORY_BACKEND": "none", "LOG_LEVEL": "CRITICAL",
    "OPENROUTER_API_KEY": "sk-or-golden-not-a-key",
    "TASK_PROVIDER": PROVIDER, "TASK_MODEL": MODEL,
    "WALLET_SIGNER": "local",
}


# ── render (parent) ─────────────────────────────────────────────────────────

def render(out: Path) -> None:
    """Render every surface into ``out`` (replaced): the base posture in one
    scrubbed child, the prod-like variant in a second one."""
    rendered = dict(_render_child({}, SURFACES))
    for surface, value in _render_child(PROD_ENV, PROD_SURFACES).items():
        rendered[PROD_PREFIX + surface] = value
    with tempfile.TemporaryDirectory(prefix="psurf-out-") as tmp:
        staging = Path(tmp) / "out"
        _write(staging, rendered)
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(staging, out)


def _render_child(extra_env: Dict[str, str], surfaces) -> dict:
    """One scrubbed child: ``{surface: (blocks, tools)}``, already normalised."""
    with tempfile.TemporaryDirectory(prefix="psurf-") as tmp:
        root = Path(os.path.realpath(tmp))
        for d in ("home", "data", "proj", "tmp"):
            (root / d).mkdir()
        env = dict(_PINNED_ENV)
        env.update(extra_env)
        env.update({"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": str(root / "home"), "POLYROB_DATA_DIR": str(root / "data"),
                    "TMPDIR": str(root / "tmp"), "PYTHONPATH": _pythonpath()})
        staging = root / "rendered.json"
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child", str(staging),
             "--root", str(root), "--surfaces", ",".join(surfaces)],
            cwd=str(root / "proj"), env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("prompt-surface render failed:\n"
                               + proc.stdout[-4000:] + proc.stderr[-8000:])
        return json.loads(staging.read_text(encoding="utf-8"))


def _pythonpath() -> str:
    """This tree, then its first-party pack dirs: a render of a tree reads THAT
    tree's packs, not whichever checkout the editable install points at."""
    packs = sorted(str(d) for d in (REPO / "packs").glob("*") if d.is_dir())
    return os.pathsep.join([str(REPO), *packs])


# ── render (child) ──────────────────────────────────────────────────────────

class _StubLLM:
    """Enough surface for Agent construction; never called."""
    model_name = MODEL
    provider = PROVIDER

    def get_num_tokens(self, text):  # pragma: no cover - defensive
        return max(1, len(str(text)) // CHARS_PER_TOKEN)


def _pin_process() -> None:
    """Pins that cannot be set through the environment."""
    import platform
    import socket

    def _no_net(*_a, **_k):
        raise OSError("network is off in the prompt-surface render")

    socket.socket.connect = _no_net  # type: ignore[assignment]
    socket.create_connection = _no_net  # type: ignore[assignment]
    socket.getaddrinfo = _no_net  # type: ignore[assignment]
    platform.system = lambda: "Linux"
    platform.release = lambda: "golden"

    import agents.task.agent.core.env_context as env_context
    env_context._host_capabilities = lambda *a, **k: "git, python3"
    import polyrob.app.bootstrap as bootstrap
    bootstrap.maybe_register_cli_embedder = lambda *a, **k: None
    from core import install_facts, version
    version.running_version_line = lambda *a, **k: "<VERSION>"
    install_facts.missing_extras = lambda *a, **k: []
    # The prod variant's mail inbox (AgentMail) is provisioned over the network
    # at load; the render is offline, so the provisioning step is a no-op there.
    if os.environ.get("EMAIL_PROVIDER") != "agentmail":
        return
    try:
        from tools.email_tool import EmailTool

        async def _offline_init(self):
            return None
        EmailTool._initialize = _offline_init  # type: ignore[assignment]
    except Exception:  # pragma: no cover - the email tool is optional
        pass


def _child(out: Path, root: Path, surfaces) -> None:
    sys.path.insert(0, str(REPO))
    import logging
    logging.disable(logging.CRITICAL)
    from core.packs.loader import register_policies
    register_policies()
    _pin_process()
    _seed_stores()
    rendered = asyncio.run(_render_all(root, surfaces))
    out.write_text(json.dumps({
        surface: ([(key, normalise(text, root)) for key, text in blocks],
                  [json.loads(normalise(json.dumps(schema, ensure_ascii=False), root,
                                        clock=False)) for schema in tools])
        for surface, (blocks, tools) in rendered.items()}, ensure_ascii=False),
        encoding="utf-8")


def _seed_stores() -> None:
    """An empty, READABLE fresh instance: the goal board, the cron store and
    the telemetry log exist, so the step-1 blocks read "nothing" rather than
    "could not read" (whose error text would carry a machine path)."""
    from agents.task.goals.board import GoalBoard
    from core.event_log import get_event_log, telemetry_db_path
    from core.runtime_paths import cron_db_path, goals_db_path
    from cron.jobs import CronJobStore
    GoalBoard(goals_db_path())
    CronJobStore(cron_db_path())
    get_event_log(telemetry_db_path())


async def _render_all(root: Path, surfaces) -> Dict[str, Tuple[List[Tuple[str, str]], list]]:
    from polyrob.app.bootstrap import build_cli_container
    container = await build_cli_container(require_llm=False)
    _bind_chat_surfaces(container)
    task_agent = container.get_agent("task_agent")
    task_agent.task_available = True
    out = {}
    for surface in surfaces:
        out[surface] = await _render_surface(task_agent, surface)
    return out


def _bind_chat_surfaces(container) -> None:
    """The chat bus and a Telegram surface (no bot, never sends), as a gateway
    has them: the owner DM and the room sessions then bind their chat key and
    carry the ``<surface>`` block their production sessions carry."""
    os.environ["SINGULAR_CHAT_ENABLED"] = "true"
    from core.surfaces.bootstrap import install_surface_bus
    from core.surfaces.registry import SurfaceRegistry
    from surfaces.telegram.surface import TelegramSurface
    install_surface_bus(container)
    registry = SurfaceRegistry()
    registry.add(TelegramSurface(bot=None))
    container.register_service("surface_registry", registry)


async def _render_surface(task_agent, surface: str):
    from core import autonomy_marker as am
    from core.surfaces.envelopes import SessionSource

    sid = f"golden-{surface.replace('_', '-')}"
    kwargs = {}
    task = "hi"
    user = OWNER
    if surface in ("owner_telegram_dm", "leaf"):
        from surfaces.telegram.interactive_tools import interactive_tool_ids
        kwargs = dict(session_source=SessionSource(surface_id="telegram", chat_id="42",
                                                   chat_type="dm"),
                      chat_session_key="agent:main:telegram:dm:42",
                      tool_ids=interactive_tool_ids())
    elif surface == "owner_repl":
        kwargs = dict(session_source=SessionSource(surface_id="cli", chat_id="local",
                                                   chat_type="dm"))
    elif surface == "cron":
        from tools.cronjob_tools import resolve_cron_tools
        task = "Daily: summarise the new issues in the release tracker."
        am.mark_autonomous(sid, cron_job_id="golden-job")
        am.note_job("golden-job", task)
        kwargs = dict(tool_ids=list(resolve_cron_tools({})), creator="cron")
    elif surface == "goal":
        from agents.task.goals.dispatcher import default_goal_tools
        from core.config_policy.rigs import resolve_rig_tools
        task = "Goal: keep the project README accurate."
        am.mark_autonomous(sid, goal_id="golden-goal")
        am.note_goal("golden-goal", task)
        kwargs = dict(tool_ids=list(resolve_rig_tools({}, default_goal_tools())))
    elif surface == "room":
        from core.surfaces.room_policy import room_tool_ids
        os.environ["GROUP_CHAT_ENABLED"] = "true"
        task = "<addressed>\nmember|9 (member) → you: hi\n</addressed>"
        kwargs = dict(session_source=SessionSource(surface_id="telegram",
                                                   chat_id="-1001234567890",
                                                   chat_type="supergroup"),
                      chat_session_key="agent:main:telegram:supergroup:-1001234567890",
                      tool_ids=room_tool_ids())
    elif surface == "correspondent":
        user = OWNER
        task = ("[conversation-resume] email:someone@example.com sent a message to a "
                "conversation whose original session is no longer available.")
        from agents.task.tool_defaults import correspondent_session_tools
        kwargs = dict(creator="correspondent", tool_ids=correspondent_session_tools())

    await task_agent.create_session(user, request={"task": task}, session_id=sid,
                                    skip_credit_check=True, **kwargs)
    orch = task_agent.get_orchestrator(sid)
    if surface == "leaf":
        child = await orch.sub_agent_manager._build_child_controller()
        agent = await orch.create_agent(
            task="Read the README and list its section headings.", llm=_StubLLM(),
            agent_name="golden_leaf", controller=child, is_sub_agent=True,
            profile_id="executor", parent_session_id=sid)  # as delegate_task builds it
    else:
        agent = await orch.create_agent(task=task, llm=_StubLLM(), agent_name="executor")
    for name in ("_maybe_prefetch_memory", "_maybe_inject_live_health",
                 "_maybe_inject_operations", "_maybe_inject_episodic_digest",
                 "_maybe_inject_continuity_bridge", "_maybe_inject_owner_thread",
                 "_maybe_inject_autonomous_continuity"):
        fn = getattr(agent, name, None)
        if fn is not None:
            await fn()
    return _blocks(agent), agent.controller.get_all_actions_for_provider(PROVIDER)


def _blocks(agent) -> List[Tuple[str, str]]:
    """(header, text) per foundation layer, then per queued step-1 block."""
    from agents.task.agent.messages.foundation_layers import (
        FOUNDATION_LAYERS, layer_messages)
    mm = agent.message_manager
    out: List[Tuple[str, str]] = []
    for layer in FOUNDATION_LAYERS:
        msgs = layer_messages(mm, layer)
        for i, msg in enumerate(msgs):
            key = layer.key if len(msgs) == 1 else f"{layer.key}[{i}]"
            out.append((key, _content(msg)))
    for msg in list(getattr(mm, "_ephemeral_messages", None) or []):
        md = getattr(msg, "metadata", None) or {}
        if md.get("per_call") or md.get("state_message"):
            continue
        out.append((f"step1:{getattr(msg, 'origin', None) or 'untagged'}", _content(msg)))
    return out


def _content(msg) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = "\n".join(str(p.get("text", p)) if isinstance(p, dict) else str(p)
                            for p in content)
    return str(content)


# ── normalise + write ───────────────────────────────────────────────────────

#: A clock stamp: a date WITH a time (a bare date in prose is static text).
_STAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                    r"(?:Z|[+-]\d{2}:?\d{2}| ?UTC)?")


def _clock_dates() -> set:
    """Yesterday, today and tomorrow (UTC): the dates a render-time clock can
    print. A stamp on any other date is static text and stays as written."""
    import datetime as _dt
    today = _dt.datetime.now(_dt.timezone.utc).date()
    return {str(today + _dt.timedelta(days=d)) for d in (-1, 0, 1)}


def normalise(text: str, root: Path, *, clock: bool = True) -> str:
    """Temp paths -> placeholders; with ``clock``, a render-time stamp ->
    ``<NOW>``."""
    for path, token in ((root / "data", "<DATA>"), (root / "home", "<HOME>"),
                        (root / "proj", "<PROJECT>"), (root / "tmp", "<TMP>"),
                        (root, "<ROOT>")):
        text = text.replace(str(path), token)
    if clock:
        dates = _clock_dates()
        text = _STAMP.sub(lambda m: "<NOW>" if m.group(1) in dates else m.group(0), text)
    return text


def _dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _tool_name(schema: dict) -> str:
    return str((schema.get("function") or schema).get("name") or "?")


def _write(out: Path, rendered) -> None:
    out.mkdir(parents=True)
    (out / "tools").mkdir()
    sizes: Dict[str, dict] = {}
    variants: Dict[str, Dict[str, str]] = {}   # tool -> surface -> schema json
    for surface, (blocks, tools) in rendered.items():
        lines, layers = [], {}
        for key, text in blocks:
            lines.append(f"===== {key} =====\n{text}\n")
            n = len(text)
            row = layers.setdefault(key, {"chars": 0, "tokens": 0})
            row["chars"] += n
            row["tokens"] = row["chars"] // CHARS_PER_TOKEN
        (out / f"{surface}.system.txt").write_text("\n".join(lines), encoding="utf-8")
        names = []
        for schema in tools:
            name = _tool_name(schema)
            body = _dump(schema)
            variants.setdefault(name, {})[surface] = body
            names.append({"name": name,
                          "sha": hashlib.sha256(body.encode()).hexdigest()[:12]})
        (out / f"{surface}.tools.json").write_text(_dump({"tools": names}), encoding="utf-8")
        system_chars = sum(r["chars"] for r in layers.values())
        schema_chars = len(json.dumps(tools))
        sizes[surface] = {
            "layers": layers,
            "system": {"chars": system_chars, "tokens": system_chars // CHARS_PER_TOKEN},
            "tools": {"count": len(tools), "chars": schema_chars,
                      "tokens": schema_chars // CHARS_PER_TOKEN},
            "total_tokens": (system_chars + schema_chars) // CHARS_PER_TOKEN,
        }
    for name, per_surface in variants.items():
        common, _n = Counter(per_surface.values()).most_common(1)[0]
        (out / "tools" / f"{name}.json").write_text(common, encoding="utf-8")
        for surface, body in per_surface.items():
            if body != common:
                (out / "tools" / f"{name}.{surface}.json").write_text(body, encoding="utf-8")
    sizes["_note"] = ("tokens = chars // 4, an estimate; regenerate with "
                      f"`{REGEN}`")
    (out / "sizes.json").write_text(_dump(sizes), encoding="utf-8")


# ── compare ─────────────────────────────────────────────────────────────────

def differences(committed: Path, fresh: Path) -> List[str]:
    """``stale:`` / ``not committed:`` / ``only in committed:`` per file."""
    def files(d: Path) -> Dict[str, bytes]:
        return {str(p.relative_to(d)): p.read_bytes()
                for p in sorted(d.rglob("*")) if p.is_file()} if d.exists() else {}
    a, b = files(committed), files(fresh)
    out = [f"stale: {k}" for k in sorted(a.keys() & b.keys()) if a[k] != b[k]]
    out += [f"not committed: {k}" for k in sorted(b.keys() - a.keys())]
    out += [f"only in committed: {k}" for k in sorted(a.keys() - b.keys())]
    return out


def size_delta(committed: Path, fresh: Path) -> str:
    """Per surface, per layer: the char/token change (only what moved)."""
    def load(d: Path) -> dict:
        try:
            return json.loads((d / "sizes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    a, b = load(committed), load(fresh)
    lines = []
    for surface in sorted((a.keys() | b.keys()) - {"_note"}):
        sa, sb = a.get(surface) or {}, b.get(surface) or {}
        rows = []
        la, lb = sa.get("layers") or {}, sb.get("layers") or {}
        for key in sorted(la.keys() | lb.keys()):
            ca, cb = (la.get(key) or {}).get("chars", 0), (lb.get(key) or {}).get("chars", 0)
            if ca != cb:
                rows.append(f"    {key}: {ca:,} -> {cb:,} chars "
                            f"({(cb - ca) // CHARS_PER_TOKEN:+,} tok)")
        ta, tb = sa.get("tools") or {}, sb.get("tools") or {}
        if ta != tb:
            rows.append(f"    tools[]: {ta.get('count', 0)} -> {tb.get('count', 0)} tools, "
                        f"{ta.get('tokens', 0):,} -> {tb.get('tokens', 0):,} tok")
        if rows:
            lines.append(f"  {surface}: total {sa.get('total_tokens', 0):,} -> "
                         f"{sb.get('total_tokens', 0):,} tok")
            lines.extend(rows)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when the golden is stale")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--root", help=argparse.SUPPRESS)
    ap.add_argument("--surfaces", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.child:
        _child(Path(args.child), Path(args.root),
               tuple(s for s in (args.surfaces or "").split(",") if s) or SURFACES)
        return 0
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh"
            render(fresh)
            diff = differences(GOLDEN, fresh)
            if diff:
                print("prompt-surface golden is stale:\n" + "\n".join(diff[:40]))
                print(size_delta(GOLDEN, fresh))
                print(f"regenerate: {REGEN}")
                return 1
        return 0
    before = GOLDEN.with_name(GOLDEN.name + ".prev")
    if before.exists():
        shutil.rmtree(before)
    if GOLDEN.exists():
        shutil.copytree(GOLDEN, before)
    try:
        render(GOLDEN)
        delta = size_delta(before, GOLDEN)
    finally:
        shutil.rmtree(before, ignore_errors=True)
    print(f"wrote {GOLDEN.relative_to(REPO)}")
    print(delta or "  (no size change)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
