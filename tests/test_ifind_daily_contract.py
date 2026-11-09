# -*- coding: utf-8 -*-
"""issue #86：iFinD 日线跨字段不变式与数据层模型契约（type-safety 契约入口）。"""
from __future__ import annotations

from decimal import Decimal

import pytest
from icontract import ViolationError
from pydantic import ValidationError

from data_provider.ifind_daily_adapter import IfindDailyBar, _daily_bar
from src.services.history_loader import _frame_covers, _overlap_consistent, _WindowFrame


def _bar(o="10", h="11", l="9", c="10.5", v="1000", a="10500"):
    return _daily_bar("2026-11-06", Decimal(o), Decimal(h), Decimal(l), Decimal(c), Decimal(v), Decimal(a))


def test_valid_bar_satisfies_ohlc_invariants():
    bar = _bar()
    assert bar.close == 10.5 and bar.volume == 1000


def test_low_above_open_violates_contract():
    with pytest.raises(ViolationError):
        _bar(o="9", l="9.5")


def test_high_below_close_violates_contract():
    with pytest.raises(ViolationError):
        _bar(h="10.4", c="10.5")


def test_non_positive_low_rejected():
    with pytest.raises(ViolationError):
        _bar(l="0")


def test_non_positive_volume_or_amount_rejected():
    with pytest.raises(ViolationError):
        _bar(v="0")
    with pytest.raises(ViolationError):
        _bar(a="-1")


def test_model_rejects_nonfinite_and_bad_date():
    with pytest.raises(ValidationError):
        IfindDailyBar(date="2026-11-06", open=float("nan"), high=11, low=9, close=10, volume=1, amount=1)
    with pytest.raises(ValidationError):
        IfindDailyBar(date="20261106", open=10, high=11, low=9, close=10, volume=1, amount=1)
    with pytest.raises(ValidationError):
        IfindDailyBar(date="2026-11-06", open=-1, high=11, low=9, close=10, volume=1, amount=1)


def _records(dates, close=10.0):
    return [
        {"date": d, "open": close, "high": close + 1, "low": close - 1, "close": close,
         "volume": 100, "amount": 1000}
        for d in dates
    ]


def test_overlap_consistency_detection():
    old = _records(["2026-11-05", "2026-11-06"])
    same = _records(["2026-11-05", "2026-11-06", "2026-11-07"])
    drift = _records(["2026-11-05", "2026-11-06", "2026-11-07"], close=55.5)
    assert _overlap_consistent(old, same)
    assert not _overlap_consistent(old, drift)


def test_frame_covers_rows_or_full_span():
    from datetime import date

    frame = _WindowFrame(source="IfindDailyFetcher", as_of="2026-11-06", records=_records([f"2026-11-{d:02d}" for d in range(1, 7)]))
    assert _frame_covers(frame, 6, date(2026, 11, 1))
    assert not _frame_covers(frame, 10, date(2026, 10, 30))
    # 行数不足但已覆盖请求跨度起点：是可得完整历史，不再重复整窗重取
    assert _frame_covers(frame, 10, date(2026, 11, 4))
    assert not _frame_covers(_WindowFrame(source="x", as_of="2026-11-06", records=[]), 1, date(2026, 11, 1))
