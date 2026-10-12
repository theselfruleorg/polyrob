"""Status sections: ``posture``, ``security`` and ``identity`` (instance,
on-chain identity, reach, the X logins and the browser rail).

Moved out of ``core/status_snapshot.py`` (W9, 2026-10-10 structural review)
unchanged. ``core.status_snapshot`` stays the ONE assembler and re-exports
every name here; the store reads go through ``core.status_snapshot._rows``
late-bound, so a test that patches it still reaches this builder.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Tuple

from core.status_types import HealthItem, SEVERITY_WARN, Section


def _posture_section() -> Section:
    from core.config_policy.posture_card import build_posture_card, render_posture_card
    rows = build_posture_card()
    sec = Section(name="posture", lines=render_posture_card(rows), data={"rows": rows})
    _member_env_health(sec)
    return sec


def _member_env_health(sec: Section) -> None:
    """D7: a posture member set on its own env line is IGNORED (the bundle and
    POLYROB_OVERRIDES are the only source). Say so with the exact replacement
    line; an ignored POLYROB_OVERRIDES entry is named too. Never a refusal."""
    from core.config_policy.members import (members_set_directly, override_problems,
                                            replacement_line)
    direct = members_set_directly()
    if direct:
        sec.data["members_set_directly"] = [n for n, _ in direct]
        sec.health.append(HealthItem(
            key="posture_member_direct", severity=SEVERITY_WARN,
            text=("ignored env line(s) — a posture member is set only by its bundle or "
                  "POLYROB_OVERRIDES: " + ", ".join(f"{n}={v}" for n, v in direct)),
            remedy=f"replace them with one line: {replacement_line()}"))
    problems = override_problems()
    if problems:
        sec.data["override_problems"] = [p.text for p in problems]
        sec.health.append(HealthItem(
            key="posture_overrides_invalid", severity=SEVERITY_WARN,
            text="POLYROB_OVERRIDES entries ignored: " + "; ".join(p.text for p in problems),
            remedy="fix or delete those entries (NAME=true|false, posture members only)"))


def _security_section(user_id: str, data_dir: str, now: float,
                      window_sec: int) -> Section:
    """045: the perimeter, the refusals and the threat flags (lanes 1-3).

    Counted by core/security_digest.py — this section only shapes the result and
    raises health items. An unreadable store propagates out of here so _guarded
    renders `unavailable(<reason>)`: 'no store' must never look like 'zero
    denials'.
    """
    from core.security_digest import build_security_rollup
    r = build_security_rollup(user_id, data_dir=data_dir,
                              since_ts=now - window_sec, window_sec=window_sec)
    sec = Section(name="security")
    sec.data.update({
        "inbound": r.inbound, "denied": r.denied, "refused": r.refused,
        "flagged": r.flagged, "rate_limited": r.rate_limited,
        "scan": getattr(r, "scan", "not run"),
        "top_senders": r.top_senders,
        "top_denial_reasons": r.top_denial_reasons,
        "top_refusal_reasons": r.top_refusal_reasons,
    })
    sec.lines.append(f"{r.inbound} inbound / {r.denied} denied · "
                     f"{r.refused} refused · {r.flagged} flagged · "
                     f"{r.rate_limited} rate-limited · scan {getattr(r, 'scan', 'not run')}")
    if r.flagged:
        sec.health.append(HealthItem(
            key="injection_flagged", severity=SEVERITY_WARN,
            text=f"{r.flagged} threat-scan flag(s) in the window",
            remedy="review injection_flagged rows in telemetry_events"))
    # 057 WS-G: a shared data home the sibling identities cannot write is a
    # security fact, not an ops chore — it is what turns a de-rooted unit into
    # "attempt to write a readonly database". Bounded walk; a box with no
    # `polyrob-data` group SKIPS and says so (a skipped audit is never `ok`).
    try:
        from core.data_perms import audit_data_perms
        perms = audit_data_perms(data_dir, max_offenders=3, max_entries=2000)
        sec.data["data_perms"] = {
            "skipped": perms.skipped, "reason": perms.skip_reason,
            "scanned": perms.scanned, "truncated": perms.truncated,
            "offenders": perms.offender_total, "counts": perms.counts,
            "first": [o.render() for o in perms.offenders],
        }
        sec.lines.append(perms.headline)
        if not perms.skipped and perms.offender_total:
            sec.health.append(HealthItem(
                key="data_perms", severity=SEVERITY_WARN,
                text=(f"{perms.offender_total} entr(ies) under {data_dir} the shared "
                      f"group cannot write" + (" (first 2000 checked)" if perms.truncated else "")
                      + ": " + "; ".join(o.path for o in perms.offenders)),
                remedy="re-run your deploy's data-ownership pass, or "
                       "`polyrob doctor --perms` to name them all"))
    except Exception as e:  # the walk must never take the security section down
        sec.lines.append(f"data perms: not checked ({type(e).__name__}: {e})")
    return sec


def _identity_section(instance_id: str, data_dir: Optional[str]) -> Section:
    """WHO the agent is: its instance, on-chain identity, reach and avatar image.

    The avatar is ONE image slot (``core.avatar``): set (and from where), not
    set, or unreadable. Core generates no face, so no generator, trait or voice
    is reported.

    ⚠️ An absent avatar is ``ok``, not ``degraded``, and raises NO health item.
    Setup is optional; a permanent WARN for an optional step is the noise that
    teaches an owner to skip the health block. An UNREADABLE slot is different —
    an unreadable store is not an empty one — so it raises one WARN.

    ⚠️ Read-only, and it never CREATES the identity directory — a status surface
    that writes is how an empty store becomes a real one.
    """
    from core.avatar import describe, load_avatar
    from core.instance import load_erc8004_record

    sec = Section(name="identity")
    if not data_dir:
        raise ValueError("no data dir to read the instance identity from")
    sec.data["instance_id"] = instance_id

    # 046: the ERC-8004 on-chain identity, when one was actually registered.
    # Reported BEFORE the avatar branches below return, so an instance with no
    # avatar still shows its on-chain identity and vice versa.
    record = load_erc8004_record(data_dir, instance_id)
    sec.data["erc8004"] = record
    if record:
        sec.lines.append(
            f"erc-8004: agentId {record.get('agent_id')} on "
            f"{record.get('chain')} ({record.get('registry')})")
    else:
        # Optional, like the avatar — an absent identity is a FACT the owner
        # should be able to read, not an empty space, and it raises no health
        # item because a permanent WARN for an optional step is the noise that
        # teaches an owner to skip the health block.
        sec.lines.append("erc-8004: not registered (optional)")

    # The version of the code that RUNS (the install line below is history).
    try:
        from core.version import get_version, deployed_sha, running_version_line
        sec.data["version"] = get_version()
        sec.data["deployed_sha"] = deployed_sha()
        sec.lines.append(f"running: {running_version_line()}")
    except Exception as exc:
        sec.lines.append(f"running: unreadable ({type(exc).__name__})")
    # 077: the service's own start record, a waiting restart and its outcome.
    try:
        from core.lifecycle import status_facts
        facts = status_facts(data_dir)
        sec.lines.extend(facts["lines"])
        sec.data.update(facts["data"])
        sec.health.extend(HealthItem(key=k, text=t, remedy=r) for k, t, r in facts["health"])
    except Exception as exc:  # noqa: BLE001 — one fact, never the whole section
        sec.lines.append(f"service: unreadable ({type(exc).__name__})")

    # 062: HOW this instance was installed. The record lives in the per-user
    # config home, not the (per-project) data home, so it reads the same from
    # every directory; an absent one says "not recorded", never a guess.
    try:
        from core.bootstrap_marker import describe as _describe_bootstrap
        from core.bootstrap_marker import read_marker as _read_marker
        _status, _record = _read_marker()
        sec.data["install"] = _record or {"status": _status}
        sec.lines.append(_describe_bootstrap().replace("bootstrap: ", "install: ", 1))
    except Exception as exc:
        sec.data["install"] = {"status": "unreadable"}
        sec.lines.append(f"install: unreadable ({type(exc).__name__})")

    # 2026-09-17: the agent's OWN reachable identities — the address it sends
    # mail AS and the X account it posts AS. Until now no seat could answer
    # "do I have an email / an X account", so a bootstrap skill had to guess.
    # Env PRESENCE only, never a value; the X browser-session record lives in
    # the tools tier and is checked by the agent's own `x_login_check` verb.
    _identity_reach_lines(sec, data_dir)

    st = load_avatar(data_dir, instance_id)
    sec.data["avatar"] = st.state
    sec.data["avatar_default"] = st.is_default
    if st.is_set:
        sec.data.update({"avatar_source": st.source, "path": str(st.path),
                         "avatar_set_at": st.set_at})
    line = f"{instance_id} — avatar {describe(st)}"
    if st.state == "none":
        line += " (optional: `polyrob avatar set <image>`)"
    sec.lines.append(line)
    if st.state == "unreadable":
        sec.health.append(HealthItem(
            key="avatar_unreadable",
            text=f"the avatar slot is unreadable: {st.detail or 'unknown'}",
            remedy="set it again with `polyrob avatar set <image>`, or "
                   "`polyrob avatar clear`"))
    return sec


def _identity_reach_lines(sec: Section, data_dir: Optional[str]) -> None:
    """Append `email:` and `x:` facts to the identity section (read-only).

    email — `resolve_agent_email` (explicit override → provisioned AgentMail
    inbox → legacy GMAIL login), or `none` with the remedy. An unset
    `AGENTMAIL_API_KEY` is named so the owner knows which lever provisions one.
    x — the API rail is "configured" when the four OAuth 1.0a keys are all
    present (`TWITTER_ENABLED` decides whether WRITES are armed); the handle is
    `TWITTER_BOT_USERNAME` when set. Nothing here reads a secret VALUE.
    """
    from core.instance import resolve_agent_email
    email = None
    try:
        email = resolve_agent_email(data_home=Path(data_dir) if data_dir else None)
    except Exception as e:
        sec.lines.append(f"email: unreadable ({type(e).__name__})")
    else:
        sec.data["email"] = email
        if email:
            sec.lines.append(f"email: {email}")
        else:
            has_key = bool((os.environ.get("AGENTMAIL_API_KEY") or "").strip())
            sec.lines.append(
                "email: none — " + ("AGENTMAIL_API_KEY is set; the inbox is provisioned "
                                    "the first time the email tool initialises"
                                    if has_key else
                                    "set AGENTMAIL_API_KEY (own inbox) or GMAIL_EMAIL/"
                                    "GMAIL_APP_PASSWORD (SMTP)"))
    api_keys = ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
                "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET")
    present = [k for k in api_keys if (os.environ.get(k) or "").strip()]
    handle = (os.environ.get("TWITTER_BOT_USERNAME") or "").strip().lstrip("@")
    from core.config_policy.members import member_flag
    writes = member_flag("TWITTER_ENABLED")
    sec.data["x_api"] = "configured" if len(present) == 4 else (
        "partial" if present else "none")
    sec.data["x_handle"] = handle or None
    if len(present) == 4:
        sec.lines.append(
            f"x: api configured{' @' + handle if handle else ''} — writes "
            f"{'ON' if writes else 'OFF (TWITTER_ENABLED)'}; X login session: ask x_login_check")
    elif present:
        missing = [k for k in api_keys if k not in present]
        sec.lines.append(f"x: api PARTIAL — missing {', '.join(missing)}")
    else:
        sec.lines.append("x: no api keys — create an X developer app for the agent's "
                         "account and set TWITTER_API_KEY/SECRET_KEY/ACCESS_TOKEN/"
                         "ACCESS_TOKEN_SECRET (polls + search need the API rail); "
                         "X login session: x_login_check")
    _browser_rail_line(sec)
    _x_session_line(sec, data_dir)
    line, state = x_oauth2_line(data_dir)
    sec.data["x_oauth2"] = state
    sec.lines.append(line)


#: What the owner runs when the X OAuth 2.0 login is dead and the verdict
#: carries no remedy of its own (the verdict's remedy wins when present).
X_OAUTH2_RELOGIN_REMEDY = "/x login (or `polyrob x-account oauth-login` from an owner shell)"


def _x_oauth2_relogin_text(v) -> str:
    from core.credential_verdicts import since_text
    return (f"X login (OAuth 2.0, DMs): re-login needed since {since_text(v)} — "
            f"{v.remedy or X_OAUTH2_RELOGIN_REMEDY}")


def x_oauth2_store_changed_since(v, data_dir: Optional[str]) -> bool:
    """Was the X token store written AFTER verdict *v*'s last refusal?

    The pack treats a record obtained after the verdict as a re-login that
    could not clear it (another process or home). Core cannot decrypt the
    record's ``obtained_at``, so it reads only what it can see: the store
    file's mtime. Shared by this line and the cron credential preflight."""
    if not data_dir:
        return False
    try:
        return os.path.getmtime(os.path.join(str(data_dir), ".x_session.json")) > float(v.last_seen)
    except (OSError, TypeError, ValueError):
        return False


def x_oauth2_line(data_dir: Optional[str]) -> Tuple[str, str]:
    """The X OAuth 2.0 user login (the DM read/send rail) as ONE honest line and
    a state: ``relogin_needed`` | ``stored`` | ``env_token`` | ``none`` |
    ``unreadable``.

    The pack's refresher records an ``x_oauth2`` credential verdict when the
    login dies and clears it on a successful login/refresh — an OPEN verdict
    is the only signal this tier trusts for "dead". Otherwise the encrypted
    token store's KEY set is read (``<data home>/.x_session.json``, keys
    ``<instance>|x_oauth2``); the Fernet record is never decrypted here, so
    "stored" means a login exists, not that its token is fresh (the pack's
    status section carries the expiry). An unparseable store is reported as
    unreadable, never as absent. Shared by ``/status`` and ``polyrob doctor``."""
    from core.credential_verdicts import active
    opened = active("x_oauth2")
    path = os.path.join(str(data_dir), ".x_session.json") if data_dir else None
    if opened:
        v = min(opened, key=lambda x: x.first_seen)
        text = _x_oauth2_relogin_text(v)
        if x_oauth2_store_changed_since(v, data_dir):
            text += " (the token store changed since — the next X call re-checks)"
        return text, "relogin_needed"
    stored = False
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                keys = json.load(f)
        except (OSError, ValueError) as e:
            return (f"X login (OAuth 2.0, DMs): unreadable ({type(e).__name__}: store "
                    f"file present but not parseable)", "unreadable")
        stored = isinstance(keys, dict) and any(str(k).endswith("|x_oauth2") for k in keys)
    if stored:
        return ("X login (OAuth 2.0, DMs): stored (no dead-login verdict; the token "
                "refreshes itself)", "stored")
    if (os.environ.get("TWITTER_OAUTH2_ACCESS_TOKEN") or "").strip():
        return ("X login (OAuth 2.0, DMs): static env token only (TWITTER_OAUTH2_ACCESS_TOKEN "
                f"expires ~2h after mint) — {X_OAUTH2_RELOGIN_REMEDY}", "env_token")
    return (f"X login (OAuth 2.0, DMs): none — DM reads need it; {X_OAUTH2_RELOGIN_REMEDY}",
            "none")


def _x_session_line(sec: Section, data_dir: Optional[str]) -> None:
    """056 WS9: the X browser rail's login session — present or absent — and a
    WARN naming the owner ceremony when the rail is enabled but has no session.
    A read of the store file's key set only (no decryption); an unreadable file
    is reported as such, never as absent. The file is the shared Fernet token
    store keyed ``<user>|<provider>``: the browser rail reads provider ``x``,
    the API rail's OAuth2 token lives under ``x_oauth2`` — so "file non-empty"
    is NOT "session stored" (2026-09-19: status said stored while `x_browser`
    refused; only the OAuth2 key existed). Only a ``|x`` key counts."""
    from core.env import bool_env
    enabled = bool_env("X_BROWSER_ENABLED", False)
    path = os.path.join(str(data_dir or ""), ".x_session.json") if data_dir else None
    present: Optional[bool]
    if not path or not os.path.exists(path):
        present = False
    else:
        try:
            with open(path, encoding="utf-8") as f:
                keys = json.load(f)
            present = isinstance(keys, dict) and any(
                str(k).endswith("|x") for k in keys)
        except (OSError, ValueError):
            sec.lines.append("X login session: unreadable (store file present but not parseable)")
            return
    sec.data["x_session"] = present
    if present:
        sec.lines.append("X login session: stored (browser rail x_post/x_reply/x_dm ready)")
        return
    if enabled:
        sec.lines.append("X login session: none — browser rail enabled but cannot post/reply")
        sec.health.append(HealthItem(
            key="x_session_absent", severity=SEVERITY_WARN,
            text="X browser rail enabled (X_BROWSER_ENABLED) but no login session is stored — "
                 "public replies/posts through the browser cannot run",
            remedy="run `polyrob x-account capture-session` from an owner shell (≈2 min, "
                   "log in as the agent's account)"))
    else:
        sec.lines.append("X login session: none (browser rail off)")


def _browser_rail_line(sec: Section) -> None:
    """Append `browser:` — the ONE rail line (core/security/browser_rail.py).

    A custody process cannot launch Chromium; it may only connect to a
    separately isolated browser. Until 2026-09-17 no seat said whether that
    rail existed, and the agent told the owner "no remote browser configured"
    while one was configured and running. A loopback endpoint is probed (a
    local read); a remote one is reported as configured, unprobed — the
    snapshot does no network read by default. The health item fires only when
    an endpoint IS configured and fails: an unset rail is an owner choice, not
    a fault (same rule as the avatar).
    """
    try:
        from core.security.browser_rail import browser_rail_status
        rail = browser_rail_status(probe=False)
    except Exception as e:
        sec.lines.append(f"browser: unreadable ({type(e).__name__})")
        return
    sec.data["browser_rail"] = rail.state
    sec.data["browser_endpoint"] = rail.endpoint
    sec.lines.append(f"browser: {rail.line()}")
    if rail.state == "unreachable":
        sec.health.append(HealthItem(
            "browser_rail_unreachable",
            f"remote browser ({rail.endpoint}) configured but unreachable: {rail.reason}",
            rail.remedy, SEVERITY_WARN))
