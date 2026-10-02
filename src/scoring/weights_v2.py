# -*- coding: utf-8 -*-
"""评分框架 v2：六维 × 细分指标 × 权重（方案 v2.1 定稿，单一评分源）。

权重（用户定稿 2026-10-02）：基本面 30 / 情绪 15 / 资金 15 / 消息 15 / 技术 15 / 宏观 10。
每维度内指标权重和 = 1.0；全维度权重和 = 1.0（icontract 校验）。

打分纪律（决策 #2）：缺数据指标强制中性 50 + confidence=low + data_gap=True，
按权重归一，绝不编分；inferred 分必须带依据句。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from icontract import require

SCORING_VERSION_V2 = "v2.0"

# 维度 → 权重
DIMENSION_WEIGHTS_V2: Dict[str, float] = {
    "基本面": 0.30,
    "情绪面": 0.15,
    "资金面": 0.15,
    "消息面": 0.15,
    "技术面": 0.15,
    "宏观": 0.10,
}

# 维度 → [(指标 id, 指标名, 维度内权重, 打分来源)]
# 打分来源：rule=机器规则 / llm=LLM 依据句打分 / f1/f2/intel=对应维度供数（P1 接线）
INDICATORS_V2: Dict[str, List[Tuple[str, str, float, str]]] = {
    "基本面": [
        ("sector_vs_leader", "大盘/板块/龙头对比", 0.25, "f2"),
        ("equity_mgmt", "股权架构与高管", 0.20, "llm"),
        ("business_financial", "业务与财务健康", 0.35, "f1"),
        ("valuation", "估值", 0.20, "rule"),
    ],
    "消息面": [
        ("real_news", "真消息与市场关注点", 0.50, "intel"),
        ("event_plan", "未来事件与预案", 0.50, "intel"),
    ],
    "资金面": [
        ("capital_flow", "资金流向", 0.34, "rule"),
        ("institution_change", "机构/大户持仓变动", 0.33, "rule"),
        ("chip_cost", "筹码成本结构", 0.33, "rule"),
    ],
    "情绪面": [
        ("institute_view", "机构评价", 0.50, "llm"),
        ("community_view", "社区评价", 0.50, "llm"),
    ],
    "技术面": [
        ("chanlun_struct", "缠论结构", 0.40, "chanlun"),
        ("compass_weekly", "趋势罗盘周线过滤", 0.30, "compass"),
        ("support_indicator", "支撑压力与 MACD/RSI", 0.30, "rule"),
    ],
    "宏观": [
        ("liquidity", "宏观流动性", 0.50, "llm"),
        ("risk_appetite", "风险偏好", 0.50, "llm"),
    ],
}


@require(
    lambda: abs(sum(DIMENSION_WEIGHTS_V2.values()) - 1.0) < 1e-6,
    "维度权重和必须 = 1.0",
)
def validate_v2_weights() -> None:
    """校验 v2 权重表（维度和 + 每维度内指标和）。非法即抛，启动期暴露。"""
    for dim, indicators in INDICATORS_V2.items():
        total = sum(w for _, _, w, _ in indicators)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"维度「{dim}」指标权重和 = {total}，必须 = 1.0")
        if dim not in DIMENSION_WEIGHTS_V2:
            raise ValueError(f"指标表维度「{dim}」缺少维度权重")
    for dim in DIMENSION_WEIGHTS_V2:
        if dim not in INDICATORS_V2:
            raise ValueError(f"维度「{dim}」缺少指标定义")
