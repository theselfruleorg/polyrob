"""The ONE per-command shell guard (073 W2).

``classify(command)`` answers WHICH shell commands may run without the owner.
The compute posture decides WHERE a command can run; this decides what. Every
shell rail asks here — the `shell` tool, its controller pre-hook and the
approval exemption — so the floor can never differ between them.

Three verdicts:

- ``floor``     — refused always, on every backend, INSIDE containers too (some harnesses
                  skip their checks in a container; a bind-mounted workspace and
                  published ports make "inside" leaky, so we do not). No approval,
                  no allowlist lifts it: deleting a root/home tree, a fork bomb,
                  writing a raw disk, a root ``curl | sh``, writing the agent's own
                  config/wallet state, stopping the agent's own service.
- ``dangerous`` — runs only on an owner decision (the existing approval
                  provider). Recursive delete outside the working directory,
                  permission sweeps, force-push, service/container control,
                  ``sudo``, nested interpreters, an env dump that leaves the
                  screen, pipe-to-shell, obfuscated quoting.
- ``ok``        — everything else.

Two passes, both must agree it is safe: regexes over the RAW text (catches
content inside quotes, heredocs and subshells) and a token pass over the
shell-lexed text (defeats quote-splicing such as ``r''m -rf /``). Text that the
lexer cannot parse is ``dangerous`` (a heredoc with an apostrophe is common;
the raw floor regexes still run over it). A ``$(…)``/backtick substitution is
classified with this same guard and then counts as one argument value; one the
parser is unsure of stays ``dangerous``. A relative ``rm -r build`` is ``ok``; the
shell tool checks the persisted cwd with :func:`relative_delete_refusal`.

Pure: no I/O, no env. Core tier so `run_code` shell-outs, `self_env` and
`coding.run_tests` can reuse it.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

FLOOR = "floor"
DANGEROUS = "dangerous"
OK = "ok"


@dataclass(frozen=True)
class Verdict:
    level: str  # floor | dangerous | ok
    reason: str = ""

    @property
    def is_floor(self) -> bool:
        return self.level == FLOOR

    @property
    def is_dangerous(self) -> bool:
        return self.level == DANGEROUS


# --- targets a recursive delete / permission sweep may never hit -------------------
_FLOOR_DIRS = frozenset({
    "/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "${HOME}/", "$HOME/*",
    "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc",
    "/root", "/sbin", "/srv", "/sys", "/usr", "/var", "/Users", "/System",
    "/Library", "/Applications", "/Volumes", "/private", "/nix",
})

# The agent's own config, wallet and signer state (any write = floor). The
# wallet dir holds the audit ledger (``wallet/audit.jsonl``); a local install
# keeps it under the data home (``~/.polyrob/data/wallet``).
_PROTECTED_PATH_RE = re.compile(
    r"(/etc/polyrob\b|/var/lib/polyrob\b|\.polyrob/\.env\b|polyrob\.env\b"
    r"|\.polyrob/(data/)?(wallet|signer|keys?)\b|/run/polyrob-signer\b)"
)
#: The agent's OWN workspace inside the server data home: the shared project
#: folder (prod: ``POLYROB_PROJECT_DIR=/var/lib/polyrob/project``) and a per-session
#: ``workspace/``. Its default cwd already IS this folder, so a relative path never
#: met the tripwire above; naming the same file absolutely must not be refused.
#: A tail with a ``..`` component is NOT the workspace and stays protected.
_PATH_STOP = r"\s;&|'\"`()<>"
_OWN_WORKSPACE_RE = re.compile(
    r"/var/lib/polyrob/(?:project|data/auto/[^/" + _PATH_STOP + r"]+/sessions/[^/"
    + _PATH_STOP + r"]+/workspace)(?=$|[/" + _PATH_STOP + r"])(/[^" + _PATH_STOP + r"]*)?")


def _mask_own_workspace(text: str) -> str:
    """``text`` with every path inside the agent's own workspace replaced by a
    neutral word, for the protected-path checks only."""
    def _sub(m: "re.Match") -> str:
        tail = m.group(1) or ""
        # `..` leaves the workspace; a `.polyrob/` state dir or an env file
        # inside it is config a later process may load, never plain work.
        if (re.search(r"(^|/)\.\.(/|$)", tail) or re.search(r"(^|/)\.polyrob(/|$)", tail)
                or re.search(r"(^|/)[^/]*\.env\b|(^|/)\.env\b", tail)):
            return m.group(0)
        return "_own_workspace_"
    return _OWN_WORKSPACE_RE.sub(_sub, text)


_WRITE_VERB_RE = re.compile(
    r"(>>?|\btee\b|\brm\b|\bmv\b|\bcp\b|\bsed\s+-i|\bchmod\b|\bchown\b|\btruncate\b"
    r"|\bln\b|\bdd\b|\binstall\b|\bunlink\b|\bshred\b)"
)

_FLOOR_RAW: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"(\b\w+|:)\s*\(\)\s*\{[^}]*\1\s*\|\s*\1\s*&"), "fork bomb"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formats a filesystem"),
    (re.compile(r"\bdd\b[^\n;|&]*\bof=/dev/(r?disk|sd|hd|nvme|xvd|vd|mmcblk)"),
     "writes a raw disk"),
    (re.compile(r">\s*/dev/(r?disk\d|sd[a-z]|hd[a-z]|nvme\d|xvd[a-z]|vd[a-z]|mmcblk\d)"),
     "writes a raw disk"),
    (re.compile(r"\b(wipefs|fdisk|sfdisk|parted)\b[^\n;|&]*/dev/"), "rewrites a disk"),
    (re.compile(r"\bdiskutil\s+(erase\w*|zero\w*|partition\w*|secureErase|reformat)\b",
                re.IGNORECASE), "erases a disk"),
    (re.compile(r"\bshred\b[^\n;|&]*/dev/"), "shreds a device"),
    (re.compile(r"\b(curl|wget|fetch)\b[^\n;]*\|\s*(sudo|doas)\b[^\n;]*\b(ba|z|da|k)?sh\b"),
     "pipes a download into a root shell"),
    (re.compile(r"--no-preserve-root\b"), "deletes the root filesystem"),
    (re.compile(r"\b(systemctl|service)\b[^\n;|&]*\b(stop|restart|kill|disable|mask|reload)\b"
                r"[^\n;|&]*\bpolyrob", re.IGNORECASE),
     "stops the agent's own service"),
    (re.compile(r"\blaunchctl\b[^\n;|&]*\b(unload|bootout|stop|kill|remove|disable)\b"
                r"[^\n;|&]*polyrob", re.IGNORECASE),
     "stops the agent's own service"),
    (re.compile(r"\b(pkill|killall)\b[^\n;|&]*\b(polyrob|rob\b)", re.IGNORECASE),
     "kills the agent's own process"),
    (re.compile(r"\bkill\b[^\n;|&]*\$\{?PPID\b"), "kills the agent's own process"),
    (re.compile(r"\bkill\s+(-\w+\s+)*-1\b"), "kills every process"),
)

#: ``env`` alone (a dump), not ``env FOO=1 make``.
_ENV_DUMP_RE = re.compile(r"(^|[;&|(]\s*)env\s*($|[;&|)>])")

_DANGEROUS_RAW: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(curl|wget|fetch)\b[^\n;]*\|\s*(\S*/)?(ba|z|da|k|fi)?sh\b"),
     "pipes a download into a shell"),
    (re.compile(r"\b(curl|wget)\b[^\n;]*\|\s*(\S*/)?(python\d*|perl|ruby|node)\b"),
     "pipes a download into an interpreter"),
    (re.compile(r"\bbase64\s+(-d|--decode|-D)\b[^\n;]*\|\s*\S*(sh|python\d*|perl)\b"),
     "decodes and runs hidden text"),
    (re.compile(r"\$'[^']*\\(x[0-9a-fA-F]{2}|[0-7]{3}|u[0-9a-fA-F]{4})"),
     "obfuscated ANSI-C quoting"),
    (re.compile(r"\b(ba|z|da|k|fi)?sh\s+-\w*c\b"), "nested shell (-c)"),
    (re.compile(r"\b(python\d*(\.\d+)?|perl|ruby|node|php)\s+-\w*[ce]\b"),
     "nested interpreter (-c/-e)"),
    (re.compile(r"\bgit\s+push\b[^\n;|&]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)"),
     "force-push rewrites remote history"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "discards local changes"),
    (re.compile(r"\bgit\s+clean\s+-\w*f"), "deletes untracked files"),
    (re.compile(r"\bgit\s+(branch|tag)\s+-D\b"), "deletes refs"),
    (re.compile(r"\bfind\b[^\n;|&]*\s-(delete|exec\s+rm)\b"), "bulk delete with find"),
    (re.compile(r"\bxargs\b[^\n;|&]*\brm\b"), "bulk delete with xargs"),
    (re.compile(r"\bchmod\s+(-\w*R|--recursive)\b"), "recursive permission change"),
    (re.compile(r"\bchmod\s+(0?777|a\+rwx|o\+w)\b"), "world-writable permissions"),
    (re.compile(r"\bchown\s+(-\w*R|--recursive)\b"), "recursive owner change"),
    (re.compile(r"(>>?|\btee\b[^\n;|&]*)\s*\S*(\.ssh/|authorized_keys|\.bashrc|\.zshrc"
                r"|\.profile|\.bash_profile|\.zprofile|\.zshenv|\.gitconfig)"),
     "writes a login, shell or ssh config file"),
    (re.compile(r"(>>?|\btee\b[^\n;|&]*)\s*/etc/"), "writes system config"),
    (re.compile(r"\bcrontab\s+-(r|e|\s*-)"), "changes the crontab"),
    (re.compile(r"\bDOCKER_HOST="), "re-points the container daemon"),
    (re.compile(r"\b(apt(-get)?|yum|dnf|pacman|zypper)\s+(remove|purge|autoremove|erase|-R)\b"),
     "removes system packages"),
    (re.compile(r"\bbrew\s+(uninstall|remove|rm)\b"), "removes packages"),
    (re.compile(r"\b(pip\d*|uv\s+pip)\s+uninstall\b"), "removes packages"),
    (re.compile(r"\bnpm\s+(uninstall|rm|remove)\s+(-g|--global)\b"), "removes global packages"),
    (re.compile(r"/proc/[^\s/]+/environ\b"), "reads a process environment"),
    (_ENV_DUMP_RE, "dumps the environment"),
    (re.compile(r"(^|[;&|(]\s*)(export\s+-p|declare\s+-x)\b"), "dumps the environment"),
    (re.compile(r"\bmv\b[^\n;|&]*\s/dev/null\b"), "moves files into /dev/null"),
    (re.compile(r">\s*/dev/(r?disk|sd|hd|nvme)"), "writes a device"),
    (re.compile(r"\bkill(all)?\s+-9\b"), "force-kills processes"),
    (re.compile(r"\bhistory\s+-c\b"), "erases the shell history"),
    (re.compile(r"\b(g?make|just)\b[^\n;|&]*\s-f\s*(-|/dev/stdin|/dev/fd/\d+|/proc/self/fd/\d+)(\s|$)"),
     "runs a build file from standard input"),
)

_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", "(", ")", "\n", ";;"})
_SEPARATOR_CHARS = frozenset(";&|()\n")
_PREFIX_WORDS = frozenset({
    "sudo", "doas", "command", "builtin", "exec", "nohup", "time", "nice", "ionice",
    "env", "xargs", "stdbuf", "timeout", "caffeinate", "then", "do", "else", "!",
})
#: Shell grammar words that may precede a command (brace groups, compound heads).
_GRAMMAR_WORDS = frozenset({"{", "}", "if", "elif", "while", "until", "fi", "done", "esac",
                            "then", "do", "else", "!"})
#: Commands that RUN another command given somewhere in their arguments. Option
#: values make the position ambiguous (`timeout -s KILL 10 rm …`), so every later
#: word of a wrapper is also judged as a possible command head.
_WRAPPERS = frozenset({
    "sudo", "doas", "command", "builtin", "exec", "nohup", "time", "nice", "ionice",
    "env", "xargs", "stdbuf", "timeout", "caffeinate", "busybox", "toybox", "setsid",
    "chronic", "flock", "script", "watch", "unbuffer", "uv", "uvx", "pipx", "poetry",
    "pdm", "hatch", "conda", "mamba", "npx", "pnpx", "bunx", "nix-shell", "chroot",
    "runuser", "su", "taskset", "chrt", "prlimit", "systemd-run", "strace", "ltrace",
    "gdb", "valgrind", "parallel", "entr", "faketime", "firejail", "bwrap",
})
#: Programs that run nothing named in their own arguments: after a wrapper, a
#: word that names one ends the "every later word may be a command" scan.
#: (No interpreter, no `git`/`npm`/`make`/`cargo`/`go`/`coverage`: those can run a
#: command or module given on the line.)
_PLAIN_PROGRAMS = frozenset({
    "pytest", "py.test", "tox", "nox", "mypy", "ruff", "black", "isort", "flake8",
    "pylint", "pyright", "eslint", "prettier", "tsc", "jest", "vitest", "mocha",
    "pip", "pip3", "grep", "rg", "ls", "cat", "echo", "head", "tail", "wc",
})
_PACKAGE_MANAGERS = frozenset({"uv", "poetry", "pdm", "hatch", "conda", "mamba", "pipx"})
#: A package manager's first word that installs or inspects (never `run`/`exec`/`tool`).
_PM_SUBCOMMANDS = frozenset({
    "add", "remove", "install", "sync", "lock", "pip", "build", "publish", "update",
    "upgrade", "list", "show", "init", "venv", "export", "tree", "version", "check",
    "search", "info", "create", "new",
})
_INTERPRETERS_RE = re.compile(r"(python\d*(\.\d+)?|pypy\d*|perl|ruby|node|php|bash|sh|zsh"
                              r"|dash|ksh|fish|tclsh|lua|deno|bun)")
_RM_NAMES = frozenset({"rm", "unlink", "rmdir"})
_AS_OTHER_USER = frozenset({"sudo", "doas", "su", "pkexec"})
#: Command words that are dangerous wherever they appear as the COMMAND (never as an
#: argument: `cat docker.md` is fine, `docker rm -f x` is not).
_DANGEROUS_HEADS = {
    "polyrob": "runs the owner's administrative CLI",
    "rob": "runs the owner's administrative CLI",
    "docker": "controls services or containers",
    "podman": "controls services or containers",
    "kubectl": "controls services or containers",
    "systemctl": "controls services or containers",
    "launchctl": "controls services or containers",
    "service": "controls services or containers",
    "eval": "eval runs constructed text",
    "printenv": "dumps the environment",
    "shutdown": "powers off the machine",
    "reboot": "powers off the machine",
    "halt": "powers off the machine",
    "poweroff": "powers off the machine",
    "crontab": "changes the crontab",
}


def _lex(command: str) -> Optional[List[str]]:
    """Shell-lex ``command`` into tokens, operators as their own tokens.

    None when the text does not lex (unbalanced quotes). Quote splicing is
    resolved here: ``r''m`` and ``"r"m`` both become ``rm``.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


def _segments(tokens: Sequence[str]) -> List[List[str]]:
    out: List[List[str]] = []
    cur: List[str] = []
    for tok in tokens:
        if tok in _SEPARATORS or (tok and set(tok) <= _SEPARATOR_CHARS):
            if cur:
                out.append(cur)
            cur = []
            continue
        cur.append(tok.strip("`"))
    if cur:
        out.append(cur)
    return out


def _head(segment: List[str]) -> Tuple[str, List[str]]:
    """The command word of a segment after prefix words (``sudo``, ``env X=1`` …)."""
    i = 0
    while i < len(segment):
        tok = segment[i]
        base = os.path.basename(tok)
        if base in _PREFIX_WORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tok):
            i += 1
            # skip the option words of a prefix (`sudo -u x`, `timeout 5`, `nice -n 5`)
            while i < len(segment) and (segment[i].startswith("-") or segment[i].isdigit()):
                i += 1
            continue
        if not tok:
            i += 1
            continue
        return base, segment[i + 1:]
    return "", []


def _candidates(segment: List[str]) -> List[Tuple[str, List[str]]]:
    """Every ``(command, args)`` this segment may run: the head after grammar words
    and assignments and — when that head is a wrapper (``timeout``, ``busybox``,
    ``uv run``, ``xargs`` …) — EVERY later word as well, since option values make
    the wrapped command's position ambiguous. ``find -exec`` contributes the word
    after each ``-exec``/``-execdir``/``-ok``. Names are lower-cased: a
    case-insensitive filesystem runs ``POLYROB`` as ``polyrob``. Linear in the
    segment length (no recursion)."""
    def _at(k: int) -> Optional[int]:
        while k < len(segment) and (not segment[k] or segment[k] in _GRAMMAR_WORDS
                                    or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", segment[k])):
            k += 1
        # A word that starts with `#` in command position opens a comment (a
        # heredoc's `#!/bin/bash` line included): nothing there runs.
        if k < len(segment) and segment[k].startswith("#"):
            return None
        return k if k < len(segment) else None

    head = _at(0)
    if head is None:
        return []
    starts = [head]
    wrapper = os.path.basename(segment[head]).lower()
    if wrapper in _WRAPPERS:
        # The ambiguity is option VALUES only. Once a word that cannot be an
        # option value (its predecessor is not an option) names a program that
        # runs nothing named on the line (`pytest`, `pip`), or a package manager's
        # first word is a non-run subcommand (`uv pip`, `poetry add`), the words
        # after it are ITS arguments: `timeout 600 pytest -k docker` and
        # `uv add docker` run no `docker`.
        first = True
        for k in range(head + 1, len(segment)):
            starts.append(k)
            word = segment[k]
            if not word or word.startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", word):
                continue
            prev_is_opt = segment[k - 1].startswith("-") and k - 1 > head
            name = os.path.basename(word).lower()
            if not prev_is_opt and (name in _PLAIN_PROGRAMS or (
                    first and wrapper in _PACKAGE_MANAGERS and name in _PM_SUBCOMMANDS)):
                break
            first = False
    for k, tok in enumerate(segment):
        if tok in ("-exec", "-execdir", "-ok", "-okdir") and k + 1 < len(segment):
            starts.append(k + 1)
    out: List[Tuple[str, List[str]]] = []
    seen = set()
    for k in starts:
        k = _at(k)
        if k is None or k in seen:
            continue
        seen.add(k)
        out.append((os.path.basename(segment[k]).lower(), segment[k + 1:]))
    return out


#: A recursive delete of an absolute or home path anywhere in the raw text
#: (inside ``awk system()``, an alias, a here-string …).
_RAW_RM_RE = re.compile(r"\brm\s+(-\S+\s+)*-\w*[rR]\w*\s+(-\S+\s+)*['\"]?(/|~|\$\{?HOME)")
#: The owner's administrative CLI named as a command anywhere in the raw text,
#: whatever the case (``POLYROB`` runs it on a case-insensitive filesystem), as a
#: module (``-m cli.polyrob``) or from a copied binary.
_RAW_ADMIN_CLI_RE = re.compile(
    r"(?i)((^|[;&|({`'\"!=:]|\b(exec|system|spawn|popen|run|call|execvp?e?)\W*)\s*"
    r"(\S*/)?polyrob(-\w+)?(?=\s+[a-z]|\s*$|\s*[;&|)'\"`])"
    r"|\bcli[./]polyrob\b"
    r"|\b(cp|ln|install|rsync|mv)\b[^\n;|&]*\bpolyrob\b)")
_RAW_ADMIN_DB_RE = re.compile(r"(?i)\bsqlite3?\b[^\n;|&]*(approval|\.polyrob|/polyrob\b|polyrob[^\s]*\.db)")
#: The agent's own state databases by FILE NAME, wherever the path points
#: (``sqlite3 ../cron.db``, ``$D/data/goals.db``): the path rule above needs a
#: ``.polyrob``/``/polyrob`` component a relative or variable path never has.
_STATE_DB_NAMES = frozenset({
    "app_services.db", "artifacts.db", "autonomy_state.db", "bot.db", "bridges.db",
    "cards.db", "control.sqlite3", "conversations.db", "correspondents.db", "cron.db",
    "dapp_sessions.db", "dead_targets.db", "defi_tokens.db", "deployed_apps.db",
    "goals.db", "group_allowlist.db", "login_attempts.db", "memory.db", "open_positions.db",
    "outbox.db", "owner_thread.db", "pairing.db", "payment_assets.db", "publications.db",
    "refusal_taint.db", "room_actions.db", "session_registry.db", "signer.sqlite",
    "submissions.sqlite", "surface_state.db", "surfaces.db", "token_denylist.db",
    "token_pins.db", "token_provenance.db", "verdicts.db", "wakes.db", "wa_window.db",
    "x_write_attempts.db",
})
#: SQL (or a sqlite3 dot-command) that changes a database. A read — SELECT,
#: ``.schema``, ``.tables``, ``.dump`` — stays ok.
_SQL_WRITE_RE = re.compile(
    r"(?im)\b(insert|update|delete|replace|drop|alter|create|attach|detach|vacuum|reindex)\b"
    r"|\bpragma\s+\w+\s*="
    r"|^\s*\.(?!(schema|tables|dump|indexes|indices|headers|mode|width|count)\b)\w+")


def _sqlite_state_write(args: List[str]) -> bool:
    """True when a ``sqlite3`` call opens one of the agent's state databases and
    may change it: write SQL anywhere on the line, ``-init``, or no SQL at all
    (interactive or stdin — what runs is not on the line)."""
    dbs = [a for a in args
           if os.path.basename(a.strip("\"'")).lower() in _STATE_DB_NAMES]
    if not dbs:
        return False
    rest = [a for a in args if a not in dbs]
    if "-init" in rest:
        return True
    if not [a for a in rest if not a.startswith("-")]:
        return True
    return any(_SQL_WRITE_RE.search(a) for a in rest)


def _norm_target(t: str) -> str:
    t = t.strip().strip("\"'")
    if len(t) > 1 and t.endswith("/") and not t.endswith("*/"):
        t = t.rstrip("/") or "/"
    return t


def _rm_verdict(args: List[str], relative_ok: bool = False) -> Optional[Verdict]:
    recursive = False
    targets: List[str] = []
    end_of_opts = False
    for a in args:
        if not end_of_opts and a == "--":
            end_of_opts = True
            continue
        if not end_of_opts and a.startswith("--"):
            if a in ("--recursive", "--dir"):
                recursive = True
            continue
        if not end_of_opts and a.startswith("-") and len(a) > 1:
            if "r" in a or "R" in a:
                recursive = True
            continue
        targets.append(_norm_target(a))
    for t in targets:
        if t in _FLOOR_DIRS or t in ("/.", "/..", "~/.", "$HOME/.") or re.fullmatch(r"/+\*?", t):
            return Verdict(FLOOR, f"deletes a protected tree ({t})")
    if recursive and not (targets and all(_relative_work_path(t) for t in targets)
                          and relative_ok):
        return Verdict(DANGEROUS, "recursive delete")
    return None


#: First path components a relative recursive delete never removes without the owner:
#: the agent's own state, a repository's history, credentials and login config.
_KEEP_DIRS = frozenset({".polyrob", ".git", ".ssh", ".gnupg", ".aws", ".config", ".env",
                        ".kube", ".docker", ".local"})


def _relative_work_path(t: str) -> bool:
    """True for a plain relative path below the cwd (``build``, ``dist/*``,
    ``node_modules``): no ``..``, no leading ``/`` or ``~``, no expansion, not the
    cwd itself and not a protected name."""
    if not t or t[0] in "/~-" or any(c in t for c in "$`\\\n{}"):
        return False
    parts = [p for p in t.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return False
    if re.fullmatch(r"[*?.\[\]]+", parts[0]):  # `*`, `.*`, `?` — the whole cwd
        return False
    for part in parts:
        # `.g*` reaches `.git`; a pattern that does not start with `.` never matches a dotfile
        if part.lower() in _KEEP_DIRS or (part.startswith(".") and re.search(r"[*?\[]", part)):
            return False
    return not _PROTECTED_PATH_RE.search("/" + t)


def _cd_is_safe(segments: List[List[str]]) -> bool:
    """False when the line changes directory to anywhere but a plain relative path
    (``cd ~``, ``cd /``, ``cd ..``, ``cd`` alone, ``env -C /``): a relative delete
    after it no longer means "below the working directory"."""
    for seg in segments:
        for name, args in _candidates(seg):
            if name in ("cd", "pushd", "popd", "chdir"):
                words = [a for a in args if a != "--"]
                if name == "popd" or len(words) != 1 or not _relative_work_path(words[0]):
                    return False
        for k, tok in enumerate(seg):
            if tok in ("-C", "--chdir") or tok.startswith("--chdir="):
                if os.path.basename(seg[0]) == "env" or "rm" in seg[k:]:
                    return False
    return True


def _perm_verdict(name: str, args: List[str]) -> Optional[Verdict]:
    recursive = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "R" in a)
                    for a in args)
    if recursive:
        for a in args:
            if _norm_target(a) in _FLOOR_DIRS:
                return Verdict(FLOOR, f"recursive {name} on a protected tree ({_norm_target(a)})")
    return None


#: What a parsed command substitution becomes in the outer line: a value (an
#: argument is fine) whose use as a COMMAND NAME still needs the owner.
_CMDSUB = "$__cmdsub__"
_MAX_SUBST_DEPTH = 6
_HEREDOC_RE = re.compile(r"<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2")
_LEADING_CAT_HEREDOC_RE = re.compile(
    r"\A\s*cat[ \t]+<<(-?)[ \t]*(['\"]?)([A-Za-z_][\w.-]*)\2[ \t]*\n")


def _skip_heredoc_bodies(s: str, j: int, heredocs: List[Tuple[str, bool]]) -> Optional[int]:
    """``j`` is just after the newline that ends a heredoc's command line; the
    index after the last pending delimiter line, or None when one never closes."""
    n = len(s)
    for delim, tabs in heredocs:
        while True:
            k = s.find("\n", j)
            end = n if k < 0 else k
            line = s[j:end]
            if (line.lstrip("\t") if tabs else line) == delim:
                j = n if k < 0 else k + 1
                break
            if k < 0:
                return None
            j = k + 1
    return j


def _dq_end(s: str, j: int) -> Optional[int]:
    """``s[j] == '"'``: the index after the closing quote (nested ``$(…)`` aware)."""
    k, n = j + 1, len(s)
    while k < n:
        c = s[k]
        if c == "\\":
            k += 2
            continue
        if c == '"':
            return k + 1
        if s.startswith("$(", k):
            e = _subst_end(s, k)
            if e is None:
                return None
            k = e
            continue
        if c == "`":
            e = s.find("`", k + 1)
            if e < 0:
                return None
            k = e + 1
            continue
        k += 1
    return None


def _subst_end(s: str, i: int) -> Optional[int]:
    """``s[i:i+2] == "$("``: the index after its closing ``)``, following bash's
    quoting, nested substitutions and heredocs inside; None when unsure."""
    if s.startswith("$((", i):
        return None  # arithmetic: not parsed here
    j, n, depth = i + 2, len(s), 1
    heredocs: List[Tuple[str, bool]] = []
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                return None
            j = k + 1
            continue
        if c == '"':
            k = _dq_end(s, j)
            if k is None:
                return None
            j = k
            continue
        if c == "`":
            k = s.find("`", j + 1)
            if k < 0:
                return None
            j = k + 1
            continue
        if s.startswith("$(", j):
            k = _subst_end(s, j)
            if k is None:
                return None
            j = k
            continue
        if s.startswith("<<", j) and not s.startswith("<<<", j):
            m = _HEREDOC_RE.match(s, j)
            if m:
                heredocs.append((m.group(3), m.group(1) == "-"))
                j = m.end()
                continue
        if c == "\n" and heredocs:
            k = _skip_heredoc_bodies(s, j + 1, heredocs)
            if k is None:
                return None
            heredocs = []
            j = k
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return None


def _strip_leading_cat_heredoc(text: str) -> str:
    """``cat <<'EOF' … EOF`` that OPENS a substitution prints its body: the body is
    the substitution's value (a commit message), not commands. Only a quoted
    delimiter or a body with no expansion; anything else is left as written."""
    m = _LEADING_CAT_HEREDOC_RE.match(text)
    if not m:
        return text
    delim, tabs, quoted = m.group(3), m.group(1) == "-", bool(m.group(2))
    end = _skip_heredoc_bodies(text, m.end(), [(delim, tabs)])
    if end is None:
        return text
    body = text[m.end():end]
    if not quoted and re.search(r"[$`\\]", body):
        return text
    return "cat\n" + text[end:]


def _expand(raw: str, depth: int) -> Tuple[str, Optional[Verdict]]:
    """Classify every ``$(…)`` / backtick substitution of ``raw`` with the same guard
    and return ``raw`` with each one replaced by a value placeholder, plus the worst
    inner verdict. A substitution this parser is unsure of is LEFT in the text, so
    the outer line still answers "shell expansion requires an owner decision"."""
    out: List[str] = []
    worst: Optional[Verdict] = None
    i, n = 0, len(raw)
    in_dq = False
    heredocs: List[Tuple[str, bool, bool]] = []
    word_start = True

    def _inner(text: str) -> Optional[Verdict]:
        v = classify(text, _depth=depth + 1, _strict=True)
        if v.is_floor:
            return v
        if v.is_dangerous:
            prefix = "" if v.reason.startswith("command substitution:") else "command substitution: "
            return Verdict(DANGEROUS, prefix + v.reason)
        return None

    try:
        while i < n:
            c = raw[i]
            if c == "\\":
                out.append(raw[i:i + 2])
                i += 2
                word_start = False
                continue
            if not in_dq and c == "'":
                k = raw.find("'", i + 1)
                if k < 0:
                    return raw, None
                out.append(raw[i:k + 1])
                i = k + 1
                word_start = False
                continue
            if not in_dq and c == "#" and word_start:
                k = raw.find("\n", i)
                k = n if k < 0 else k
                out.append(raw[i:k])  # a comment: copied as written
                i = k
                continue
            if c == '"':
                in_dq = not in_dq
                out.append(c)
                i += 1
                word_start = False
                continue
            if raw.startswith("$(", i) or c == "`":
                if c == "`":
                    k = raw.find("`", i + 1)
                    if k < 0 or "\\" in raw[i + 1:k]:
                        return raw, None
                    inner, end = raw[i + 1:k], k + 1
                else:
                    end = _subst_end(raw, i)
                    if end is None:
                        return raw, None
                    inner = raw[i + 2:end - 1]
                v = _inner(inner)
                if v is not None and v.is_floor:
                    return raw, v
                worst = worst or v
                out.append(_CMDSUB)
                i = end
                word_start = False
                continue
            if not in_dq and raw.startswith("<<", i) and not raw.startswith("<<<", i):
                m = _HEREDOC_RE.match(raw, i)
                if m:
                    heredocs.append((m.group(3), m.group(1) == "-", bool(m.group(2))))
                    out.append(m.group(0))
                    i = m.end()
                    word_start = False
                    continue
            if c == "\n" and not in_dq and heredocs:
                out.append(c)
                i += 1
                for delim, tabs, quoted in heredocs:
                    end = _skip_heredoc_bodies(raw, i, [(delim, tabs)])
                    if end is None:
                        return raw, None
                    seg = raw[i:end]
                    if not quoted and ("$(" in seg or "`" in seg):
                        return raw, None  # an expanding body: not parsed here
                    out.append(seg)
                    i = end
                heredocs = []
                word_start = True
                continue
            out.append(c)
            word_start = c in " \t\n;&|()"
            i += 1
    except RecursionError:
        return raw, None
    if in_dq or heredocs:
        return raw, None
    return "".join(out), worst


#: Commands that only SHOW what they read: an env dump piped through them stays on
#: the agent's own screen (none writes a file, runs a command or opens a socket).
_ENV_READERS = frozenset({"env", "printenv"})
_SHOW_FILTERS = frozenset({"grep", "egrep", "fgrep", "head", "tail", "wc", "cut", "cat",
                           "tr", "nl", "echo", "true", "column"})


def _env_read_only(tokens: Sequence[str], raw: str) -> bool:
    """True when every command of the line is an env reader or a show-only filter
    and nothing is redirected: ``env | grep X``, ``printenv PATH``."""
    if ">" in re.sub(r"\d?>\s*/dev/null|\d?>&\d", "", raw) or "<(" in raw:
        return False
    for seg in _segments(tokens):
        for name, _args in _candidates(seg):
            if name not in _ENV_READERS and name not in _SHOW_FILTERS:
                return False
    return True


#: ``${NAME}``-family expansions that only produce a value.
_SAFE_PARAM_RE = re.compile(
    r"\$\{#?(?!IFS\b)[A-Za-z_][A-Za-z0-9_]*"
    r"(?:(?::?[-=+?]|##?|%%?|//?)[^$`{}!@\\\[\]]*|:\d+(?::\d+)?)?\}")


def classify(command: str, *, _depth: int = 0, _strict: bool = False) -> Verdict:
    """Classify one shell command line (may hold many statements).

    A ``$(…)`` or backtick substitution is classified with the same guard (its
    value then counts as an argument). ``_strict`` marks such an inner command:
    its output feeds another command, so an env read there is never "only shown".
    """
    raw = command or ""
    if not raw.strip():
        return Verdict(OK)
    if _depth > _MAX_SUBST_DEPTH:
        return Verdict(DANGEROUS, "nested command substitution too deep to read")
    if _strict:
        raw = _strip_leading_cat_heredoc(raw)
    raw, sub_verdict = _expand(raw, _depth)
    if sub_verdict is not None and sub_verdict.is_floor:
        return sub_verdict

    # Floor, raw pass.
    for pat, why in _FLOOR_RAW:
        if pat.search(raw):
            return Verdict(FLOOR, why)
    raw_masked = _mask_own_workspace(raw)
    if _PROTECTED_PATH_RE.search(raw_masked) and _WRITE_VERB_RE.search(raw):
        return Verdict(FLOOR, "writes the agent's own config, wallet or signer state")

    tokens = _lex(raw)
    lexed_dangerous: Optional[Verdict] = sub_verdict
    env_ok = (not _strict) and tokens is not None and _env_read_only(tokens, raw)
    if tokens is None:
        lexed_dangerous = Verdict(DANGEROUS, "unparseable quoting")
    else:
        joined = " ".join(tokens)
        # Floor, lexed pass (quote splicing removed).
        for pat, why in _FLOOR_RAW:
            if pat.search(joined):
                return Verdict(FLOOR, why)
        joined_masked = _mask_own_workspace(joined)
        if _PROTECTED_PATH_RE.search(joined_masked) and _WRITE_VERB_RE.search(joined):
            return Verdict(FLOOR, "writes the agent's own config, wallet or signer state")
        segments = _segments(tokens)
        cd_safe = _cd_is_safe(segments)
        for seg in segments:
            if os.path.basename(seg[0]) in _AS_OTHER_USER and lexed_dangerous is None:
                lexed_dangerous = Verdict(DANGEROUS, "runs as another user")
            for name, args in _candidates(seg):
                if name.startswith("$") and lexed_dangerous is None:
                    lexed_dangerous = Verdict(DANGEROUS, "runs a command named by a variable")
                if _INTERPRETERS_RE.fullmatch(name) and lexed_dangerous is None:
                    if not any(not a.startswith("-") for a in args) or "-" in args:
                        lexed_dangerous = Verdict(DANGEROUS, "runs an interpreter on inline input")
                if name in _AS_OTHER_USER and lexed_dangerous is None:
                    lexed_dangerous = Verdict(DANGEROUS, "runs as another user")
                if name.startswith("polyrob-") and lexed_dangerous is None:
                    lexed_dangerous = Verdict(DANGEROUS, "runs the owner's administrative CLI")
                if name in _DANGEROUS_HEADS and lexed_dangerous is None:
                    if not (name == "crontab" and args in (["-l"], [])
                            or name == "printenv" and env_ok):
                        lexed_dangerous = Verdict(DANGEROUS, _DANGEROUS_HEADS[name])
                if name in _RM_NAMES:
                    v = _rm_verdict(args, relative_ok=cd_safe)
                    if v is not None and v.is_floor:
                        return v
                    if v is not None and lexed_dangerous is None:
                        lexed_dangerous = v
                elif name in ("chmod", "chown", "chgrp"):
                    v = _perm_verdict(name, args)
                    if v is not None:
                        return v
                elif (name in ("sqlite3", "sqlite") and lexed_dangerous is None
                      and _sqlite_state_write(args)):
                    lexed_dangerous = Verdict(DANGEROUS, "writes the agent's own databases")
        if lexed_dangerous is None:
            for pat, why in _DANGEROUS_RAW:
                if env_ok and pat is _ENV_DUMP_RE:
                    continue
                if pat.search(joined):
                    lexed_dangerous = Verdict(DANGEROUS, why)
                    break
        if _PROTECTED_PATH_RE.search(joined_masked) and lexed_dangerous is None:
            lexed_dangerous = Verdict(DANGEROUS, "reads the agent's own config or secrets")

    if lexed_dangerous is not None:
        return lexed_dangerous
    # Backstops over the RAW text: what the token pass cannot see (inside quotes,
    # awk/vim/git-alias payloads, here-strings) still reaches the owner.
    if _RAW_RM_RE.search(raw):
        return Verdict(DANGEROUS, "recursive delete of an absolute or home path")
    if _RAW_ADMIN_CLI_RE.search(raw_masked):
        return Verdict(DANGEROUS, "runs the owner's administrative CLI")
    if _RAW_ADMIN_DB_RE.search(raw_masked):
        return Verdict(DANGEROUS, "opens the agent's own databases")
    # Command substitution, ${…} expansion and IFS tricks build text the guard
    # cannot see; a plain $NAME argument (`echo $HOME`) is only a value.
    if ("`" in raw or "$(" in raw or "${" in _SAFE_PARAM_RE.sub("", raw)
            or re.search(r"\$\{?IFS\b", raw) or "\\\n" in raw):
        return Verdict(DANGEROUS, "shell expansion or escaping requires an owner decision")
    for pat, why in _DANGEROUS_RAW:
        if env_ok and pat is _ENV_DUMP_RE:
            continue
        if pat.search(raw):
            return Verdict(DANGEROUS, why)
    return Verdict(OK)


#: Top-level trees where a person's own folders live (a project is fine there).
_USER_TREES = frozenset({"/home", "/Users", "/Volumes"})


_LN_SYMLINK_FLAG_RE = re.compile(r"^-[A-Za-z]*s[A-Za-z]*$")


def _makes_symlink(segments: List[List[str]]) -> bool:
    """True when the line creates a symlink (``ln -s``, ``ln --symbolic``,
    ``cp -s``): a later relative delete in the same line can walk through it,
    and the link does not exist yet when this check reads the disk."""
    for seg in segments:
        for name, args in _candidates(seg):
            if name in ("ln", "cp") and any(
                    a in ("--symbolic", "--symbolic-link") or _LN_SYMLINK_FLAG_RE.match(a)
                    for a in args):
                return True
    return False


def _resolve(path: str, keep_last: bool) -> str:
    """``path`` with every symlink resolved — the final component kept as a name
    when ``keep_last`` (``rm -r link`` removes the link, not what it points at)."""
    if keep_last:
        head, tail = os.path.split(path)
        return os.path.join(os.path.realpath(head or "/"), tail)
    return os.path.realpath(path)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def relative_delete_refusal(command: str, cwd: Optional[str],
                            home: Optional[str] = None,
                            data_home: Optional[str] = None,
                            resolve_links: bool = False) -> Optional[str]:
    """The guard answers ``ok`` to a relative recursive delete (``rm -rf build``)
    because it lands below the working directory. The shell keeps its cwd across
    calls, so the tool asks this with the REAL cwd: a reason string when that
    directory is the filesystem root, a system tree, the home folder or one of its
    parents, or the agent's own state — else None.

    ``resolve_links`` (the host backend, whose cwd is this machine's): the target
    is also checked after resolving symlinks, so ``ln -s .. up`` (earlier) then
    ``rm -rf up/data`` is the delete of ``../data`` it really is. A line that
    CREATES a symlink and also deletes recursively is refused outright — the link
    does not exist yet when the disk is read. ``data_home`` (the agent's data
    directory) and every parent of it are never a relative delete target, nor is
    anything inside it but a session workspace."""
    tokens = _lex(command or "")
    if tokens is None or not cwd:
        return None
    base = os.path.normpath(cwd)
    home_n = os.path.normpath(home) if home else None
    data_n = os.path.normpath(data_home) if data_home else None
    data_r = os.path.realpath(data_n) if (data_n and resolve_links) else data_n
    segments = _segments(tokens)
    links_made = _makes_symlink(segments)

    def _state_hit(path: str, data_root: Optional[str]) -> bool:
        if _PROTECTED_PATH_RE.search(_mask_own_workspace(path)):
            return True
        if re.search(r"/\.polyrob(/data)?$", path):
            return True
        if data_root:
            if _under(data_root, path):          # the data home or a parent of it
                return True
            if _under(path, data_root) and not _own_work(path, data_root):
                return True
        return False

    def _own_work(path: str, data_root: str) -> bool:
        """The agent's own work folders inside the data home: a session workspace
        (``<data>/auto/…`` or, on a server, ``<data>/data/auto/…``) and the shared
        project folder (``POLYROB_PROJECT_DIR``, prod ``/var/lib/polyrob/project``)
        — never a ``.polyrob`` state dir, an env file or a ``..`` inside them."""
        if re.match(re.escape(data_root.rstrip(os.sep))
                    + r"/(data/)?auto/[^/]+/sessions/[^/]+/workspace(/|$)", path):
            return True
        m = _OWN_WORKSPACE_RE.fullmatch(path)
        if m and (m.group(1) or "/") != "/" and _mask_own_workspace(path) == "_own_workspace_":
            return True
        project = (os.environ.get("POLYROB_PROJECT_DIR") or "").strip()
        if project:
            proj = os.path.normpath(project)
            proj_r = os.path.realpath(proj) if resolve_links else proj
            for p in {proj, proj_r}:
                # Strictly BELOW the project folder (never the folder itself).
                if p == data_root or path == p or not _under(path, p) \
                        or not _under(p, data_root):
                    continue
                tail = path[len(p):]
                if not (re.search(r"(^|/)\.polyrob(/|$)", tail)
                        or re.search(r"(^|/)[^/]*\.env\b", tail)):
                    return True
        return False

    def _refusal(t: str, full: str, why: str = "") -> str:
        return (f"refused: `rm -r {t}` here would delete {full}{why}, and that is not "
                f"inside a project folder below the shell's working directory ({base}). "
                "cd into the project first, or name the absolute path (the owner decides).")

    for seg in segments:
        for name, args in _candidates(seg):
            # A relative `cd` earlier in the line moves where the delete lands
            # (`classify` already refuses any other cd before a relative delete).
            if name in ("cd", "pushd", "chdir"):
                words = [a for a in args if a != "--"]
                if len(words) == 1 and _relative_work_path(words[0]):
                    base = os.path.normpath(os.path.join(base, words[0]))
                continue
            if name not in _RM_NAMES:
                continue
            if not any(a == "--recursive" or (a.startswith("-") and not a.startswith("--")
                                              and ("r" in a or "R" in a)) for a in args):
                continue
            for a in args:
                t = _norm_target(a)
                if not _relative_work_path(t):
                    continue
                full = os.path.normpath(os.path.join(base, t))
                if links_made:
                    return (f"refused: this line creates a symlink and deletes `{t}` "
                            "recursively; the delete could follow the new link out of "
                            "the working directory. Run them as two commands (the owner "
                            "decides a delete through a link).")
                top = "/" + full.split("/")[1] if full.startswith("/") else ""
                own = _mask_own_workspace(full).startswith("_own_workspace_")
                system = top in _FLOOR_DIRS and top not in _USER_TREES and not own
                if (system or base in _FLOOR_DIRS
                        or re.fullmatch(r"/(home|Users)/[^/]+", base)
                        or (home_n and (base == home_n or home_n.startswith(base + os.sep)))
                        or _state_hit(full, data_n)):
                    return _refusal(t, full)
                if resolve_links:
                    keep_last = not (a.rstrip().endswith("/") or re.search(r"[*?\[]", t))
                    real = _resolve(full, keep_last)
                    real_base = os.path.realpath(base)
                    if not _under(real, real_base):
                        return _refusal(t, real, " (through a symlink)")
                    if _state_hit(real, data_r):
                        return _refusal(t, real)
    return None


_ESCALATION_WORD_RE = re.compile(r"(^|[;&|(`\s])(sudo|doas|su|pkexec)(\s|$)")
#: Heads whose quoted argument is itself run as a shell line (``bash -c "…"``).
_RUNS_QUOTED_TEXT_RE = re.compile(r"^(" + _INTERPRETERS_RE.pattern + r"|ssh|eval|su|watch)$")


def mentions_sudo(command: str, _depth: int = 0) -> bool:
    """True when any statement of ``command`` RUNS ``sudo``/``doas``/``su``/``pkexec``
    — as a command, after a wrapper (``xargs sudo``), in a ``$(…)``/backtick, or in
    the quoted line an interpreter runs (``bash -c "sudo …"``). The word as an
    argument (``grep -rn sudo docs/``, ``which sudo``, ``echo su``) is not a run.
    Text that does not lex falls back to the plain word match (the wider answer)."""
    text = command or ""
    tokens = _lex(text)
    if tokens is None or _depth > 3:
        return bool(_ESCALATION_WORD_RE.search(text))
    for tok in tokens:
        # A substitution runs wherever it stands, inside double quotes too.
        if ("$(" in tok or "`" in tok) and mentions_sudo(
                tok.replace("$(", " ; ").replace("`", " ; "), _depth + 1):
            return True
    for segment in _segments(tokens):
        for name, _args in _candidates(segment):
            if name in _AS_OTHER_USER:
                return True
        head = _candidates(segment)[:1]
        runs_quoted = bool(head and _RUNS_QUOTED_TEXT_RE.match(head[0][0]))
        for tok in segment:
            if runs_quoted and any(ch.isspace() for ch in tok) and mentions_sudo(tok, _depth + 1):
                return True
    return False


def parse_globs(raw) -> Tuple[str, ...]:
    """A comma/newline list (env) or a list (pref) -> clean glob patterns."""
    if raw is None:
        return ()
    items = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).replace("\n", ",").split(",")
    return tuple(sorted({str(i).strip() for i in items if str(i).strip()}))


def matches_any(command: str, globs: Sequence[str], *, for_allow: bool = False) -> Optional[str]:
    """The first glob (``fnmatch``, case-sensitive) matching the WHOLE command line
    — whitespace-normalised — or None. ``git push*`` matches ``git push origin``."""
    import fnmatch
    # A wildcard grants arguments to ONE literal command, never a new statement,
    # expansion, redirection or script embedded after the allowed prefix. Deny
    # rules remain broad: narrowing them here would create a separate bypass.
    if for_allow and (any(c in (command or "") for c in "\n\r;&|()<>$`\\")
                      or _lex(command or "") is None):
        return None
    line = " ".join((command or "").split())
    for g in globs or ():
        if fnmatch.fnmatchcase(line, " ".join(g.split())):
            return g
    return None


def glob_for(command: str) -> str:
    """The exact-command pattern an "always allow" writes (glob metachars escaped)."""
    line = " ".join((command or "").split())
    return "".join(f"[{c}]" if c in "*?[" else c for c in line)


__all__ = ["Verdict", "classify", "mentions_sudo", "relative_delete_refusal", "parse_globs",
           "matches_any", "glob_for", "FLOOR", "DANGEROUS", "OK"]
