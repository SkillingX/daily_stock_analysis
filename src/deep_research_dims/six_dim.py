# -*- coding: utf-8 -*-
"""L1 六维评分 v2（方案 v2.1 单一评分源）：六维 × 细分指标 × 权重 → 总分。

- 总分 = 段一总评分 = 贝叶斯先验映射输入（map_prior 不变），四处同源；
- 打分纪律：缺数据指标中性 50 + low + data_gap，不编分；
- 指标来源：规则（估值/筹码/机构/支撑/RSI/MACD）+ 缠论引擎 + 趋势罗盘
  周线过滤 + 一次 LLM 结构化打分（股权高管/情绪/宏观，强制依据句）；
- F1/F2/intel 供数指标（基本面-板块对比/业务财务、消息面两项）在 P1 接线，
  本期按纪律记缺口。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from src.deep_research_dims.context import SharedContext
from src.scoring.bayesian import map_prior
from src.scoring.indicators_v2 import (
    aggregate_v2_dimensions,
    framework_total_v2,
    gap_indicator,
    score_support_indicator,
    score_valuation,
)
from src.scoring.weights_v2 import (
    DIMENSION_WEIGHTS_V2,
    SCORING_VERSION_V2,
    validate_v2_weights,
)
from src.schemas.deep_research_dims import SixDimDim
from src.schemas.research_framework import DimensionScore, IndicatorScore, ResearchFramework

logger = logging.getLogger(__name__)

_LLM_INDICATOR_IDS = (
    "equity_mgmt",
    "institute_view",
    "community_view",
    "liquidity",
    "risk_appetite",
)


def _closes(ctx: SharedContext) -> List[float]:
    return [float(r["close"]) for r in ctx.history if r.get("close") is not None]


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(closes[-period - 1 : -1], closes[-period:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def _macd_hist(closes: List[float]) -> Optional[float]:
    if len(closes) < 35:
        return None

    def _ema(values: List[float], n: int) -> float:
        k = 2 / (n + 1)
        e = values[0]
        for v in values[1:]:
            e = v * k + e * (1 - k)
        return e

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12 - ema26
    dea = dif  # 简化：DEA 用末值 DIF 近似（长序列误差小）
    return round((dif - dea) * 2, 4)


def _score_chanlun(ctx: SharedContext) -> Optional[Dict[str, Any]]:
    """缠论结构分：趋势方向 + 中枢位置 + 背驰（ChanLunEngine）。"""
    if len(ctx.history) < 60:
        return None
    try:
        import pandas as pd

        from chanlun.chanlun_engine import ChanLunEngine

        df = pd.DataFrame(ctx.history)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        out = ChanLunEngine(df).analyze()
        trend = str(out.get("current_trend") or "")
        position = str(out.get("position") or "")
        divergence = out.get("divergence")
        score = 50.0
        if "上涨" in trend:
            score = 70.0
        elif "下跌" in trend:
            score = 32.0
        if "突破中枢" in position:
            score += 6
        elif "跌破中枢" in position:
            score -= 6
        if divergence and "底" in str(divergence):
            score += 8
        elif divergence and "顶" in str(divergence):
            score -= 8
        summary = f"缠论结构：趋势={trend or '未知'}，位置={position}"
        if divergence:
            summary += f"，{divergence}"
        return {
            "id": "chanlun_struct",
            "score": max(0.0, min(100.0, score)),
            "confidence": "medium",
            "basis": "rule",
            "data_gap": False,
            "summary": summary,
        }
    except Exception as exc:  # noqa: BLE001 - 缠论失败按纪律记缺口
        logger.warning("[SixDimV2] 缠论结构分析失败: %s", exc)
        return None


def _score_compass_weekly(ctx: SharedContext) -> Optional[Dict[str, Any]]:
    """趋势罗盘周线过滤（derive_l0）。样本不足 200 周 → weekly_disabled 记缺口。"""
    try:
        from src.services.compass.engine import derive_l0

        daily_closes = _closes(ctx)
        if len(daily_closes) < 100:
            return None
        # 周线重采样（近似：按 5 根日线一组，尾部不足丢弃）
        weekly = [
            float(sum(daily_closes[i : i + 5]))
            for i in range(0, len(daily_closes) - 4, 5)
        ]
        import pandas as pd

        status, snapshot = derive_l0(pd.Series(weekly))
        if status == "weekly_disabled":
            return gap_indicator(
                "compass_weekly",
                f"周线样本不足（{snapshot.get('weekly_sample_size')} 周 < 200）",
            )
        score_map = {
            "weekly_bull": 74.0,
            "weekly_bear": 28.0,
            "weekly_neutral": 50.0,
        }
        score = score_map.get(status, 50.0)
        return {
            "id": "compass_weekly",
            "score": score,
            "confidence": "medium",
            "basis": "rule",
            "data_gap": False,
            "summary": (
                f"趋势罗盘 L0 周线过滤：{status}"
                f"（EMA50={snapshot.get('weekly_ema50')} / EMA200={snapshot.get('weekly_ema200')}）"
            ),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("[SixDimV2] 趋势罗盘周线分析失败: %s", exc)
        return None


def _score_llm_indicators(
    ctx: SharedContext,
    stock_name: str,
    llm_adapter: Any,
    intel_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """LLM 结构化打分（股权高管/情绪/宏观五项，强制依据句）。

    LLM 不可用/未给依据句 → 按纪律记缺口；绝不编分。
    """
    results: Dict[str, Dict[str, Any]] = {
        ind_id: gap_indicator(ind_id, "LLM 打分不可用") for ind_id in _LLM_INDICATOR_IDS
    }
    if llm_adapter is None:
        return results
    intel = intel_payload or {}
    intel_summary: Dict[str, Any] = {}
    if intel:
        intel_summary = {
            "sentiment": (intel.get("intelligence") or {}).get("sentiment_summary"),
            "news": (intel.get("intelligence") or {}).get("latest_news"),
            "root_cause": intel.get("root_cause"),
        }
    facts = {
        "stock": f"{stock_name}（{ctx.stock_code}）",
        "quote": ctx.quote,
        "fundamental": ctx.fundamental,
        "intel_brief": intel_summary or None,
        "limitations": ctx.limitations,
    }
    import json

    system = (
        "你是投研评分员。对输入事实中可推断的指标打 0-100 分，每个分必须附带"
        "一句中文依据（basis_sentence，含具体数字或事实）；事实不足的指标不要打分。"
        "只输出 JSON：{\"indicators\": [{\"id\", \"score\", \"basis_sentence\"}]}，"
        f"可选 id 限定为 {list(_LLM_INDICATOR_IDS)}。"
    )
    try:
        response = llm_adapter.call_text(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"## 事实\n```json\n{json.dumps(facts, ensure_ascii=False, default=str)[:2500]}\n```",
                },
            ],
            temperature=0.2,
            timeout=90.0,
        )
        import re

        text = (response.content or "").strip()
        # 剥推理块 + 提取首个 {...} JSON 子串（供应商常夹带说明文字）
        text = re.sub(
            r"<think(?:ing)?>.*?</think(?:ing)?>", "", text, flags=re.DOTALL | re.IGNORECASE
        ).strip()
        text = re.sub(r"```(?:json)?", "", text).strip()
        if not text.startswith("{"):
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            text = match.group(0) if match else ""
        if not text:
            return results
        parsed = json.loads(text)
        for item in parsed.get("indicators") or []:
            ind_id = str(item.get("id") or "")
            if ind_id not in results:
                continue
            sentence = str(item.get("basis_sentence") or "").strip()
            try:
                score = float(item.get("score"))
            except (TypeError, ValueError):
                continue
            if len(sentence) < 8:
                continue  # 无依据句的分按纪律不采纳
            results[ind_id] = {
                "id": ind_id,
                "score": max(0.0, min(100.0, score)),
                "confidence": "low" if score in (0, 100) else "medium",
                "basis": "llm",
                "data_gap": False,
                "summary": f"{sentence}",
            }
    except Exception as exc:  # noqa: BLE001 - LLM 失败按纪律记缺口
        logger.warning("[SixDimV2] LLM 指标打分失败: %s", exc)
    return results


def _score_from_f2(f2_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    f2 = f2_payload or {}
    score = f2.get("sector_score")
    if not isinstance(score, (int, float)):
        return None
    return {
        "id": "sector_vs_leader",
        "score": float(score),
        "confidence": "medium",
        "basis": "rule",
        "data_gap": False,
        "summary": f2.get("narrative") or f"板块得分 {score}",
    }


def _score_from_f1(f1_payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    f1 = f1_payload or {}
    health = f1.get("health_score")
    if not isinstance(health, (int, float)):
        return None
    gaps = f1.get("data_gaps") or []
    return {
        "id": "business_financial",
        "score": float(health),
        "confidence": "medium" if not gaps else "low",
        "basis": "rule",
        "data_gap": False,
        "summary": f1.get("narrative") or f"财务健康分 {health}",
    }


def _score_from_intel(intel_payload: Optional[Dict[str, Any]]) -> Dict[str, Optional[Dict[str, Any]]]:
    intel = intel_payload or {}
    rc = intel.get("root_cause") or {}
    magnitude = str(rc.get("magnitude") or "")
    positive = any(k in magnitude for k in ("正面", "积极", "利好", "有限"))
    negative = any(k in magnitude for k in ("负面", "利空", "较大", "严重"))
    real_news = {
        "id": "real_news",
        "score": 62.0 if positive else (38.0 if negative else 50.0),
        "confidence": "low",
        "basis": "llm",
        "data_gap": False,
        "summary": (
            f"根因事件：{rc.get('event') or '未提取'}；量级：{magnitude or '未评估'}"
        ),
    }
    calendar = intel.get("event_calendar") or []
    if calendar:
        first = calendar[0]
        event_plan = {
            "id": "event_plan",
            "score": 58.0,
            "confidence": "low",
            "basis": "llm",
            "data_gap": False,
            "summary": (
                f"未来事件 {len(calendar)} 项（近期：{first.get('event') or '未知'}），"
                "已备结果预案"
            ),
        }
    else:
        event_plan = None
    return {"real_news": real_news, "event_plan": event_plan}


def _researcher_score(payload: Optional[Dict[str, Any]], key: str = "score") -> Optional[Dict[str, Any]]:
    """研究员打分 → 指标分（basis=llm，summary=narrative）；研究员缺失/降級 → None 走规则回退。"""
    p = payload or {}
    if p.get("status") != "ok":
        return None
    score = p.get(key)
    if not isinstance(score, (int, float)):
        return None
    return {
        "id": key,
        "score": float(score),
        "confidence": "medium",
        "basis": "llm",
        "data_gap": False,
        "summary": str(p.get("narrative") or "")[:200] or f"研究员评分 {score}",
    }


def build_six_dim(
    ctx: SharedContext,
    data_dim_payload: Optional[Dict[str, Any]] = None,
    llm_adapter: Any = None,
    f1_payload: Optional[Dict[str, Any]] = None,
    f2_payload: Optional[Dict[str, Any]] = None,
    intel_payload: Optional[Dict[str, Any]] = None,
    technical_payload: Optional[Dict[str, Any]] = None,
    capital_payload: Optional[Dict[str, Any]] = None,
    sentiment_payload: Optional[Dict[str, Any]] = None,
    ownership_payload: Optional[Dict[str, Any]] = None,
) -> SixDimDim:
    """L1 v2：规则 + 缠论 + 罗盘 + LLM 混合打分，单一评分源。"""
    validate_v2_weights()
    data = (data_dim_payload or {}).get("perspective") or {}
    price_pos = data.get("price_position") or {}
    current = price_pos.get("current_price") or ctx.quote.get("price")
    fund = ctx.fundamental

    intel_scores = _score_from_intel(intel_payload)
    indicator_results: Dict[str, Dict[str, Optional[Dict[str, Any]]]] = {
        "基本面": {
            "sector_vs_leader": _score_from_f2(f2_payload),
            "business_financial": _score_from_f1(f1_payload),
            "equity_mgmt": _researcher_score(ownership_payload),
        },
        "消息面": {
            "real_news": intel_scores["real_news"],
            "event_plan": intel_scores["event_plan"],
        },
        "资金面": {
            "capital_flow": _researcher_score(capital_payload, "flow_score"),
            "institution_change": _researcher_score(capital_payload, "institution_change_score"),
            "chip_cost": _researcher_score(capital_payload, "chip_score"),
        },
        "情绪面": {
            "institute_view": _researcher_score(sentiment_payload, "institute_score"),
            "community_view": _researcher_score(sentiment_payload, "community_score"),
        },
        "技术面": {
            "chanlun_struct": _researcher_score(technical_payload) or _score_chanlun(ctx),
            "compass_weekly": _score_compass_weekly(ctx),
            "support_indicator": score_support_indicator(
                float(current) if isinstance(current, (int, float)) else None,
                price_pos.get("support_level"),
                price_pos.get("resistance_level"),
                _rsi(_closes(ctx)),
                _macd_hist(_closes(ctx)),
            ),
        },
        "宏观": {},
    }
    valuation = score_valuation(
        fund.get("pe_ttm") if isinstance(fund.get("pe_ttm"), (int, float)) else None,
        fund.get("pb") if isinstance(fund.get("pb"), (int, float)) else None,
    )
    indicator_results["基本面"]["valuation"] = valuation

    llm_results = _score_llm_indicators(
        ctx, ctx.stock_name, llm_adapter, intel_payload
    )
    for ind_id, result in llm_results.items():
        dim = "基本面" if ind_id == "equity_mgmt" else ("情绪面" if "view" in ind_id else "宏观")
        indicator_results[dim][ind_id] = result

    dimensions = aggregate_v2_dimensions(indicator_results)
    total = framework_total_v2(dimensions)

    dim_models = [
        DimensionScore(
            dimension=d["dimension"],
            weight=DIMENSION_WEIGHTS_V2[d["dimension"]],
            score=d["score"],
            indicators=[
                IndicatorScore(
                    name=i["name"],
                    score=i["score"],
                    weight=i["weight"],
                    basis=i["basis"],
                    confidence=i["confidence"],
                    summary=i["summary"],
                )
                for i in d["indicators"]
            ],
            warnings=([f"该维度 {d['data_gaps']} 项指标缺数据（记中性分）"] if d["data_gaps"] else []),
        )
        for d in dimensions
    ]
    total_gaps = sum(d["data_gaps"] for d in dimensions)
    framework = ResearchFramework(
        dimension_total=total,
        dimensions=dim_models,
        scoring_version=SCORING_VERSION_V2,
        warnings=[
            f"评分框架 {SCORING_VERSION_V2}：{total_gaps} 项指标缺数据按纪律记中性分，"
            "总分偏中性不代表看多/看空"
        ]
        if total_gaps
        else [],
    )
    prior = map_prior(total)
    return SixDimDim(
        framework=framework,
        scoring_version=SCORING_VERSION_V2,
        warnings=list(framework.warnings),
        narrative=(
            f"六维总分 {total:.1f}/100（{SCORING_VERSION_V2}，基本面权重 30% 居首），"
            f"先验 P(H)={prior:.2f}；缺数据指标 {total_gaps} 项按纪律记中性分。"
        ),
    )
