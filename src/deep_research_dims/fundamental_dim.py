# -*- coding: utf-8 -*-
"""F1 财务与基本面维度：盈利/成长/安全/估值四框架详表（规则装配，不编分）。"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

from icontract import ensure
from data_provider.cross_source_validator import AnchorQuality, adopted_field_record, reading_from_field_record

from src.deep_research_dims.context import SharedContext
from src.scoring.indicators_v2 import score_valuation
from src.schemas.deep_research_dims import FinancialDataQuality, FinancialFieldQuality, FundamentalDim


_FUNDAMENTAL_FIELDS = {"pe_ratio": "pe_ttm", "pb_ratio": "pb", "roe": "roe", "gross_margin": "gross_margin", "revenue_yoy": "revenue_growth", "net_profit_yoy": "net_profit_yoy"}


def financial_field_quality(fund: Dict[str, Any], field: str) -> FinancialFieldQuality:
    """Reuse the current adopted record; a legacy scalar supplies no semantic proof."""
    metadata = (fund.get("field_meta") or {}).get(field) or {}
    comparison = ((fund.get("cross_validation") or {}).get("anchors") or {}).get(field) or {}
    readings = tuple(reading for item in comparison.get("readings", []) if (reading := reading_from_field_record(item.get("value"), item)) is not None)
    if metadata.get("value", fund.get(_FUNDAMENTAL_FIELDS.get(field, field))) != fund.get(_FUNDAMENTAL_FIELDS.get(field, field)):
        metadata = {}
    failures = tuple(dict.fromkeys(f"{item['source']}:{item['reason_code']}" for item in [*metadata.get("source_errors", []), *comparison.get("source_errors", [])]))
    reading = reading_from_field_record(fund.get(_FUNDAMENTAL_FIELDS.get(field, field)), metadata)
    if reading is None:
        return FinancialFieldQuality(quality=AnchorQuality(status="missing", reason_codes=tuple((metadata.get("quality") or {}).get("reason_codes") or ())), comparison_readings=readings, source_errors=failures)
    quality = AnchorQuality.model_validate_json(json.dumps(metadata.get("quality") or {"status": "unverified"})) if reading.normalization_error != "metadata_invalid" else AnchorQuality()
    record = adopted_field_record(field, reading, quality, selection_reason=metadata.get("selection_reason", "unknown"))
    return FinancialFieldQuality(reading=reading, quality=quality, rule_eligible=record["rule_eligible"], input_reasons=tuple(record["input_reasons"]), selection_reason=record["selection_reason"], comparison_readings=readings, source_errors=failures)


def eligible_financial_value(fund: Dict[str, Any], field: str) -> Optional[float]:
    """All named rule consumers use the same field eligibility."""
    evidence = financial_field_quality(fund, field)
    return evidence.reading.value if evidence.rule_eligible and evidence.reading is not None else None


# issue #73：data_gaps 中属于 PE/PB 估值输入的缺口条目——F1 非估值豁免只容忍这些。
_VALUATION_GAP_LABELS = frozenset({"估值", "市盈率PE(TTM)", "市净率PB"})


def nonvaluation_financial_inputs_ok(dim: "FundamentalDim") -> bool:
    """F1 整维 status gate 的非估值豁免谓词（issue #73，reader 澄清口径）。

    仅当 F1 完整，或 F1 的不完整**只**来自 PE/PB 估值输入——四个非估值财务评分
    输入（ROE、毛利率、营收同比、归母净利同比）均为**实际 iFinD** reading 且
    rule_eligible（沿用既有 adopted_field_record 的证券/财期/单位资格），且
    data_gaps 中不含任何非估值缺口（如资产负债结构、核心评分字段）时成立。
    核心/非 iFinD 字段缺失或不合格、资金或研究缺口一律不放行；F1 自身的
    status/degraded_reason 不被改变。策略/上下文边界（仅获准 iFinD 主源深研
    启用）由调用点（orchestrator 证据包装配）判定，本谓词只认字段证据本身。
    """
    if dim is None:
        return False
    if dim.status == "ok":
        return True
    quality = dim.data_quality
    if quality is None:
        return False
    fields = quality.fields
    for field in ("roe", "gross_margin", "revenue_yoy", "net_profit_yoy"):
        evidence = fields.get(field)
        if (evidence is None or evidence.reading is None
                or not evidence.rule_eligible or evidence.reading.source != "ifind"):
            return False
    return not any(gap not in _VALUATION_GAP_LABELS for gap in dim.data_gaps)


def _band(value: Optional[float], bands: list[tuple[float, float, str]]) -> tuple[Optional[float], str]:
    """分档打分：(分数, 标签)；无数据 → (None, '数据缺失')。"""
    if value is None:
        return None, "数据缺失"
    for low, score, label in bands:
        if value >= low:
            return score, label
    return bands[-1][1] - 10 if bands else None, "极低"


@ensure(lambda result: result.data_quality is not None and result.data_quality.rule_usable_count <= result.data_quality.obtained_count, "Usable fields must have been acquired")
@ensure(lambda result: result.data_quality is not None and (result.health_score is None) == (result.data_quality.health_components == 0), "Health exists exactly when a component participates")
@ensure(lambda ctx, result: result.financial_safety.get("leverage") is None or abs(Decimal(str(result.financial_safety["leverage"])) - Decimal(str(ctx.fundamental["balance_sheet"]["liabilities"])) / Decimal(str(ctx.fundamental["balance_sheet"]["assets"])) * 100) <= Decimal("0.005"), "Debt ratio uses assets and liabilities from the same balance sheet")
def build_fundamental_dim(
    ctx: SharedContext,
    llm_scenarios: Optional[Dict[str, Any]] = None,
) -> FundamentalDim:
    """F1：消费 fundamental_context 快照（valuation/growth/institution 块）。"""
    fund = ctx.fundamental
    gaps: list[str] = []
    fields = {field: financial_field_quality(fund, field) for field in _FUNDAMENTAL_FIELDS}
    # issue #71 P1：单源公布值采纳的规则可用性由统一契约（adopted_field_record 的
    # selection_reason 识别）承载，这里不再有本地可用性例外。
    usable = {field: evidence.reading.value if evidence.rule_eligible and evidence.reading is not None else None for field, evidence in fields.items()}
    obtained = {field: evidence.reading.value if evidence.reading is not None else None for field, evidence in fields.items()}

    roe = obtained["roe"]
    roe_score, roe_label = _band(
        usable["roe"],
        [(20, 85.0, "优秀"), (15, 75.0, "良好"), (10, 60.0, "一般"), (5, 45.0, "偏弱")],
    )
    gm = obtained["gross_margin"]
    gm_score, gm_label = _band(
        usable["gross_margin"],
        [(50, 82.0, "高毛利"), (30, 65.0, "中高"), (15, 50.0, "中等"), (0, 40.0, "低毛利")],
    )
    if roe is not None and roe_score is None:
        roe_label = "不可用于规则"
    if gm is not None and gm_score is None:
        gm_label = "不可用于规则"
    profitability = {
        "roe": {"value": roe, "score": roe_score, "label": roe_label},
        "gross_margin": {"value": gm, "score": gm_score, "label": gm_label},
    }
    if roe_score is None:
        gaps.append("ROE")
    if gm_score is None:
        gaps.append("毛利率")

    rev = obtained["revenue_yoy"]
    rev_score, rev_label = _band(
        usable["revenue_yoy"],
        [(30, 80.0, "高增长"), (10, 65.0, "稳健增长"), (0, 50.0, "微增"), (-100, 35.0, "负增长")],
    )
    np_yoy = obtained["net_profit_yoy"]
    np_score, np_label = _band(
        usable["net_profit_yoy"],
        [(30, 80.0, "高增长"), (10, 65.0, "稳健"), (0, 50.0, "微增"), (-100, 35.0, "下滑")],
    )
    scissors = None
    rev_evidence, np_evidence = fields["revenue_yoy"], fields["net_profit_yoy"]
    rev_reading, np_reading = rev_evidence.reading, np_evidence.reading
    # issue #71：来源/口径约束仅当任一腿为 P1 单源公布值时适用——两腿须同为
    # iFinD 同批公布值（未知口径）；非 P1 旧对保持原 period/basis 条件不变
    # （跨源 restated 同期对旧行为保留，不被收敛误伤）。
    p1_involved = (rev_evidence.selection_reason == "ifind_published_single_source"
                   or np_evidence.selection_reason == "ifind_published_single_source")
    same_caliber_pair = (
        rev_reading is not None and np_reading is not None
        and (not p1_involved
             or (rev_reading.source == np_reading.source == "ifind"
                 and rev_reading.adjustment is None and np_reading.adjustment is None
                 and rev_evidence.selection_reason == "ifind_published_single_source"
                 and np_evidence.selection_reason == "ifind_published_single_source"))
    )
    if rev_score is not None and np_score is not None and same_caliber_pair and rev_reading.period is not None and rev_reading.period == np_reading.period and rev_reading.period_basis != "unknown" and rev_reading.period_basis == np_reading.period_basis:
        scissors = float(round(Decimal(str(np_reading.value)) - Decimal(str(rev_reading.value)), 2))
    if rev is not None and rev_score is None:
        rev_label = "不可用于规则"
    if np_yoy is not None and np_score is None:
        np_label = "不可用于规则"
    growth_quality = {
        "revenue_yoy": {"value": rev, "score": rev_score, "label": rev_label},
        "net_profit_yoy": {"value": np_yoy, "score": np_score, "label": np_label},
        "scissors": scissors,  # 净利-营收增速剪刀差：负值=增收不增利
    }
    if rev_score is None:
        gaps.append("营收增速")
    if np_score is None:
        gaps.append("净利增速")

    balance = fund.get("balance_sheet") or {}
    assets, liabilities = balance.get("assets"), balance.get("liabilities")
    leverage = None
    # issue #71：资产负债结构最小扩展——iFinD 同源同财期、合并报表(调整)、币种/
    # 单位确证、金额有效即可计算当前 leverage。披露日期：已知未来日期拒绝；
    # 未知（None）按「当前分析可用、非历史时点可得」如实标注（不编造日期）。
    ifind_balance_current = (
        balance.get("source") == "ifind"
        and balance.get("statement_scope") == "consolidated"
        and balance.get("adjustment") == "restated"
        and balance.get("currency") == "CNY" and balance.get("unit") == "currency_base"
        and (balance.get("disclosed_at") is None or balance.get("disclosed_at") <= date.today().isoformat())
    )
    if balance.get("source") == "choice" and balance.get("currency") == "CNY" and balance.get("unit") == "currency_base" and balance.get("period") and balance.get("disclosed_at") and isinstance(assets, (int, float)) and isinstance(liabilities, (int, float)) and assets > 0 and liabilities >= 0:
        leverage = float(round(Decimal(str(liabilities)) / Decimal(str(assets)) * 100, 2))
    elif ifind_balance_current and isinstance(assets, (int, float)) and isinstance(liabilities, (int, float)) and assets > 0 and liabilities >= 0:
        leverage = float(round(Decimal(str(liabilities)) / Decimal(str(assets)) * 100, 2))
    # issue #64：机构持仓变化退役——新报告 financial_safety 不再产出该字段，
    # 渲染不再出现「机构持仓变化：缺失」退役缺口（历史保存载荷兼容不变）。
    financial_safety = {
        "leverage": leverage,
        "period": balance.get("period"),
        "source": balance.get("source"),
        # issue #71：沿用既有 note 键（渲染层已映射为「说明」），不新增内部键
        # 泄漏到报告；缺失披露/非历史时点的解释文本原样保留。
        **({"note": "披露日期未提供：仅用于当前分析，非历史时点可得"}
           if leverage is not None and balance.get("source") == "ifind" and not balance.get("disclosed_at") else {}),
    }
    if leverage is None:
        gaps.append("资产负债结构")

    valuation = score_valuation(
        usable["pe_ratio"],
        usable["pb_ratio"],
    )
    valuation_detail = {
        "pe_ttm": obtained["pe_ratio"],
        "pb": obtained["pb_ratio"],
        "market_cap": fund.get("market_cap"),
        "assessment": valuation["summary"] if valuation else "估值输入不可用于规则" if obtained["pe_ratio"] is not None or obtained["pb_ratio"] is not None else "估值数据缺失",
        "score": valuation["score"] if valuation else None,
    }
    if valuation is None:
        gaps.append("估值")
    # issue #73：字段级真实缺口——PE 有效时估值组件有分（score_valuation 走 PE
    # 口径），但六字段完整性仍要求 PB 合格；缺哪个必填字段如实列名，不再出现
    # 「财务未完整而缺口 0 项」假象。完整门槛不变（仍要求六字段 rule_eligible）。
    if fields["pe_ratio"].reading is None or not fields["pe_ratio"].rule_eligible:
        gaps.append("市盈率PE(TTM)")
    if fields["pb_ratio"].reading is None or not fields["pb_ratio"].rule_eligible:
        gaps.append("市净率PB")
    gaps[:] = list(dict.fromkeys(gaps))

    scores = [s for s in (roe_score, gm_score, rev_score, np_score, valuation["score"] if valuation else None) if s is not None]
    health = float(round(sum(Decimal(str(score)) for score in scores) / Decimal(len(scores)), 2)) if scores else None
    n = sum(evidence.reading is not None for evidence in fields.values())
    m = sum(evidence.rule_eligible for evidence in fields.values())
    complete = m == 6 and len(scores) == 5
    warnings = list(dict.fromkeys(fund.get("errors") or [])) + ["各字段仅按自身证据使用，组件完整不代表同期财报。", "规则评分置信度与跨源核验状态分别解释。"]
    if not all(evidence.quality.status == "verified" for evidence in fields.values()):
        warnings.append("财务字段尚未全部独立核验；未核验、单源、不可比或冲突限制详见字段证据。")
    p1_used = [field for field, evidence in fields.items()
               if evidence.selection_reason == "ifind_published_single_source"]
    if p1_used:
        warnings.append("ROE/毛利率/营收同比/归母净利同比按 iFinD 公布值单源采用（调整口径未知、未独立核验，不与其他来源混算）。")
    if not complete:
        warnings.append("当前仅可进行获准指标的局部评估，不能作为完整财务健康结论。")
    quality = FinancialDataQuality(state="none" if n == 0 else "complete" if n == 6 else "partial", obtained_count=n, rule_usable_count=m, health_components=len(scores), fields=fields, warnings=tuple(warnings))

    return FundamentalDim(
        status="ok" if complete else "degraded",
        degraded_reason=None if complete else "财务指标缺失或存在不可用于规则的输入",
        data_quality=quality,
        # issue #73：已采纳事实投影不再依赖 cross_validation 块存在——iFinD 主源
        # 深研零 Choice 出站后不再产生核验块，六项财务事实及其来源/期间/资格记录
        # 照常进入保存/API/报告（单源资格如实保留，不合成 verified 记录）。
        financial_snapshot={field: financial_field_quality(fund, field) for field in ("revenue", "net_profit", "roe", "gross_margin", "revenue_yoy", "net_profit_yoy")},
        profitability=profitability,
        growth_quality=growth_quality,
        financial_safety=financial_safety,
        valuation_detail=valuation_detail,
        data_gaps=gaps,
        health_score=health,
        narrative=(
            f"{'财务组件完整评估' if complete else '局部指标评估'}：评分 {health if health is not None else '无可用评分'}（参与 {len(scores)}/5 组件）；"
            f"缺口 {len(gaps)} 项：{'、'.join(gaps)}。"
        ),
    )
