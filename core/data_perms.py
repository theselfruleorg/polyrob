"""Is the shared data home still writable by every identity that shares it?

WS-G (proposal 057 R6). Three de-rooted units (`polyrob`, `polyrob-email`,
`polyrob-webview`) and four root units write ONE directory as members of the
`polyrob-data` group. A single root-run step — an owner CLI verb typed without
`sudo -u`, a deploy-time import test, an ad-hoc script — creates a file the
other members can never write, and the failure surfaces hours later as
``sqlite3.OperationalError: attempt to write a readonly database`` in a unit
that did nothing wrong (prod 2026-09-18 15:47Z, 2026-09-19 17:09Z).

Until this module there was no ``S_IWGRP`` check anywhere in the tree: the
deploy repaired ``wallet/`` and ``auto/`` only, and ``doctor`` had nothing to
say about permissions at all.

The audit is a pure read (it never chowns, chmods or creates anything) and it
is deliberately CHEAP: a bounded walk with an entry budget, so a
several-hundred-thousand-file session tree costs a bounded stat sweep rather
than an unbounded one. On a box where the group does not exist — every dev
checkout — it SKIPS and says so; a skipped audit must never render as a clean
one.

Rules, per entry (symlinks are never followed and never judged):

* every file and directory must have group ``polyrob-data``;
* every directory must be group-writable AND ``setgid`` (so the group is
  inherited by whatever the next process creates in it);
* every file must be group-writable;
* nothing under ``auto/``, ``wallet/`` or ``locks/`` may be owned by root;
* ``wallet/`` is the ONE documented exception to group-write: the deploy pins
  it to ``polyrob-agent:polyrob-data`` mode 750 on purpose (only the agent
  identity signs), so its subtree is judged on group + root-ownership only.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from typing import List, Optional

#: The group every shared-data identity is a member of
#: (``deployment/hardening/install-service-identities.sh``).
DEFAULT_DATA_GROUP = "polyrob-data"

#: Subtrees where a ROOT-owned entry is itself the defect: the agent's session
#: tree, the wallet directory, and the cross-process turn/tick locks.
ROOT_SENSITIVE_DIRS = ("auto", "wallet", "locks")

#: Judged on group + root-ownership only — mode 750 is the deploy's rule.
GROUP_WRITE_EXEMPT_DIRS = ("wallet",)

#: Never walked: caches and VCS metadata carry no shared state.
_SKIP_NAMES = frozenset({"__pycache__", ".git", "node_modules"})

REASON_WRONG_GROUP = "wrong_group"
REASON_NOT_GROUP_WRITABLE = "not_group_writable"
REASON_NO_SETGID = "no_setgid"
REASON_ROOT_OWNED = "root_owned"


def apply_birth_mode(path) -> None:
    """Give a file the audit's own rule at birth: group-writable, never world-writable.

    The ONE place the rule lives. `tempfile.mkstemp` births a file 0600 and
    SQLite births one 0644 whatever the umask says, so every writer that
    creates a store inside the shared data home (the session state/tool-call
    checkpoints, `control.sqlite3`, every `init_schema` db) lands an entry the
    sibling identities cannot write until the next deploy's chmod pass — and
    the audit above then names the agent's OWN files as offenders (19 of them,
    prod 2026-09-21 00:08Z), burying a real one. Never widens beyond group;
    fail-open (a missing path or a foreign filesystem is not the writer's
    problem). The directory's group is inherited via setgid, not set here.
    """
    try:
        mode = os.stat(path).st_mode & 0o777
        os.chmod(path, (mode | 0o060) & ~0o002)
    except OSError:
        pass


def _max_entries() -> int:
    from core.env import int_env
    return int_env("DATA_PERMS_MAX_ENTRIES", 200_000)


@dataclass
class PermsOffender:
    path: str
    kind: str  # "dir" | "file"
    reasons: List[str]
    group: str = ""
    owner: str = ""
    mode: str = ""

    def render(self) -> str:
        bits = ",".join(self.reasons)
        extra = f" [{self.owner}:{self.group} {self.mode}]" if self.mode else ""
        return f"{self.kind} {self.path}: {bits}{extra}"


@dataclass
class PermsReport:
    """Counts + the first N offenders. ``skipped`` is NOT ``ok``."""

    data_dir: str
    group: str = DEFAULT_DATA_GROUP
    gid: Optional[int] = None
    group_exists: bool = True
    scanned: int = 0
    truncated: bool = False
    skipped: bool = False
    skip_reason: str = ""
    counts: dict = field(default_factory=dict)
    offenders: List[PermsOffender] = field(default_factory=list)
    offender_total: int = 0

    @property
    def ok(self) -> bool:
        """True only when the audit RAN and found nothing."""
        return not self.skipped and self.offender_total == 0

    @property
    def headline(self) -> str:
        if self.skipped:
            return f"data perms: not checked ({self.skip_reason})"
        if self.offender_total == 0:
            return (f"data perms: OK ({self.scanned} entries, group {self.group}, "
                    f"group-writable, setgid dirs)")
        parts = [f"{k}={v}" for k, v in sorted(self.counts.items()) if v]
        return (f"data perms: {self.offender_total} offender(s) in {self.data_dir} "
                f"({', '.join(parts)})")

    @property
    def remedy(self) -> str:
        return ("re-run the deploy ownership pass, or by hand: "
                f"chgrp -R {self.group} {self.data_dir} && chmod -R g+rwX {self.data_dir} && "
                f"find {self.data_dir} -type d -exec chmod g+s {{}} + "
                "(and run owner CLI verbs as `sudo -u polyrob-agent polyrob …`)")


def _resolve_gid(group: str) -> Optional[int]:
    try:
        import grp  # POSIX only
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    try:
        return grp.getgrnam(group).gr_gid
    except (KeyError, OSError):
        return None


def _name_for_gid(gid: int) -> str:
    try:
        import grp
        return grp.getgrgid(gid).gr_name
    except Exception:
        return str(gid)


def _name_for_uid(uid: int) -> str:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except Exception:
        return str(uid)


def _rel_top(data_dir: str, path: str) -> str:
    try:
        rel = os.path.relpath(path, data_dir)
    except ValueError:  # pragma: no cover - different drives (win)
        return ""
    if rel in (".", os.pardir) or rel.startswith(os.pardir):
        return ""
    return rel.split(os.sep, 1)[0]


def audit_data_perms(data_dir: str, group: str = DEFAULT_DATA_GROUP,
                     *, max_offenders: int = 20,
                     max_entries: Optional[int] = None) -> PermsReport:
    """Walk ``data_dir`` and name every entry the shared group cannot write.

    Never raises: an unreadable tree, a missing group or a non-POSIX host all
    come back as ``skipped`` with the reason. Pure read.
    """
    report = PermsReport(data_dir=data_dir, group=group)
    if not data_dir or not os.path.isdir(data_dir):
        report.skipped = True
        report.skip_reason = f"no data home at {data_dir or '<unset>'}"
        return report
    if not hasattr(os, "getuid"):  # pragma: no cover - non-POSIX
        report.skipped = True
        report.skip_reason = "not a POSIX host"
        return report
    gid = _resolve_gid(group)
    if gid is None:
        report.group_exists = False
        report.skipped = True
        report.skip_reason = (
            f"group {group} does not exist here (dev checkout — the shared-identity "
            f"rule applies to a deployed box only)")
        return report
    report.gid = gid

    budget = _max_entries() if max_entries is None else max_entries
    counts = {REASON_WRONG_GROUP: 0, REASON_NOT_GROUP_WRITABLE: 0,
              REASON_NO_SETGID: 0, REASON_ROOT_OWNED: 0}

    def judge(path: str, st: os.stat_result, is_dir: bool) -> List[str]:
        top = _rel_top(data_dir, path)
        reasons: List[str] = []
        if st.st_gid != gid:
            reasons.append(REASON_WRONG_GROUP)
        if top not in GROUP_WRITE_EXEMPT_DIRS:
            if not st.st_mode & stat.S_IWGRP:
                reasons.append(REASON_NOT_GROUP_WRITABLE)
            if is_dir and not st.st_mode & stat.S_ISGID:
                reasons.append(REASON_NO_SETGID)
        if st.st_uid == 0 and top in ROOT_SENSITIVE_DIRS:
            reasons.append(REASON_ROOT_OWNED)
        return reasons

    def record(path: str, st: os.stat_result, is_dir: bool, reasons: List[str]) -> None:
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
        report.offender_total += 1
        if len(report.offenders) < max_offenders:
            report.offenders.append(PermsOffender(
                path=path, kind="dir" if is_dir else "file", reasons=reasons,
                group=_name_for_gid(st.st_gid), owner=_name_for_uid(st.st_uid),
                mode=oct(stat.S_IMODE(st.st_mode))))

    try:
        root_st = os.lstat(data_dir)
    except OSError as e:
        report.skipped = True
        report.skip_reason = f"cannot stat {data_dir}: {e}"
        return report
    report.scanned += 1
    root_reasons = judge(data_dir, root_st, True)
    if root_reasons:
        record(data_dir, root_st, True, root_reasons)

    for dirpath, dirnames, filenames in os.walk(data_dir, topdown=True,
                                                onerror=None, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_NAMES]
        for name, is_dir in [(d, True) for d in dirnames] + [(f, False) for f in filenames]:
            if report.scanned >= budget:
                report.truncated = True
                break
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue  # a symlink's own mode is meaningless; the target is walked
            report.scanned += 1
            reasons = judge(path, st, is_dir)
            if reasons:
                record(path, st, is_dir, reasons)
        if report.truncated:
            break
    report.counts = counts
    return report


def render_perms_lines(report: PermsReport) -> List[str]:
    """The ``doctor --perms`` body — one headline, then the first N offenders."""
    lines = [report.headline]
    if report.truncated:
        lines.append(f"  (walk stopped at the {report.scanned}-entry budget — "
                     f"raise DATA_PERMS_MAX_ENTRIES to scan the whole tree)")
    for off in report.offenders:
        lines.append(f"  {off.render()}")
    extra = report.offender_total - len(report.offenders)
    if extra > 0:
        lines.append(f"  … and {extra} more")
    if not report.ok and not report.skipped:
        lines.append(f"  remedy: {report.remedy}")
    return lines
