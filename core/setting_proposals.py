"""093 D1/D2 — which setting change the agent may PROPOSE, and how.

``propose_setting(command=…)`` hands one owner seat line to :func:`classify`.
There are three answers, and only three:

* ``card`` — the line is an EXISTING owner verb the owner can already type,
  in an allowed shape: ``/mcp add <id> <https-url>``, ``/config set <key>
  <value>`` for a safe preference or a remote-writable flag, or a non-money
  tool grant (``/goal grant <id> tools=…``, ``/job edit <id> grant tools=…``).
  The card holds the line; Confirm runs it on the seat as if the owner typed
  it, so every gate under the verb runs again (a card adds no authority).
* ``operator`` — a setting no chat seat can write (the env file, a backend, a
  unit): it can never be a card. The caller files it for the operator with the
  exact ``KEY=value`` line.
* ``refused`` — anything else: a money, approval, ingress or trust setting, a
  secret, an unknown key, an unknown shape.

Pure: no store, no network. The who/when words come from
``core.tool_gates.REMEDY_ACTORS`` and ``core.config_service`` (the ONE write
oracle's own ``applies``), never from here.
"""
import re
from dataclasses import dataclass, field, replace
from typing import List, Optional
from urllib.parse import urlparse

CARD = "card"
OPERATOR = "operator"
REFUSED = "refused"

#: The shapes, as the refusal names them.
SHAPES = ("/mcp add <id> <https-url>", "/config set <key> <value>",
          "/goal grant <id> tools=<a,b>", "/job edit <id> grant tools=<a,b>")

MCP_RISK = ("Risk: the server's tools gain network reach from this agent, and what "
            "they return is untrusted input; the server sees every argument the agent "
            "sends it.")
CONFIG_RISK = "Risk: it changes how the agent behaves; /config set reverts it."
GRANT_RISK = ("Risk: the run can use these tools without asking again; no money "
              "tool is on this line.")
#: An ``exec`` tool on a grant line (``core.tool_capabilities``).
EXEC_RISK = ("Risk: {tools} run commands or code on this host as the agent's user — "
             "they can read and change any file that user can, and reach the network.")

#: 093 review: settings that open the agent to the outside or publish as it — an
#: ingress or a public exposure. ``never_live`` names money/approval/ingress by name
#: segment; these are the ones it misses. The owner sets them himself.
_EXPOSURE_EXACT = frozenset({
    "AGENT_BUILDER_MODE",        # ship = publish an agent-built app
    "CRON_WRITE_JOBS_ENABLED",   # scheduled jobs that write outward
    "HF_DEPLOY_ENABLED", "HF_DEPLOY_ORG",
    "PUBLISH_ENABLED", "APP_SERVICE_ENABLED",
})
_EXPOSURE_SUFFIXES = ("_API_ENABLED", "_PORT")
_EXPOSURE_PREFIXES = ("UVICORN_",)

_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,127}$")
_REF = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: ``core.config_service`` applies -> the ``core.tool_gates.APPLIES`` word.
_APPLIES = {"live": "now", "next-turn": "next-turn", "next-session": "new-session",
            "restart": "restart"}


@dataclass(frozen=True)
class SettingProposal:
    outcome: str
    words: List[str] = field(default_factory=list)   # the seat line (card)
    env_line: str = ""                               # KEY=value (operator)
    change: str = ""
    risk: str = ""
    who: str = ""
    when: str = ""
    reason: str = ""                                 # refused: why

    @property
    def line(self) -> str:
        return " ".join(self.words)


def _refused(reason: str) -> SettingProposal:
    return SettingProposal(REFUSED, reason=reason)


def _applies_word(applies: str) -> str:
    head = str(applies or "").split(" ", 1)[0]
    return _APPLIES.get(head, "restart")


def _mcp(words: List[str]) -> SettingProposal:
    if len(words) != 4:
        return _refused("an MCP card carries exactly /mcp add <id> <https-url> — no API "
                        "key on a card line (the owner adds a key himself)")
    sid, url = words[2], words[3]
    if not _SERVER_ID.match(sid):
        return _refused(f"{sid!r} is not a server id (letters, digits, - _ .)")
    try:
        p = urlparse(url)
        host = p.hostname or ""
        p.port  # noqa: B018 — a bad port raises here
    except ValueError:
        return _refused("an MCP server is an https:// URL")
    if p.scheme != "https" or not p.netloc or not host:
        return _refused("an MCP server is an https:// URL")
    # 093 review: what the owner reads must be where the card connects. A userinfo
    # part (https://github.com@attacker.example/) hides the real host, a query can
    # carry a key, and a non-ASCII host can look like another one.
    if "@" in p.netloc or p.username is not None or p.password is not None:
        return _refused("an MCP server URL carries no user@ part")
    if p.query or "?" in url or p.fragment or "#" in url:
        return _refused("an MCP server URL carries no ?query or #fragment (no key on a "
                        "card line; the owner adds a key himself)")
    if not p.netloc.isascii() or not url.isascii():
        return _refused("an MCP server URL is plain ASCII (no look-alike host)")
    from core.tool_gates import REMEDY_ACTORS
    a = REMEDY_ACTORS["no-mcp-server"]
    return SettingProposal(CARD, words=["/mcp", "add", sid, url],
                           change=f"Add the MCP server '{sid}' at {url} (host: {host}).",
                           risk=MCP_RISK, who=a.who, when=a.when)


def _exposure(key: str) -> bool:
    name = str(key or "").upper()
    return (name in _EXPOSURE_EXACT or name.endswith(_EXPOSURE_SUFFIXES)
            or name.startswith(_EXPOSURE_PREFIXES))


def _never_proposed(key: str) -> Optional[str]:
    from core.config_policy.live_apply import never_live
    if never_live(key) or _exposure(key):
        return (f"'{key}' is a money, approval, ingress, exposure or trust setting — the "
                f"agent never proposes it; the owner sets it himself")
    return None


def _flag_refusal(key: str, value: str) -> Optional[str]:
    """Why the agent may never propose *key* = *value* (a flag), or None: an
    unknown key, a secret, a money/approval/ingress/exposure/trust flag, or a
    raise-only safety stop being cleared."""
    from core import config_service
    try:
        info = config_service.describe(key)
    except KeyError:
        return config_service.unknown_key_message(key)
    if getattr(info, "secret", False):
        return (f"'{key}' holds a secret — it is never on a card or in a message; the "
                f"operator sets it on the host")
    stop = _never_proposed(key)
    if stop:
        return stop
    if key in config_service._RAISE_ONLY_FLAGS and config_service.remote_write_refused(
            key, value):
        return (f"'{key}' is a safety stop — the agent may propose raising it, never "
                f"clearing it; the owner resumes with /resume")
    return None


def _overrides_refusal(value: str) -> Optional[str]:
    """093 review: ``POLYROB_OVERRIDES`` is a LIST of member flags; each member
    passes the same checks as a direct key, so the list cannot carry a money,
    approval or secret flag past them."""
    entries = [e.strip() for e in str(value or "").split(",")]
    if not any(entries):
        return "POLYROB_OVERRIDES needs NAME=value entries"
    for entry in entries:
        name, sep, val = entry.partition("=")
        name, val = name.strip(), val.strip()
        if not entry or not sep or not _KEY.match(name) or not val:
            return f"{entry!r} is not a NAME=value entry"
        why = _flag_refusal(name, val)
        if why:
            return why
    return None


def _config(words: List[str]) -> SettingProposal:
    if len(words) < 4:
        return _refused("usage: /config set <key> <value>")
    key, value = words[2], " ".join(words[3:])
    if not _KEY.match(key):
        return _refused(f"{key!r} is not a setting key")
    from core import config_service
    from core.prefs import PREF_SCHEMA
    if key in PREF_SCHEMA:
        spec = PREF_SCHEMA[key]
        if key.split(".", 1)[0] == "budget":
            return _refused(f"'{key}' is a money bound — the agent never proposes it")
        if getattr(spec, "sensitivity", "") != "safe":
            return _refused(f"'{key}' is a guarded preference — the owner sets it with "
                            f"/config set and approves it on /pending")
        return SettingProposal(CARD, words=["/config", "set", key, *words[3:]],
                               change=f"Set the preference {key} = {value}.",
                               risk=CONFIG_RISK, who="owner-verb",
                               when=_applies_word(spec.applies))
    stop = _flag_refusal(key, value)
    if stop:
        return _refused(stop)
    if key == "POLYROB_OVERRIDES":
        stop = _overrides_refusal(value)
        if stop:
            return _refused(stop)
    info = config_service.describe(key)
    if config_service.remote_write_refused(key, value):
        from core.config_policy.members import set_line
        env_line = set_line(key, value)
        return SettingProposal(OPERATOR, env_line=env_line,
                               change=f"Set {env_line} in the operator's env file.",
                               who="operator-env", when="restart")
    return SettingProposal(CARD, words=["/config", "set", key, *words[3:]],
                           change=f"Set {key} = {value}.", risk=CONFIG_RISK,
                           who="owner-verb", when=_applies_word(info.applies))


def _grant(words: List[str]) -> SettingProposal:
    verb = words[0]
    if verb == "/goal":
        ok = len(words) == 4 and words[1] == "grant"
        ref, opts = (words[2], words[3:]) if ok else ("", [])
    else:
        ok = len(words) == 5 and words[1] == "edit" and words[3] == "grant"
        ref, opts = (words[2], words[4:]) if ok else ("", [])
    if not ok or not _REF.match(ref) or len(opts) != 1 or not opts[0].startswith("tools="):
        return _refused("a grant card carries exactly /goal grant <id> tools=<a,b> or "
                        "/job edit <id> grant tools=<a,b> (no rig, target or skills)")
    tools = [t.strip() for t in opts[0][len("tools="):].split(",") if t.strip()]
    if not tools:
        return _refused("name at least one tool")
    from core.tool_capabilities import TOOL_CAPABILITIES
    unknown = [t for t in tools if t not in TOOL_CAPABILITIES]
    if unknown:
        return _refused(f"not a known tool id: {', '.join(unknown)}")
    money = [t for t in tools if "money" in TOOL_CAPABILITIES[t]]
    if money:
        return _refused(f"{', '.join(money)} is a money tool — never on a setting card; "
                        f"a money move is propose_action, a money grant is the owner's own")
    # 093 review: a tool that writes the agent's OWN environment (self_env) is
    # never on a card; an exec tool gets the risk it really carries.
    self_writing = [t for t in tools if "writes_self" in TOOL_CAPABILITIES[t]]
    if self_writing:
        return _refused(f"{', '.join(self_writing)} changes the agent's own environment — "
                        f"never on a setting card; the owner grants it himself")
    risk = GRANT_RISK
    exec_tools = [t for t in tools if "exec" in TOOL_CAPABILITIES[t]]
    if exec_tools:
        risk = f"{GRANT_RISK} {EXEC_RISK.format(tools=', '.join(exec_tools))}"
    what = "goal" if verb == "/goal" else "scheduled job"
    from core.tool_gates import REMEDY_ACTORS
    a = REMEDY_ACTORS["autonomous-ceiling"]
    return SettingProposal(CARD, words=list(words),
                           change=f"Grant the tools {', '.join(tools)} to the {what} {ref}.",
                           risk=risk, who=a.who, when=a.when)


def grant_parts(p: SettingProposal) -> Optional[tuple]:
    """``(verb, ref, tools)`` of a grant card, else None."""
    w = p.words
    if p.outcome != CARD or not w or w[0] not in ("/goal", "/job"):
        return None
    opt = w[-1]
    if not opt.startswith("tools="):
        return None
    return w[0], w[2], [t for t in opt[len("tools="):].split(",") if t.strip()]


def with_current_tools(p: SettingProposal, current: Optional[List[str]]) -> SettingProposal:
    """A grant line REPLACES the goal's or job's tool list (``/goal grant``,
    ``/job edit … grant``): the card says so, with the list it replaces.
    ``current`` None = it could not be read, and the card says that."""
    parts = grant_parts(p)
    if parts is None:
        return p
    verb, ref, tools = parts
    what = "goal" if verb == "/goal" else "scheduled job"
    if current is None:
        old = "its current tools could not be read"
    elif current:
        old = "replaces its current tools: " + ", ".join(current)
    else:
        old = "it carries no tools now"
    change = (f"Set the tools of the {what} {ref} to {', '.join(tools)} — this "
              f"{old}." if current else
              f"Set the tools of the {what} {ref} to {', '.join(tools)} ({old}).")
    return replace(p, change=change)


def classify(command: str) -> SettingProposal:
    """The ONE decision: card, operator, or refused (see the module doc)."""
    words = str(command or "").split()
    if not words:
        return _refused("no command")
    words[0] = words[0].lower()
    if len(words) > 1 and words[0] in ("/mcp", "/config", "/goal", "/job"):
        words[1] = words[1].lower()
    head = tuple(words[:2])
    if head == ("/mcp", "add"):
        return _mcp(words)
    if head == ("/config", "set"):
        return _config(words)
    if head in (("/goal", "grant"), ("/job", "edit")):
        return _grant(words)
    return _refused("propose_setting carries only: " + "; ".join(SHAPES)
                    + ". A money move is propose_action.")


__all__ = ["CARD", "OPERATOR", "REFUSED", "SHAPES", "MCP_RISK", "EXEC_RISK", "SettingProposal",
           "classify", "grant_parts", "with_current_tools"]
