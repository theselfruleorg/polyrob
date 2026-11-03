"""core/retired_deps.py: a dropped third-party dependency is uninstalled on every
update path, so its stale pins cannot fail `pip check` (2026-11-03: the old
tweepy pinned oauthlib<4 and rolled a prod deploy back)."""
from types import SimpleNamespace

from core import retired_deps


def _dist(name, requires=()):
    return SimpleNamespace(metadata={"Name": name}, requires=list(requires))


def test_tweepy_is_retired_and_named():
    assert "tweepy" in retired_deps.RETIRED_THIRD_PARTY


def test_an_unrequired_retired_dist_is_uninstalled():
    gone = []
    rc = retired_deps.main([], distributions=[_dist("tweepy", ["oauthlib<4,>=3.2.0"]),
                                              _dist("oauthlib")], uninstall=gone.append)
    assert rc == 0 and gone == ["tweepy"]


def test_a_retired_dist_something_needs_is_kept_and_named(capsys):
    gone = []
    rc = retired_deps.main([], distributions=[_dist("tweepy"), _dist("other", ["Tweepy>=4"])],
                           uninstall=gone.append)
    assert rc == 1 and gone == []
    assert "still required by other" in capsys.readouterr().err


def test_an_extra_only_requirement_does_not_keep_it():
    gone = []
    retired_deps.main([], distributions=[_dist("tweepy"),
                                         _dist("other", ['tweepy; extra == "x"'])],
                      uninstall=gone.append)
    assert gone == ["tweepy"]


def test_nothing_installed_is_a_no_op():
    gone = []
    assert retired_deps.main([], distributions=[_dist("requests")], uninstall=gone.append) == 0
    assert gone == []
