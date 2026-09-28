"""bot/bybit_exec.py::BybitExec.recent_fill_price -- polling for a just-filled
order's executions (live S009 logged fill=None on 157 of 187 fills because a
single immediate read raced Bybit's execution indexing)."""
from __future__ import annotations

from bot.bybit_exec import BybitExec


def _exec_with_responses(responses):
    """BybitExec without credentials/network: _get replays `responses`."""
    ex = BybitExec.__new__(BybitExec)
    ex.category = "linear"
    calls = []

    def _get(path, params, signed=True):
        calls.append(params)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    ex._get = _get
    return ex, calls


def _fills(oid, *legs):
    return {"list": [{"orderId": oid, "execPrice": str(p), "execQty": str(q)} for p, q in legs]}


def test_polls_until_executions_appear_and_returns_vwap():
    ex, calls = _exec_with_responses([{"list": []}, {"list": []}, _fills("o1", (10.0, 1), (12.0, 3))])
    sleeps = []
    assert ex.recent_fill_price("XUSDT", "o1", sleep=sleeps.append) == 11.5
    assert len(calls) == 3 and len(sleeps) == 2
    assert all(c["orderId"] == "o1" for c in calls)


def test_gives_up_after_attempts_and_returns_none():
    ex, calls = _exec_with_responses([{"list": []}])
    assert ex.recent_fill_price("XUSDT", "o1", attempts=3, sleep=lambda s: None) is None
    assert len(calls) == 3


def test_transient_read_error_is_retried_not_fatal():
    ex = BybitExec.__new__(BybitExec)
    ex.category = "linear"
    answers = iter([TimeoutError("read timed out"), _fills("o1", (5.0, 2))])

    def _get(path, params, signed=True):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    ex._get = _get
    assert ex.recent_fill_price("XUSDT", "o1", sleep=lambda s: None) == 5.0
