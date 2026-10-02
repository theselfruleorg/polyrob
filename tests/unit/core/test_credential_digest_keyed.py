"""Low (2026-09-23 analysis): the credential-verdict digest is an HMAC keyed by a
private per-install secret, not a static-salt sha256 anyone can dictionary-attack."""
import hashlib
import os

import pytest

import core.credential_verdicts as cv


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("VERDICTS_DB_PATH", str(tmp_path / "verdicts.db"))
    cv._reset_for_tests()
    yield tmp_path
    cv._reset_for_tests()


def test_digest_is_keyed_and_stable(home):
    d = cv.credential_digest("abcd-efgh-ijkl-mnop")
    legacy = hashlib.sha256(b"polyrob-credential-verdict:abcd-efgh-ijkl-mnop").hexdigest()[:12]
    assert d != legacy and len(d) == 12
    key = home / "verdicts.key"
    assert key.is_file() and (os.stat(key).st_mode & 0o077) == 0
    cv._DIGEST_KEYS.clear()  # a new process reads the same key
    assert cv.credential_digest("abcd-efgh-ijkl-mnop") == d


def test_planted_shared_key_is_not_trusted(home):
    key = home / "verdicts.key"
    key.write_text("0" * 64)
    os.chmod(key, 0o664)
    known = cv.credential_digest("pw")
    import hmac
    assert known != hmac.new(b"0" * 64, b"pw", hashlib.sha256).hexdigest()[:12]


def test_symlinked_key_is_not_followed(home, tmp_path):
    target = tmp_path / "elsewhere.key"
    target.write_text("1" * 64)
    os.chmod(target, 0o600)
    os.symlink(str(target), str(home / "verdicts.key"))
    import hmac
    assert cv.credential_digest("pw") != hmac.new(b"1" * 64, b"pw", hashlib.sha256).hexdigest()[:12]


def test_success_clears_rows_for_older_digests_of_the_same_rail(home):
    cv.record_rejection("smtp", "s:587:u#aaaaaaaaaaaa", code="535")
    cv.record_rejection("smtp", "s:587:u2#bbbbbbbbbbbb", code="535")
    cv.clear_rejection("smtp", "s:587:u#cccccccccccc")
    assert cv.verdict("smtp", "s:587:u#aaaaaaaaaaaa") is None
    assert cv.verdict("smtp", "s:587:u2#bbbbbbbbbbbb") is not None
