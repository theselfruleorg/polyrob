"""Tests for tools.user_directory.UserDirectory (P2c, Singular Chat Interface).

UserDirectory is a container-registerable service over a SQLite ``user_profiles``
table. It maps raw platform ids (telegram chat ids, CLI ids, etc.) to a STABLE
internal ``user_id`` and provides the reverse/email lookups that ``cron/delivery.py``
calls (sync, by internal user_id).

Ported (reused, not forked) from ../rob_dev_telegram_version:
- modules/database/user_profiles.py (get_or_create_by_tg_id, generate_user_id, schema)
- utils/user_id_utils.py (UserIDResolver bidirectional mapping)
"""
import os

import pytest

from tools.user_directory import UserDirectory


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "user_profiles.db")


@pytest.fixture
def ud(db_path):
    return UserDirectory(db_path)


def test_get_or_create_is_idempotent_same_tg_id(ud):
    """Same tg_id always yields the same internal user_id."""
    a = ud.get_or_create_by_tg_id("12345")
    b = ud.get_or_create_by_tg_id("12345")
    assert a == b
    assert a  # non-empty


def test_different_tg_ids_yield_different_internal_ids(ud):
    a = ud.get_or_create_by_tg_id("12345")
    b = ud.get_or_create_by_tg_id("67890")
    assert a != b


def test_stable_id_is_deterministic_without_prior_row(db_path):
    """The internal id derivation is deterministic: a fresh DB derives the SAME id
    for the same tg_id (SHA256-derived, not a random UUID)."""
    a = UserDirectory(db_path).get_or_create_by_tg_id("12345")
    # brand-new DB, same tg_id -> same derived internal id
    other = str(db_path) + ".2"
    b = UserDirectory(other).get_or_create_by_tg_id("12345")
    assert a == b


def test_get_telegram_chat_id_round_trips(ud):
    internal = ud.get_or_create_by_tg_id("12345")
    assert ud.get_telegram_chat_id(internal) == "12345"


def test_get_telegram_chat_id_unknown_returns_none(ud):
    assert ud.get_telegram_chat_id("u_does_not_exist") is None


def test_resolve_internal_telegram_matches_get_or_create(ud):
    via_resolve = ud.resolve_internal("12345", "telegram")
    via_tg = ud.get_or_create_by_tg_id("12345")
    assert via_resolve == via_tg


def test_resolve_internal_namespaces_by_surface(ud):
    """The same raw id on two surfaces must NOT collide to one internal id."""
    tg = ud.resolve_internal("12345", "telegram")
    cli = ud.resolve_internal("12345", "cli")
    assert tg != cli


def test_resolve_internal_idempotent_per_surface(ud):
    a = ud.resolve_internal("abc", "cli")
    b = ud.resolve_internal("abc", "cli")
    assert a == b


def test_get_email_none_when_unset_then_set(ud):
    internal = ud.get_or_create_by_tg_id("12345")
    assert ud.get_email(internal) is None
    ud.get_or_create_by_tg_id("12345", profile={"email": "a@b.com"})
    assert ud.get_email(internal) == "a@b.com"


def test_get_email_unknown_returns_none(ud):
    assert ud.get_email("u_nope") is None


def test_profile_persists_chat_id_for_reverse_lookup(db_path):
    """A fresh UserDirectory over the same db sees the mapping (persisted, not in-memory)."""
    internal = UserDirectory(db_path).get_or_create_by_tg_id("999")
    assert UserDirectory(db_path).get_telegram_chat_id(internal) == "999"
