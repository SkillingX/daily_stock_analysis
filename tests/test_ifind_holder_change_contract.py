"""Two explicit top-ten snapshots measure concentration, never institutional buying.

issue #64：资金路径零股东出站的执行契约由 tests/test_issue64_capital_research.py
覆盖；本文件只保留 iFinD 两期快照的适配器契约（Decimal 差值/合法零/不合格拒绝）。
"""
from decimal import Decimal
import json
from typing import Any

import pytest

from data_provider.ifind_fundamental_adapter import IfindShareholders


def holders(period: str, ratio: float = 6.0) -> dict[str, Any]:
    return {"coverage": "complete", "items": [
        {"name": f"股东{rank}", "rank": rank, "report_date": period,
         "ratio": {"source": "ifind", "value": ratio, "period": period, "unit": "percentage_point"},
         "shares": {"source": "ifind", "value": 100.0, "period": period, "unit": "shares"}}
        for rank in range(1, 11)
    ]}


def test_concentration_uses_each_period_top_ten_and_decimal_difference() -> None:
    from data_provider.ifind_fundamental_adapter import shareholder_concentration_change
    current = holders("2026-09-30", 6.126)
    current["items"][9]["name"] = "新进入前十，不能称原股东清仓"
    values = shareholder_concentration_change(IfindShareholders.model_validate_json(json.dumps(current)), IfindShareholders.model_validate_json(json.dumps(holders("2026-06-30", 6.211))))
    assert values == (Decimal("-0.850"), Decimal("61.260"), Decimal("62.110"))


def test_zero_change_is_valid() -> None:
    from data_provider.ifind_fundamental_adapter import shareholder_concentration_change
    values = shareholder_concentration_change(
        IfindShareholders.model_validate_json(json.dumps(holders("2026-09-30"))),
        IfindShareholders.model_validate_json(json.dumps(holders("2026-06-30"))),
    )
    assert values == (Decimal(0), Decimal(60), Decimal(60))


@pytest.mark.parametrize("bad", ["missing_rank", "duplicate_rank", "wrong_unit", "partial", "mixed_period", "ratio_period", "future", "reversed", "sum_exceeds_100"])
def test_unqualified_snapshots_never_produce_change(bad: str) -> None:
    from data_provider.ifind_fundamental_adapter import shareholder_concentration_change
    current = holders("2026-09-30")
    previous = holders("2026-06-30")
    if bad == "missing_rank": current["items"].pop()
    if bad == "duplicate_rank": current["items"][-1]["rank"] = 1
    if bad == "wrong_unit": current["items"][0]["ratio"]["unit"] = "multiple"
    if bad == "partial": current["coverage"] = "partial"
    if bad == "mixed_period": current["items"][0]["report_date"] = "2026-06-30"
    if bad == "ratio_period": current["items"][0]["ratio"]["period"] = "2026-06-30"
    if bad == "future": current = holders("2099-09-30")
    if bad == "reversed": previous = holders("2026-12-31")
    if bad == "sum_exceeds_100": current = holders("2026-09-30", 11.0)
    with pytest.raises(ValueError):
        shareholder_concentration_change(IfindShareholders.model_validate_json(json.dumps(current)), IfindShareholders.model_validate_json(json.dumps(previous)))
