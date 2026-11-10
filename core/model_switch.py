"""094: the ``/model`` grammar, the alias table, the reasoning levels and the
price guard — the pure half of the hot model switch.

Every seat (Telegram, console, REPL) parses ``/model`` here and resolves a name
here; the switch itself (validate against the catalog, store the pin, swap at the
next step) is ``agents/task/model_switch_service.py``. Nothing in this module does
I/O except :func:`resolve_name`, which reads the owner's aliases through the
caller-supplied mapping and the operator pins through ``core.runtime_config``.

Grammar (any order after the name)::

    /model                                  show the live model and where it comes from
    /model <name>                           this chat, from the next step (until /new)
    /model <name> default                   also the default for every new owner chat
    /model <name> reasoning=<level>         with a reasoning level (off|on|low|medium|high|<tokens>)
    /model reasoning=<level>                only the reasoning level, same model
    /model <name> go                        confirm a switch the price guard quoted
    /model reset [default]                  back to the default (and clear the default)
    /model aliases                          list the aliases
    /model alias <name> <provider>/<model>  add an owner alias (`/model alias <name> -` removes it)

``<name>`` is an alias, ``<provider>/<model>`` (``openrouter/anthropic/claude-sonnet-5.5``),
a bare model id on the operator's provider (``anthropic/claude-sonnet-5.5`` on
OpenRouter), or ``<provider> <model>`` (two words).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

#: The major models, one short name each: ``<provider>/<the id that provider
#: serves>`` (the same grammar the owner types). OpenRouter carries every family;
#: a native provider only its own.
#: Ids age: the catalog check in the switch refuses a stale one with the nearest
#: live ids, so a stale row fails loudly, never silently.
BUILTIN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "deepseek": ("openrouter/deepseek/deepseek-v4.1-flash", "deepseek/deepseek-flash"),
    "deepseek-pro": ("openrouter/deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
    "glm": ("openrouter/z-ai/glm-5.3",),
    "glm-flash": ("openrouter/z-ai/glm-5.3-flash",),
    "glm-prime": ("openrouter/z-ai/glm-5.3-prime",),
    "opus": ("openrouter/anthropic/claude-opus-5.5", "anthropic/claude-opus-5-5"),
    "sonnet": ("openrouter/anthropic/claude-sonnet-5.5", "anthropic/claude-sonnet-5-5"),
    "haiku": ("openrouter/anthropic/claude-haiku-5.5", "anthropic/claude-haiku-5-5"),
    "fable": ("openrouter/anthropic/claude-fable-5.1", "anthropic/claude-fable-5-1"),
    "gpt": ("openrouter/openai/gpt-5.5", "openai/gpt-5.5"),
    "gpt-mini": ("openrouter/openai/gpt-5.4-mini", "openai/gpt-5.4-mini"),
    "gemini": ("openrouter/google/gemini-3.1-pro-preview", "gemini/gemini-3.1-pro-preview"),
    "gemini-flash": ("openrouter/google/gemini-3.8-flash", "gemini/gemini-3.8-flash"),
    "grok": ("openrouter/x-ai/grok-4.7",),
    "kimi": ("openrouter/moonshotai/kimi-k3",),
    "qwen": ("openrouter/qwen/qwen3.7-max",),
    "minimax": ("openrouter/minimax/minimax-m3",),
    "mistral": ("openrouter/mistralai/mistral-large-4-0",),
    "llama": ("openrouter/meta-llama/llama-4-maverick",),
}

#: Providers whose model ids are ``vendor/model`` (a vendor name is not a provider there).
AGGREGATORS = ("openrouter",)

#: Names that resolve through the operator's env, not the table.
SYMBOLIC_ALIASES = ("default", "heavy")

REASONING_LEVELS = ("off", "on", "low", "medium", "high")
#: The most reasoning tokens a level may ask for.
MAX_REASONING_TOKENS = 64000

_ALIAS_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")
_CONFIRM_WORDS = ("go", "confirm", "execute")


@dataclass
class ModelCommand:
    """One parsed ``/model`` line."""
    action: str                      # show|set|reset|aliases|alias|reasoning|error
    name: str = ""                   # the typed name (alias, id, provider/model)
    provider: str = ""               # an explicit provider (two-word form)
    default: bool = False            # also the owner default
    reasoning: Optional[str] = None  # a validated level, or None = unchanged
    confirm: bool = False            # the owner said `go`
    alias_target: str = ""           # for `alias <name> <target>`
    error: str = ""
    words: List[str] = field(default_factory=list)


def normalize_reasoning(raw: str) -> Optional[str]:
    """A valid reasoning level (``off``…``high`` or a token count), else None."""
    value = str(raw or "").strip().lower()
    if value in REASONING_LEVELS:
        return value
    if value.isdigit() and 0 < int(value) <= MAX_REASONING_TOKENS:
        return str(int(value))
    return None


def parse_model_args(words: Sequence[str]) -> ModelCommand:
    """Parse the words after ``/model``. Never raises; a bad line is ``action='error'``."""
    ws = [str(w).strip() for w in (words or []) if str(w).strip()]
    cmd = ModelCommand(action="show", words=list(ws))
    if not ws:
        return cmd
    head = ws[0].lower()
    if head == "aliases" and len(ws) == 1:
        cmd.action = "aliases"
        return cmd
    if head == "alias":
        if len(ws) != 3:
            return _err(cmd, "Usage: /model alias <name> <provider>/<model>  (or `-` to remove it)")
        name = ws[1].lower()
        if not set(name) <= _ALIAS_CHARS or name in BUILTIN_ALIASES or name in SYMBOLIC_ALIASES:
            return _err(cmd, f"'{ws[1]}' cannot be an alias name (letters, digits, - _ . and "
                             "not a built-in name).")
        cmd.action, cmd.name, cmd.alias_target = "alias", name, ws[2]
        return cmd
    rest: List[str] = []
    for w in ws:
        lw = w.lower()
        if lw in _CONFIRM_WORDS:
            cmd.confirm = True
        elif lw == "default":
            cmd.default = True
        elif lw.startswith("reasoning="):
            level = normalize_reasoning(lw.split("=", 1)[1])
            if level is None:
                return _err(cmd, f"Unknown reasoning level '{w.split('=', 1)[1]}'. Use "
                                 f"{'|'.join(REASONING_LEVELS)} or a token count "
                                 f"(1-{MAX_REASONING_TOKENS}).")
            cmd.reasoning = level
        else:
            rest.append(w)
    # `default` alone is the symbolic alias; `reset` is its own verb.
    if not rest and cmd.default and cmd.reasoning is None:
        rest, cmd.default = ["default"], False
    if rest and rest[0].lower() == "reset":
        if len(rest) > 1:
            return _err(cmd, "Usage: /model reset [default]")
        cmd.action = "reset"
        return cmd
    if not rest:
        if cmd.reasoning is not None:
            cmd.action = "reasoning"
            return cmd
        return _err(cmd, "Usage: /model <name> [default] [reasoning=<level>]")
    if len(rest) == 1:
        cmd.action, cmd.name = "set", rest[0]
        return cmd
    if len(rest) == 2 and "/" not in rest[0]:
        cmd.action, cmd.provider, cmd.name = "set", rest[0].lower(), rest[1]
        return cmd
    return _err(cmd, "Usage: /model <name> | /model <provider> <model> | /model <provider>/<model>")


def _err(cmd: ModelCommand, text: str) -> ModelCommand:
    cmd.action, cmd.error = "error", text
    return cmd


@dataclass
class Resolved:
    """A name resolved to a ``(provider, model)`` pair, or a refusal."""
    provider: str = ""
    model: str = ""
    via: str = ""        # "alias sonnet" | "your alias fav" | "id" | "DEFAULT_MODEL" | ...
    error: str = ""


def alias_row(name: str) -> Dict[str, str]:
    """``{provider: model id}`` for one built-in alias."""
    return dict(entry.split("/", 1) for entry in BUILTIN_ALIASES.get(name, ()))


def _split_provider(name: str, known: Sequence[str]) -> Tuple[str, str]:
    """``("openrouter", "anthropic/x")`` for ``openrouter/anthropic/x`` when the
    first segment is a known provider; else ``("", name)``."""
    head, _, tail = name.partition("/")
    if tail and head.lower() in {k.lower() for k in known}:
        return head.lower(), tail
    return "", name


def resolve_name(cmd_name: str, *, explicit_provider: str = "",
                 operator_provider: str = "", keyed_providers: Sequence[str] = (),
                 known_providers: Sequence[str] = (),
                 owner_aliases: Optional[Mapping[str, str]] = None,
                 env=None) -> Resolved:
    """Resolve the typed name to ``(provider, model)``.

    Order: an owner alias > a built-in alias (on the operator's provider when it
    serves the family, else the first keyed provider that does) > a symbolic alias
    (``default``/``heavy`` from the operator env) > ``provider/model`` > a bare id on
    the operator's provider. An alias that names nothing on a keyed provider is
    refused — never a guess."""
    name = str(cmd_name or "").strip()
    low = name.lower()
    op = (operator_provider or "").lower()
    keyed = [p.lower() for p in keyed_providers]
    if explicit_provider:
        return Resolved(explicit_provider.lower(), name, "id")
    aliases = {str(k).lower(): str(v) for k, v in (owner_aliases or {}).items()}
    if low in aliases:
        prov, model = _split_provider(aliases[low], known_providers)
        return Resolved(prov or op, model, f"your alias {low}")
    if low in BUILTIN_ALIASES:
        row = alias_row(low)
        order = ([op] if op else []) + [p for p in keyed if p != op]
        for prov in order:
            if prov in row:
                return Resolved(prov, row[prov], f"alias {low}")
        served = ", ".join(sorted(row))
        return Resolved(error=f"'{low}' is served by {served}; no key for those here.")
    if low in SYMBOLIC_ALIASES:
        from core.runtime_config import heavy_model_pin, operator_model_pin
        model = operator_model_pin(env) if low == "default" else heavy_model_pin(env)
        if not model:
            flag = "DEFAULT_MODEL" if low == "default" else "HEAVY_MODEL"
            return Resolved(error=f"'{low}' needs {flag} in the operator env; it is not set.")
        return Resolved(op, model, "DEFAULT_MODEL" if low == "default" else "HEAVY_MODEL")
    # On an aggregator (OpenRouter) a one-slash name is the aggregator's own id —
    # `anthropic/claude-sonnet-5.5` — not provider `anthropic`; the native provider
    # is the two-word form (`/model anthropic claude-sonnet-5-5`).
    if op in AGGREGATORS and name.count("/") == 1 and not name.lower().startswith(op + "/"):
        return Resolved(op, name, "id")
    prov, model = _split_provider(name, known_providers)
    if prov:
        return Resolved(prov, model, "id")
    if not op:
        return Resolved(error="No provider is pinned: write /model <provider>/<model>.")
    return Resolved(op, name, "id")


def fmt_price(price: Optional[Tuple[float, float]], cached: Optional[float] = None) -> str:
    if not price:
        return "price unknown"
    p_in, p_out = price
    mid = f" / ${cached:g} cached" if cached is not None else ""
    return f"${p_in:g} in{mid} / ${p_out:g} out per M"


def alias_lines(owner_aliases: Optional[Mapping[str, str]] = None,
                read_error: str = "") -> List[str]:
    lines = ["Built-in aliases (the id the provider serves):"]
    for name in BUILTIN_ALIASES:
        ids = ", ".join(f"{p}: {m}" for p, m in alias_row(name).items())
        lines.append(f"  {name} — {ids}")
    lines.append("  default — DEFAULT_MODEL · heavy — HEAVY_MODEL (the operator env)")
    if read_error:  # an unreadable store is not an empty one
        lines.append(f"⚠️ Your aliases could not be read: {read_error}")
    elif owner_aliases:
        lines.append("Your aliases:")
        lines += [f"  {k} — {v}" for k, v in sorted(owner_aliases.items())]
    lines.append("Add one: /model alias <name> <provider>/<model>")
    return lines
