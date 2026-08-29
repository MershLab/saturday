"""Settings pane: advanced config validators exposed by the model/safety panes."""

from __future__ import annotations

from saturday import webui
from saturday.webui import _CFG_SKIP, _b_float_range, _b_int_range, _b_int_range_opt, _v_bool


def test_range_validators_bounds():
    assert _b_int_range(1, 600)({"request_timeout": 120}, None, "request_timeout") == 120
    assert _b_int_range(1, 600)({"tool_timeout": 0}, None, "tool_timeout") is _CFG_SKIP
    assert _b_float_range(0, 1)({"top_p": 0.9}, None, "top_p") == 0.9
    assert _b_float_range(0, 1)({"top_p": 1.4}, None, "top_p") is _CFG_SKIP


def test_optional_int_clears_with_null():
    _opt = _b_int_range_opt(0, 10_000_000)
    assert _opt({"compact_above_tokens": None}, None, "compact_above_tokens") is None
    assert _opt({"max_context_tokens": 5000}, None, "max_context_tokens") == 5000
    assert _opt({"max_context_tokens": -1}, None, "max_context_tokens") is _CFG_SKIP
    assert _opt({}, None, "max_context_tokens") is _CFG_SKIP


def test_bool_toggles_only_accept_bools():
    assert _v_bool({"stream": False}, None, "stream") is False
    assert _v_bool({"stream": "no"}, None, "stream") is _CFG_SKIP


def test_config_fields_expose_advanced_knobs():
    keys = {k for k, _ in webui._CONFIG_FIELDS}
    assert {
        "top_p",
        "request_timeout",
        "tool_timeout",
        "max_retries",
        "memory_max_chars",
        "max_context_tokens",
        "compact_above_tokens",
        "stream",
        "shell_allow_network",
    } <= keys
