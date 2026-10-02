"""search_google URL-encodes the query (prompt-skill review 2026-09-29): a raw
'&' or '#' cut the query short or added a parameter."""
from urllib.parse import parse_qs, urlparse

from tools.browser.browser import google_search_url


def test_query_is_url_encoded_and_round_trips():
    q = "AT&T #1 50% off c++ ?x=y"
    url = google_search_url(q)
    qs = parse_qs(urlparse(url).query)
    assert qs["q"] == [q]
    assert qs["udm"] == ["14"]
    assert "#" not in url


def test_spaces_become_plus():
    assert google_search_url("polyrob agent").startswith(
        "https://www.google.com/search?q=polyrob+agent&")
