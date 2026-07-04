"""h_self.py — the ``/self`` REPL slash-command handler.

Read-only view of the running POLYROB instance's IDENTITY:

- framework name + instance id + bound owner,
- the operator-authored, frozen **SOUL** self-context (``<home>/identity/*.md``),
- the agent-writable **SELF** doc (``<home>/identity/{instance}/user_{uid}/self.md``).

This is a pure *reader*. It never writes identity docs — the write path is the
separate ``self_context_manage`` agent action. It mirrors the exact
``home_dir``/``load_self_context``/``load_self_doc`` call pattern that
``agents/task/agent/core/construction.py`` uses to pin the SELF_CONTEXT
foundation message, so what ``/self`` shows matches what the running agent sees.

Kept in its own module (the god-file split convention — new behavior gets its
own file, see ``handlers.py``'s header). Registration is wired by the REPL /
default-registry builder, not here. Fail-open: any backend error degrades to a
friendly one-liner (never raises into the REPL).
"""

from __future__ import annotations

from typing import Any

from cli.ui.commands.registry import CommandContext

# How much of each identity doc to show inline before truncating. The docs are
# already char-capped at load time (SELF_CONTEXT_TOTAL_MAX_CHARS ~60k / the SELF
# doc ~2.2k); this keeps a single ``/self`` bubble legible in the REPL.
_DOC_PREVIEW_CHARS = 1500
_TRUNCATION_MARKER = "… (truncated)"


def _preview(text: str, limit: int = _DOC_PREVIEW_CHARS) -> str:
    """Return *text* trimmed to *limit* chars with a truncation marker if cut."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n" + _TRUNCATION_MARKER


def _resolve_home_dir(ctx: CommandContext) -> Any:
    """Resolve the POLYROB home dir the SAME way construction.py does.

    construction.py pins the SELF_CONTEXT foundation message from the container
    config's ``data_dir`` (fallback ``"data"``); mirror it so ``/self`` reads the
    identical ``<home>/identity/`` tree the running agent was seeded from.
    """
    try:
        cfg = getattr(ctx.container, "config", None)
        return getattr(cfg, "data_dir", "data") or "data"
    except Exception:
        return "data"


def h_self(ctx: CommandContext) -> None:
    """Show the running instance's identity: SOUL + SELF docs (read-only).

    Usage:
      /self   — framework/instance/owner header, then the SOUL and SELF docs.

    Read-only. Fail-open per field: a missing/unavailable loader degrades to a
    placeholder, never raises into the REPL.
    """
    try:
        from core.instance import (
            FRAMEWORK_NAME,
            load_self_context,
            load_self_doc,
            resolve_instance_id,
            resolve_owner_principal,
        )

        instance_id = resolve_instance_id()
        owner = resolve_owner_principal() or "unbound (local owner)"
        home_dir = _resolve_home_dir(ctx)
        user_id = ctx.user_id or "local"

        # SOUL — operator-authored, frozen, instance-global.
        try:
            soul = load_self_context(home_dir)
        except Exception:
            soul = ""
        # SELF — agent-writable, per-(instance, user).
        try:
            self_doc = load_self_doc(home_dir, user_id, instance_id)
        except Exception:
            self_doc = ""

        lines = [
            f"framework: {FRAMEWORK_NAME}",
            f"instance:  {instance_id}",
            f"owner:     {owner}",
            f"user:      {user_id}",
            "",
        ]

        lines.append("SOUL (operator-authored, frozen):")
        if soul:
            lines.append(_preview(soul))
        else:
            lines.append("  No SOUL/identity doc authored.")

        lines.append("")
        lines.append("SELF (agent-writable identity doc):")
        if self_doc:
            lines.append(_preview(self_doc))
        else:
            lines.append("  No SELF/identity doc authored.")

        ctx.emit("\n".join(lines), title="self")
    except Exception as exc:
        ctx.emit(f"Could not resolve self-context: {exc}", title="self")
