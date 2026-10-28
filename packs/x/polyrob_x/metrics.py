"""Tweet metrics the model reads: X's repost figure, not the API's split pair."""
from typing import Any, Dict


def with_repost_total(metrics: Any) -> Dict[str, Any]:
    """A copy of a tweet's ``public_metrics`` with ``reposts_and_quotes`` added.

    X's API splits a repost into ``retweet_count`` and ``quote_count``; X's own
    app shows them as one repost figure. A model handed only the raw pair reads
    ``retweet_count`` as "reposts" (2026-10-19..21: a 100-repost goal read 77
    while it stood at 65 + 70 = 135, and the job kept pushing it). Every tweet
    read carries the sum so the owner's number is the one the model sees.
    """
    if metrics is None:          # no metrics read: never invent a zero count
        return {}
    if not isinstance(metrics, dict):
        try:
            metrics = dict(metrics or {})
        except Exception:
            return {}
    out = dict(metrics)

    def _n(key: str) -> int:
        try:
            return int(out.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    out["reposts_and_quotes"] = _n("retweet_count") + _n("quote_count")
    return out


def with_repost_row(row: Any) -> Dict[str, Any]:
    """A raw tweet ``data`` dict with its ``public_metrics`` passed through
    :func:`with_repost_total` (the batch read hands back raw API rows)."""
    out = dict(row or {})
    if "public_metrics" in out:
        out["public_metrics"] = with_repost_total(out["public_metrics"])
    return out
