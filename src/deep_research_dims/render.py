# -*- coding: utf-8 -*-
"""双轨报告成文：Jinja 模板直灌（零 LLM），数字全部来自结构化维度数据。

含成文后结构校验（validator 两层化中的"成文后结构校验"层）。
卖方展示层确定性算术（隐含空间 %、期望值 3×3 敏感性、财务快照表）在此规则装配，
仅加减乘除与取值，不引入业务公式，等价于模板宏的 Python 化。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from src.schemas.deep_research_dims import (
    BayesianDim,
    ConclusionDim,
    DataDim,
    GuardrailEvent,
    HistoryDim,
    IntelDim,
    PhaseDim,
    PlanDim,
    ScenariosDim,
    SignalDim,
    SixDimDim,
    SupplyChainDim,
)

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates",
)
_TEMPLATE_NAME_V1 = "deep_research_dual_track.j2"
_TEMPLATE_NAME_V2 = "deep_research_report_v2.j2"
# 新报告一律走 v2 模板（内部自带 LLM 缺席降级）；v1 仅用于历史报告回溯。
_TEMPLATE_NAME = _TEMPLATE_NAME_V2

REQUIRED_HEADINGS = [
    "## 一、结论",
    "## 二、盘面",
    "## 三、走势",
    "## 四、风险矩阵",
    "## 五、评分表",
    "## 六、分项依据",
    "## 附录",
]

# 维度 id → 中文节名（v2 分维度节标题与子报告头共用）
DIM_LABELS: Dict[str, str] = {
    "business": "业务画像与竞争地位",
    "fundamental": "财务与基本面（F1）",
    "sector": "板块分析（F2）",
    "supply_chain": "产业链解读",
    "intel": "消息面详析",
    "capital": "资金面分析",
    "technical": "技术面分析",
    "sentiment": "情绪面分析",
    "us_china": "中美竞争分析",
    "ownership": "股权架构与高管",
    "six_dim": "六维指标明细",
    "scenarios": "情景与时间层级",
    "bayesian": "贝叶斯证据链",
    "conclusion": "投资结论",
    "signal": "信号",
    "data": "数据透视",
    "plan": "作战计划",
    "phase": "阶段决策",
    "history": "历史对比",
}

# v2「七、分维度深度」的节顺序（观点句标题来自经理终稿，缺省用中文节名）
VIEW_DIM_ORDER = [
    "business", "fundamental", "supply_chain", "intel", "capital",
    "technical", "sentiment", "us_china", "ownership",
]


# _format_cn 键名专用中文化（结构与正文替换分离：键名可大胆映射，正文不受影响）
KEY_ZH_MAP: Dict[str, str] = {
    "value": "数值", "score": "评分", "label": "评价",
    "products": "产品清单", "products_known": "已知产品明细",
    "moat": "护城河", "position": "行业定位", "industry_status": "行业地位",
    "source_grade": "来源等级", "revenue_breakdown": "收入结构",
    "market_benchmark": "同业对比", "broad_market_benchmark": "大盘基准",
    "index_environment_for_context_only": "当日大盘环境（仅背景参考）",
    "current": "现值", "change_pct": "涨跌幅", "source": "来源", "note": "说明",
    "structure": "结构", "name": "名称", "tag": "定位",
    "vs_baijiu_peers": "同业对比", "vs_market": "与大盘对比",
    "moat_gap": "护城河差距", "valuation_premium": "估值溢价",
    "today_indices": "当日指数", "sh000001": "上证指数", "sh000300": "沪深300",
    "sz399006": "创业板指", "sh000688": "科创50",
    "shanghai_change_pct": "上证指数涨跌幅", "shenzhen_change_pct": "深证成指涨跌幅",
    "chinext_change_pct": "创业板指涨跌幅", "csi300_change_pct": "沪深300涨跌幅",
    "star50_change_pct": "科创50涨跌幅",
    "industry_position": "行业地位", "moat_weakness": "护城河短板",
    "benchmark": "对比基准", "sector_peers": "板块同业", "source_level": "来源等级",
    "role_cn": "中国链位置", "role_us": "美国/全球链位置",
    "export_control": "出口管制", "sanction_risk": "制裁风险", "substitution": "替代进度",
}


def _format_cn(value: Any) -> str:
    """把 dict/list/JSON 字符串递归格式化为中文可读多行文本（治 JSON 裸奔）。

    - 字符串若整体是 JSON 则解析后递归；普通字符串原样返回。
    - dict → 「- **键**：值」分行（键经 KEY_ZH_MAP 中文化）。
    - list → 标量列表逐项 bullet；dict 列表每项一行「键：值」聚合。
    """
    if value is None:
        return "缺失"
    if isinstance(value, str):
        s = value.strip()
        if s.startswith(("{", "[")):
            try:
                return _format_cn(json.loads(s))
            except ValueError:
                return value
        return value
    if isinstance(value, dict):
        lines: List[str] = []
        for k, v in value.items():
            label = KEY_ZH_MAP.get(str(k), ZH_TOKEN_MAP.get(str(k), str(k)))
            if isinstance(v, (dict, list)):
                sub = _format_cn(v)
                if not sub:
                    continue
                lines.append(f"- **{label}**：")
                lines.extend(f"  {line}" for line in sub.split("\n"))
            else:
                cell = "缺失" if v is None else v
                lines.append(f"- **{label}**：{cell}")
        return "\n".join(lines)
    if isinstance(value, list):
        items: List[str] = []
        for entry in value:
            if entry is None:
                items.append("- 缺失")
            elif isinstance(entry, dict):
                flat = "；".join(
                    f"{KEY_ZH_MAP.get(str(k), ZH_TOKEN_MAP.get(str(k), str(k)))}：{_format_cn(v)}"
                    if isinstance(v, (dict, list))
                    else f"{KEY_ZH_MAP.get(str(k), ZH_TOKEN_MAP.get(str(k), str(k)))}：{'缺失' if v is None else v}"
                    for k, v in entry.items()
                )
                items.append(f"- {flat}" if flat else "")
            else:
                items.append(f"- {entry}")
        return "\n".join(i for i in items if i)
    return str(value)


# 财务指标 {value, score, label} 结构 → 投资人可读一行（内部评分降级为括号注）
_FUND_METRIC_ZH: Dict[str, str] = {
    "roe": "ROE（净资产收益率）",
    "gross_margin": "毛利率",
    "revenue_yoy": "营收同比增速",
    "net_profit_yoy": "净利润同比增速",
}


def _format_metric_block(block: Any) -> str:
    """盈利/成长质量的固定结构 dict → 可读行：「- ROE：7.9%（评价：偏弱）」。"""
    if not isinstance(block, dict) or not block:
        return ""
    lines: List[str] = []
    for key, metric in block.items():
        if key == "scissors":
            continue
        zh = _FUND_METRIC_ZH.get(key, key)
        if not isinstance(metric, dict):
            cell = str(metric) if metric not in (None, "") else "数据缺失"
            lines.append(f"- {zh}：{cell}")
            continue
        value = metric.get("value")
        label = str(metric.get("label") or "").strip()
        if value in (None, "", "缺失"):
            lines.append(f"- {zh}：数据缺失")
        else:
            suffix = f"（评价：{label}）" if label and label != "数据缺失" else ""
            lines.append(f"- {zh}：{value}%{suffix}" if isinstance(value, (int, float)) else f"- {zh}：{value}{suffix}")
    scissors = block.get("scissors")
    if scissors not in (None, "", "缺失"):
        lines.append(f"- 剪刀差（营收-净利增速差）：{scissors}%")
    return "\n".join(lines)


def _sanitize_reason(reason: Any) -> str:
    """降级原因对投资人可见：只留首行、截断，校验错误收敛为中文短语。"""
    first = str(reason or "未知原因").split("\n")[0].strip()
    if "validation error" in first.lower():
        return "数据校验未通过"
    return first[:60] or "未知原因"


_EVENT_DATE_MAX = 24


def _clean_event_date(value: Any) -> str:
    """事件日期收口：括号闭合截断 + 长度限制。

    反例（研究员原文直出）：「未知(按A股规则需在2026年4月30日」——括号不闭合、无右界。
    """
    text = str(value or "").strip()
    if not text:
        return "日期待定"
    # 有未闭合的左括号：截到左括号前；有闭合括号且超长：截到右括号
    open_idx = text.find("(")
    open_idx_fw = text.find("（")
    if open_idx == -1 or (open_idx_fw != -1 and open_idx_fw < open_idx):
        open_idx = open_idx_fw
    if open_idx != -1:
        close_idx = max(text.rfind(")"), text.rfind("）"))
        if close_idx <= open_idx:
            text = text[:open_idx].strip()
    if len(text) > _EVENT_DATE_MAX:
        text = text[:_EVENT_DATE_MAX].rstrip("，,。.（(") + "…"
    return text or "日期待定"


def _event_plans_view(intel: Any) -> List[Dict[str, Any]]:
    """事件日历视图：date 经清洗，事件/结果/预案原样透传。"""
    out = []
    for ev in getattr(intel, "event_calendar", []) or []:
        out.append(
            {
                "event": str(getattr(ev, "event", "") or "").strip(),
                "date": _clean_event_date(getattr(ev, "date", None)),
                "outcomes": [str(o) for o in (getattr(ev, "outcomes", None) or [])],
                "plans": [str(p) for p in (getattr(ev, "plans", None) or [])],
            }
        )
    return out


def _degraded_note(dim: Any) -> str:
    status = getattr(dim, "status", "ok")
    if status == "degraded":
        return f"⚠️ 本维度生成不充分（{_sanitize_reason(getattr(dim, 'degraded_reason', None))}），以下内容为降级占位。"
    if status == "skipped":
        return "— 本维度未选择生成（省钱模式），以下内容为默认占位，不代表分析结论。"
    return ""


def _dim_indicator_md(six_dim: Any, dim_name: str) -> str:
    """六维中某维度指标的预格式化 markdown 行（模板直接输出）。"""
    framework = getattr(six_dim, "framework", None)
    if not framework:
        return ""
    lines: List[str] = []
    for d in framework.dimensions:
        if d.dimension != dim_name:
            continue
        for i in d.indicators:
            gap = "（缺口）" if getattr(i, "data_gap", False) else ""
            lines.append(
                f"- {i.name}（权重 {round(i.weight * 100)}%）：{i.score} 分 —— {i.summary}{gap}"
            )
    return "\n".join(lines)


def _dedup_history_rows(history: Any, cap: int = 5) -> List[Any]:
    """同日同结论去重（缓存命中会反复写 journal 导致历史刷屏），最多留 cap 条。"""
    seen = set()
    rows: List[Any] = []
    for r in getattr(history, "rows", []) or []:
        key = (str(r.created_at)[:10], str(r.rating_hint), str(r.one_sentence))
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
    return rows[:cap]


def _pct(value: Any) -> str:
    """机器胜率（0-1 小数）→ 投资人可读百分比；非数值原样返回。"""
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _num(value: Any, digits: int = 2) -> str:
    """机器精度数字收敛；非数值原样返回。"""
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _bayes_summary(bayesian: Any) -> str:
    """贝叶斯链投资人可读一行（胜率百分比化，Edge 收敛两位）。"""
    b = getattr(bayesian, "bayesian", None)
    if not b:
        return ""
    line = (
        f"- 先验胜率 {_pct(getattr(b, 'prior_p', None))} → 后验胜率 {_pct(getattr(b, 'posterior_p', None))}"
        f" ｜ 认知差 Edge {_num(getattr(b, 'edge', None))}"
        f" ｜ 仓位建议：{getattr(b, 'position_suggestion', None) or 'N/A'}"
    )
    return line


def _to_float(value: Any) -> Optional[float]:
    """宽松转 float；None/非数值/布尔返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upside_pct(expected_value: Any, current_price: Any) -> Optional[float]:
    """隐含空间 % =（期望值 - 现价）/ 现价；输入不成立返回 None。"""
    ev = _to_float(expected_value)
    price = _to_float(current_price)
    if ev is None or price is None or price <= 0:
        return None
    return round((ev - price) / price * 100, 1)


def _ev_sensitivity(scenarios: Any) -> str:
    """期望值 3×3 敏感性矩阵（全规则）：中性锚 ±10% × 中性概率 40/50/60%。

    回答卖方式问题「情景假设松动能动摇结论吗」。中性概率取 p 时，
    乐观/悲观按原比例瓜分剩余 (1-p)。任一情景缺失即返回空串。
    """
    scs = getattr(getattr(scenarios, "scenarios", None), "scenarios", None)
    if not scs or len(scs) < 3:
        return ""
    by_type: Dict[str, Any] = {}
    for s in scs:
        by_type.setdefault(str(getattr(s, "type", "")), s)
    bull = by_type.get("optimistic") or by_type.get("bullish")
    neutral = by_type.get("neutral")
    bear = by_type.get("pessimistic") or by_type.get("bearish")
    if not (bull and neutral and bear):
        return ""
    n_anchor = _to_float(getattr(neutral, "value_anchor", None))
    b_prob = _to_float(getattr(bull, "probability", None))
    n_prob = _to_float(getattr(neutral, "probability", None))
    r_prob = _to_float(getattr(bear, "probability", None))
    b_anchor = _to_float(getattr(bull, "value_anchor", None))
    r_anchor = _to_float(getattr(bear, "value_anchor", None))
    if None in (n_anchor, b_prob, n_prob, r_prob, b_anchor, r_anchor):
        return ""
    if (
        n_prob <= 0 or n_prob >= 1 or (b_prob is not None and r_prob is not None and b_prob + r_prob <= 0)
    ):  # type: ignore[operator]
        return ""

    # 上面的 None in 检查已排除 None，但 pyright 推不动，加 isinstance narrow：
    assert n_anchor is not None and b_anchor is not None and r_anchor is not None
    n_anchor_f: float = n_anchor
    b_anchor_f: float = b_anchor
    r_anchor_f: float = r_anchor
    shifts = [
        ("中性锚 -10%", n_anchor_f * 0.9),  # type: ignore[operator]
        ("中性锚不变", n_anchor_f),  # type: ignore[operator]
        ("中性锚 +10%", n_anchor_f * 1.1),  # type: ignore[operator]
    ]
    probs = [0.4, 0.5, 0.6]
    header = "| 期望值（现价对照） | " + " | ".join(f"中性概率 {int(p * 100)}%" for p in probs) + " |"
    sep = "|" + "---|" * (len(probs) + 1)
    rows = [header, sep]
    b_anchor_f2: float = b_anchor
    r_anchor_f2: float = r_anchor
    for label, anchor in shifts:
        cells = []
        anchor_f: float = anchor
        for p in probs:
            rest = 1.0 - p
            ev = (
                b_anchor_f2 * (rest * b_prob / (b_prob + r_prob))  # type: ignore[operator]
                + anchor_f * p  # type: ignore[operator]
                + r_anchor_f2 * (rest * r_prob / (b_prob + r_prob))  # type: ignore[operator]
            )
            cells.append(f"{ev:.2f}")
        rows.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


_SNAPSHOT_METRICS: List[tuple[str, str, str]] = [
    # (中文名, fundamental 字段路径源, 键)
    ("ROE", "profitability", "roe"),
    ("毛利率", "profitability", "gross_margin"),
    ("营收同比增速", "growth_quality", "revenue_yoy"),
    ("净利同比增速", "growth_quality", "net_profit_yoy"),
    ("PE(TTM)", "valuation_detail", "pe_ttm"),
    ("PB", "valuation_detail", "pb"),
    ("市值", "valuation_detail", "market_cap"),
]

# 六维总评分区间 → 定性（对标卖方评分卡：分数必须落到一句话区间定位）
_SCORE_BANDS: List[tuple[float, str]] = [
    (75.0, "强烈看多"),
    (60.0, "偏多"),
    (45.0, "中性震荡"),
    (30.0, "观望偏空"),
    (0.0, "强烈看空"),
]


def _score_band_view(total: Any, as_of: str) -> Dict[str, str]:
    """评分区间条 + 结算验证日期（生成后 1 个月，评分明细锚点）。"""
    try:
        score = float(total)
    except (TypeError, ValueError):
        score = None
    band = next((label for low, label in _SCORE_BANDS if score is not None and score >= low), "数据不足")
    settlement = "N/A"
    try:
        from datetime import date

        base = date.fromisoformat(str(as_of)[:10])
        month = (base.month % 12) + 1
        year = base.year + (1 if base.month == 12 else 0)
        settlement = date(year, month, base.day).isoformat()
    except ValueError:
        pass
    # 显示原值（与第五节评分表口径一致），不二次四舍五入
    return {"score": "数据不足" if score is None else f"{round(score, 2)}", "band": band, "settlement": settlement}


def _fin_snapshot(fundamental: Any) -> str:
    """财务快照表（单期最新披露）：指标/最新值/评价三列，缺口按纪律标「数据缺失」。"""
    lines = ["| 指标 | 最新披露值 | 评价 |", "|------|-----------|------|"]
    gaps = 0
    for label, block_name, key in _SNAPSHOT_METRICS:
        block = getattr(fundamental, block_name, None) or {}
        metric = block.get(key) if isinstance(block, dict) else None
        if isinstance(metric, dict):
            value = metric.get("value")
            eval_label = str(metric.get("label") or "").strip()
        else:
            value, eval_label = metric, ""
        if value in (None, "", "缺失"):
            gaps += 1
            lines.append(f"| {label} | 数据缺失 | — |")
        else:
            suffix = f"%" if isinstance(value, (int, float)) and key not in ("pe_ttm", "pb", "market_cap") else ""
            cell = f"{value}{suffix}" if isinstance(value, (int, float)) else str(value)
            lines.append(f"| {label} | {cell} | {eval_label or '—'} |")
    if gaps:
        lines.append(f"\n*快照为阶段 0 单期数据，{gaps} 项缺口按纪律不编造；补齐数据后看年报/中报分产品收入拆分与两期毛利率方向。*")
    return "\n".join(lines)


def build_view(
    stock_name: str,
    stock_code: str,
    as_of: str,
    ctx_limitations: List[str],
    dims: Dict[str, Any],
    guardrail_events: List[GuardrailEvent],
    report_id: str = "",
    final_conclusion: str = "",
    manager: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把契约模型装配成模板视图（纯数据搬运，不做计算）。"""
    signal: SignalDim = dims["signal"]
    data: DataDim = dims["data"]
    intel: IntelDim = dims["intel"]
    plan: PlanDim = dims["plan"]
    phase: PhaseDim = dims["phase"]
    history: HistoryDim = dims["history"]
    six_dim: SixDimDim = dims["six_dim"]
    bayesian: BayesianDim = dims["bayesian"]
    conclusion: ConclusionDim = dims["conclusion"]
    supply_chain: SupplyChainDim = dims["supply_chain"]
    scenarios: ScenariosDim = dims["scenarios"]
    fundamental: Any = dims["fundamental"]
    business: Any = dims["business"]
    sector: Any = dims["sector"]
    technical: Any = dims["technical"]
    capital: Any = dims["capital"]
    sentiment: Any = dims["sentiment"]
    ownership: Any = dims["ownership"]
    us_china: Any = dims["us_china"]

    manager = manager or {}
    from src.deep_research_dims.manager_writeup import section_title

    dim_sections = [
        {"id": dim_id, "label": DIM_LABELS[dim_id], "env": dims[dim_id]}
        for dim_id in VIEW_DIM_ORDER
        if dim_id in dims
    ]

    return {
        "stock_name": stock_name,
        "stock_code": stock_code,
        "as_of": as_of,
        "report_id": report_id,
        "final_conclusion": final_conclusion,
        "report_type": "首次覆盖" if not getattr(history, "rows", None) else "跟踪更新",
        "manager": manager,
        "title_angle": str(manager.get("title_angle") or ""),
        "executive_summary": str(manager.get("executive_summary") or ""),
        "thesis_points": manager.get("thesis_points") or [],
        "risk_matrix": manager.get("risk_matrix") or [],
        "money_paths": manager.get("money_paths") or [],
        "decision_matrix": manager.get("decision_matrix") or [],
        "scenario_triggers": manager.get("scenario_triggers") or {},
        "dim_verdicts": manager.get("dim_verdicts") or {},
        "score_band": _score_band_view(
            getattr(getattr(six_dim, "framework", None), "dimension_total", None), as_of
        ),
        "upside_pct": _upside_pct(
            getattr(scenarios, "expected_value", None),
            getattr(getattr(getattr(data, "perspective", None), "price_position", None), "current_price", None),
        ),
        "current_price": getattr(
            getattr(getattr(data, "perspective", None), "price_position", None), "current_price", None
        ),
        "ev_sensitivity": _ev_sensitivity(scenarios),
        "fin_snapshot": _fin_snapshot(fundamental),
        "event_plans": _event_plans_view(intel),
        "section_title": lambda key: section_title(manager, key),
        "section_narrative": lambda key: str((manager.get("section_narratives") or {}).get(key) or ""),
        "history_rows": _dedup_history_rows(history),
        "dim_sections": dim_sections,
        "ind_capital": _dim_indicator_md(six_dim, "资金面"),
        "ind_technical": _dim_indicator_md(six_dim, "技术面"),
        "ind_sentiment": _dim_indicator_md(six_dim, "情绪面"),
        "ind_macro": _dim_indicator_md(six_dim, "宏观"),
        "fund_profitability_md": _format_metric_block(getattr(fundamental, "profitability", None)),
        "fund_growth_md": _format_metric_block(getattr(fundamental, "growth_quality", None)),
        "bayes_summary": _bayes_summary(bayesian),
        "dim_anchor_items": [
            {
                "id": dim_id,
                "label": ZH_TOKEN_MAP.get(dim_id, dim_id),
                "anchor": anchor,
            }
            for dim_id, anchor in DIM_SECTION_ANCHORS.items()
        ],
        "signal": signal,
        "data": data,
        "intel": intel,
        "plan": plan,
        "phase": phase,
        "history": history,
        "six_dim": six_dim,
        "bayesian": bayesian,
        "conclusion": conclusion,
        "supply_chain": supply_chain,
        "scenarios": scenarios,
        "fundamental": fundamental,
        "business": business,
        "sector": sector,
        "technical": technical,
        "capital": capital,
        "sentiment": sentiment,
        "ownership": ownership,
        "us_china": us_china,
        "guardrail_events": guardrail_events,
        "limitations": ctx_limitations,
        "degraded_note": _degraded_note,
    }


# 报告中继英→中（专业名词保留：PE/PB/ROE/MACD/RSI/EV/TTM/LR/ATR 等不映射）
ZH_TOKEN_MAP: Dict[str, str] = {
    # 情景类型
    "optimistic": "乐观", "neutral": "中性", "pessimistic": "悲观",
    # 趋势罗盘状态
    "weekly_bull": "周线多头", "weekly_bear": "周线空头",
    "weekly_neutral": "周线中性", "weekly_disabled": "周线样本不足",
    "bullish": "多头", "bearish": "空头",
    # 概率/评分标注
    "strong_positive": "强利好", "weak_positive": "弱利好",
    "weak_negative": "弱利空", "strong_negative": "强利空",
    "industry_base_rate": "行业基率", "neutral_default": "中性默认",
    # 供应链/校验状态
    "existing_report": "复用专项报告", "confirmed": "双源确认",
    "partial": "部分确认", "conflict": "双源冲突",
    "unverified": "未验证", "not_applicable": "不适用",
    # 研究员叙事英文标签
    "inferred": "推断", "data_gap": "数据缺口", "data_gaps": "数据缺口",
    "primary": "公告级信源", "secondary": "次级信源",
    # 维度 id（附录索引展示）
    "signal": "信号", "data": "数据", "intel": "消息", "plan": "计划",
    "phase": "阶段", "history": "历史", "six_dim": "六维", "bayesian": "贝叶斯",
    "conclusion": "结论", "supply_chain": "产业链", "scenarios": "情景",
    "fundamental": "财务", "sector": "板块", "technical": "技术",
    "capital": "资金", "sentiment": "情绪", "ownership": "股权",
    "us_china": "中美", "business": "业务",
}

_ZH_PATTERN = None


def _zh_polish(markdown: str) -> str:
    """已知英文 token 整词替换为中（链接/URL 行跳过，防断路径）。"""
    global _ZH_PATTERN
    import re

    if _ZH_PATTERN is None:
        _ZH_PATTERN = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in ZH_TOKEN_MAP) + r")\b"
        )
    out = []
    for line in markdown.split("\n"):
        stripped = line.strip()
        if (
            "](" in line
            or "http" in line
            or stripped.startswith("<!-- dim:")
            or "<a name=" in line  # 锚点名是机器契约（dim id），不做中文化
        ):
            out.append(line)
            continue
        out.append(_ZH_PATTERN.sub(lambda m: ZH_TOKEN_MAP[m.group(1)], line))
    return "\n".join(out)


def render_markdown(
    view: Dict[str, Any], template_name: Optional[str] = None
) -> str:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATE_DIR),
        autoescape=select_autoescape(enabled_extensions=()),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["cn"] = _format_cn
    template = env.get_template(template_name or _TEMPLATE_NAME)
    return _zh_polish(template.render(**view))


def validate_structure(markdown: str) -> List[str]:
    """成文后结构校验：返回缺失章节列表（空=通过）。"""
    return [h for h in REQUIRED_HEADINGS if h not in markdown]


# ---------------------------------------------------------------------------
# 按维度切分子报告（方向 A：每维度独立 .md，内容即合并报告对应章节，天然满足
# 「子报告 ≥ 合并报告章节」验收；skipped 维度章节含"未选择生成"注记）
# ---------------------------------------------------------------------------

# 维度 id → 报告章节锚点（#### 级，与五段模板严格对齐；切分子报告用）
DIM_SECTION_ANCHORS: Dict[str, str] = {
    "fundamental": "#### 财务与基本面（F1）",
    "sector": "#### 板块分析（F2）",
    "supply_chain": "#### 产业链解读",
    "intel": "#### 消息面详析",
    "six_dim": "#### 六维指标明细",
    "scenarios": "#### 情景与时间层级",
    "bayesian": "#### 贝叶斯证据链",
    "conclusion": "#### 投资结论",
    "signal": "#### 信号",
    "data": "#### 数据透视",
    "plan": "#### 作战计划",
    "phase": "#### 阶段决策",
    "history": "#### 历史对比",
    "technical": "#### 技术面分析",
    "capital": "#### 资金面分析",
    "sentiment": "#### 情绪面分析",
    "ownership": "#### 股权架构与高管",
    "us_china": "#### 中美竞争分析",
    "business": "#### 业务画像与竞争地位",
}


_DIM_MARKER_PREFIX = "<!-- dim:"


def split_dim_sections(markdown: str) -> Dict[str, str]:
    """把整份报告切成 {dim_id: 章节 markdown}。

    v2 报告按 `<!-- dim:{id} -->` 标记切分（观点式标题下锚点不再固定）；
    旧版报告回退到 DIM_SECTION_ANCHORS 锚点行切分，保证历史报告子链接可用。
    章节范围 = 标记行下一行起，到下一个标记/`## ` 标题前。
    """
    lines = markdown.split("\n")

    marker_idx: Dict[str, int] = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith(_DIM_MARKER_PREFIX) and s.endswith("-->"):
            dim_id = s[len(_DIM_MARKER_PREFIX) : -3].strip()
            if dim_id and dim_id not in marker_idx:
                marker_idx[dim_id] = i

    if marker_idx:
        starts = sorted(marker_idx.values())
        sections: Dict[str, str] = {}
        for dim_id, start in sorted(marker_idx.items(), key=lambda x: x[1]):
            end = len(lines)
            for j in starts:
                if j > start:
                    end = j
                    break
            for j in range(start + 1, end):
                if lines[j].strip().startswith("## "):
                    end = j
                    break
            body = "\n".join(lines[start + 1 : end]).strip()
            sections[dim_id] = body + "\n"
        return sections

    # —— 旧版锚点切分（v1 模板历史报告）——
    # 锚点行号（按出现顺序）
    anchors: List[tuple[str, int]] = []
    for dim_id, anchor in DIM_SECTION_ANCHORS.items():
        for i, line in enumerate(lines):
            if line.strip() == anchor:
                anchors.append((dim_id, i))
                break
    anchors.sort(key=lambda x: x[1])

    sections = {}
    for idx, (dim_id, start) in enumerate(anchors):
        end = len(lines)
        for j in range(start + 1, len(lines)):
            stripped = lines[j].strip()
            if (
                stripped.startswith("#### ")
                or stripped.startswith("### ")
                or stripped.startswith("## ")
            ):
                end = j
                break
        sections[dim_id] = "\n".join(lines[start:end]).strip() + "\n"
    return sections


def build_dim_report(
    dim_id: str,
    section_markdown: str,
    stock_name: str,
    stock_code: str,
    as_of: str,
    report_id: str = "",
) -> str:
    """单维度子报告：小头（标题/数据截至/免责）+ 章节正文。"""
    label = DIM_LABELS.get(dim_id, DIM_SECTION_ANCHORS.get(dim_id, dim_id).lstrip("# ").strip())
    header = (
        f"# {stock_name}（{stock_code}）· {label}（子报告）\n\n"
        f"> 数据截至：{as_of}｜摘自双轨深度投研报告，单维度详版\n"
        f"> 本报告链接：查看 /api/v1/deep-research/reports/{report_id}/dims/{dim_id}"
        f" ｜ 下载加 ?download=1\n\n"
    )
    footer = "\n---\n\n*本报告由 AI 生成，不构成投资建议。*\n"
    return header + section_markdown.rstrip() + "\n" + footer
