"""Issue #35：股权可选字段形状适配与数据局限提示的采用结果披露。"""
from types import SimpleNamespace
from typing import Any

import pytest

from src.deep_research.researchers.ownership import parse as parse_ownership
from src.deep_research_dims.context import SharedContext, _safe_chip, _safe_fundamental
from src.deep_research_dims.render import build_view, render_markdown
from src.deep_research_dims.six_dim import _chip_from_context
from tests.test_deep_research_dims import _all_default_dims

_CORE_PAYLOAD: dict[str, Any] = {
    "controller": "李水波（家族控股）",
    "top_holders": [{"name": "李水波", "source": "供应商资料"}],
    "executives": [{"name": "高管甲", "source": "公告资料"}],
}


def _parse_with(changes: Any) -> tuple[Any, list[str]]:
    dim, _used = parse_ownership({**_CORE_PAYLOAD, "recent_changes": changes}, steps=1)
    return dim.recent_changes, dim.data_gaps


@pytest.mark.parametrize("changes", [
    [{"name": "董事变更公告", "source": "公告资料", "date": "2026-06-01"}],
    '[{"name": "董事变更公告", "date": "2026-06-01"}]',
    {"window": "近12个月", "items": [{"name": "董事变更公告"}]},
    [],
    None,
])
def test_unambiguous_shapes_yield_records_without_gaps(changes: Any) -> None:
    records, gaps = _parse_with(changes)
    assert [holder.name for holder in records] in ([], ["董事变更公告"])
    assert records or changes in ([], None)
    assert gaps == []


def test_json_array_string_is_decoded_strictly() -> None:
    records, _gaps = _parse_with('[{"name": "减持公告", "date": "2026-03-01"}]')
    assert records[0].name == "减持公告"
    assert records[0].detail == "2026-03-01"


@pytest.mark.parametrize("changes,reason", [
    ("not json", "非法 JSON 字符串"),
    ({"window": "近12个月"}, "无法识别的对象结构"),
    ({"foo": 1}, "无法识别的对象结构"),
    (42, "类型无法识别"),
])
def test_unrecognized_shapes_record_gap_and_keep_core_facts(changes: Any, reason: str) -> None:
    dim, _used = parse_ownership({**_CORE_PAYLOAD, "recent_changes": changes, "score": 85}, steps=1)
    assert dim.recent_changes == []
    assert dim.controller == _CORE_PAYLOAD["controller"]
    assert [holder.name for holder in dim.top_holders] == ["李水波"]
    assert [holder.name for holder in dim.executives] == ["高管甲"]
    assert any(reason in gap for gap in dim.data_gaps)
    # 可选变动缺项如实披露但不降低章节状态：核心资料齐全时保留评分。
    assert dim.status == "ok" and dim.score == 85


@pytest.mark.parametrize("field,label", [
    ("top_holders", "前十大股东"),
    ("executives", "核心高管"),
])
def test_wrapped_shapes_on_core_lists_do_not_wipe_the_dim(field: str, label: str) -> None:
    """核心列表出现与变动列表相同的包装形状时解包或记缺项，不再整维崩溃。"""
    payload = {**_CORE_PAYLOAD, field: {"items": [{"name": "解包条目"}]}}
    dim, _used = parse_ownership(payload, steps=1)
    assert [holder.name for holder in getattr(dim, field)] == ["解包条目"]
    assert dim.controller == _CORE_PAYLOAD["controller"]

    payload = {**_CORE_PAYLOAD, field: "not json", "recent_changes": []}
    dim, _used = parse_ownership(payload, steps=1)
    assert getattr(dim, field) == []
    assert any(label in gap and "非法 JSON" in gap for gap in dim.data_gaps)
    assert label in dim.data_gaps or dim.status == "degraded"


@pytest.mark.parametrize("changes", [
    [{}],
    {"items": [{}]},
    "[{}]",
    [{"name": "董事变更公告"}, 123],
    [{"name": "董事变更公告"}, "有效条目", {}],
])
def test_empty_or_invalid_entries_are_dropped_not_fabricated(changes: Any) -> None:
    """空对象被条目模型填占位名的记录不算有效变动，在适配边界剔除并记缺项。"""
    dim, _used = parse_ownership({**_CORE_PAYLOAD, "recent_changes": changes, "score": 85}, steps=1)
    assert all(holder.name != "资料缺失" for holder in dim.recent_changes)
    assert any("未通过校验" in gap for gap in dim.data_gaps)
    assert dim.controller and dim.top_holders and dim.executives  # 其余核心资料保留。
    assert dim.status == "ok" and dim.score == 85  # 单条可选记录缺项不拖累整章。


def test_holder_details_recovered_do_not_mark_research_complete() -> None:
    """股东明细已取得但控制人/高管不足：恢复明细不等于股权研究完成。"""
    details = {"coverage": "complete", "items": [{"name": "李水波", "rank": 1, "report_date": "2026-06-30"}]}
    dim, _used = parse_ownership(
        {"recent_changes": [], "shareholder_details": details}, steps=1
    )
    assert dim.shareholder_details is not None and dim.shareholder_details.items
    assert dim.status == "degraded"
    assert "实际控制人信息" in dim.data_gaps and "核心高管" in dim.data_gaps


# ---------------------------------------------------------------------------
# B：数据局限提示区分原始来源、已采用资料与缺口范围
# ---------------------------------------------------------------------------

_FULL_COMPACT = {
    "errors": ["choice:targets:timeout"],
    "valuation": {"data": {"pe_ratio": 30.0, "pb_ratio": 2.0, "total_mv": 1.0e10}},
    "growth": {"data": {"roe": 5.0, "gross_margin": 30.0, "revenue_yoy": 10.0, "net_profit_yoy": -7.5}},
    "institution": {"data": {}},
    "boards": {"data": {}},
    "research": {},
}


@pytest.fixture
def fundamental_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import data_tools
    from src.deep_research_dims import dim_cache

    monkeypatch.setattr(dim_cache, "load_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(dim_cache, "save_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(data_tools, "_compact_fundamental_context", lambda raw: dict(raw))


def test_all_obtained_with_failed_supplements_is_not_reported_as_incomplete(
    fundamental_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.tools import data_tools

    monkeypatch.setattr(
        data_tools, "_get_fetcher_manager",
        lambda: SimpleNamespace(get_fundamental_context=lambda code, budget_seconds: dict(_FULL_COMPACT)),
    )
    ctx = SharedContext("300260", "新莱应材", "2026-10-20")
    ctx.fundamental = _safe_fundamental("300260", ctx)
    notice = next(item for item in ctx.limitations if item.startswith("基本面核心指标"))
    assert "已取得 7/7 项" in notice
    assert "部分补充查询未完成" in notice and "已取得指标不受影响" in notice
    assert "快照不缓存" not in notice


def test_partial_indicators_list_the_missing_fields(
    fundamental_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.agent.tools import data_tools

    compact = {**_FULL_COMPACT, "errors": [], "valuation": {"data": {"pb_ratio": 2.0, "total_mv": 1.0e10}}}
    monkeypatch.setattr(
        data_tools, "_get_fetcher_manager",
        lambda: SimpleNamespace(get_fundamental_context=lambda code, budget_seconds: dict(compact)),
    )
    ctx = SharedContext("300260", "新莱应材", "2026-10-20")
    ctx.fundamental = _safe_fundamental("300260", ctx)
    notice = next(item for item in ctx.limitations if item.startswith("基本面核心指标"))
    assert "已取得 6/7 项" in notice and "市盈率TTM" in notice and "pe_ttm" not in notice
    assert "部分补充查询未完成" not in notice  # 无来源错误时不冒充查询失败。


def test_chip_states_disclose_adoption_and_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.agent.tools import data_tools

    real_chip = SimpleNamespace(
        code="300260", date="2026-10-20", source="akshare", profit_ratio=0.6, avg_cost=50.0,
        cost_90_low=40.0, cost_90_high=60.0, concentration_90=0.4,
        cost_70_low=45.0, cost_70_high=55.0, concentration_70=0.2,
        to_dict=lambda: {"code": "300260", "source": "akshare", "avg_cost": 50.0},
    )
    estimated = {"source": "estimated_from_history", "estimated": True, "avg_cost": 51.0, "profit_ratio": 0.5}

    def manager(chip: Any):
        return SimpleNamespace(get_chip_distribution=lambda code: chip)

    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: manager(real_chip))
    ctx_real = SharedContext("300260", "新莱应材", "2026-10-20")
    assert _safe_chip("300260", ctx_real) and not any("筹码" in item for item in ctx_real.limitations)

    monkeypatch.setattr(data_tools, "_get_fetcher_manager", lambda: manager(None))
    monkeypatch.setattr(data_tools, "_estimate_chip_fallback", lambda code: dict(estimated))
    ctx_estimated = SharedContext("300260", "新莱应材", "2026-10-20")
    chip = _safe_chip("300260", ctx_estimated)
    assert chip["source"] == "estimated_from_history" and chip["estimated"] is True
    assert ctx_estimated.limitations == ["原始筹码分布数据未取得，资金面采用历史行情估算（精度有限）"]

    monkeypatch.setattr(data_tools, "_estimate_chip_fallback", lambda code: None)
    ctx_missing = SharedContext("300260", "新莱应材", "2026-10-20")
    assert _safe_chip("300260", ctx_missing) == {}
    assert ctx_missing.limitations == ["筹码分布数据缺失"]


def test_estimated_chip_is_labelled_in_six_dim_and_report() -> None:
    ctx = SharedContext("300260", "新莱应材", "2026-10-20")
    ctx.quote = {"price": 52.0}
    ctx.chip = {"source": "estimated_from_history", "estimated": True, "avg_cost": 50.0, "profit_ratio": 0.6}
    entry = _chip_from_context(ctx)
    assert entry is not None and entry["summary"].startswith("（历史行情估算，精度有限）")

    ctx.limitations = ["原始筹码分布数据未取得，资金面采用历史行情估算（精度有限）"]
    dims = _all_default_dims()
    markdown = render_markdown(build_view(ctx.stock_name, ctx.stock_code, ctx.as_of, ctx.limitations, dims, []))
    assert "原始筹码分布数据未取得，资金面采用历史行情估算" in markdown
    assert "快照不缓存" not in markdown
