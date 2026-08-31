"""Tool-output compression: keep the answer, not just the opening."""
from __future__ import annotations

import pytest

from saturday.compress import MIN_BUDGET, compress


def test_text_within_budget_is_returned_byte_for_byte():
    """Compression must be invisible to output that already fits."""
    text = "line one\nline two\n\ttabbed\n\ntrailing spaces   \n"
    assert compress(text, 10_000) == text
    assert compress("", 100) == ""


def test_the_tail_survives_which_is_what_slicing_destroyed():
    """The motivating defect: payload[:LIMIT] kept 900 PASSED lines and threw
    away the failure and the verdict that came after them."""
    noise = "\n".join(f"tests/test_mod_{i % 40}.py::case_{i} PASSED" for i in range(900))
    verdict = "E       KeyError: 'exp'\n========= 1 failed, 900 passed in 12.3s ========="
    text = "session starts\n" + noise + "\n" + verdict

    sliced = text[:2000]
    assert "1 failed" not in sliced and "KeyError" not in sliced  # the old behaviour

    out = compress(text, 2000)
    assert len(out) <= 2000
    assert "1 failed, 900 passed" in out
    assert "KeyError" in out
    assert out.startswith("session starts")


def test_failure_lines_outrank_ordinary_ones():
    filler = "\n".join(f"processing item {i} of the batch normally" for i in range(600))
    text = filler + "\nTraceback (most recent call last):\nValueError: bad input\n" + filler
    out = compress(text, 1500)
    assert "Traceback" in out and "ValueError: bad input" in out


def test_repeated_lines_collapse_with_a_count():
    text = "start\n" + ("retrying connection...\n" * 400) + "done"
    out = compress(text, 1200)
    assert "[x400]" in out
    assert "start" in out and "done" in out
    assert len(out) <= 1200


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_output_never_exceeds_the_budget(seed):
    """Fuzzed: an earlier version overflowed by appending its own notes after
    the budget check, and again when collapsing repeats."""
    import random

    rng = random.Random(seed)
    shapes = ["", "ERROR: something failed", "retrying...", "a b c d e f g "]
    for _ in range(1500):
        budget = rng.randint(MIN_BUDGET, 6000)
        n = rng.randint(0, 300)
        text = "\n".join(rng.choice(shapes) + "x" * rng.randint(0, 120) for _ in range(n))
        out = compress(text, budget)
        assert len(out) <= budget, (budget, len(text), len(out))
        if len(text) <= budget:
            assert out == text


def test_a_tiny_budget_falls_back_to_a_plain_cut():
    text = "a" * 5000
    out = compress(text, 100)
    assert len(out) == 100


def test_elisions_are_stated_so_the_model_is_not_misled():
    text = "\n".join(f"unique line {i} with distinct words {i * 7}" for i in range(500))
    out = compress(text, 1500)
    assert "omitted" in out or "compressed" in out


def test_single_long_line_is_still_bounded():
    """No newlines at all: the line-based path has nothing to rank."""
    out = compress("x" * 100_000, 1000)
    assert len(out) <= 1000
