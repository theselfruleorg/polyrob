"""Doctor's own alerts, folded into the status snapshot's health list BEFORE the
headline is rendered (proposal 116 W5.10, W7.4).

`polyrob doctor` printed its credential and host alerts AFTER the snapshot's
headline — "Health: OK" over a "! no provider credential found" line — and,
once the snapshot raised its own ``no_provider`` CRIT, printed that fact a
second time below it. Now one fact is one health item:

- a credential alert the snapshot's providers section already carries
  (``no_provider``, ``no_live_provider``, ``providers_unreadable``, a tripped
  credit sentinel) is not printed again; the one reason only doctor can see (a
  key that is present but malformed or expired) is added to the snapshot's item;
- a fact only doctor knows (a credential alert the snapshot did not raise, a
  CRITICAL host item) becomes a CRIT health item, so the headline counts it.
"""
import dataclasses
import os
from typing import List, Optional

#: The providers-section items under which a credential alert is the same fact.
_PROVIDER_KEYS = ("no_provider", "no_live_provider", "providers_unreadable")


def _covers_providers(keys) -> bool:
    return any(k in _PROVIDER_KEYS or k.startswith("credit_sentinel:") for k in keys)


def _split(line: str) -> tuple:
    """``"  ! what — remedy"`` / ``"CRITICAL what"`` -> ``(what, remedy)``."""
    body = line.strip().lstrip("!").strip()
    if body.upper().startswith("CRITICAL"):
        body = body[len("CRITICAL"):].strip(" :")
    what, sep, remedy = body.partition(" — ")
    return what.strip(), (remedy.strip() if sep else "")


def fold_doctor_alerts(snap, credential_lines: List[str], host_criticals: List[str]):
    """A copy of *snap* whose health list holds doctor's alerts (the cached
    snapshot is never changed). *snap* itself when there is nothing to fold."""
    from core.status_types import SEVERITY_CRIT, HealthItem
    health = list(snap.health)
    keys = {h.key for h in health}
    added = []
    for line in credential_lines:
        what, remedy = _split(line)
        if not _covers_providers(keys):
            added.append(HealthItem(key="doctor_credentials", severity=SEVERITY_CRIT,
                                    text=what, remedy=remedy))
        elif what.startswith("no usable") and remedy and "no_provider" in keys:
            health = [dataclasses.replace(h, text=f"{h.text} (a credential is present but "
                                                  f"not usable: {remedy})")
                      if h.key == "no_provider" else h for h in health]
    for i, line in enumerate(host_criticals):
        what, remedy = _split(line)
        added.append(HealthItem(key=f"host:{i}", severity=SEVERITY_CRIT, text=what,
                                remedy=remedy))
    if not added and health == list(snap.health):
        return snap
    return dataclasses.replace(snap, health=added + health)


def doctor_view(snap, env: Optional[dict] = None):
    """The snapshot doctor renders: its own credential and CRITICAL host alerts
    folded in. The credential check runs only when the providers section shows
    no live and no usable provider (the old gate)."""
    from cli.commands import doctor
    from cli.commands.doctor_host import host_readiness_lines
    env = dict(os.environ) if env is None else env
    prov = (snap.sections or {}).get("providers")
    data = prov.data if (prov is not None and prov.available) else {}
    creds = [] if (data.get("live") or data.get("usable")) else doctor.credential_alert_lines(env)
    host = [ln for ln in host_readiness_lines(env) if ln.startswith("CRITICAL")]
    return fold_doctor_alerts(snap, creds, host)
