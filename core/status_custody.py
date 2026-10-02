"""The ``custody`` status section (066 P0): can a child of THIS process read the seed?

Two facts, both about the process that renders the status (the agent unit for
Telegram ``/status``; the CLI's own process for ``polyrob doctor --json``):

* **dumpable** — :mod:`core.security.process_hardening`: ``held`` means a
  same-UID child gets ``EPERM`` on ``/proc/<pid>/environ``; ``unsupported``
  means not Linux; ``failed``/``not_applied`` on Linux is a real hole.
* **env** — :mod:`core.security.custody_env`: after the wallet config loads,
  no custody secret may remain in ``os.environ`` (every child would inherit it).

* **lazy installs** (066 P1) — the ``LAZY_DEPS_MODE``, whether the seedless
  installer unit is provisioned here, the overlay's lock digest and the features
  installed in it.

A process that holds no seed (the web and email units) says so; it is not an
error. Nothing here reads, returns or logs a secret value — only names.
"""
from core.status_snapshot import SEVERITY_CRIT, SEVERITY_WARN, HealthItem, Section, _guarded


def custody_section() -> Section:
    from core.security.custody_env import (custody_loaded, custody_secret, holds_custody_secret,
                                           secrets_in_process_env)
    from core.security.host_execution import wallet_custody_enabled
    from core.security.process_hardening import (STATE_FAILED, STATE_NOT_APPLIED,
                                                 _is_linux, hardening_state)
    if not wallet_custody_enabled():
        return Section(name="custody", lines=["no signing material in this process"],
                       data={"custody": False})
    from core.signer import MODE_LOCAL, signer_mode
    mode = signer_mode()
    if mode != MODE_LOCAL:
        return _signer_section(mode, hardening_state(), secrets_in_process_env())
    holds_seed = bool(custody_secret("AGENT_WALLET_MASTER_SEED"))
    holds_key = holds_custody_secret()
    hard = hardening_state()
    in_env = secrets_in_process_env()
    loaded = custody_loaded()
    sec = Section(name="custody", data={
        "custody": True, "holds_seed": holds_seed, "holds_key": holds_key,
        "dumpable": hard.state,
        "dumpable_detail": hard.detail, "loaded": loaded, "secrets_in_env": in_env,
    })
    if not holds_key:
        sec.lines.append("wallet on, no signing key in this process (public-only; signing is in the agent unit)")
        return sec
    if loaded:
        env_word = ("scrubbed" if not in_env
                    else "NOT scrubbed (" + ", ".join(in_env) + ")")
    else:
        env_word = "seed not taken yet (the wallet config has not loaded in this process)"
    sec.lines.append(f"this process: non-dumpable {hard.state} · env {env_word}")
    sec.lines.append(_lazy_line(sec))
    _local_custody_health(sec, hard, loaded, in_env)
    return sec


def _local_custody_health(sec: Section, hard, loaded: bool, in_env) -> None:
    """The P0 health checks apply whenever this process still signs locally."""
    from core.security.process_hardening import STATE_FAILED, STATE_NOT_APPLIED, _is_linux
    if _is_linux() and hard.state in (STATE_FAILED, STATE_NOT_APPLIED):
        sec.health.append(HealthItem(
            key="custody_dumpable", severity=SEVERITY_CRIT,
            text=("this process holds signing material and is DUMPABLE — a same-UID "
                  f"child can read /proc/<pid>/environ ({hard.detail})"),
            remedy="restart the agent unit; the startup log names why prctl failed"))
    if loaded and in_env:
        sec.health.append(HealthItem(
            key="custody_env", severity=SEVERITY_WARN,
            text="custody secret(s) back in os.environ after the wallet load: " + ", ".join(in_env),
            remedy="a later env load re-set them; every child now inherits them — restart the unit"))


def _lazy_line(sec: Section) -> str:
    """One line on the trusted lazy installs. "unreadable" is never "none"."""
    try:
        from core.lazy_deps import overlay_status
        st = overlay_status()
    except Exception as exc:  # noqa: BLE001
        return f"lazy installs: status unreadable ({type(exc).__name__})"
    sec.data["lazy"] = st
    from core.lazy_deps import _local_mode
    if st["spool"]:
        route = "installer unit (polyrob-deps)"
    elif _local_mode():
        route = f"in process into {st['local_root']} (wheel-only under custody)"
    else:
        route = "NO installer unit — lazy installs refuse on this custody server"
    installed = ", ".join(st["installed"]) or "none"
    return (f"lazy installs: {st['mode']} · {route} · overlay {st['lock_digest'] or '?'}"
            f" · installed: {installed}")


def _signer_section(mode: str, hard, in_env) -> Section:
    """066 P2: ``WALLET_SIGNER`` = ``shadow``/``remote``. Unreadable is never zero."""
    from core.security.custody_env import holds_custody_secret
    sec = Section(name="custody", data={"custody": True, "signer_mode": mode,
                                        "dumpable": hard.state, "secrets_in_env": in_env})
    if mode == "remote":
        holds = holds_custody_secret()
        sec.data["holds_key"] = holds
        from core.signer.remote import remote_state
        state = remote_state()
        sec.data["remote"] = state
        sec.lines.append("signer: REMOTE · this process holds "
                         + ("A KEY (it must not)" if holds else "no key")
                         + " · " + state["detail"])
        if holds:
            sec.health.append(HealthItem(
                key="signer_key_in_agent", severity=SEVERITY_CRIT,
                text="WALLET_SIGNER=remote but this process holds custody key material",
                remedy="remove EnvironmentFile=/etc/polyrob/wallet.env from the agent unit "
                       "(install-signer.sh cutover) and restart"))
        if not state["verified"]:
            sec.health.append(HealthItem(
                key="signer_unverified", severity=SEVERITY_CRIT,
                text="remote signer NOT verified — every send refuses: " + state["detail"],
                remedy="check `systemctl status polyrob-signer`, then restart the agent unit"))
    else:
        from core.security.custody_env import custody_loaded
        loaded = custody_loaded()
        sec.data["loaded"] = loaded
        env_word = "scrubbed" if not in_env else "NOT scrubbed (" + ", ".join(in_env) + ")"
        sec.lines.append("signer: SHADOW · this process still signs locally "
                         f"(non-dumpable {hard.state} · env {env_word})")
        if holds_custody_secret():
            _local_custody_health(sec, hard, loaded, in_env)
    sec.lines.append(_signer_ping_line(sec))
    if mode == "shadow":
        sec.lines.append(_shadow_line(sec))
    sec.lines.append(_lazy_line(sec))
    return sec


def _signer_ping_line(sec: Section) -> str:
    try:
        from core.signer.client import SignerClient
        pong = SignerClient(timeout=3.0).call("ping")
    except Exception as exc:  # noqa: BLE001
        sec.data["signer_reachable"] = False
        sec.health.append(HealthItem(
            key="signer_unreachable",
            severity=SEVERITY_CRIT if sec.data.get("signer_mode") == "remote" else SEVERITY_WARN,
            text=f"polyrob-signer did not answer ({type(exc).__name__})",
            remedy="`sudo systemctl status polyrob-signer`; journal: `journalctl -u polyrob-signer`"))
        return f"signer: unreachable ({type(exc).__name__}: {exc})"
    sec.data["signer_reachable"] = True
    caps = pong.get("caps") or {}
    sec.data["signer_caps"] = caps
    _cap_drift_health(sec, caps)
    line = (f"signer: reachable · hard caps ${caps.get('per_tx_usd')}/tx, "
            f"${caps.get('daily_usd')}/24h · chains {', '.join(caps.get('chains') or []) or 'none'}")
    if pong.get("paused"):
        line += " · PAUSED"
    pending = int(pong.get("pending_approvals") or 0)
    if pending:
        line += f" · {pending} approval(s) waiting (`polyrob owner pending` on the box)"
    return line


def _cap_drift_health(sec: Section, caps) -> None:
    """068 G7b: the gate allowing MORE than the signer will sign. An
    unresolvable comparison is SAID (in data and a line), never a false drift."""
    try:
        from core.wallet.signer_cap_drift import (cap_drift, drift_remedy, drift_text,
                                                  gate_caps_now)
        drifts = cap_drift(caps, gate_caps_now())
    except Exception as exc:  # noqa: BLE001
        sec.data["signer_cap_drift"] = f"unavailable ({type(exc).__name__})"
        sec.lines.append(f"signer cap comparison: unavailable ({type(exc).__name__})")
        return
    sec.data["signer_cap_drift"] = [
        {"leg": d.leg, "signer": d.signer,
         # 068 B12: a disabled leg is "unlimited" (inf is not valid JSON).
         "gate": ("unlimited" if d.gate is not None and d.gate == float("inf") else d.gate),
         "drifted": d.drifted}
        for d in drifts]
    for d in drifts:
        if d.drifted:
            sec.health.append(HealthItem(
                key=f"signer_cap_drift_{d.leg}",
                severity=SEVERITY_CRIT if sec.data.get("signer_mode") == "remote" else SEVERITY_WARN,
                text=drift_text(d), remedy=drift_remedy(d)))


def _shadow_line(sec: Section) -> str:
    from core.signer.shadow import summary
    st = summary(7.0)
    sec.data["shadow"] = st
    if not st.get("readable"):
        return f"shadow log: unreadable ({st.get('error')})"
    c = st["counts"]
    line = (f"shadow (7d): {c['agree']} agree · {c['disagree']} disagree · "
            f"{c['no_intent']} unguarded · {c['unreachable']} unreachable")
    bad = c["disagree"] + c["no_intent"]
    if bad:
        last = st.get("last_disagreement") or {}
        sec.health.append(HealthItem(
            key="signer_shadow_disagree", severity=SEVERITY_WARN,
            text=f"{bad} shadow disagreement(s) in 7 days; last: {last.get('code') or last.get('kind')}"
                 f" — {str(last.get('reason') or '')[:160]}",
            remedy="read <data>/wallet/signer_shadow.jsonl; do not cut over to remote until a clean week"))
    return line


from core.status_sections import register_status_section  # noqa: E402

# 067 P5a: the ``custody`` slot (process-level: no tenant needed).
register_status_section("custody", lambda ctx: _guarded("custody", custody_section),
                        tenant=False)
