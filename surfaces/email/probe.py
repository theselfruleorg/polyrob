"""``polyrob surfaces probe email``: an IMAP login + logout, or the AgentMail inbox
list (a read — nothing is fetched or sent). See ``surfaces/_probe.py``."""
import asyncio
import imaplib

from surfaces._probe import ProbeResult, http_json, missing


async def probe(env) -> ProbeResult:
    # OS13: the ONE resolver the tool and the status seat use; a local
    # re-derivation answered differently for an unknown EMAIL_PROVIDER value.
    from core.config_policy.capability_toggles import email_provider
    agentmail = (env.get("AGENTMAIL_API_KEY") or "").strip()
    if email_provider(env) == "agentmail":
        if not agentmail:
            return ProbeResult.unavailable("missing AGENTMAIL_API_KEY")
        status, payload, err = await http_json(
            "GET", "https://api.agentmail.to/v0/inboxes?limit=1",
            headers={"Authorization": f"Bearer {agentmail}"})
        if err:
            return ProbeResult.unavailable(err)
        if status == 200:
            return ProbeResult.ok("AgentMail key accepted")
        return ProbeResult.failed(f"HTTP {status}: the AgentMail key was refused")
    gap = missing(env, "GMAIL_EMAIL", "GMAIL_APP_PASSWORD")
    if gap:
        return gap
    host = (env.get("GMAIL_IMAP_SERVER") or "imap.gmail.com").strip()

    def _login():
        conn = imaplib.IMAP4_SSL(host, timeout=10)
        try:
            conn.login(env["GMAIL_EMAIL"].strip(), env["GMAIL_APP_PASSWORD"].strip())
        finally:
            try:
                conn.logout()
            except Exception:
                pass

    try:
        await asyncio.wait_for(asyncio.to_thread(_login), timeout=20)
    except imaplib.IMAP4.error:
        return ProbeResult.failed(f"IMAP login refused at {host}")
    except Exception as e:
        return ProbeResult.unavailable(f"{host} unreachable ({type(e).__name__})")
    return ProbeResult.ok(f"IMAP login ok at {host}")
