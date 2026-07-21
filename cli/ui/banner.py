"""banner.py — the first-run session banner for the POLYROB CLI.

Two quiet lines printed once per process at session start on BOTH surfaces
(REPL + one-shot ``polyrob run``).  The banner is context, not content — it must
never compete visually with the first message of the conversation:

    ● polyrob v0.8.1 · gemini-2.5-flash (gemini)
      session 7a95c9a2 · tools filesystem, task · /help for commands

Design constraints (mirrors ``blocks.py``):
- Pure builders: ``banner_panel`` returns a Rich renderable; ``banner_plain``
  returns deterministic plain text.  No I/O, no Console — snapshot-testable.
- ``provider_key_status`` reads configuration and returns provider NAMES only,
  never secret values (kept for ``rob model list``-style callers; the banner
  itself no longer displays key status).
- ``print_banner`` is the one I/O entry point: it picks Rich vs plain based on
  the renderer type and prints once.
"""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

from rich.text import Text

from cli.ui.theme import ICONS, style


def provider_key_status(config: Any) -> List[str]:
    """Return the NAMES of providers that have an API key configured (Seam 1).

    Never returns key values — only provider names, in canonical PROFILES order.
    Delegates to ``config.available_providers()`` (the SSOT wrapper); for a config
    stand-in that lacks it, falls back to scanning ``*_api_key`` attributes in the
    same canonical order. A missing attribute is treated as "no key".
    """
    fn = getattr(config, "available_providers", None)
    if callable(fn):
        try:
            return list(fn())
        except Exception:
            pass
    from modules.llm.profiles import PROFILES
    ready: List[str] = []
    for name in PROFILES:
        try:
            if getattr(config, f"{name}_api_key", None):
                ready.append(name)
        except Exception:
            continue
    return ready


def _short_session(session_id: str) -> str:
    """First 8 chars of a session id (or the whole thing if shorter)."""
    sid = str(session_id or "")
    return sid[:8] if sid else "—"


def _tools_str(tool_ids: Sequence[str]) -> str:
    return ", ".join(str(t) for t in tool_ids) if tool_ids else "—"


def _keys_str(providers_with_keys: Sequence[str]) -> str:
    return ", ".join(providers_with_keys) if providers_with_keys else "none"


def _session_info_str(
    *,
    framework: str = "",
    instance_id: str = "",
    user_id: str = "",
    memory_backend: str = "",
    autonomy_on: Optional[bool] = None,
) -> str:
    """Build the OPT-IN third banner line, or "" when no session info is supplied.

    Surfaces the polyrob-framework / instance distinction plus the headline
    session params (user, memory backend, autonomy). Returns "" when neither a
    framework nor an instance is given, so the banner stays the quiet two-liner
    (the default, byte-equivalent to before).
    """
    if not (framework or instance_id):
        return ""
    parts: List[str] = []
    if framework:
        parts.append(str(framework))
    if instance_id:
        parts.append(f"instance {instance_id}")
    if user_id:
        parts.append(f"user {user_id}")
    if memory_backend:
        parts.append(f"memory {memory_backend}")
    if autonomy_on is not None:
        parts.append(f"autonomy {'on' if autonomy_on else 'off'}")
    return f" {ICONS.bullet} ".join(parts)


def banner_panel(
    *,
    version: str,
    model: str,
    provider: str,
    tool_ids: Sequence[str],
    session_id: str,
    providers_with_keys: Sequence[str] = (),
    show_help_hint: bool = False,
    framework: str = "",
    instance_id: str = "",
    user_id: str = "",
    memory_backend: str = "",
    autonomy_on: Optional[bool] = None,
) -> Text:
    """Build the Rich first-run banner: two quiet lines (+ optional third).

    The banner must never compete with the first message — one identity line
    (``● polyrob v0.8.1 · model (provider)``) and one dim context line (session,
    tools, optional ``/help`` hint).  When session-info kwargs (``framework`` /
    ``instance_id`` / …) are supplied, an OPT-IN dim third line surfaces the
    polyrob-framework / instance distinction + user / memory / autonomy.
    ``providers_with_keys`` is accepted for backwards compatibility but no longer
    displayed (key status lives in ``polyrob model list``).
    """
    body = Text()
    body.append(f"{ICONS.speaker} ", style=style("speaker_dot"))
    body.append(f"polyrob v{version}", style=style("speaker_name"))
    body.append(f" {ICONS.bullet} {model} ({provider})\n", style=style("meta"))
    second = f"  session {_short_session(session_id)} {ICONS.bullet} tools {_tools_str(tool_ids)}"
    if show_help_hint:
        second += f" {ICONS.bullet} /help {ICONS.bullet} /session"
    body.append(second, style=style("meta"))
    info = _session_info_str(
        framework=framework, instance_id=instance_id, user_id=user_id,
        memory_backend=memory_backend, autonomy_on=autonomy_on,
    )
    if info:
        body.append(f"\n  {info}", style=style("meta"))
    return body


def banner_plain(
    *,
    version: str,
    model: str,
    provider: str,
    tool_ids: Sequence[str],
    session_id: str,
    providers_with_keys: Sequence[str] = (),
    show_help_hint: bool = False,
    framework: str = "",
    instance_id: str = "",
    user_id: str = "",
    memory_backend: str = "",
    autonomy_on: Optional[bool] = None,
) -> str:
    """Build the deterministic plain-text first-run banner (no ANSI, 2 or 3 lines)."""
    second = f"session {_short_session(session_id)} · tools {_tools_str(tool_ids)}"
    if show_help_hint:
        second += " · /help · /session"
    out = f"polyrob v{version} · {model} ({provider})\n{second}"
    info = _session_info_str(
        framework=framework, instance_id=instance_id, user_id=user_id,
        memory_backend=memory_backend, autonomy_on=autonomy_on,
    )
    if info:
        out += f"\n{info}"
    return out


def print_banner(
    renderer: Any,
    *,
    version: str,
    model: str,
    provider: str,
    tool_ids: Sequence[str],
    session_id: str,
    config: Optional[Any] = None,
    providers_with_keys: Optional[Sequence[str]] = None,
    show_help_hint: bool = False,
    framework: str = "",
    instance_id: str = "",
    user_id: str = "",
    memory_backend: str = "",
    autonomy_on: Optional[bool] = None,
) -> None:
    """Print the first-run banner once, via *renderer*.

    Rich vs plain is decided by the renderer kind: a renderer exposing a
    ``console`` attribute (``RichRenderer``) gets the styled two-liner;
    everything else (``PlainRenderer``) gets the plain text via
    ``print_block``.  *config*/*providers_with_keys* are accepted for
    backwards compatibility; key status is no longer shown here.
    """
    if providers_with_keys is None:
        providers_with_keys = provider_key_status(config) if config is not None else []

    console = getattr(renderer, "console", None)
    if console is not None:
        console.print(
            banner_panel(
                version=version,
                model=model,
                provider=provider,
                tool_ids=tool_ids,
                session_id=session_id,
                providers_with_keys=providers_with_keys,
                show_help_hint=show_help_hint,
                framework=framework,
                instance_id=instance_id,
                user_id=user_id,
                memory_backend=memory_backend,
                autonomy_on=autonomy_on,
            )
        )
        return

    text = banner_plain(
        version=version,
        model=model,
        provider=provider,
        tool_ids=tool_ids,
        session_id=session_id,
        providers_with_keys=providers_with_keys,
        show_help_hint=show_help_hint,
        framework=framework,
        instance_id=instance_id,
        user_id=user_id,
        memory_backend=memory_backend,
        autonomy_on=autonomy_on,
    )
    # PlainRenderer.print_block writes the block verbatim (no title).
    renderer.print_block(text)
