"""registry.py — slash-command registry primitives for the POLYROB CLI.

The ``Command``/``CommandRegistry`` model, the ``CommandContext`` handed to every
handler, the ``SlashCompleter`` + ``build_completer``, and the ``ReplExit`` signal.
The handler functions live in ``handlers.py``; the public surface is re-exported
from the package ``__init__`` so ``from cli.ui.commands import X`` is unchanged
(D6 — the 1135-line god-file split).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Union,
)


class ReplExit(Exception):
    """Raised by ``/exit`` (or ``/quit``) to signal the REPL should stop."""


# ---------------------------------------------------------------------------
# Command context (handed to every handler — no globals)
# ---------------------------------------------------------------------------


@dataclass
class CommandContext:
    """Everything a slash-command handler can need.

    All fields are optional so handlers can be unit-tested with a partial stub
    (a fake state / renderer / conversation is enough for most commands).

    Attributes:
        renderer:     The active ``Renderer`` (its ``.console`` is used for Rich
                      output; ``print_block`` is the plain fallback).
        state:        The shared ``SessionState`` (live tokens/cost/ctx).
        conversation: The ``Conversation`` (``.agent``, ``.turns``).
        container:    The DI container (``get_service``).
        task_agent:   The ``TaskAgent`` (``.session_manager``, ``.get_orchestrator``).
        orchestrator: The session's orchestrator (holds ``usage_tracker``).
        session_id:   The active session id.
        user_id:      The resolving user id.
        args:         Parsed argument list (set by ``dispatch`` per invocation).
        raw:          The raw command line (after the leading ``/``).
    """

    renderer: Any = None
    state: Any = None
    conversation: Any = None
    container: Any = None
    task_agent: Any = None
    orchestrator: Any = None
    session_id: str = ""
    user_id: str = "local"
    args: List[str] = field(default_factory=list)
    raw: str = ""
    registry: Optional["CommandRegistry"] = None

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def agent(self) -> Any:
        """The agent behind the conversation (or ``None``)."""
        return getattr(self.conversation, "agent", None)

    @property
    def message_manager(self) -> Any:
        """The agent's message_manager (or ``None``)."""
        agent = self.agent
        return getattr(agent, "message_manager", None) if agent is not None else None

    def emit(self, text: str, *, title: str = "", style: str = "") -> None:
        """Print a block of text through the renderer (rich or plain).

        SECURITY (P1 finalization): slash-command output (e.g. ``/memory search``,
        ``/export``) can echo tool results or stored content that contains
        credential shapes. Scrub here — this is the single choke point for command
        output, mirroring the tool-trace renderer's own scrub.
        """
        try:
            from cli.ui.secrets import scrub_secrets
            text = scrub_secrets(text)
        except Exception:
            pass  # never let scrubbing failure suppress output
        renderer = self.renderer
        if renderer is None:
            print(text)
            return
        try:
            renderer.print_block(text, title=title, style=style)
        except Exception:
            print(text)

    def console(self) -> Any:
        """Return the renderer's Rich console, or ``None`` for the plain path."""
        renderer = self.renderer
        return getattr(renderer, "console", None) if renderer is not None else None


# Handler signature: sync (-> None) or async (-> Awaitable[None]).
HandlerResult = Union[None, Awaitable[None]]
Handler = Callable[[CommandContext], HandlerResult]


# ---------------------------------------------------------------------------
# Command + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """A single slash command.

    Attributes:
        name:    Canonical name (no leading slash), e.g. ``"status"``.
        handler: The handler callable (sync or async).
        help:    One-line help text shown by ``/help``.
        aliases: Alternative invocation names (no leading slash).
        usage:   Optional argument-usage hint shown in ``/help``.
    """

    name: str
    handler: Handler
    help: str = ""
    aliases: tuple = ()
    usage: str = ""


class CommandRegistry:
    """Registry of slash commands, keyed by canonical name + aliases."""

    def __init__(self) -> None:
        self._commands: Dict[str, Command] = {}
        self._aliases: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Registration / lookup
    # ------------------------------------------------------------------

    def register(self, command: Command) -> None:
        """Register *command* under its name and all aliases.

        Raises ``ValueError`` on a duplicate name/alias collision so a typo
        can't silently shadow an existing command.
        """
        if command.name in self._commands or command.name in self._aliases:
            raise ValueError(f"Duplicate command name: {command.name!r}")
        self._commands[command.name] = command
        for alias in command.aliases:
            if alias in self._commands or alias in self._aliases:
                raise ValueError(f"Duplicate command alias: {alias!r}")
            self._aliases[alias] = command.name

    def lookup(self, name: str) -> Optional[Command]:
        """Return the ``Command`` for *name* (or alias), or ``None``."""
        key = name.lstrip("/").lower()
        if key in self._commands:
            return self._commands[key]
        canonical = self._aliases.get(key)
        if canonical is not None:
            return self._commands.get(canonical)
        return None

    def __contains__(self, name: str) -> bool:
        return self.lookup(name) is not None

    def commands(self) -> List[Command]:
        """All registered commands (canonical, de-duplicated, name-sorted)."""
        return sorted(self._commands.values(), key=lambda c: c.name)

    def names(self) -> List[str]:
        """All invokable names (canonical + aliases), sorted — for completion."""
        return sorted(set(self._commands) | set(self._aliases))

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, line: str, ctx: CommandContext) -> bool:
        """Dispatch a raw input *line* (starting with ``/``) to its handler.

        Returns ``True`` when the line was a slash command (handled, even if
        unknown — an unknown command prints a hint and is still "handled" so
        the REPL doesn't treat it as a turn).  Returns ``False`` for non-slash
        input so the caller routes it to the conversation.

        ``/exit`` / ``/quit`` raise ``ReplExit``.
        """
        if not line.startswith("/"):
            return False

        body = line[1:].strip()
        parts = body.split()
        name = parts[0].lower() if parts else ""
        ctx.args = parts[1:]
        ctx.raw = body

        command = self.lookup(name)
        if command is None:
            ctx.emit(f"Unknown command: /{name} — type /help")
            return True

        # F5: a crashing slash handler must NOT propagate out of dispatch and
        # tear down the REPL.  ReplExit is control flow (/exit) and must escape;
        # everything else becomes a styled "command error" line and the loop
        # continues (the command is still "handled" → not routed as a turn).
        try:
            result = command.handler(ctx)
            if _is_awaitable(result):
                await result
        except ReplExit:
            raise
        except Exception as exc:
            import click

            click.echo(
                click.style(f"[polyrob] command error: /{name}: {exc}", fg="red")
            )
        return True


def _is_awaitable(obj: Any) -> bool:
    import inspect

    return inspect.isawaitable(obj)


# ---------------------------------------------------------------------------
# Completer
# ---------------------------------------------------------------------------


class SlashCompleter:
    """prompt_toolkit ``Completer`` that completes ``/command`` names.

    Only fires when the buffer starts with ``/`` and has no space yet (i.e. the
    user is still typing the command name, not its arguments).  Optionally
    completes ``/resume <id>`` session-id prefixes when a ``sessions_provider``
    is supplied (cheap, best-effort).

    Subclasses ``prompt_toolkit.completion.Completer`` lazily so importing this
    module never hard-requires prompt_toolkit (the plain path doesn't need it).
    """

    def __init__(
        self,
        registry: CommandRegistry,
        *,
        sessions_provider: Optional[Callable[[], Iterable[str]]] = None,
    ) -> None:
        self._registry = registry
        self._sessions_provider = sessions_provider

    def get_completions(self, document: Any, complete_event: Any = None):
        """Yield ``Completion`` objects for the current document."""
        from prompt_toolkit.completion import Completion

        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        # Argument completion (buffer has a space → user is typing arguments).
        # Route on the RESOLVED command so aliases (e.g. `/resume` → `replay`)
        # still complete.  Every branch is best-effort / fail-open — the completer
        # runs on every keystroke, so a backend hiccup must yield nothing, never raise.
        if " " in text:
            head, _, tail = text.partition(" ")
            cmd = self._registry.lookup(head)
            if cmd is None:
                return
            if cmd.name == "replay" and self._sessions_provider:
                yield from self._complete_session_ids(tail.strip(), Completion)
            elif cmd.name == "model":
                yield from self._complete_model(tail, Completion)
            elif cmd.name == "toolset":
                yield from self._complete_toolset(tail, Completion)
            elif cmd.name == "persona":
                yield from self._complete_persona(tail, Completion)
            elif cmd.name == "config":
                yield from self._complete_config(tail, Completion)
            return

        # Command-name completion: complete against names + aliases. An alias
        # row shows an honest pointer (``→ /canonical``) instead of repeating
        # the canonical command's help — otherwise /compact and /compress read
        # as two identical commands in the menu.
        word = text[1:].lower()
        for name in self._registry.names():
            if name.startswith(word):
                cmd = self._registry.lookup(name)
                if cmd is None:
                    meta = ""
                elif cmd.name != name:
                    meta = f"→ /{cmd.name}"
                else:
                    meta = cmd.help
                yield Completion(
                    name,
                    start_position=-len(word),
                    display=f"/{name}",
                    display_meta=meta,
                )

    _CONFIG_SUBS = ("list", "get", "set", "explain", "search", "check")

    def _complete_config(self, tail: str, Completion: Any):
        """`/config <sub>`, then key completion for get/set/explain, then
        enum/bool value completion for set (018 P2a). Fail-open — the config
        service is a registry read, but any hiccup must yield nothing."""
        try:
            parts = tail.split(" ")
            if len(parts) <= 1:
                prefix = (parts[0] if parts else "").lower()
                for sub in self._CONFIG_SUBS:
                    if sub.startswith(prefix):
                        yield Completion(sub, start_position=-len(prefix),
                                         display=sub)
                return
            sub = parts[0].lower()
            if sub not in ("get", "set", "explain"):
                return
            if len(parts) == 2:
                prefix = parts[1]
                from core.config_service import known_keys
                shown = 0
                for key in known_keys():
                    if key.startswith(prefix):
                        yield Completion(key, start_position=-len(prefix),
                                         display=key)
                        shown += 1
                        if shown >= 40:
                            return
                return
            if sub == "set" and len(parts) == 3:
                key, prefix = parts[1], parts[2].lower()
                for val in self._config_value_candidates(key):
                    if val.startswith(prefix):
                        yield Completion(val, start_position=-len(parts[2]),
                                         display=val)
        except Exception:
            return

    @staticmethod
    def _config_value_candidates(key: str):
        try:
            from core.prefs import PREF_SCHEMA
            spec = PREF_SCHEMA.get(key)
            if spec is not None:
                if spec.type == "bool":
                    return ("on", "off", "true", "false")
                if spec.type == "enum":
                    return tuple(spec.enum_values)
                return ()
            from core.flags import REGISTRY
            flag = REGISTRY.get(key)
            if flag is not None and flag.kind == "bool":
                return ("on", "off", "true", "false")
        except Exception:
            pass
        return ()

    def _complete_session_ids(self, prefix: str, Completion: Any):
        try:
            ids = list(self._sessions_provider() or [])
        except Exception:
            return
        for sid in ids:
            sid = str(sid)
            if sid.startswith(prefix):
                yield Completion(sid, start_position=-len(prefix), display=sid[:16])

    def _complete_model(self, tail: str, Completion: Any):
        """Complete `/model <provider>` then `/model <provider> <model>`.

        First argument → distinct provider names; once a provider + space is
        typed, the models registered for that provider.  Source of truth:
        ``modules.llm.available_models.available_models()`` (``ModelChoice`` rows).
        Fail-open: any backend error yields nothing.
        """
        try:
            from modules.llm.available_models import available_models

            choices = list(available_models() or [])
        except Exception:
            return

        if " " in tail:
            # Second argument: complete this provider's model names.
            provider, _, partial = tail.partition(" ")
            provider = provider.strip().lower()
            seen: set = set()
            for ch in choices:
                if str(getattr(ch, "provider", "")).lower() != provider:
                    continue
                model = str(getattr(ch, "model", ""))
                if not model or model in seen or not model.lower().startswith(partial.lower()):
                    continue
                seen.add(model)
                yield Completion(
                    model,
                    start_position=-len(partial),
                    display=model,
                    display_meta=str(getattr(ch, "display_name", "") or ""),
                )
            return

        # First argument: distinct provider names.
        partial = tail
        seen = set()
        for ch in choices:
            provider = str(getattr(ch, "provider", ""))
            if not provider or provider in seen or not provider.lower().startswith(partial.lower()):
                continue
            seen.add(provider)
            yield Completion(
                provider,
                start_position=-len(partial),
                display=provider,
                display_meta="provider",
            )

    def _complete_toolset(self, tail: str, Completion: Any):
        """Complete `/toolset <name>` against the named toolsets (single arg).

        Source: ``agents.task.tool_defaults.TOOLSETS`` — the same dict
        ``_h_toolset`` resolves against.  Fail-open.
        """
        if " " in tail:  # single-arg command — nothing past the name
            return
        try:
            from agents.task.tool_defaults import TOOLSETS

            names = sorted(TOOLSETS.keys())
        except Exception:
            return
        partial = tail
        for name in names:
            if name.startswith(partial.lower()):
                yield Completion(
                    name,
                    start_position=-len(partial),
                    display=name,
                    display_meta="toolset",
                )

    def _complete_persona(self, tail: str, Completion: Any):
        """Complete `/persona <name>` against the available personas (single arg).

        Source: ``cli.ui.commands.handlers._list_persona_names()`` — exactly what
        ``_h_persona`` lists.  Fail-open (returns nothing if no source is found).
        """
        if " " in tail:  # single-arg command — nothing past the name
            return
        try:
            from cli.ui.commands.handlers import _list_persona_names

            names = list(_list_persona_names() or [])
        except Exception:
            return
        partial = tail
        for name in names:
            if str(name).startswith(partial.lower()):
                yield Completion(
                    str(name),
                    start_position=-len(partial),
                    display=str(name),
                    display_meta="persona",
                )


def build_completer(
    registry: CommandRegistry,
    *,
    sessions_provider: Optional[Callable[[], Iterable[str]]] = None,
) -> Any:
    """Build a concrete prompt_toolkit ``Completer`` from the registry.

    Returns an object whose class actually subclasses
    ``prompt_toolkit.completion.Completer`` (prompt_toolkit type-checks the
    completer at session construction).  Importing prompt_toolkit is deferred
    here so the plain / headless path never requires it.
    """
    from prompt_toolkit.completion import Completer

    inner = SlashCompleter(registry, sessions_provider=sessions_provider)

    class _PTCompleter(Completer):
        def get_completions(self, document, complete_event):
            yield from inner.get_completions(document, complete_event)

    return _PTCompleter()


# ===========================================================================
# Handlers
# ===========================================================================
