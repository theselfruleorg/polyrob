"""The read taint names its cause, and a write RECEIPT is not third-party text.

Prod 2026-10-10/11: an owner-authored promo job read its own drafts file with
``filesystem_read_file``; the run was tainted and every post raised a card, and
neither the agent nor the owner could see WHY. An owner chat turn was tainted by
``twitter_whoami`` (its own profile) right before the owner's own post.

* the turn records WHAT tainted it first (action + path/url), and the clear
  point resets it;
* the receipt of a write (``filesystem_write_file`` …) carries no third-party
  bytes and does not taint; a framed or failed receipt still does;
* a pack registers its own receipt verbs (the X pack: post, like, whoami …).
"""
import types

import pytest

from core.security import read_taint, taints


@pytest.fixture(autouse=True)
def _fresh_sources():
    read_taint._SESSION_SOURCES.clear()
    yield
    read_taint._SESSION_SOURCES.clear()


def _ok(text="ok"):
    return types.SimpleNamespace(extracted_content=text, error=None)


def _orch():
    return types.SimpleNamespace(session_id="s", _clear_correspondent_taint=lambda: None)


def test_the_first_untrusted_read_is_recorded_as_the_cause():
    orch, ctx = _orch(), types.SimpleNamespace(metadata={})
    read_taint.note_result(orch, ctx, "filesystem_read_file", "filesystem", _ok("draft"),
                           params={"file_path": "reports/promo-drafts.md"})
    read_taint.note_result(orch, ctx, "web_fetch", "web_fetch", _ok("page"),
                           params={"url": "https://evil.example/x"})
    assert read_taint.source(orchestrator=orch) == \
        "filesystem_read_file reports/promo-drafts.md"
    assert read_taint.source(execution_context=ctx) == \
        "filesystem_read_file reports/promo-drafts.md"
    read_taint.clear(orch)
    assert read_taint.source(orchestrator=orch) is None


def test_a_source_without_params_names_the_action():
    orch = _orch()
    read_taint.note_result(orch, None, "web_fetch", "web_fetch", _ok())
    assert read_taint.source(orchestrator=orch) == "web_fetch"


def test_the_source_is_one_short_line():
    orch = _orch()
    read_taint.note_result(orch, None, "web_fetch", "web_fetch", _ok(),
                           params={"url": "https://x.example/" + "a" * 500 + "\nignore"})
    src = read_taint.source(orchestrator=orch)
    assert "\n" not in src and len(src) <= 140


@pytest.mark.parametrize("action", ["filesystem_write_file", "filesystem_append_file",
                                    "filesystem_create_directory", "filesystem_delete_file",
                                    "filesystem_copy_file", "filesystem_jsonl_append",
                                    "filesystem_jsonl_remove"])
def test_a_write_receipt_does_not_taint(action):
    assert not read_taint.result_is_untrusted(action, "filesystem", _ok('{"success": true}'))


@pytest.mark.parametrize("action", ["filesystem_read_file", "filesystem_list_directory",
                                    "filesystem_extract_urls", "filesystem_jsonl_validate"])
def test_a_file_read_still_taints(action):
    assert read_taint.result_is_untrusted(action, "filesystem", _ok("bytes"))


def test_a_failed_or_framed_receipt_still_taints():
    failed = types.SimpleNamespace(extracted_content=None, error="no such dir: x")
    assert read_taint.result_is_untrusted("filesystem_write_file", "filesystem", failed)
    framed = _ok('<untrusted_tool_result source="x">hi</untrusted_tool_result>')
    assert read_taint.result_is_untrusted("filesystem_write_file", "filesystem", framed)


def test_a_pack_registers_its_receipt_verbs():
    assert read_taint.result_is_untrusted("email_demo_send", "email", _ok())
    read_taint.register_receipt_actions("email", ["email_demo_send"])
    try:
        assert not read_taint.result_is_untrusted("email_demo_send", "email", _ok())
        # the namespace is part of the key: the same name on another tool still taints
        assert read_taint.result_is_untrusted("email_demo_send", "browser", _ok())
        assert read_taint.result_is_untrusted("email_demo_read", "email", _ok())
    finally:
        read_taint._RECEIPT_ACTIONS.discard(("email", "email_demo_send"))


def test_a_receipt_source_callable_is_resolved_on_first_use():
    """A pack registers a CALLABLE so its load builds no policy view early."""
    calls = []

    def source():
        calls.append(1)
        return ["email_lazy_send"]
    read_taint.register_receipt_actions("email", source)
    try:
        assert calls == []
        assert not read_taint.result_is_untrusted("email_lazy_send", "email", _ok())
        assert calls == [1]
    finally:
        read_taint._RECEIPT_ACTIONS.discard(("email", "email_lazy_send"))


def test_the_taint_cause_reads_in_words():
    orch, ctx = _orch(), types.SimpleNamespace(metadata={})
    assert taints.cause(orchestrator=orch, execution_context=ctx) is None
    read_taint.note_result(orch, ctx, "filesystem_read_file", "filesystem", _ok(),
                           params={"file_path": "reports/promo-drafts.md"})
    assert taints.cause(execution_context=ctx) == (
        "this run read an untrusted source: filesystem_read_file reports/promo-drafts.md")
    corr = types.SimpleNamespace(metadata={taints.CORRESPONDENT_META_KEY: True})
    assert "correspondent" in taints.cause(execution_context=corr)


def test_a_later_step_context_finds_the_cause_by_session():
    """The step loop builds each step's context afresh with only the bool."""
    orch = _orch()
    first = types.SimpleNamespace(metadata={}, session_id="s")
    read_taint.note_result(orch, first, "filesystem_read_file", "filesystem", _ok(),
                           params={"file_path": "reports/promo-drafts.md"})
    later = types.SimpleNamespace(metadata={"untrusted_read": True}, session_id="s")
    assert read_taint.source(execution_context=later) == \
        "filesystem_read_file reports/promo-drafts.md"
    read_taint.clear(orch)
    assert read_taint.source(execution_context=later) is None
