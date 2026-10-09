"""073 W9 — code that calls tools (cross-agent ``execute_code`` parity).

``code_execution.run_code(language="python", code=..., tools=True)`` lets the
script call an ALLOWLIST of agent tools::

    from polyrob_tools import web_fetch, read_file, write_file, shell
    page = web_fetch("https://example.com")
    write_file(file_path="notes.md", content=page[:2000])

Shape:

- A ``polyrob_tools`` stub module is generated per run and injected IN the
  script (a one-line prelude that builds the module from source text and puts
  it in ``sys.modules``). Both backends run python with ``-I`` (no
  ``PYTHONPATH``, no cwd on ``sys.path``), so a file on disk would not import;
  the prelude also means nothing is written into the workspace. The prelude is
  ONE line, so a traceback's line numbers are the script's own plus one.
- Each stub call opens ONE connection to a per-run Unix socket, sends one JSON
  line ``{"id", "nonce", "tool", "args"}`` and reads one JSON line back
  ``{"id", "ok", "result" | "error"}``. The server lives in the agent process
  for the duration of the run only.
- ⚠️ Every call re-enters the ONE dispatch path —
  ``tools.controller.tool_call_bridge.perform_tool_call`` ->
  ``Controller.multi_act`` — with a clone of the run's execution context, so
  every pre-tool-call hook (operator denylist, wallet authority, the shell
  command guard, the correspondent gate, every approval lane) fires against
  the REAL action. Never call a tool function directly from here.
- Caps per run: :data:`MAX_CALLS` calls and :data:`WALL_CLOCK_SEC` seconds;
  results truncated to :data:`MAX_RESULT_CHARS`; a refused or failed call is
  raised in the script as ``polyrob_tools.ToolError``.

The allowlist is a short explicit list of read-mostly actions; the hard
exclusions are DERIVED from ``core.tool_capabilities`` at call time (never
``money``, never ``shared_identity``, never an outward ``writes_*`` effect,
never ``delegate_blocked`` except the guarded foreground shell, never
``high_impact`` except read egress + that shell), so a re-classified tool
drops out without an edit here.

Holds NO ``@BaseTool.action`` closures, so ``from __future__ import
annotations`` is safe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Per-run ceilings (cross-agent parity: 50 calls / 300 s).
MAX_CALLS = 50
WALL_CLOCK_SEC = 300.0
#: One tool result handed back to the script is cut to this many characters.
MAX_RESULT_CHARS = 50_000
#: One request line from the script is refused above this size.
MAX_REQUEST_BYTES = 1_000_000
#: Where the per-run socket dir is mounted inside a docker sandbox.
CONTAINER_RPC_DIR = "/polyrob_rpc"
#: The backends that can reach a socket of this process.
SUPPORTED_BACKENDS = ("local_subprocess", "docker")


def tool_rpc_enabled() -> bool:
    """``CODE_EXEC_TOOL_CALLS`` (default OFF): arm ``run_code(tools=True)``."""
    from core.env import bool_env
    return bool_env("CODE_EXEC_TOOL_CALLS", False)


# -- the allowlist --------------------------------------------------------------


@dataclass(frozen=True)
class RpcTool:
    """One script-facing function: ``alias(...)`` -> the registered ``action``."""
    alias: str
    action: str
    primary: Optional[str]  # the parameter one positional argument fills
    doc: str


#: Read-mostly actions a script may call. Names are the REGISTERED action keys
#: (``core.action_names.namespaced_action_name``). Message/send, delegation,
#: MCP, money and every other high-impact verb are absent on purpose.
RPC_TOOLS: Tuple[RpcTool, ...] = (
    RpcTool("web_fetch", "web_fetch_fetch_url", "url", "Fetch one URL as text/markdown."),
    RpcTool("web_search", "perplexity_search", "query", "Search the web (perplexity)."),
    RpcTool("kb_search", "knowledge_kb_search", "query", "Search the knowledge base."),
    RpcTool("kb_list", "knowledge_kb_list", None, "List knowledge-base sources."),
    RpcTool("memory_search", "memory_search", "query", "Search long-term memory."),
    RpcTool("session_search", "session_search", "query", "Search past sessions."),
    RpcTool("read_file", "filesystem_read_file", "file_path", "Read a workspace file."),
    RpcTool("write_file", "filesystem_write_file", "file_path", "Write a workspace file."),
    RpcTool("append_file", "filesystem_append_file", "file_path", "Append to a workspace file."),
    RpcTool("list_directory", "filesystem_list_directory", "directory",
            "List a workspace directory."),
    RpcTool("shell", "shell_run", "command",
            "Run ONE foreground shell command (guarded; no background jobs)."),
)
_BY_ALIAS: Dict[str, RpcTool] = {t.alias: t for t in RPC_TOOLS}

#: Action names that are never callable from a script, whatever the table says.
NEVER_ACTIONS = frozenset({
    "send_message", "message", "done", "subtask", "parallel_subtasks",
    "delegate_task", "tool_call", "code_execution_run_code", "memory",
    "skill_manage", "preferences", "mcp_install",
})
#: ``high_impact`` tool ids a script MAY reach: read egress and the guarded shell.
#: Their hooks (correspondent gate, command guard, approvals) still run per call.
HIGH_IMPACT_EXEMPT = frozenset({"web_fetch", "perplexity", "shell"})
#: ``delegate_blocked`` tool ids a script MAY reach (the foreground shell only).
DELEGATE_BLOCKED_EXEMPT = frozenset({"shell"})
#: Outward write effects a script tool may never carry.
FORBIDDEN_EFFECTS = frozenset({
    "writes_money", "writes_comms", "writes_social", "writes_public",
    "writes_self", "writes_network",
})


def policy_refusal(action: str, tool_id: Optional[str]) -> Optional[str]:
    """Why *action* (owned by *tool_id*) may not be called from a script, or None.

    The allowlist is :data:`RPC_TOOLS`; the exclusions derive from
    ``core.tool_capabilities`` so a re-classified tool drops out on its own.
    """
    if action in NEVER_ACTIONS:
        return f"'{action}' is never callable from code"
    if action not in {t.action for t in RPC_TOOLS}:
        return f"'{action}' is not on the code-tools allowlist"
    if not tool_id:
        return None  # a Controller built-in on the allowlist (memory/session search)
    if tool_id == "mcp" or tool_id.startswith("mcp_"):
        return "MCP tools are never callable from code"
    from core.tool_capabilities import TOOL_CAPABILITIES
    caps = TOOL_CAPABILITIES.get(tool_id)
    if caps is None:
        return f"tool '{tool_id}' is not classified in the capability table"
    if "money" in caps:
        return f"tool '{tool_id}' can move money"
    if "shared_identity" in caps:
        return f"tool '{tool_id}' acts as the instance's shared identity"
    bad = sorted(caps & FORBIDDEN_EFFECTS)
    if bad:
        return f"tool '{tool_id}' has an outward write effect ({', '.join(bad)})"
    if "delegate_blocked" in caps and tool_id not in DELEGATE_BLOCKED_EXEMPT:
        return f"tool '{tool_id}' is delegate-blocked"
    if "high_impact" in caps and tool_id not in HIGH_IMPACT_EXEMPT:
        return f"tool '{tool_id}' is high-impact"
    return None


# -- the stub -------------------------------------------------------------------

_STUB_TEMPLATE = '''\
"""polyrob_tools — agent tools callable from this script (073 W9).

Each call goes to the agent over a Unix socket and runs through the agent's
normal tool path: approvals and guards still apply. A refused or failed call
raises ToolError. Caps per run: {max_calls} calls, {wall:g} s.
"""
import itertools as _it
import json as _json
import socket as _socket

_SOCK = {sock!r}
_NONCE = {nonce!r}
_IDS = _it.count(1)
TOOLS = {tools!r}


class ToolError(Exception):
    """A tool call was refused or failed."""


def call(tool, *args, **kwargs):
    """Call the allowlisted tool *tool* (one positional = its main parameter)."""
    spec = TOOLS.get(tool)
    if spec is None:
        raise ToolError("unknown tool %r (allowed: %s)" % (tool, ", ".join(sorted(TOOLS))))
    if len(args) > 1 or (args and not spec[0]):
        raise ToolError("%s takes keyword arguments" % tool)
    if args:
        kwargs[spec[0]] = args[0]
    rid = next(_IDS)
    line = _json.dumps({{"id": rid, "nonce": _NONCE, "tool": tool, "args": kwargs}})
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout({sock_timeout:g})
    buf = b""
    try:
        s.connect(_SOCK)
        s.sendall(line.encode("utf-8") + b"\\n")
        while not buf.endswith(b"\\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    except OSError as e:
        raise ToolError("tool RPC transport failed: %s" % e) from None
    finally:
        s.close()
    if not buf:
        raise ToolError("tool RPC: the agent closed the call without a reply")
    resp = _json.loads(buf.decode("utf-8"))
    if resp.get("id") != rid:
        raise ToolError("tool RPC: reply id mismatch")
    if not resp.get("ok"):
        raise ToolError(resp.get("error") or "tool call failed")
    return resp.get("result")


def _bind(name):
    def _f(*args, **kwargs):
        return call(name, *args, **kwargs)
    _f.__name__ = name
    _f.__doc__ = TOOLS[name][1]
    return _f


for _n in TOOLS:
    globals()[_n] = _bind(_n)
__all__ = ["ToolError", "call", "TOOLS"] + sorted(TOOLS)
'''


def stub_source(socket_path: str, nonce: str, *, wall_sec: float = WALL_CLOCK_SEC) -> str:
    """The ``polyrob_tools`` module source for one run."""
    tools = {t.alias: (t.primary, t.doc) for t in RPC_TOOLS}
    return _STUB_TEMPLATE.format(
        sock=socket_path, nonce=nonce, tools=tools, max_calls=MAX_CALLS,
        wall=wall_sec, sock_timeout=wall_sec + 30,
    )


def wrap_script(code: str, socket_path: str, nonce: str,
                *, wall_sec: float = WALL_CLOCK_SEC) -> str:
    """Prefix *code* with a ONE-line prelude that installs ``polyrob_tools``."""
    src = stub_source(socket_path, nonce, wall_sec=wall_sec)
    prelude = (
        "import sys as _prt_sys, types as _prt_types; "
        "_prt_m = _prt_types.ModuleType('polyrob_tools'); "
        f"exec(compile({src!r}, '<polyrob_tools>', 'exec'), _prt_m.__dict__); "
        "_prt_sys.modules['polyrob_tools'] = _prt_m; "
        "del _prt_sys, _prt_types, _prt_m"
    )
    # A `from __future__` import must stay the first statement, so the prelude
    # goes after the module docstring and every leading __future__ import.
    cut = _future_end_line(code)
    if cut:
        lines = code.splitlines(keepends=True)
        head = "".join(lines[:cut])
        if not head.endswith("\n"):
            head += "\n"
        return head + prelude + "\n" + "".join(lines[cut:])
    return prelude + "\n" + code


def _future_end_line(code: str) -> int:
    """Line number ending the docstring + ``from __future__`` block that must stay
    first in *code*, or 0 when there is none (or the code does not parse)."""
    import ast
    try:
        body = ast.parse(code).body
    except SyntaxError:
        return 0
    end = 0
    seen_future = False
    for i, node in enumerate(body):
        if i == 0 and isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                and isinstance(node.value.value, str):
            end = node.end_lineno or end
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            end = node.end_lineno or end
            seen_future = True
            continue
        break
    return end if seen_future else 0


# -- the socket dir -------------------------------------------------------------


def _numeric_owner(user: Optional[str]) -> Optional[Tuple[int, int]]:
    """``"uid:gid"`` -> ints, else None (a name or an empty value)."""
    try:
        uid_s, _, gid_s = str(user or "").partition(":")
        return int(uid_s), int(gid_s or uid_s)
    except ValueError:
        return None


def make_socket_dir(*, owner: Optional[str] = None) -> str:
    """Create a fresh private dir (mode 0700) for one run's socket.

    A SHORT path (``mkdtemp`` under the system temp dir): an ``AF_UNIX`` path is
    limited to ~104 bytes, so a deep session workspace cannot hold it. When this
    process is root and a sandbox runs as another numeric uid (the docker
    backend's forced ``65534:65534``), the dir stays root-owned (the docker bind
    check refuses any other owner) but becomes traverse-only (0711); the SOCKET
    itself is then handed to that uid with mode 0600 (:func:`_hand_socket_to`),
    and every request must carry the run's nonce.
    """
    base = "/tmp" if os.path.isdir("/tmp") else None
    path = tempfile.mkdtemp(prefix="prpc-", dir=base)
    os.chmod(path, 0o700)
    ids = _numeric_owner(owner)
    if ids and hasattr(os, "geteuid") and os.geteuid() == 0 and ids[0] != 0:
        os.chmod(path, 0o711)
    return path


def _hand_socket_to(path: str, owner: Optional[str]) -> None:
    os.chmod(path, 0o600)
    ids = _numeric_owner(owner)
    if ids and hasattr(os, "geteuid") and os.geteuid() == 0 and ids[0] != 0:
        try:
            os.chown(path, ids[0], ids[1])
        except OSError:
            logger.warning("tool RPC: could not hand socket %s to uid %s", path, ids[0])


@dataclass
class SocketPlan:
    """Where one run's socket lives, on the host and as the script sees it."""
    host_dir: str
    host_path: str
    script_path: str
    #: Bind the host dir into an ephemeral docker run (``ExecutionRequest.tool_rpc_dir``).
    mount_dir: Optional[str] = None
    #: The dir is ours to remove after the run (not a persistent container's mount).
    owns_dir: bool = True


def plan_socket(backend: Any) -> Tuple[Optional[SocketPlan], Optional[str]]:
    """``(plan, None)`` for a backend that can reach this process, else
    ``(None, honest_reason)``."""
    name = str(getattr(backend, "name", "") or "")
    if name not in SUPPORTED_BACKENDS:
        return None, (
            f"tools=True is not supported on the '{name or 'unknown'}' execution backend: "
            "the script must reach a Unix socket of the agent process, which only "
            "local_subprocess and docker (on the agent's own host) can do.")
    sock_name = f"{secrets.token_hex(8)}.sock"
    if name == "local_subprocess":
        d = make_socket_dir()
        p = os.path.join(d, sock_name)
        return SocketPlan(host_dir=d, host_path=p, script_path=p), None
    owner = getattr(backend, "user", None)
    if getattr(backend, "_session_id", None) is not None:
        # PERSISTENT container: mounts are fixed at `docker run -d`; the backend
        # mounted ONE rpc dir at setup when CODE_EXEC_TOOL_CALLS was on.
        host_dir = getattr(backend, "tool_rpc_host_dir", None)
        if not host_dir:
            return None, (
                "tools=True needs the persistent sandbox container to carry the tool "
                "RPC mount, and this one was started without it (CODE_EXEC_TOOL_CALLS "
                "was off at its setup). Start a new session.")
        return SocketPlan(host_dir=host_dir, host_path=os.path.join(host_dir, sock_name),
                          script_path=f"{CONTAINER_RPC_DIR}/{sock_name}",
                          owns_dir=False), None
    d = make_socket_dir(owner=owner)
    return SocketPlan(host_dir=d, host_path=os.path.join(d, sock_name),
                      script_path=f"{CONTAINER_RPC_DIR}/{sock_name}", mount_dir=d), None


def cleanup_plan(plan: Optional[SocketPlan]) -> None:
    if plan is None:
        return
    try:
        os.unlink(plan.host_path)
    except OSError:
        pass
    if plan.owns_dir:
        shutil.rmtree(plan.host_dir, ignore_errors=True)


# -- the server -----------------------------------------------------------------

#: ``dispatch(action_name, args) -> ActionResult`` — the caller binds the controller.
Dispatch = Callable[[str, Dict[str, Any]], Awaitable[Any]]
#: ``resolve_tool(action_name) -> (registered?, tool_id)``.
Resolve = Callable[[str], Tuple[bool, Optional[str]]]


@dataclass
class RpcStats:
    calls: int = 0
    refused: int = 0
    log: List[str] = field(default_factory=list)


class ToolRpcServer:
    """The per-run JSON-lines server. One connection = one call; calls are
    serialized (the Controller is not built for concurrent re-entry)."""

    def __init__(self, *, socket_path: str, nonce: str, dispatch: Dispatch,
                 resolve: Resolve, owner: Optional[str] = None,
                 max_calls: int = MAX_CALLS, wall_sec: float = WALL_CLOCK_SEC,
                 max_result_chars: int = MAX_RESULT_CHARS,
                 shell_ceiling: Optional[float] = None) -> None:
        self.socket_path = socket_path
        self._nonce = nonce
        self._dispatch = dispatch
        self._resolve = resolve
        self._owner = owner
        self.max_calls = max_calls
        self.wall_sec = wall_sec
        self.max_result_chars = max_result_chars
        self._shell_ceiling = shell_ceiling
        self._server: Optional[asyncio.AbstractServer] = None
        self._lock = asyncio.Lock()
        self._deadline = 0.0
        self.stats = RpcStats()

    async def start(self) -> None:
        self._deadline = time.monotonic() + self.wall_sec
        self._server = await asyncio.start_unix_server(
            self._handle, path=self.socket_path, limit=MAX_REQUEST_BYTES + 1024)
        _hand_socket_to(self.socket_path, self._owner)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=5)
            except Exception:
                pass
            self._server = None

    async def __aenter__(self) -> "ToolRpcServer":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        rid: Any = None
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=30)
            except (asyncio.TimeoutError, ValueError, asyncio.LimitOverrunError):
                raw = b""
            if not raw:
                reply = {"id": None, "ok": False, "error": "empty or oversized request"}
            else:
                try:
                    req = json.loads(raw.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("request is not an object")
                except ValueError as e:
                    req, reply = None, {"id": None, "ok": False, "error": f"bad request: {e}"}
                if req is not None:
                    rid = req.get("id")
                    async with self._lock:
                        reply = await self._serve(req)
                    reply["id"] = rid
            writer.write((json.dumps(reply, default=str) + "\n").encode("utf-8"))
            await writer.drain()
        except Exception as e:  # never let a script crash the agent's loop
            logger.warning("tool RPC: connection failed: %s", e)
        finally:
            try:
                writer.close()
            except Exception:
                pass

    def _refuse(self, msg: str) -> Dict[str, Any]:
        self.stats.refused += 1
        self.stats.log.append(f"refused: {msg[:120]}")
        return {"ok": False, "error": msg}

    async def _serve(self, req: Dict[str, Any]) -> Dict[str, Any]:
        if not secrets.compare_digest(str(req.get("nonce") or ""), self._nonce):
            return self._refuse("tool RPC: bad nonce")
        alias = str(req.get("tool") or "")
        args = req.get("args") or {}
        if not isinstance(args, dict):
            return self._refuse("tool RPC: args must be an object")
        spec = _BY_ALIAS.get(alias)
        if spec is None:
            return self._refuse(
                f"tool '{alias}' is not callable from code "
                f"(allowed: {', '.join(sorted(_BY_ALIAS))})")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return self._refuse(
                f"tool RPC wall-clock cap reached ({self.wall_sec:g} s per run)")
        if self.stats.calls >= self.max_calls:
            return self._refuse(f"tool RPC call cap reached ({self.max_calls} calls per run)")
        registered, tool_id = self._resolve(spec.action)
        if not registered:
            return self._refuse(
                f"tool '{alias}' ({spec.action}) is not loaded in this session")
        why = policy_refusal(spec.action, tool_id)
        if why:
            return self._refuse(f"tool '{alias}' refused: {why}")
        args = dict(args)
        if spec.action == "shell_run":
            if args.get("background"):
                return self._refuse("shell from code runs in the foreground only")
            args["background"] = False
            want = args.get("timeout")
            try:
                want_f = float(want) if want is not None else 180.0
            except (TypeError, ValueError):
                return self._refuse("shell timeout must be a number")
            cap = remaining
            if self._shell_ceiling:
                cap = min(cap, float(self._shell_ceiling))
            args["timeout"] = max(1.0, min(want_f, cap))

        self.stats.calls += 1
        try:
            result = await asyncio.wait_for(self._dispatch(spec.action, args),
                                            timeout=max(1.0, remaining))
        except asyncio.TimeoutError:
            return self._refuse(
                f"tool '{alias}' ran past the wall-clock cap ({self.wall_sec:g} s per run)")
        except Exception as e:
            return self._refuse(f"tool '{alias}' failed: {type(e).__name__}: {e}")
        return self._shape(alias, result)

    def _shape(self, alias: str, result: Any) -> Dict[str, Any]:
        err = getattr(result, "error", None)
        if err:
            return self._refuse(str(err))
        content = getattr(result, "extracted_content", None) if result is not None else None
        if content is None and result is not None and not hasattr(result, "extracted_content"):
            content = result if isinstance(result, str) else str(result)
        text = "" if content is None else str(content)
        # perform_tool_call answers a mis-shaped call with a refusal RESULT, not an error.
        if text.startswith("tool_call refused:") or text.startswith("tool_call:"):
            return self._refuse(text)
        if len(text) > self.max_result_chars:
            cut = len(text) - self.max_result_chars
            text = text[: self.max_result_chars] + f"\n...[truncated {cut} chars]"
        self.stats.log.append(f"ok: {alias}")
        return {"ok": True, "result": text}


def new_nonce() -> str:
    return secrets.token_hex(16)


__all__ = [
    "CONTAINER_RPC_DIR", "MAX_CALLS", "MAX_RESULT_CHARS", "RPC_TOOLS", "RpcTool",
    "SocketPlan", "ToolRpcServer", "WALL_CLOCK_SEC", "cleanup_plan", "make_socket_dir",
    "new_nonce", "plan_socket", "policy_refusal", "stub_source", "tool_rpc_enabled",
    "wrap_script",
]
