"""The host's own state, read-only: what the owner must act on outside the agent.

The operator's ``scripts/host_baseline.sh`` SETS the host baseline at the first install
(a public install follows docs/guide/self-hosting.md, "Host baseline"); this module
only READS whether the host still holds it, for the two places the owner already looks:
``polyrob doctor`` (via ``cli.commands.doctor_host``) and the deploy output
(``python -m core.host_baseline`` after VERIFY OK). Each line names one state and the
act that fixes it. It never edits the host and never raises.

A box made on 2026-11-21 accepted SSH passwords, held a kernel update with no word to
anyone, and ran its agent unit started but not enabled (gone after a reboot). Those are
the lines here. Off a systemd Linux host (a laptop install) it says nothing.
"""
from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Sequence

#: The long-running family of ``scripts/deploy_transaction.sh::deploy_capture_units``.
FAMILY_UNITS = (
    "polyrob.service", "polyrob-email.service", "polyrob-gateway.service",
    "polyrob-webview.service", "polyrob-x402-api.service", "polyrob-apps.service",
    "polyrob-browser-server.service", "polyrob-signer.service",
)

Runner = Callable[[Sequence[str]], Optional[str]]


def _run(argv: Sequence[str]) -> Optional[str]:
    """stdout+stderr of ``argv``, or None when it cannot run."""
    try:
        cp = subprocess.run(list(argv), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return (cp.stdout or "") + (cp.stderr or "")


def _sshd_first(root: Path, keyword: str) -> Optional[str]:
    """The value sshd uses for ``keyword``: the FIRST one met, Includes expanded in
    place (sorted), global section only (a ``Match`` block ends it). None = unread."""
    keyword = keyword.lower()

    def walk(path: Path, depth: int) -> Optional[str]:
        if depth > 8:
            return None
        for raw in path.read_text(errors="replace").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.replace("=", " ", 1).split(None, 1)
            key, val = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
            if key == "match":
                return None
            if key == "include":
                for pattern in val.split():
                    if not pattern.startswith("/"):
                        pattern = "/etc/ssh/" + pattern
                    for inc in sorted(glob.glob(str(root) + pattern)):
                        found = walk(Path(inc), depth + 1)
                        if found is not None:
                            return found
                continue
            if key == keyword:
                return val.split()[0].lower() if val else None
        return None

    return walk(root / "etc/ssh/sshd_config", 0)


def ssh_lines(root: Path) -> List[str]:
    cfg = root / "etc/ssh/sshd_config"
    if not cfg.exists():
        return []
    try:
        value = _sshd_first(root, "PasswordAuthentication") or "yes"  # the sshd default
    except OSError as exc:
        return [f"host: SSH password login: unknown (cannot read the sshd config: {type(exc).__name__})"]
    if value == "yes":
        return ["host: SSH accepts password logins — once a key login works, turn passwords off "
                "(docs/guide/self-hosting.md, \"Host baseline\")"]
    return []


def update_lines(root: Path, run: Runner) -> List[str]:
    out: List[str] = []
    flag = root / "var/run/reboot-required"
    if flag.exists():
        pkgs = root / "var/run/reboot-required.pkgs"
        try:
            names = sorted(set(pkgs.read_text().split())) if pkgs.exists() else []
        except OSError:
            names = []
        why = f" ({', '.join(names[:4])}{' …' if len(names) > 4 else ''})" if names else ""
        out.append(f"host: a reboot is required{why} — reboot while the agent is idle "
                   "(no cron job, goal or live turn running)")
    checker = root / "usr/lib/update-notifier/apt-check"
    if checker.exists():
        text = run([str(checker)])
        if text is None:
            out.append("host: pending updates: unknown (apt-check did not run)")
        else:
            try:
                total, security = (int(x) for x in text.strip().splitlines()[-1].split(";")[:2])
            except (ValueError, IndexError):
                out.append("host: pending updates: unknown (apt-check output not understood)")
            else:
                if total:
                    out.append(f"host: {total} package update(s) wait, {security} of them security — "
                               "apt-get upgrade --with-new-pkgs")
    return out


def unit_lines(run: Runner, units: Sequence[str] = FAMILY_UNITS) -> List[str]:
    out: List[str] = []
    for unit in units:
        active = (run(["systemctl", "is-active", unit]) or "").strip()
        if active != "active":
            continue
        enabled = (run(["systemctl", "is-enabled", unit]) or "").strip() or "unknown"
        if enabled not in ("enabled", "static", "indirect", "generated", "alias"):
            out.append(f"host: {unit} runs but is {enabled}: it does not start after a reboot — "
                       f"systemctl enable {unit}")
    return out


def host_baseline_lines(env: Optional[dict] = None, *, root: str = "/",
                        run: Runner = _run) -> List[str]:
    """Every host line, or [] off a systemd Linux host. ``env`` is the doctor signature."""
    base = Path(root)
    if not (base / "run/systemd/system").is_dir():
        return []
    out: List[str] = []
    for part in (lambda: ssh_lines(base), lambda: update_lines(base, run), lambda: unit_lines(run)):
        try:
            out.extend(part())
        except Exception as exc:  # noqa: BLE001 — a probe never fails its caller
            out.append(f"host: check failed ({type(exc).__name__})")
    return out


if __name__ == "__main__":  # the deploy prints these after VERIFY OK
    for line in host_baseline_lines(dict(os.environ)):
        print(line)
