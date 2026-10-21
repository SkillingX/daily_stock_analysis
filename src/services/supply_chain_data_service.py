# -*- coding: utf-8 -*-
"""
供应链数据获取服务 - B链路(Serenity) + C链路(LLM推断) + 知识库

链路 B: Serenity 供应链深度分析 (瓶颈评分)
链路 C: 增强 LLM 推断 (基于股票知识和行业信息)
备用: 知识库 (常见股票的供应链信息)
"""

import logging
import os
import re
from datetime import datetime, timezone
from typing import cast, Any, Dict, List, Optional, Tuple

from src.llm.concurrency import limited_completion
from src.config import get_config
from src.llm.generation_params import apply_minimax_reasoning_params

logger = logging.getLogger(__name__)


# ============================================================
# 行业推断模板（_infer_from_industry 用）
# ============================================================
# (关键词元组, 模板 dict) 顺序表，按顺序匹配首个命中即返回；fallback 见下方。
# 模板字段：company_position / upstream / downstream / chokepoints /
# us_china_chain / industry_drivers。

_INDUSTRY_INFERENCE_TEMPLATES: Tuple[Tuple[Tuple[str, ...], Dict[str, Any]], ...] = (
    (
        ("半导体", "芯片", "集成电路", "晶圆"),
        {
            "company_position": "半导体/集成电路设计制造",
            "upstream": ["硅片", "光刻胶", "EDA软件", "半导体设备"],
            "downstream": ["消费电子", "汽车电子", "通信设备", "工业控制"],
            "chokepoints": [
                {
                    "type": "技术",
                    "description": "先进制程技术壁垒",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "受美国出口管制影响",
                "sanction_risk": "高",
                "dual_chain_impact": "显著",
            },
            "industry_drivers": ["国产替代加速", "AI算力需求", "汽车电动化"],
            # §10.b 产能展望字段
            "capacity_unit_hint": "万片/月",
            "benchmark_utilization": 85.0,
            "seasonal_pattern": "Q3>Q4>Q2>Q1",
        },
    ),
    (
        ("白酒", "茅台", "五粮液", "泸州", "汾酒", "洋河"),
        {
            "company_position": "高端白酒生产",
            "upstream": ["糯高粱", "小麦", "水", "包装材料"],
            "downstream": ["高端消费", "商务宴请", "礼品市场"],
            "chokepoints": [
                {"type": "品牌", "description": "品牌护城河", "confidence": "high"}
            ],
            "us_china_chain": {
                "role": "纯国内业务",
                "sanction_risk": "低",
                "dual_chain_impact": "极小",
            },
            "industry_drivers": ["消费升级", "品牌集中度提升", "文化建设"],
            # §10.b 产能展望字段
            "capacity_unit_hint": "万千升/年",
            "benchmark_utilization": 75.0,
            "seasonal_pattern": "Q4>Q2>Q3>Q1",
        },
    ),
    (
        ("新能源", "锂电池", "动力电池", "宁德", "亿纬"),
        {
            "company_position": "新能源电池制造",
            "upstream": ["锂矿", "钴镍", "隔膜", "电解液", "铜箔"],
            "downstream": ["新能源汽车", "储能系统", "消费电子"],
            "chokepoints": [
                {
                    "type": "产能",
                    "description": "规模效应+成本控制",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "全球供应主导",
                "sanction_risk": "中",
                "dual_chain_impact": "美国市场受限",
            },
            "industry_drivers": ["新能源汽车渗透率提升", "储能需求爆发", "原材料成本"],
            # §10.b 产能展望字段
            "capacity_unit_hint": "GWh/年",
            "benchmark_utilization": 80.0,
            "seasonal_pattern": "Q3>Q4>Q2>Q1",
        },
    ),
    (
        ("医药", "制药", "生物", "医疗", "恒瑞", "迈瑞"),
        {
            "company_position": "医药/医疗器械",
            "upstream": ["原料药", "药用辅料", "医疗器械零部件"],
            "downstream": ["医院", "药店", "患者"],
            "chokepoints": [
                {
                    "type": "认证",
                    "description": "药品/器械审批壁垒",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "国内市场为主",
                "sanction_risk": "低",
                "dual_chain_impact": "较小",
            },
            "industry_drivers": ["老龄化", "创新药政策", "国产替代"],
            # §10.b 产能展望字段
            "capacity_unit_hint": "万盒/年",
            "benchmark_utilization": 70.0,
            "seasonal_pattern": "Q2>Q4>Q1>Q3",
        },
    ),
    (
        ("光伏", "太阳能", "隆基", "通威"),
        {
            "company_position": "光伏产业链",
            "upstream": ["硅料", "硅片", "银浆", "光伏玻璃"],
            "downstream": ["光伏电站", "分布式光伏", "EPC厂商"],
            "chokepoints": [
                {
                    "type": "产能",
                    "description": "产能周期与技术迭代",
                    "confidence": "medium",
                }
            ],
            "us_china_chain": {
                "role": "全球光伏主导",
                "sanction_risk": "高",
                "dual_chain_impact": "美国限制中国光伏产品",
            },
            "industry_drivers": ["碳中和政策", "光伏经济性提升", "全球能源转型"],
            # §10.b 产能展望字段
            "capacity_unit_hint": "GW/年",
            "benchmark_utilization": 70.0,
            "seasonal_pattern": "Q3>Q4>Q1>Q2",
        },
    ),
    (
        ("面板", "显示", "京东方", "TCL", "OLED"),
        {
            "company_position": "显示面板制造",
            "upstream": ["玻璃基板", "偏光片", "驱动芯片", "发光材料"],
            "downstream": ["手机", "电视", "车载显示", "IT产品"],
            "chokepoints": [
                {
                    "type": "产能",
                    "description": "高世代线投资门槛",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "全球产能主导",
                "sanction_risk": "中",
                "dual_chain_impact": "技术追赶中",
            },
            "industry_drivers": ["大尺寸化", "OLED渗透", "车载需求"],
            # §10.b 产能展望字段
            "capacity_unit_hint": "万平米/月",
            "benchmark_utilization": 75.0,
            "seasonal_pattern": "Q3>Q4>Q2>Q1",
        },
    ),
    (
        ("互联网", "腾讯", "阿里", "百度", "字节", "京东"),
        {
            "company_position": "互联网平台",
            "upstream": ["服务器", "带宽", "云计算基础设施"],
            "downstream": ["个人用户", "企业客户", "广告主", "商家"],
            "chokepoints": [
                {
                    "type": "网络",
                    "description": "用户生态与数据壁垒",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "纯国内业务为主",
                "sanction_risk": "中",
                "dual_chain_impact": "监管政策影响大",
            },
            "industry_drivers": ["数字化转型", "电商渗透", "内容消费升级"],
        },
    ),
    (
        ("银行", "招商", "宁波", "平安银行"),
        {
            "company_position": "商业银行",
            "upstream": ["存款客户", "金融市场"],
            "downstream": ["企业贷款", "个人贷款", "中间业务"],
            "chokepoints": [
                {"type": "牌照", "description": "银行牌照壁垒", "confidence": "high"}
            ],
            "us_china_chain": {
                "role": "纯国内业务",
                "sanction_risk": "低",
                "dual_chain_impact": "极小",
            },
            "industry_drivers": ["息差变化", "资产质量", "中间业务发展"],
        },
    ),
    (
        ("证券", "券商", "中信", "华泰", "国泰", "海通", "广发"),
        {
            "company_position": "证券经纪/投行",
            "upstream": ["机构客户", "上市公司"],
            "downstream": ["个人投资者", "机构投资者", "企业客户"],
            "chokepoints": [
                {
                    "type": "牌照",
                    "description": "证券业务牌照壁垒",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "纯国内业务",
                "sanction_risk": "低",
                "dual_chain_impact": "极小",
            },
            "industry_drivers": ["资本市场活跃度", "注册制改革", "财富管理转型"],
        },
    ),
    (
        ("保险", "平安保险", "中国人寿", "太平洋保险"),
        {
            "company_position": "保险",
            "upstream": ["投保人", "资本市场"],
            "downstream": ["个人客户", "企业客户"],
            "chokepoints": [
                {"type": "牌照", "description": "保险牌照壁垒", "confidence": "high"}
            ],
            "us_china_chain": {
                "role": "纯国内业务",
                "sanction_risk": "低",
                "dual_chain_impact": "极小",
            },
            "industry_drivers": ["人口老龄化", "保障意识提升", "政策支持"],
        },
    ),
    (
        ("房地产", "万科", "保利", "招商蛇口", "金地", "华侨城"),
        {
            "company_position": "房地产开发",
            "upstream": ["地方政府", "建筑公司", "原材料供应商"],
            "downstream": ["购房者", "企业客户"],
            "chokepoints": [
                {
                    "type": "资金",
                    "description": "资金密集+政策周期",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "纯国内业务",
                "sanction_risk": "低",
                "dual_chain_impact": "极小",
            },
            "industry_drivers": ["政策调控", "城镇化", "改善性需求"],
        },
    ),
    (
        ("汽车", "比亚迪", "长城", "吉利", "长安", "上汽", "广汽"),
        {
            "company_position": "汽车制造",
            "upstream": ["零部件供应商", "钢铁", "芯片"],
            "downstream": ["个人消费者", "经销商", "集团采购"],
            "chokepoints": [
                {
                    "type": "技术",
                    "description": "发动机/电动化技术",
                    "confidence": "medium",
                }
            ],
            "us_china_chain": {
                "role": "国内为主",
                "sanction_risk": "低",
                "dual_chain_impact": "较小",
            },
            "industry_drivers": ["新能源转型", "出口增长", "智能驾驶"],
        },
    ),
    (
        ("家电", "格力", "美的", "海尔", "海信", "TCL"),
        {
            "company_position": "家电制造",
            "upstream": ["钢铁", "塑料", "电子元器件"],
            "downstream": ["个人消费者", "经销商", "企业客户"],
            "chokepoints": [
                {
                    "type": "品牌",
                    "description": "家电品牌渠道壁垒",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "全球产能",
                "sanction_risk": "低",
                "dual_chain_impact": "较小",
            },
            "industry_drivers": ["消费升级", "海外拓展", "智能家居"],
        },
    ),
    (
        ("食品", "伊利", "蒙牛", "海天", "农夫山泉"),
        {
            "company_position": "食品饮料制造",
            "upstream": ["农产品", "原材料", "包装材料"],
            "downstream": ["个人消费者", "商超", "餐饮"],
            "chokepoints": [
                {
                    "type": "渠道",
                    "description": "食品饮料渠道壁垒",
                    "confidence": "high",
                }
            ],
            "us_china_chain": {
                "role": "纯国内业务",
                "sanction_risk": "低",
                "dual_chain_impact": "极小",
            },
            "industry_drivers": ["消费升级", "品牌集中度", "渠道下沉"],
        },
    ),
    (
        ("纺织", "服装", "李宁", "安踏", "波司登"),
        {
            "company_position": "纺织服装",
            "upstream": ["棉花", "化纤", "面料"],
            "downstream": ["个人消费者", "经销商", "品牌商"],
            "chokepoints": [
                {"type": "品牌", "description": "服装品牌壁垒", "confidence": "medium"}
            ],
            "us_china_chain": {
                "role": "全球供应链",
                "sanction_risk": "低",
                "dual_chain_impact": "品牌出海",
            },
            "industry_drivers": ["国货崛起", "运动消费", "品牌升级"],
        },
    ),
)

_INDUSTRY_INFERENCE_FALLBACK: Dict[str, Any] = {
    "company_position": "待分析",
    "upstream": ["待分析"],
    "downstream": ["待分析"],
    "chokepoints": [
        {"type": "unknown", "description": "需详细产业链分析", "confidence": "low"}
    ],
    "us_china_chain": {
        "role": "待分析",
        "sanction_risk": "待评估",
        "dual_chain_impact": "待评估",
    },
    "industry_drivers": [],
}




class SupplyChainDataService:
    """
    供应链数据获取服务

    支持两种数据获取链路 + 知识库备用：
    1. LLM 推断 (C) - 从基本面分析文本中提取
    2. Serenity 深度分析 (B) - 瓶颈评分卡 (可选)
    3. 知识库备用 - 常见股票的供应链信息
    """

    def __init__(self):
        self._llm_model = os.environ.get(
            "LITELLM_MODEL", "openai/MiniMax-M3.1-Flash"
        )

    def fetch_all(
        self,
        stock_code: str,
        stock_name: str,
        fundamental_analysis: str = "",
        market: str = "cn",
        enable_serenity: bool = False,
    ) -> Dict[str, Any]:
        """
        获取完整供应链数据 (B链路 + C链路 + 知识库)

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            fundamental_analysis: 基本面分析文本
            market: 市场 (cn/hk/us)
            enable_serenity: 是否启用 Serenity 深度分析 (耗时较长)

        Returns:
            Dict with supply chain data from all sources
        """
        result: Dict[str, Any] = {
            "data_sources": [],
            "company_position": "",
            "upstream": [],
            "downstream": [],
            "chokepoints": [],
            "us_china_chain": {},
            "industry_drivers": [],
            "chain_map": [],
            "serenity_score": None,
            "serenity_verdict": None,
        }

        # 知识库数据 (作为基础数据)
        kb_data = self._fetch_from_knowledge_base(stock_code, stock_name)
        if kb_data.get("upstream") or kb_data.get("downstream"):
            result["data_sources"].append("knowledge_base")
            result["company_position"] = kb_data.get("company_position", "")
            result["upstream"] = kb_data.get("upstream", [])
            result["downstream"] = kb_data.get("downstream", [])
            result["chokepoints"] = kb_data.get("chokepoints", [])
            result["us_china_chain"] = kb_data.get("us_china_chain", {})
            result["industry_drivers"] = kb_data.get("industry_drivers", [])
            logger.info(f"[SupplyChainDataService] Knowledge base hit for {stock_code}")

        # C链路: LLM 推断 (补充而非覆盖知识库 - 只有当知识库数据为空时才使用LLM数据)
        llm_data = self._fetch_from_llm(stock_code, stock_name, fundamental_analysis)
        if llm_data:
            result["data_sources"].append("llm")
            self._merge_llm_into_result(result, llm_data)

        # B链路: Serenity 深度分析 (可选，耗时较长)
        if enable_serenity:
            serenity_data = self._fetch_from_serenity(stock_code, stock_name, market)
            if serenity_data:
                result["data_sources"].append("serenity")
                result["serenity_score"] = serenity_data.get("score")
                result["serenity_verdict"] = serenity_data.get("verdict")
                result["serenity_factors"] = serenity_data.get("factors")
                result["serenity_penalties"] = serenity_data.get("penalties")

        upstream_raw: Any = result.get("upstream") or []
        downstream_raw: Any = result.get("downstream") or []
        logger.info(
            f"[SupplyChainDataService] {stock_code} supply chain: sources={result.get('data_sources')}, "
            f"upstream={len(upstream_raw)}, downstream={len(downstream_raw)}"
        )

        return result

    # LLM 数据合并到 result 的字段名（顺序敏感：先合并的字段优先级更高）
    _LLM_MERGE_FIELDS: Tuple[str, ...] = (
        "company_position",
        "upstream",
        "downstream",
        "chokepoints",
        "us_china_chain",
        "industry_drivers",
    )

    @staticmethod
    def _has_valid_data(data: Dict[str, Any], key: str) -> bool:
        """[拆分] 判断 data[key] 是否为「非空 / 非占位符」值。"""
        val = data.get(key)
        if val is None:
            return False
        if isinstance(val, str) and val.strip() in (
            "",
            "null",
            "None",
            "待分析",
            "待评估",
        ):
            return False
        if isinstance(val, list) and len(val) == 0:
            return False
        if isinstance(val, dict):
            return any(
                str(v).strip() not in ("", "null", "None", "待分析", "待评估")
                for v in val.values()
                if v is not None
            )
        return True

    def _merge_llm_into_result(
        self, result: Dict[str, Any], llm_data: Dict[str, Any]
    ) -> None:
        """[拆分] 把 LLM 推断数据合并到 result（仅当 result 字段为空/占位符时补充）。"""
        for field in self._LLM_MERGE_FIELDS:
            if self._has_valid_data(llm_data, field) and not self._has_valid_data(
                result, field
            ):
                result[field] = llm_data.get(field)

    def _fetch_from_llm(
        self,
        stock_code: str,
        stock_name: str,
        fundamental_analysis: str,
    ) -> Dict[str, Any]:
        """C链路: 从基本面分析文本中 LLM 推断供应链信息"""
        if not fundamental_analysis or len(fundamental_analysis) < 30:
            return {}

        # 清除代理设置（保存原值，finally 恢复）
        _saved_env: dict[str, str | None] = {}
        for key in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ]:
            _saved_env[key] = os.environ.pop(key, None)

        prompt = f"""从以下基本面分析文本中提取供应链信息：

股票：{stock_name}（{stock_code}）

文本：
{fundamental_analysis[:4000]}

请提取以下信息（JSON格式）：
{{
    "company_position": "公司在产业链中的位置描述（30字内）",
    "upstream": ["上游关键原材料或组件1", "上游关键原材料或组件2"],
    "downstream": ["下游主要应用领域1", "下游主要应用领域2"],
    "chokepoints": [{{"type": "专利/技术/产能/地理/认证", "description": "瓶颈点描述（20字内）"}}],
    "us_china_chain": {{
        "role": "在中美供应链中扮演的角色",
        "sanction_risk": "制裁风险（低/中/高）",
        "dual_chain_impact": "中美双链影响评估"
    }},
    "industry_drivers": ["产业驱动因素1", "产业驱动因素2"]
}}

只输出JSON，不要其他内容。如果文本中没有相关信息，字段值填null或空数组。"""

        try:
            from litellm import completion

            call_kwargs = apply_minimax_reasoning_params(
                {
                    "model": self._llm_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 800,
                },
                [self._llm_model],
                get_config().minimax_reasoning_effort,
            )
            response = limited_completion(completion, **call_kwargs)

            response_obj: Any = response
            content_raw: Any = response_obj.choices[0].message.content
            content: str = content_raw if content_raw is not None else ""

            try:
                import json_repair

                parsed_result = json_repair.loads(content)
            except Exception:
                json_match = re.search(
                    r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content, re.DOTALL
                )
                if json_match:
                    parsed_result = json_repair.loads(json_match.group(0))  # type: ignore[union-attr]
                else:
                    logger.warning("[SupplyChainDataService] LLM parse failed")
                    return {}

            normalized = self._normalize_llm_output(cast(Dict[str, Any], parsed_result))
            logger.info("[SupplyChainDataService] LLM extraction completed")
            return normalized

        except Exception as e:
            logger.warning(f"[SupplyChainDataService] LLM fetch failed: {e}")
            return {}
        finally:
            # 恢复被清除的代理环境变量
            for key, val in _saved_env.items():
                if val is not None:
                    os.environ[key] = val

    def _normalize_llm_output(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """标准化 LLM 输出"""
        result: Dict[str, Any] = {}

        if raw.get("company_position") or raw.get("chain_position"):
            result["company_position"] = str(
                raw.get("company_position") or raw.get("chain_position", "")
            )

        upstream = raw.get("upstream", [])
        if isinstance(upstream, list):
            result["upstream"] = [str(u) for u in upstream if u]

        downstream = raw.get("downstream", [])
        if isinstance(downstream, list):
            result["downstream"] = [str(d) for d in downstream if d]

        chokepoints = raw.get("chokepoints", [])
        if isinstance(chokepoints, list):
            result["chokepoints"] = []
            for cp in chokepoints:
                if isinstance(cp, dict):
                    result["chokepoints"].append(
                        {
                            "type": cp.get("type", "unknown"),
                            "description": str(cp.get("description", "")),
                            "confidence": "medium",
                        }
                    )

        us_china = raw.get("us_china_chain", {})
        if isinstance(us_china, dict):
            result["us_china_chain"] = {
                "role": str(us_china.get("role", "待分析")),
                "sanction_risk": str(us_china.get("sanction_risk", "待评估")),
                "dual_chain_impact": str(us_china.get("dual_chain_impact", "待评估")),
            }

        drivers = raw.get("industry_drivers", [])
        if isinstance(drivers, list):
            result["industry_drivers"] = [str(d) for d in drivers if d]

        return result

    def _fetch_from_serenity(
        self,
        stock_code: str,
        stock_name: str,
        market: str = "cn",
    ) -> Optional[Dict[str, Any]]:
        """B链路: Serenity 供应链深度分析"""
        try:
            from src.services.supply_chain import scorecard

            factors, penalties = self._infer_serenity_factors(stock_name, market)

            data = {
                "ticker": stock_code,
                "company": stock_name,
                "market": market,
                "factors": factors,
                "penalties": penalties,
                "evidence": [],
                "what_could_weaken_view": [],
            }

            result, verdict = scorecard.score(data)

            return {
                "score": result.get("final_score"),
                "verdict": scorecard.verdict_zh(verdict),
                "factors": result.get("factor_details"),
                "penalties": result.get("penalty_details"),
                "raw_points": result.get("raw_factor_points"),
                "penalty_points": result.get("penalty_points"),
            }

        except Exception as e:
            logger.warning(f"[SupplyChainDataService] Serenity analysis failed: {e}")
            return None

    def _infer_serenity_factors(
        self,
        stock_name: str,
        market: str = "cn",
    ) -> tuple[Dict[str, float], Dict[str, float]]:
        """基于股票名称和行业知识推断 Serenity 因子评分"""
        name_lower = stock_name.lower()

        factors = {
            k: 2.5
            for k in [
                "demand_inflection",
                "architecture_coupling",
                "chokepoint_severity",
                "supplier_concentration",
                "expansion_difficulty",
                "evidence_quality",
                "valuation_disconnect",
                "catalyst_timing",
            ]
        }

        penalties = {
            k: 0.0
            for k in [
                "dilution_financing",
                "governance",
                "geopolitics",
                "liquidity",
                "hype_risk",
                "accounting_quality",
                "cyclicality",
                "alternative_design_risk",
            ]
        }

        # 行业知识库推断
        if any(
            kw in name_lower for kw in ["半导体", "芯片", "集成电路", "晶圆", "封测"]
        ):
            factors["chokepoint_severity"] = 4.0
            factors["supplier_concentration"] = 4.0
            factors["expansion_difficulty"] = 4.5
            penalties["geopolitics"] = 3.0
        elif any(
            kw in name_lower for kw in ["汽车", "新能源", "电动车", "锂电", "电池"]
        ):
            factors["demand_inflection"] = 3.5
            factors["expansion_difficulty"] = 3.5
            penalties["cyclicality"] = 2.0
        elif any(kw in name_lower for kw in ["白酒", "茅台", "五粮液", "泸州", "汾酒"]):
            factors["evidence_quality"] = 3.5
            penalties["hype_risk"] = 1.0
        elif any(kw in name_lower for kw in ["医药", "制药", "生物", "医疗"]):
            factors["chokepoint_severity"] = 3.5
            factors["evidence_quality"] = 3.0
            penalties["accounting_quality"] = 1.5
        elif any(
            kw in name_lower for kw in ["互联网", "软件", "云", "数据", "阿里", "腾讯"]
        ):
            factors["architecture_coupling"] = 3.0
            penalties["geopolitics"] = 2.5
        elif any(kw in name_lower for kw in ["银行", "保险", "证券", "金融"]):
            factors["evidence_quality"] = 3.5
            penalties["cyclicality"] = 2.5

        return factors, penalties

    def _fetch_from_knowledge_base(
        self,
        stock_code: str,
        stock_name: str,
    ) -> Dict[str, Any]:
        """从知识库获取供应链信息"""
        kb = self._get_stock_knowledge_base()
        code_key = stock_code.upper().replace(".SH", "").replace(".SZ", "")
        name_lower = stock_name.lower()

        # 匹配知识库
        for kb_code, kb_data in kb.items():
            if code_key == kb_code or kb_code in code_key or code_key in kb_code:
                return kb_data

        # 基于名称的行业推断
        return self._infer_from_industry(name_lower)

    def _infer_from_industry(self, name_lower: str) -> Dict[str, Any]:
        """基于行业推断供应链信息。

        v3 重构：原实现是 16 个 elif 分支（复杂度 16）；改为按模块级
        ``_INDUSTRY_INFERENCE_TEMPLATES`` 顺序匹配首个命中即返回，行为等价。
        """
        for keywords, template in _INDUSTRY_INFERENCE_TEMPLATES:
            if any(kw in name_lower for kw in keywords):
                return dict(template)
        return dict(_INDUSTRY_INFERENCE_FALLBACK)

    def _get_stock_knowledge_base(self) -> Dict[str, Dict[str, Any]]:
        """获取股票供应链知识库"""
        return {
            "600519": {
                "company_position": "高端白酒生产",
                "upstream": ["糯高粱", "小麦", "赤水河水", "包装材料"],
                "downstream": ["高端消费者", "商务宴请", "礼品市场"],
                "chokepoints": [
                    {
                        "type": "品牌",
                        "description": "飞天茅台品牌护城河",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "纯国内业务",
                    "sanction_risk": "低",
                    "dual_chain_impact": "极小",
                },
                "industry_drivers": ["消费升级", "品牌稀缺性", "酱香型产能约束"],
            },
            "300750": {
                "company_position": "动力电池制造",
                "upstream": ["锂矿", "钴镍", "隔膜", "电解液", "铜箔"],
                "downstream": ["新能源汽车", "储能系统", "消费电子"],
                "chokepoints": [
                    {
                        "type": "产能",
                        "description": "规模效应+成本控制",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "全球动力电池主导",
                    "sanction_risk": "中",
                    "dual_chain_impact": "美国市场受限",
                },
                "industry_drivers": ["新能源汽车渗透率", "储能需求", "原材料价格"],
            },
            "002594": {
                "company_position": "新能源汽车制造",
                "upstream": ["动力电池", "汽车芯片", "零部件"],
                "downstream": ["个人消费者", "出租车", "网约车"],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "电动车核心技术+垂直整合",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "国内新能源主导",
                    "sanction_risk": "中",
                    "dual_chain_impact": "出口市场受限",
                },
                "industry_drivers": ["新能源渗透率", "海外拓展", "智能驾驶"],
            },
            "688981": {
                "company_position": "晶圆代工",
                "upstream": ["硅片", "光刻机", "光刻胶", "EDA软件"],
                "downstream": ["IC设计厂商", "消费电子", "汽车电子"],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "先进制程设备受限",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "受美国出口管制",
                    "sanction_risk": "高",
                    "dual_chain_impact": "显著",
                },
                "industry_drivers": ["国产替代", "AI芯片需求", "成熟制程扩张"],
            },
            "601012": {
                "company_position": "光伏硅片/组件",
                "upstream": ["硅料", "银浆", "光伏玻璃"],
                "downstream": ["光伏电站", "分布式光伏", "EPC厂商"],
                "chokepoints": [
                    {
                        "type": "产能",
                        "description": "成本控制+规模效应",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "全球光伏主导",
                    "sanction_risk": "高",
                    "dual_chain_impact": "美国限制",
                },
                "industry_drivers": ["碳中和", "光伏平价", "全球能源转型"],
            },
            "000858": {
                "company_position": "高端白酒生产",
                "upstream": ["糯高粱", "小麦", "包装材料"],
                "downstream": ["高端消费者", "商务消费", "礼品市场"],
                "chokepoints": [
                    {
                        "type": "品牌",
                        "description": "五粮液品牌壁垒",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "纯国内业务",
                    "sanction_risk": "低",
                    "dual_chain_impact": "极小",
                },
                "industry_drivers": ["消费升级", "品牌集中度", "高端化"],
            },
            "600276": {
                "company_position": "创新药研发",
                "upstream": ["原料药", "临床CRO", "实验动物"],
                "downstream": ["医院", "药店", "患者"],
                "chokepoints": [
                    {
                        "type": "研发",
                        "description": "创新药研发周期长+成功率低",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "国内为主",
                    "sanction_risk": "低",
                    "dual_chain_impact": "较小",
                },
                "industry_drivers": ["老龄化", "创新药政策", "国际化"],
            },
            "300760": {
                "company_position": "医疗器械制造",
                "upstream": ["电子元器件", "传感器", "原材料"],
                "downstream": ["医院", "诊所", "经销商"],
                "chokepoints": [
                    {
                        "type": "认证",
                        "description": "医疗器械注册认证壁垒",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "国内医疗器械龙头",
                    "sanction_risk": "低",
                    "dual_chain_impact": "较小",
                },
                "industry_drivers": ["医疗新基建", "国产替代", "海外拓展"],
            },
            "002475": {
                "company_position": "精密制造/消费电子",
                "upstream": ["连接器", "线缆", "电子元器件"],
                "downstream": ["苹果", "华为", "消费电子品牌"],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "精密制造工艺",
                        "confidence": "medium",
                    }
                ],
                "us_china_chain": {
                    "role": "苹果供应链核心",
                    "sanction_risk": "低",
                    "dual_chain_impact": "较小",
                },
                "industry_drivers": ["消费电子升级", "TWS耳机", "汽车电子"],
            },
            "00700": {
                "company_position": "互联网平台/游戏",
                "upstream": ["服务器", "带宽", "云计算"],
                "downstream": ["个人用户", "企业客户", "广告主"],
                "chokepoints": [
                    {
                        "type": "网络",
                        "description": "微信生态壁垒",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "纯国内业务",
                    "sanction_risk": "中",
                    "dual_chain_impact": "监管政策",
                },
                "industry_drivers": ["数字化", "游戏出海", "金融科技"],
            },
            "09988": {
                "company_position": "电商平台/云计算",
                "upstream": ["服务器", "带宽", "物流基础设施"],
                "downstream": ["电商买家", "商家", "云服务用户"],
                "chokepoints": [
                    {
                        "type": "网络",
                        "description": "电商平台效应",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "国内电商主导",
                    "sanction_risk": "中",
                    "dual_chain_impact": "监管政策",
                },
                "industry_drivers": ["电商渗透率", "云计算增长", "菜鸟物流"],
            },
            "300274": {
                "company_position": "光伏逆变器",
                "upstream": ["电子元器件", "功率半导体", "磁性元件"],
                "downstream": ["光伏电站", "分布式光伏", "储能系统"],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "逆变器转换效率+储能技术",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "全球光伏逆变器主导",
                    "sanction_risk": "中",
                    "dual_chain_impact": "海外市场受限",
                },
                "industry_drivers": ["光伏装机增长", "储能需求", "海外拓展"],
            },
            "688012": {
                "company_position": "半导体设备/刻蚀机",
                "upstream": ["真空泵", "射频电源", "关键零部件"],
                "downstream": ["晶圆代工厂", "IDM厂商", "LED厂商"],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "刻蚀设备技术壁垒",
                        "confidence": "high",
                    }
                ],
                "us_china_chain": {
                    "role": "国产替代核心",
                    "sanction_risk": "高",
                    "dual_chain_impact": "设备进口受限",
                },
                "industry_drivers": ["国产替代", "晶圆厂扩张", "技术突破"],
            },
            "600176": {
                "company_position": "全球最大的玻璃纤维（玻纤）及其制品制造商之一，隶属于中国建材集团。在产业链中处于上游基础材料供应商位置，为复合材料及玻纤增强材料行业提供核心增强基材，属于建材-玻纤-复合材料产业链的核心环节。",
                "upstream": [
                    "叶腊石、高岭土、石英砂等硅铝酸盐矿物原料",
                    "石灰石、纯碱、硼砂等辅助矿物原料",
                    "天然气、煤气等燃料能源",
                    "电力（高耗能行业）",
                    "铂铑合金漏板等贵金属生产装备",
                    "玻纤池窑拉丝生产线设备及窑炉技术",
                    "化工浸润剂、偶联剂等表面处理剂",
                ],
                "downstream": [
                    "建筑建材（FRP管道、保温材料、装饰板材）",
                    "交通运输与汽车工业（车身轻量化、复合材料部件）",
                    "风电叶片（新能源核心应用）",
                    "电子电气（PCB增强基材、绝缘材料）",
                    "航空航天与军工（高强玻纤复合材料）",
                    "船舶制造（船体增强材料）",
                    "家电与卫浴（玻璃钢制品）",
                ],
                "chokepoints": [
                    {
                        "type": "资源",
                        "description": "叶腊石等非金属矿资源禀赋",
                        "confidence": "medium",
                    },
                    {
                        "type": "技术",
                        "description": "池窑拉丝技术壁垒",
                        "confidence": "high",
                    },
                    {
                        "type": "能源",
                        "description": "天然气/电力成本占比高",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "玻纤出口商",
                    "sanction_risk": "低",
                    "dual_chain_impact": "美国对中国玻纤产品有双反调查，但影响有限",
                },
                "industry_drivers": [
                    "风电叶片需求",
                    "汽车轻量化趋势",
                    "建筑建材需求",
                    "海外玻纤产能退出",
                ],
            },
            "688486": {
                "company_position": "国内领先的混合信号集成电路设计企业（Fabless模式），专注于高速信号传输与高清视频处理芯片设计，涵盖HDMI/DP/eDP/USB Type-C/MIPI等接口芯片及视频转换、显示驱动芯片。处于半导体产业链中游设计环节，下游对接显示器、电视、PC、汽车电子、AR/VR等终端品牌厂商。",
                "upstream": [
                    "晶圆代工厂（中芯国际SMIC、华虹半导体等）",
                    "封装测试服务商（长电科技、通富微电、华天科技等）",
                    "EDA设计工具（Cadence、Synopsys等）",
                    "IP核授权（USB、HDMI、DisplayPort、MIPI等协议IP）",
                    "光罩与掩膜版供应商",
                    "测试设备",
                ],
                "downstream": [
                    "显示器与电视品牌厂商",
                    "笔记本电脑与PC OEM",
                    "汽车电子厂商（车载中控屏、HUD、座舱显示驱动）",
                    "AR/VR头显设备厂商",
                    "视频会议与商显设备",
                    "安防监控设备厂商",
                    "工业控制与人机交互（HMI）设备",
                    "投影仪与商显大屏厂商",
                ],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "先进制程晶圆产能获取受限",
                        "confidence": "high",
                    },
                    {
                        "type": "EDA",
                        "description": "EDA工具受美国出口管制",
                        "confidence": "high",
                    },
                    {
                        "type": "IP",
                        "description": "核心协议栈IP授权风险",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "半导体设计企业，受美国出口管制影响",
                    "sanction_risk": "高",
                    "dual_chain_impact": "先进制程获取困难，海外市场拓展受限",
                },
                "industry_drivers": [
                    "视频高清化趋势",
                    "汽车电子化",
                    "AR/VR市场",
                    "显示技术升级",
                ],
            },
            "002957": {
                "company_position": "科瑞技术是国内领先的工业自动化设备及精密自动化测试设备提供商，定位为智能制造装备综合服务商，主要为锂电池、消费电子、半导体、汽车电子、医疗器械等领域提供自动化生产与检测设备及整线解决方案，处于智能装备制造产业链中游，下游连接终端品牌厂商和大型制造企业。",
                "upstream": [
                    "伺服电机及运动控制系统",
                    "工业机器人（发那科、安川、ABB等）",
                    "精密传感器与视觉系统（基恩士、康耐视等）",
                    "气动元件（SMC、费斯托等）",
                    "PLC及工控系统（西门子、三菱等）",
                    "精密机械零部件（导轨、丝杠、轴承等）",
                    "激光器及光学组件",
                    "电子元器件与集成电路",
                    "机加工钣金件及结构件",
                    "真空设备及配套组件",
                ],
                "downstream": [
                    "锂电池生产厂商（宁德时代、比亚迪、LG新能源等）",
                    "消费电子品牌及代工厂（苹果产业链、华为、小米等）",
                    "半导体封测企业",
                    "汽车零部件及新能源汽车厂商",
                    "医疗器械制造企业",
                    "光伏组件制造商",
                    "面板显示企业",
                    "智能仓储与物流集成商",
                ],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "设备集成与调试能力",
                        "confidence": "high",
                    },
                    {
                        "type": "客户",
                        "description": "下游客户集中度高",
                        "confidence": "medium",
                    },
                    {
                        "type": "配件",
                        "description": "核心零部件依赖进口",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "智能装备供应商",
                    "sanction_risk": "低",
                    "dual_chain_impact": "较小，主要面向国内客户",
                },
                "industry_drivers": [
                    "新能源扩产",
                    "消费电子升级",
                    "半导体国产化",
                    "智能制造转型",
                ],
            },
            "002617": {
                "company_position": "露笑科技是一家从传统电磁线业务向新能源与第三代半导体材料转型的多元化科技企业，核心布局碳化硅（SiC）衬底，光伏硅片及电磁线三大业务线，处于新材料/半导体材料制造环节，是国内少数实现6英寸SiC衬底量产的企业之一。",
                "upstream": [
                    "高纯度碳化硅粉源",
                    "高纯石墨坩埚及保温材料",
                    "单晶炉（MOCVD/PVT设备）",
                    "高纯石英坩埚（光伏硅片）",
                    "电解铜/铜杆（电磁线）",
                    "硅料/多晶硅（光伏）",
                    "工业气体（氩气、氮气等）",
                    "电力与热能供应",
                    "切割钢丝（金刚线）",
                ],
                "downstream": [
                    "新能源汽车电控系统（OBC、DC-DC、逆变器）",
                    "光伏组件厂商（硅片供应）",
                    "家用电器制造（电磁线）",
                    "特高压输变电设备",
                    "工业电机及变频器",
                    "5G通信基站电源",
                    "充电桩/充电站功率模块",
                    "风电变流器",
                    "轨道交通牵引系统",
                ],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "SiC长晶良率提升",
                        "confidence": "high",
                    },
                    {
                        "type": "设备",
                        "description": "SiC长晶炉依赖进口",
                        "confidence": "high",
                    },
                    {
                        "type": "产能",
                        "description": "SiC衬底产能扩张",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "SiC材料供应商，受美国出口管制影响",
                    "sanction_risk": "高",
                    "dual_chain_impact": "SiC设备和材料进口受限",
                },
                "industry_drivers": [
                    "新能源汽车渗透率提升",
                    "光伏装机增长",
                    "SiC渗透率提升",
                    "碳化硅国产替代",
                ],
            },
            "300003": {
                "company_position": "乐普医疗是中国心血管医疗器械领域的龙头企业之一，业务覆盖医疗器械、药品、医疗服务三大板块。在产业链中处于中游医疗器械制造环节，整合上游原材料/零部件供应，向下游医院、药店及患者提供心血管介入器械、诊断试剂、药品以及心血管专科医疗服务。",
                "upstream": [
                    "医用金属材料（镍钛合金、不锈钢、钴铬合金等）",
                    "高分子聚合物材料（PTFE、PET、Pebax等）",
                    "药物涂层原料（雷帕霉素及其衍生物等）",
                    "生物可降解材料（PLLA、PDLLA、镁合金等）",
                    "医用电子元器件（传感器、芯片等）",
                    "体外诊断试剂原料（抗原抗体、酶等）",
                    "包装及灭菌服务",
                    "原料药与药用辅料",
                ],
                "downstream": [
                    "国内三甲医院心血管科室",
                    "基层医院及县级医疗机构",
                    "连锁药店及网上药店",
                    "心血管疾病患者（终端消费者）",
                    "第三方医学检验实验室",
                    "海外市场（CE、FDA认证后出口）",
                    "心血管专科医院及诊所",
                    "体检机构",
                ],
                "chokepoints": [
                    {
                        "type": "认证",
                        "description": "医疗器械注册证壁垒",
                        "confidence": "high",
                    },
                    {
                        "type": "研发",
                        "description": "创新器械研发周期长",
                        "confidence": "high",
                    },
                    {
                        "type": "渠道",
                        "description": "医院准入门槛",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "纯国内业务",
                    "sanction_risk": "低",
                    "dual_chain_impact": "极小",
                },
                "industry_drivers": [
                    "老龄化加速",
                    "心血管疾病发病率提升",
                    "国产替代",
                    "创新器械政策支持",
                ],
            },
            "300054": {
                "company_position": "鼎龙股份处于电子化学品和半导体材料产业链的中上游环节，是国内为数不多同时布局半导体CMP抛光材料（抛光垫、抛光液）、显示材料（光刻胶、彩色光阻、PSPI、OC等）以及先进封装材料的平台型材料厂商。在国内半导体材料国产化进程中扮演关键供应商角色。",
                "upstream": [
                    "聚氨酯/特殊聚合物树脂（CMP抛光垫核心原料）",
                    "二氧化硅/二氧化铈/二氧化铝研磨粒子",
                    "光刻胶树脂单体及光引发剂",
                    "高纯度有机溶剂与功能助剂",
                    "颜料及分散剂（彩色光阻用）",
                    "PI单体及柔性显示材料原料",
                    "碳粉用高分子树脂",
                ],
                "downstream": [
                    "半导体晶圆制造厂（中芯国际、长江存储、华虹宏力等）",
                    "显示面板厂商（京东方、TCL华星、惠科、天马等）",
                    "先进封装客户（长电科技、通富微电等）",
                    "半导体设备厂商",
                    "打印机及耗材OEM/ODM客户",
                ],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "CMP抛光液配方技术壁垒",
                        "confidence": "high",
                    },
                    {
                        "type": "认证",
                        "description": "晶圆厂认证周期长",
                        "confidence": "high",
                    },
                    {
                        "type": "材料",
                        "description": "核心原材料国产化",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "半导体材料供应商",
                    "sanction_risk": "中",
                    "dual_chain_impact": "半导体材料国产替代机遇",
                },
                "industry_drivers": [
                    "半导体扩产",
                    "面板国产化",
                    "CMP材料国产替代",
                    "先进封装发展",
                ],
            },
            "601208": {
                "company_position": "东材科技是国内功能性高分子薄膜材料及精细化工材料的领先企业，主营业务涵盖新能源材料（光伏背板膜、PVDF、锂电池粘结剂及隔膜涂层）、电子级材料（光学膜、MLCC离型膜）、电气绝缘材料以及环保阻燃材料等。定位于新材料产业的中游加工与制造环节。",
                "upstream": [
                    "PTA（精对苯二甲酸）",
                    "MEG（乙二醇）",
                    "PVDF树脂及氟化工原料",
                    "聚酯切片（膜级）",
                    "聚丙烯（PP）",
                    "聚酰亚胺（PI）单体与树脂",
                    "PET基膜",
                    "各类功能助剂",
                    "胶粘剂原料",
                    "氟化工原料",
                    "电子级化学品",
                    "铜箔",
                ],
                "downstream": [
                    "光伏组件（光伏背板膜、封装胶膜）",
                    "锂电池（正极粘结剂PVDF、隔膜涂覆材料）",
                    "新能源汽车动力电池",
                    "储能电池及储能系统",
                    "电力电气设备（电机、变压器绝缘材料）",
                    "印制电路板（CCL/FCCL用PI膜）",
                    "平板显示（OCA光学胶、MLCC离型膜）",
                    "消费电子（5G手机、平板等）",
                    "风电与轨道交通",
                    "建筑与节能材料",
                ],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "特种薄膜配方与工艺",
                        "confidence": "high",
                    },
                    {
                        "type": "原料",
                        "description": "PVDF、PI等高端原料依赖",
                        "confidence": "medium",
                    },
                    {
                        "type": "产能",
                        "description": "光伏材料产能周期",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "新材料供应商",
                    "sanction_risk": "低",
                    "dual_chain_impact": "较小",
                },
                "industry_drivers": [
                    "光伏装机增长",
                    "锂电池需求",
                    "5G建设",
                    "新能源车渗透率提升",
                ],
            },
            "300260": {
                "company_position": "新莱应材是国内高纯应用材料领域的龙头企业，主营高纯不锈钢及特种合金材料、管路系统、超高洁净部件及精密零部件的研发、生产与销售。公司处于产业链中游关键位置，向上对接特种钢材/合金原材料，向下服务于半导体设备、生物医药、食品乳品、新能源等终端应用。",
                "upstream": [
                    "高纯不锈钢原材料（304、316L等特种钢材）",
                    "镍基合金、钛合金、哈氏合金等特种合金材料",
                    "精密机械加工设备（CNC、五轴加工中心）",
                    "密封件原材料（氟橡胶、全氟醚O型圈）",
                    "电控元器件（电磁阀、传感器）",
                    "表面处理与电解抛光服务",
                    "半导体级清洗与检测服务",
                ],
                "downstream": [
                    "半导体设备厂商（应用材料AMAT、泛林LRCX、东京TEL、北方华创、中微公司等）",
                    "晶圆代工厂与IDM（中芯国际、长江存储、华虹等）",
                    "平板显示面板厂商（京东方、华星光电等）",
                    "光伏/太阳能电池厂商（隆基、通威等）",
                    "锂电池厂商（宁德时代、比亚迪等）",
                    "生物医药与制药装备",
                    "食品与乳品行业",
                ],
                "chokepoints": [
                    {
                        "type": "资质",
                        "description": "半导体设备商供应商资质认证",
                        "confidence": "high",
                    },
                    {
                        "type": "技术",
                        "description": "高纯材料洁净度控制技术",
                        "confidence": "high",
                    },
                    {
                        "type": "材料",
                        "description": "特种合金材料供应",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "半导体设备材料供应商",
                    "sanction_risk": "中",
                    "dual_chain_impact": "半导体设备出口管制带来机遇与挑战",
                },
                "industry_drivers": [
                    "半导体国产化加速",
                    "光伏扩产",
                    "新能源车渗透率提升",
                    "生物医药发展",
                ],
            },
            "688002": {
                "company_position": "睿创微纳是中国领先的非制冷红外热成像探测器及整机产品供应商，掌握MEMS红外探测器核心技术（VOx和非晶硅两条技术路线），同时具备读出电路（ROIC）芯片设计能力。位于红外光电产业链中游核心环节，向上游半导体晶圆代工延伸，向下游覆盖军用、工业、车载、消费级整机应用。",
                "upstream": [
                    "晶圆代工服务（MEMS加工及CMOS读出电路流片）",
                    "半导体专用设备（光刻机、刻蚀机、薄膜沉积设备）",
                    "红外光学材料（锗、硅、硫系玻璃等）",
                    "MEMS特种工艺气体与化学品",
                    "真空封装材料（管壳、吸气剂、玻璃盖板）",
                    "FPGA/DSP等图像处理芯片",
                    "稀土材料",
                    "IC设计EDA工具与IP核",
                ],
                "downstream": [
                    "军工领域（红外瞄准镜、夜视观瞄、导弹导引头）",
                    "安防监控（边防、海防、森林防火）",
                    "工业测温与电力检测",
                    "车载红外（夜视辅助驾驶、ADAS）",
                    "消费电子（户外狩猎热像仪）",
                    "消防与应急救援",
                    "医疗健康（体温筛查）",
                    "户外运动与消费级热成像",
                    "无人机载荷",
                ],
                "chokepoints": [
                    {
                        "type": "技术",
                        "description": "MEMS红外探测器核心技术",
                        "confidence": "high",
                    },
                    {
                        "type": "设备",
                        "description": "半导体制造设备依赖进口",
                        "confidence": "high",
                    },
                    {
                        "type": "材料",
                        "description": "红外光学材料供应",
                        "confidence": "medium",
                    },
                ],
                "us_china_chain": {
                    "role": "红外光电探测器和整机供应商",
                    "sanction_risk": "中",
                    "dual_chain_impact": "军工业务有一定影响，消费工业影响较小",
                },
                "industry_drivers": [
                    "军费开支增长",
                    "汽车智能化",
                    "安防建设",
                    "工业测温需求",
                    "户外运动市场",
                ],
            },
        }

    # =====================================================================
    # [v2] Deep Optimization Entry Points
    # =====================================================================
    #
    # V2 在原 fetch_all 基础上引入：
    # 1. KB 加权检索（SupplyChainKBRetriever）
    # 2. 两轮抽取 + 公开数据补全（TwoPassSupplyChainExtractor + FieldEnricher）
    # 3. 结构化图谱（SupplyChainGraphBuilder）
    # 4. 统一 Serenity 评分（SerenityScorer）
    # 5. 数据完整度披露
    #
    # 行为开关：
    # - fetch_all() 保持 v1 行为，不破坏现有调用
    # - fetch_all_v2() 新入口，返回 SupplyChainV2 完整结构
    # - fetch_all_v2_legacy_compat() 同时返回 v1 字典 + v2 结构

    def fetch_all_v2(
        self,
        stock_code: str,
        stock_name: str,
        fundamental_analysis: str = "",
        market: str = "cn",
        enable_serenity: bool = True,
        industry_hint: str = "",
    ) -> Any:
        """[v2] 获取完整供应链数据，返回 SupplyChainV2。

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            fundamental_analysis: 基本面分析文本（用于 LLM 二次抽取）
            market: 市场 (cn/hk/us)
            enable_serenity: 是否启用 Serenity 评分
            industry_hint: 行业/主题提示（如『高端白酒』『半导体』）

        Returns:
            SupplyChainV2 实例（含 v1 兼容字段 + v2 结构化图谱 + KB 命中 + 数据完整度）
        """
        from src.schemas.supply_chain import SupplyChainV2
        from src.services.supply_chain.graph_builder import SupplyChainGraphBuilder
        from src.services.supply_chain.kb_retriever import (
            ColdStartStrategy,
            SupplyChainKBRetriever,
        )
        from src.services.supply_chain.serenity_scorer import SerenityScorer

        # 1. KB 检索（最高优先级）
        kb_retriever = SupplyChainKBRetriever()
        kb_result = kb_retriever.retrieve(
            stock_code=stock_code,
            stock_name=stock_name,
            industry_hint=industry_hint,
            top_k=8,
        )
        cold = ColdStartStrategy(kb_result.aggregate_score)

        # 2. LLM 一次抽取（C 链路，仅在 KB 不够时降级）
        llm_data: Dict[str, Any] = {}
        if cold.llm_fallback in ("aggressive", "moderate", "selective"):
            llm_data = self._fetch_from_llm(
                stock_code, stock_name, fundamental_analysis
            )

        # 3. 派生 v1 兼容字段（公司位置 / 行业 / drivers / chokepoints / us_china）
        (
            position_v1,
            industry_v1,
            drivers_v1,
            chokepoints_v1,
            us_china_v1,
        ) = self._resolve_v1_legacy_fields(
            stock_code, stock_name, industry_hint, llm_data
        )

        # 4. 图谱构建（v2 核心）
        graph = SupplyChainGraphBuilder().build(
            ticker=stock_code,
            company=stock_name,
            industry=industry_v1,
            position=position_v1,
            kb_result=kb_result,
            llm_first_pass={
                "upstream": llm_data.get("upstream", []) or [],
                "downstream": llm_data.get("downstream", []) or [],
            },
            industry_hint=industry_hint,
            chokepoints=chokepoints_v1,
            us_china_chain=us_china_v1,
        )

        # 5. Serenity 评分（v2 统一入口）
        serenity_result: Optional[Any] = None
        if enable_serenity:
            scorer = SerenityScorer()
            llm_signals = self._extract_llm_signals(llm_data)
            serenity_result = scorer.score(
                ticker=stock_code,
                company=stock_name,
                market=market,
                graph=graph,
                kb_result=kb_result,
                llm_signals=llm_signals,
                industry_hint=industry_hint,
            )

        # 6. 组装 SupplyChainV2（v1 兼容字段 + v2 字段）
        v2 = SupplyChainV2(
            # v1 兼容
            data_sources=self._build_v2_data_sources(enable_serenity, serenity_result),
            company_position=position_v1,
            upstream=[n.name for n in graph.upstream],
            downstream=[n.name for n in graph.downstream],
            chokepoints=chokepoints_v1,
            us_china_chain=us_china_v1,
            industry_drivers=drivers_v1,
            # v2 新增
            graph=graph,
            kb_evidence=kb_result.hits,
            llm_signals=self._extract_llm_signals(llm_data),
            data_completeness=graph.data_completeness,
            serenity_score=serenity_result.final_score if serenity_result else None,
            serenity_verdict=serenity_result.verdict if serenity_result else None,
            serenity_factor_details=self._build_serenity_factor_details(
                serenity_result
            ),
            serenity_penalty_details=self._build_serenity_penalty_details(
                serenity_result
            ),
            serenity_kb_bonus_applied=self._build_serenity_kb_bonus(serenity_result),
            fetched_at=datetime.now(timezone.utc),
        )

        logger.info(
            "[SupplyChainDataService v2] %s supply chain: kb_score=%.2f (%s), "
            "upstream=%d, downstream=%d, serenity=%s",
            stock_code,
            kb_result.aggregate_score,
            cold.tier,
            len(graph.upstream),
            len(graph.downstream),
            v2.serenity_verdict,
        )

        return v2

    @staticmethod
    def _build_v2_data_sources(
        enable_serenity: bool, serenity_result: Optional[Any]
    ) -> List[str]:
        """[拆分] 根据 enable_serenity / serenity_result 派生 data_sources 列表。"""
        count = 2 + (1 if enable_serenity else 0) + (1 if serenity_result else 0)
        return ["knowledge_base", "llm", "graph", "serenity"][:count]

    @staticmethod
    def _build_serenity_factor_details(
        serenity_result: Optional[Any],
    ) -> Dict[str, Any]:
        """[拆分] 从 SerenityScoreResult 派生 factor_details dict。"""
        if not serenity_result:
            return {}
        return {
            k: {
                "rating": v.rating,
                "points": v.points,
                "kb_relevance": v.kb_relevance,
                "llm_signal": v.llm_signal,
                "industry_prior": v.industry_prior,
                "kb_bonus_applied": v.kb_bonus_applied,
            }
            for k, v in serenity_result.factors.items()
        }

    @staticmethod
    def _build_serenity_penalty_details(
        serenity_result: Optional[Any],
    ) -> Dict[str, Any]:
        """[拆分] 从 SerenityScoreResult 派生 penalty_details dict。"""
        if not serenity_result:
            return {}
        return {
            k: {"rating": v.rating, "points": v.points}
            for k, v in serenity_result.penalties.items()
        }

    @staticmethod
    def _build_serenity_kb_bonus(
        serenity_result: Optional[Any],
    ) -> Dict[str, Any]:
        """[拆分] 从 SerenityScoreResult 派生 kb_bonus_applied dict（仅含 >0 项）。"""
        if not serenity_result:
            return {}
        return {
            k: v.kb_bonus_applied
            for k, v in serenity_result.factors.items()
            if v.kb_bonus_applied > 0
        }

    def _resolve_v1_legacy_fields(
        self,
        stock_code: str,
        stock_name: str,
        industry_hint: str,
        llm_data: Dict[str, Any],
    ) -> Tuple[str, str, List[str], List[Any], Optional[Any]]:
        """[拆分] 派生 v1 兼容字段：position / industry / drivers / chokepoints / us_china。

        来源优先级：LLM 推断 → 知识库硬编码 → 默认值（"{stock_name}（{code}）"）。
        单条 chokepoint / us_china 解析失败时跳过该条，不影响整体。
        """
        position_v1, drivers_v1 = self._resolve_v1_position_and_drivers(
            stock_code, stock_name, llm_data
        )
        industry_v1 = industry_hint or "未知行业"
        chokepoints_v1 = self._build_v1_chokepoints(llm_data)
        us_china_v1 = self._build_v1_us_china_chain(llm_data)
        return (
            position_v1,
            industry_v1,
            drivers_v1,
            chokepoints_v1,
            us_china_v1,
        )

    def _resolve_v1_position_and_drivers(
        self,
        stock_code: str,
        stock_name: str,
        llm_data: Dict[str, Any],
    ) -> Tuple[str, List[str]]:
        """[拆分] v1 position / drivers 派生（LLM → KB → 默认值）。"""
        position_v1 = ""
        drivers_v1: List[str] = []

        if llm_data:
            position_v1 = llm_data.get("company_position", "") or ""
            drivers_v1 = llm_data.get("industry_drivers", []) or []

        if not position_v1:
            v1_kb = self._fetch_from_knowledge_base(stock_code, stock_name)
            if v1_kb.get("company_position"):
                position_v1 = str(v1_kb["company_position"])
            if v1_kb.get("industry_drivers"):
                drivers_v1 = list(v1_kb["industry_drivers"])

        if not position_v1:
            position_v1 = f"{stock_name}（{stock_code}）"

        return position_v1, drivers_v1

    @staticmethod
    def _build_v1_chokepoints(
        llm_data: Dict[str, Any],
    ) -> List[Any]:
        """[拆分] 从 LLM 数据派生 v1 chokepoints（单条失败容错跳过）。"""
        from src.schemas.supply_chain import Chokepoint

        out: List[Chokepoint] = []
        for cp in llm_data.get("chokepoints") or []:
            if not isinstance(cp, dict):
                continue
            try:
                out.append(
                    Chokepoint(
                        type=cp.get("type", "tech"),
                        description=str(cp.get("description", "")),
                        confidence=cp.get("confidence", "medium"),
                    )
                )
            except Exception:
                continue
        return out

    @staticmethod
    def _build_v1_us_china_chain(
        llm_data: Dict[str, Any],
    ) -> Optional[Any]:
        """[拆分] 从 LLM 数据派生 v1 us_china_chain（解析失败返回 None）。"""
        from src.schemas.supply_chain import USChinaChain

        uc = llm_data.get("us_china_chain") or {}
        if not isinstance(uc, dict) or not uc:
            return None
        try:
            return USChinaChain(
                role=str(uc.get("role", "待分析")),
                substitution_progress=uc.get("substitution_progress"),
                sanction_risk=uc.get("sanction_risk"),
                dual_chain_impact=uc.get("dual_chain_impact"),
            )
        except Exception:
            return None

    def _extract_llm_signals(self, llm_data: Dict[str, Any]) -> Dict[str, float]:
        """从 LLM 推断结果里抽取因子信号（v2 启发式映射）。"""
        signals: Dict[str, float] = {}

        chokepoints = llm_data.get("chokepoints") or []
        if isinstance(chokepoints, list) and chokepoints:
            signals["chokepoint_severity"] = min(1.0, len(chokepoints) / 3.0)

        upstream = llm_data.get("upstream") or []
        if isinstance(upstream, list) and len(upstream) >= 3:
            signals["expansion_difficulty"] = 0.6

        uc = llm_data.get("us_china_chain") or {}
        if isinstance(uc, dict):
            risk = uc.get("sanction_risk", "")
            if risk == "高":
                signals["penalty_geopolitics"] = 0.8
            elif risk == "中":
                signals["penalty_geopolitics"] = 0.4

        drivers = llm_data.get("industry_drivers") or []
        if isinstance(drivers, list) and drivers:
            signals["demand_inflection"] = min(1.0, len(drivers) / 3.0)
            signals["catalyst_timing"] = 0.5

        return signals

    def fetch_all_v2_legacy_compat(
        self,
        stock_code: str,
        stock_name: str,
        fundamental_analysis: str = "",
        market: str = "cn",
        enable_serenity: bool = True,
        industry_hint: str = "",
    ) -> Dict[str, Any]:
        """[v2] 返回 dict 形式（同时含 v1 字段和 v2 字段），便于平滑迁移。"""
        v2 = self.fetch_all_v2(
            stock_code=stock_code,
            stock_name=stock_name,
            fundamental_analysis=fundamental_analysis,
            market=market,
            enable_serenity=enable_serenity,
            industry_hint=industry_hint,
        )
        v2_dict = v2.model_dump()
        v1_only = self.fetch_all(
            stock_code=stock_code,
            stock_name=stock_name,
            fundamental_analysis=fundamental_analysis,
            market=market,
            enable_serenity=enable_serenity,
        )
        # v2 字段优先；v1 兜底
        merged = {**v1_only, **v2_dict}
        return merged

def get_industry_capacity_template(industry_hint: str) -> Dict[str, Any]:
    """根据行业提示查询产能相关模板。

    用于 §10.b 产能展望工具的降级推断。

    Args:
        industry_hint: 行业提示（如"白酒"、"半导体"、"新能源"）

    Returns:
        包含 capacity_unit_hint / benchmark_utilization / seasonal_pattern 的 dict
    """
    if not industry_hint:
        return {
            'capacity_unit_hint': '',
            'benchmark_utilization': 75.0,
            'seasonal_pattern': '',
        }
    name_lower = industry_hint.lower()
    for keywords, template in _INDUSTRY_INFERENCE_TEMPLATES:
        if any(kw in name_lower for kw in keywords):
            return {
                'capacity_unit_hint': template.get('capacity_unit_hint', ''),
                'benchmark_utilization': template.get('benchmark_utilization', 75.0),
                'seasonal_pattern': template.get('seasonal_pattern', ''),
            }
    return {
        'capacity_unit_hint': '',
        'benchmark_utilization': 75.0,
        'seasonal_pattern': '',
    }
