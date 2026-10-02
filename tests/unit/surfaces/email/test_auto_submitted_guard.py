"""OS5 — an auto-generated mail (Auto-Submitted, Precedence bulk/junk/list/
auto_reply, X-Autoreply, a mailer-daemon bounce) is never routed as a turn:
answering it starts an agent <-> auto-responder loop (RFC 3834)."""
import asyncio
from email.message import EmailMessage

import pytest

from surfaces.email.harness import normalize_email_message
from surfaces.email.inbound import is_auto_generated, process_email


class _UD:
    def resolve_internal(self, raw, surface):
        return f"u_{surface}_{raw}"


def _em(**headers):
    em = EmailMessage()
    em["From"] = headers.pop("From", "Bob <bob@example.com>")
    em["Subject"] = "Out of office"
    em["Message-ID"] = "<m1@example.com>"
    for k, v in headers.items():
        em[k.replace("_", "-")] = v
    em.set_content("I am away until Monday.")
    return em


@pytest.mark.parametrize("headers", [
    {"Auto_Submitted": "auto-replied"},
    {"Auto_Submitted": "auto-generated"},
    {"Precedence": "bulk"},
    {"Precedence": "auto_reply"},
    {"Precedence": "junk"},
    {"Precedence": "list"},
    {"X_Autoreply": "yes"},
    {"X_Autorespond": "yes"},
    {"From": "MAILER-DAEMON@mx.example.com"},
    {"From": "Mail Delivery System <postmaster@example.com>"},
])
def test_auto_generated_is_detected_and_not_routed(headers):
    norm = normalize_email_message(_em(**headers))
    assert is_auto_generated(norm)
    out = asyncio.run(process_email(None, norm, dedup=None, user_directory=_UD()))
    assert out is None


def test_human_mail_is_not_auto():
    norm = normalize_email_message(_em(Auto_Submitted="no"))
    assert not is_auto_generated(norm)
    assert not is_auto_generated(normalize_email_message(_em()))
