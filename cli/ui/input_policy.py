"""Input preparation and safe controls shared by terminal conversation loops."""
import os

# Commands that do not replace/reset the running conversation's model/history.
LIVE_COMMANDS = frozenset({
    "attach", "steer", "pause", "halt", "resume", "pending", "reject", "inbox", "asks",
    "fulfill", "status", "session", "info", "tools", "steps", "usage", "cost",
    "subagents", "goals", "cron", "crons", "files", "help", "h", "?",
    "exit", "quit", "q", "quiet", "verbose",
    # C18/C5/E16: `/cancel` STOPS the running turn, so it must reach the
    # dispatcher while one is running — a stop verb the busy turn rejects is
    # not a stop verb. `/approve` and `/gates` decide the owner queue and
    # `/meter` reads counters; none of them replaces the conversation.
    "cancel", "approve", "gates", "meter", "tokens", "apps",
    # CLI10: `/run` reads or stops a BACKGROUND run and `/cards` lists the owner's
    # open decisions — neither touches this conversation.
    "run", "cards",
})


def is_live_command(line):
    parts = line[1:].split(maxsplit=1) if line.startswith("/") else []
    if not parts:
        return False
    if parts[0].lower() in LIVE_COMMANDS:
        return True
    # CLI10: a tap token (`/approve_tap_<id>`, `/fulfill_<ask>_<letter>`,
    # `/card_<id>_ok`) is a verb with its argument folded in — recognised by the
    # ONE grammar, `core.surfaces.tappable`, never a local pattern.
    try:
        from core.surfaces.tappable import is_tappable_token
        return is_tappable_token("/" + parts[0])
    except Exception:
        return False


def prepare_text(line):
    from core.config_policy import AutonomyConfig
    if AutonomyConfig.context_references_enabled():
        from agents.task.agent.messages.context_references import preprocess_context_references
        return preprocess_context_references(line, root=os.getcwd(), confine_to_root=True)
    return line
