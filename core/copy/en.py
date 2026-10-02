"""Shared CLI/REPL/transcript vocabulary, in English.

The console keeps its own vocabulary in :mod:`webview.copy.en`. This is its
core-tier twin: the strings a module BELOW the webview layer must say — the
running transcript's narrated actions (043 A14), which the console (JS), the
CLI transcript and the pure Python narrator all render from the same source.

The rules are the ones ``webview/copy/en.py`` is held to, and for the same
reason (``docs/design/040/README.md`` rule 3): first person, monospace is for
values only (so no value is baked into a sentence — numbers arrive as
``{placeholders}``), and no machine name (no flag, no function, no tool id, no
session hash) on a default line. A tool's own name is a machine name; the
narrator never puts one on screen.

⚠️ These are product UI strings (the transcript a person reads), not agent
prose, so they follow the product voice — the present-continuous "Building the
container" is deliberate and is not held to Simplified Technical English.

Keys are ``<screen>.<thing>``. ``chat.act.*`` is one human line per tool action.
"""

STRINGS = {
    # ------------------------------------------------------------------ 043 A14
    # The narrated action line. One per tool action, human phrasing, newest
    # last. The narrator (agents/task/telemetry/narrate.py) chooses the key and
    # fills the numbers; the words live only here.

    # A file read. Singular and plural, with and without a place.
    "chat.act.files_read": "Read {count} files in {where}",
    "chat.act.files_read_nodir": "Read {count} files",
    "chat.act.file_read_one": "Read one file in {where}",
    "chat.act.file_read_one_nodir": "Read one file",

    # A file write.
    "chat.act.file_wrote": "Wrote {name}",
    "chat.act.file_wrote_plain": "Wrote a file",

    # A test run. The suite either passed clean or it did not.
    "chat.act.tests_all_passed": "Ran the test suite and all {passed} passed",
    "chat.act.tests_mixed": "Ran the test suite, {passed} passed and {failed} failed",
    "chat.act.tests_ran": "Ran the test suite",

    # Running a piece of code.
    "chat.act.code_running": "Running the code",
    "chat.act.code_done": "Ran the code",

    # Deploying an app — the running line the mockup shows on the receipt.
    "chat.act.deploy_running": "Building the container",
    "chat.act.deploy_done": "Deployed the app",

    # The floor. A tool the narrator does not know by name — never its id.
    "chat.act.working": "Working on the task",
    "chat.act.did_work": "Ran a tool",

    # A tool that did not finish. Never the raw error, which can carry a repr.
    "chat.act.failed": "An action did not finish",

    # ------------------------------------------------------------- 070 E.23
    # A status-snapshot section id in words, for "I could not check: …" on any
    # seat. The id is ``core.status_snapshot.SECTION_ORDER``; a section with no
    # key here shows its id (tests/unit/webview/test_agent_posture_axes.py
    # pins one key per section).
    "status.section.session": "this chat",
    "status.section.providers": "AI providers",
    "status.section.work": "goals",
    "status.section.approvals": "approvals",
    "status.section.loops": "background loops",
    "status.section.tools": "abilities",
    "status.section.knowledge": "memory and documents",
    "status.section.delivery": "messages to you",
    "status.section.posture": "permissions",
    "status.section.security": "security checks",
    "status.section.identity": "identity",
    "status.section.rules": "your rules",
    "status.section.apps": "apps",
    "status.section.groups": "group chats",
    "status.section.room_actions": "paid actions in group chats",
    "status.section.creations": "tokens Rob created",
    "status.section.collectibles": "NFTs",
    "status.section.liquidity": "pool deposits",
    "status.section.wallet": "wallet",
    "status.section.custody": "wallet key safety",
    "status.section.packs": "add-ons",
    "status.section.money": "money records",
    "status.section.economics": "AI credit",
}
