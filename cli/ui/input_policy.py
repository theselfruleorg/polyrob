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
})


def is_live_command(line):
    parts = line[1:].split(maxsplit=1) if line.startswith("/") else []
    return bool(parts and parts[0].lower() in LIVE_COMMANDS)


def prepare_text(line):
    from core.config_policy import AutonomyConfig
    if AutonomyConfig.context_references_enabled():
        from agents.task.agent.messages.context_references import preprocess_context_references
        return preprocess_context_references(line, root=os.getcwd(), confine_to_root=True)
    return line
