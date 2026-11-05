"""The scrub battery runs on the event loop over every message that enters history,
on text the agent did not write (web pages, mail, chat), so one regex must not be able
to stall every session. ``KV_RE``'s key prefix was an unbounded repeat with a start
point at every ``-`` or ``_``: 50 KB of ``a-`` took 66 s."""
import time

import pytest

from core.secret_scrub import scrub_secret_shapes

SIZE = 50_000


@pytest.mark.parametrize("unit", ["a-", "a_", "foo-bar-", "sk-", "3f2a9c-", "x_1-"])
def test_a_long_separator_chain_scrubs_fast(unit):
    text = (unit * (SIZE // len(unit) + 1))[:SIZE]
    started = time.perf_counter()
    scrub_secret_shapes(text)
    # Generous for a loaded CI box; the quadratic form took 3 s to 66 s here.
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("line", [
    "OPENAI_API_KEY=supersecretvalue123",
    "MY_PREFIX_API_KEY=supersecretvalue123",
    "a-b-c-d-e-f-g-h-api-key: supersecretvalue123",
    # Longer than the prefix bound: the match starts on a later segment.
    "A_B_C_D_E_F_G_H_I_J_K_L_M_N_O_P_API_KEY=supersecretvalue123",
    "x-" * 400 + "secret=supersecretvalue123",
])
def test_a_key_behind_any_prefix_length_is_still_redacted(line):
    assert "supersecretvalue123" not in scrub_secret_shapes(line)


def test_the_key_name_survives_inside_the_bound():
    out = scrub_secret_shapes("MY_PREFIX_API_KEY=supersecretvalue123")
    assert out.startswith("MY_PREFIX_API_KEY=")
