"""Audit 2026-11-21 M-2, M-7, L-18: the Authentication-Results reader.

M-7: an RFC 8601 tokenizer — a ``;`` inside a quoted string or a nested comment
never splits a clause, a malformed clause makes the header prove nothing, and
``header.d`` / ``header.i`` count only as a bare domain (``header.i`` also as
``@domain``).
L-18: ``dmarc=pass`` needs ``header.from``; alignment never stops at a public
suffix.
M-2: the AgentMail row fails closed, and only an ORDERED header list names the
topmost header.
"""
import pytest

from tools.email_providers.mime import (
    KNOWN_AUTHSERV, sender_authenticated, topmost_header,
)

_MX = frozenset({"mx.example.org"})


def _ok(ar, from_addr="boss@victim.com"):
    return sender_authenticated(ar, from_addr, authserv_ids=_MX)


# --- M-7 ---------------------------------------------------------------------

def test_quoted_localpart_semicolon_injection():
    ar = ('mx.example.org; spf=fail '
          'smtp.mailfrom="a;dkim=pass header.d=victim.com"@attacker.com')
    assert _ok(ar) is False


def test_quoted_injection_with_a_closing_clause():
    ar = ('mx.example.org; spf=pass '
          'smtp.mailfrom="x;dkim=pass header.d=victim.com;y"@attacker.com')
    assert _ok(ar) is False


def test_unquoted_echo_leaves_a_malformed_clause_and_proves_nothing():
    # An MX that drops the quotes: the stray "y@attacker.com" clause is no result.
    ar = ('mx.example.org; spf=pass smtp.mailfrom=x;'
          'dkim=pass header.d=victim.com;y@attacker.com')
    assert _ok(ar) is False


def test_stray_word_in_a_clause_proves_nothing():
    ar = "mx.example.org; dkim=pass header.d=victim.com @attacker.com"
    assert _ok(ar) is False


def test_semicolon_inside_nested_comment_does_not_split():
    ar = ("mx.example.org; spf=fail (outer (inner; dkim=pass header.d=victim.com) x) "
          "smtp.mailfrom=a@attacker.com")
    assert _ok(ar) is False


def test_unterminated_quote_or_comment_proves_nothing():
    assert _ok('mx.example.org; dmarc=pass header.from=victim.com; x="a') is False
    assert _ok("mx.example.org; dmarc=pass header.from=victim.com (open") is False


@pytest.mark.parametrize("prop", [
    "header.d=victim.com@attacker.com",
    "header.d=@victim.com",
    "header.d=victim",
    'header.d="victim.com"',
    "header.i=user@victim.com",
])
def test_dkim_domain_only_bare(prop):
    assert _ok("mx.example.org; dkim=pass " + prop) is False


@pytest.mark.parametrize("prop", ["header.d=victim.com", "header.i=@victim.com",
                                  "header.i=victim.com", "header.d=mail.victim.com"])
def test_dkim_bare_domain_counts(prop):
    assert _ok("mx.example.org; dkim=pass " + prop) is True


def test_comments_and_reason_are_skipped():
    ar = ('mx.example.org; dmarc=pass (p=REJECT (nested)) reason="ok; fine" '
          'header.from=victim.com')
    assert _ok(ar) is True


# --- L-18 --------------------------------------------------------------------

def test_dmarc_pass_without_header_from_proves_nothing():
    assert _ok("mx.example.org; dmarc=pass") is False


@pytest.mark.parametrize("d,from_addr,ok", [
    ("victim.com", "boss@victim.com", True),
    ("mail.victim.com", "boss@victim.com", True),
    ("victim.com", "boss@mail.victim.com", True),
    ("co.uk", "boss@victim.co.uk", False),
    ("victim.co.uk", "boss@mail.victim.co.uk", True),
    ("attacker.co.uk", "boss@victim.co.uk", False),
    ("com.au", "boss@shop.com.au", False),
    ("github.io", "boss@victim.github.io", False),
    ("com", "boss@victim.com", False),
    ("evilvictim.com", "boss@victim.com", False),
])
def test_alignment_never_stops_at_a_public_suffix(d, from_addr, ok):
    assert _ok("mx.example.org; dkim=pass header.d=" + d, from_addr) is ok


def test_dmarc_header_from_at_a_public_suffix_is_refused():
    assert _ok("mx.example.org; dmarc=pass header.from=co.uk", "boss@victim.co.uk") is False


# --- M-2 ---------------------------------------------------------------------

def test_agentmail_row_fails_closed_until_pinned():
    assert KNOWN_AUTHSERV["agentmail"] == frozenset()
    ar = "any-id.example; dmarc=pass header.from=victim.com"
    assert sender_authenticated(ar, "boss@victim.com",
                                authserv_ids=KNOWN_AUTHSERV["agentmail"]) is False


@pytest.mark.parametrize("headers,want", [
    ([["Authentication-Results", "top"], ["Authentication-Results", "forged"]], "top"),
    ([{"name": "authentication-results", "value": "top"},
      {"name": "Authentication-Results", "value": "forged"}], "top"),
    ([("X-Other", "a"), ("Authentication-Results", "top")], "top"),
    ({"Authentication-Results": ["top", "forged"]}, "top"),
    # A plain mapping cannot show order or a second header: no topmost one.
    ({"Authentication-Results": "forged"}, None),
    ({}, None),
    (None, None),
    ("Authentication-Results: x", None),
])
def test_topmost_header_reads_only_an_ordered_shape(headers, want):
    assert topmost_header(headers, "Authentication-Results") == want

