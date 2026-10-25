# -*- coding: utf-8 -*-
"""
Research Scoring Service.

Orchestrates the six-dimension scoring and Bayesian calculation pipeline.
P2: Integrated with data providers for concept board, institutional holdings, and northbound flow.
P3: Integrated with SupplyChainAgent and ValueAgent for LLM-driven analysis.
"""

import json
import logging
import os
from typing import cast, Dict, Any, Optional, List

from data_provider.supply_chain import latest_fiscal_year
from src.llm.concurrency import limited_completion
from src.config import get_config
from src.llm.generation_params import apply_minimax_reasoning_params
from src.scoring import (
    aggregate_framework,
    calculate_bayesian,
    get_default_weights,
    FrameworkScore,
    BayesianResult,
)
from src.scoring.indicators import (
    score_supply_chain,
    score_fundamental,
    score_capital,
    score_technical,
    score_sentiment,
    score_macro,
)
from src.repositories import PositionLedgerRepo, ScoreLedgerRepo
from src.storage import DatabaseManager
import json_repair  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)


class ResearchScoringService:
    """Service for orchestrating research scoring pipeline"""

    def __init__(self) -> None:
        self.db_manager = DatabaseManager.get_instance()
        self._data_providers_initialized = False
        self._llm_service = None
        self._agents_initialized = False

    def _init_data_providers(self) -> None:
        """Lazy initialize P2 data providers"""
        if self._data_providers_initialized:
            return

        self._data_providers_initialized = True
        self._institutional_provider = None
        self._northbound_provider = None
        self._tushare_supply_chain = None

        try:
            from data_provider.supply_chain import (
                InstitutionalHoldingsProvider,
                NorthboundFlowProvider,
                TushareSupplyChainProvider,
            )

            self._institutional_provider = InstitutionalHoldingsProvider()
            self._northbound_provider = NorthboundFlowProvider()
            self._tushare_supply_chain = TushareSupplyChainProvider()
            logger.info("[ResearchScoringService] P2 data providers initialized")
        except ImportError as e:
            logger.warning(
                f"[ResearchScoringService] P2 data providers not available: {e}"
            )

    def process(
        self,
        stock_code: str,
        stock_name: str,
        market: str = "cn",
        raw_data: Optional[Dict[str, Any]] = None,
        report_id: Optional[int] = None,
        market_implied_p: float = 0.5,
        lr: float = 1.0,
        strong_negative_evidence: bool = False,
        current_concentration: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Process full scoring pipeline.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            market: Market (cn/hk/us)
            raw_data: Raw input data for scoring
            report_id: Associated report ID
            market_implied_p: Market implied probability [0, 1]
            lr: Likelihood ratio for evidence update
            strong_negative_evidence: Whether strong negative evidence exists
            current_concentration: Current sector concentration [0, 1]

        Returns:
            Dict with framework_score, bayesian_result, and ledger_record
        """
        raw_data = raw_data or {}

        dimension_results = self._score_all_dimensions(raw_data)

        framework_result = self._aggregate_framework(dimension_results)

        bayesian_result = self._calculate_bayesian(
            framework_result.dimension_total,
            market_implied_p,
            lr,
            strong_negative_evidence,
            current_concentration,
        )

        ledger_record = self._persist_scores(
            stock_code=stock_code,
            stock_name=stock_name,
            market=market,
            framework_result=framework_result,
            bayesian_result=bayesian_result,
            report_id=report_id,
        )

        return {
            "framework_score": self._framework_to_dict(framework_result),
            "bayesian_result": self._bayesian_to_dict(bayesian_result),
            "ledger_record": ledger_record,
        }

    def _score_all_dimensions(self, raw_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Score all six dimensions"""
        results = []

        sc_result = score_supply_chain(
            chain_position=raw_data.get("chain_position"),
            moat_type=raw_data.get("moat_type"),
            moat_strength=raw_data.get("moat_strength"),
            customer_concentration=raw_data.get("customer_concentration"),
            us_china_risk=raw_data.get("us_china_risk"),
            chokepoint_type=raw_data.get("chokepoint_type"),
            evidence=raw_data.get("supply_chain_evidence"),
        )
        results.append(sc_result)

        fund_result = score_fundamental(
            pe_percentile=raw_data.get("pe_percentile"),
            pb_percentile=raw_data.get("pb_percentile"),
            roe=raw_data.get("roe"),
            revenue_growth=raw_data.get("revenue_growth"),
            earnings_growth=raw_data.get("earnings_growth"),
            gross_margin=raw_data.get("gross_margin"),
            moat_assessment=raw_data.get("moat_assessment"),
            evidence=raw_data.get("fundamental_evidence"),
        )
        results.append(fund_result)

        cap_result = score_capital(
            institutional_holding_change=raw_data.get("institutional_holding_change"),
            northbound_flow_20d=raw_data.get("northbound_flow_20d"),
            margin_balance_change=raw_data.get("margin_balance_change"),
            foreign_ratio=raw_data.get("foreign_ratio"),
            evidence=raw_data.get("capital_evidence"),
        )
        results.append(cap_result)

        tech_result = score_technical(
            ma_alignment=raw_data.get("ma_alignment"),
            price_vs_ma250=raw_data.get("price_vs_ma250"),
            volume_trend=raw_data.get("volume_trend"),
            distance_from_high=raw_data.get("distance_from_high"),
            trend_duration_months=raw_data.get("trend_duration_months"),
            evidence=raw_data.get("technical_evidence"),
        )
        results.append(tech_result)

        sent_result = score_sentiment(
            analyst_consensus=raw_data.get("analyst_consensus"),
            target_price_upside=raw_data.get("target_price_upside"),
            news_sentiment=raw_data.get("news_sentiment"),
            short_interest_ratio=raw_data.get("short_interest_ratio"),
            cognitive_difference=raw_data.get("cognitive_difference"),
            recent_catalysts=raw_data.get("recent_catalysts"),
            evidence=raw_data.get("sentiment_evidence"),
        )
        results.append(sent_result)

        macro_result = score_macro(
            monetary_policy=raw_data.get("monetary_policy"),
            liquidity_indicator=raw_data.get("liquidity_indicator"),
            sector_policy=raw_data.get("sector_policy"),
            us_china_impact=raw_data.get("us_china_impact"),
            regulatory_risk=raw_data.get("regulatory_risk"),
            evidence=raw_data.get("macro_evidence"),
        )
        results.append(macro_result)

        return results

    def _aggregate_framework(
        self, dimension_results: List[Dict[str, Any]]
    ) -> FrameworkScore:
        """Aggregate dimension results into framework score"""
        return aggregate_framework(dimension_results, version="v1")

    def _calculate_bayesian(
        self,
        dimension_total: float,
        market_implied_p: float,
        lr: float,
        strong_negative_evidence: bool,
        current_concentration: float = 0.0,
    ) -> BayesianResult:
        """Calculate Bayesian result"""
        return calculate_bayesian(
            dimension_total=dimension_total,
            market_implied_p=market_implied_p,
            lr=lr,
            strong_negative_evidence=strong_negative_evidence,
            current_concentration=current_concentration,
        )

    def _persist_scores(
        self,
        stock_code: str,
        stock_name: str,
        market: str,
        framework_result: FrameworkScore,
        bayesian_result: BayesianResult,
        report_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist scores to ledger"""
        session_local = self.db_manager._SessionLocal
        if session_local is None:
            return None
        try:
            db = session_local()
            try:
                score_repo = ScoreLedgerRepo(db)

                dimension_scores = {
                    d.dimension: d.score for d in framework_result.dimensions
                }

                record_data = {
                    "report_id": report_id,
                    "stock_code": stock_code,
                    "market": market,
                    "dimension_total": framework_result.dimension_total,
                    "supply_chain_score": dimension_scores.get("产业链定位"),
                    "fundamental_score": dimension_scores.get("基本面与价值"),
                    "capital_score": dimension_scores.get("资金面"),
                    "technical_score": dimension_scores.get("技术面"),
                    "sentiment_score": dimension_scores.get("情绪与认知差"),
                    "macro_score": dimension_scores.get("宏观与地缘"),
                    "prior_p": bayesian_result.prior_p,
                    "market_implied_p": bayesian_result.market_implied_p,
                    "edge": bayesian_result.edge,
                    "posterior_p": bayesian_result.posterior_p,
                    "position_suggestion": bayesian_result.position_suggestion,
                    "scoring_version": framework_result.version,
                    "raw_scores_json": json.dumps(
                        {
                            "dimensions": [
                                {
                                    "dimension": d.dimension,
                                    "weight": d.weight,
                                    "score": d.score,
                                    "indicators": [
                                        {
                                            "name": i.name,
                                            "score": i.score,
                                            "weight": i.weight,
                                            "basis": i.basis,
                                        }
                                        for i in d.indicators
                                    ],
                                }
                                for d in framework_result.dimensions
                            ],
                            "warnings": list(framework_result.warnings),
                        },
                        ensure_ascii=False,
                    ),
                }

                record = score_repo.create(record_data)
                if record:
                    return {
                        "id": record.id,
                        "stock_code": record.stock_code,
                        "dimension_total": record.dimension_total,
                        "edge": record.edge,
                        "position_suggestion": record.position_suggestion,
                    }
                return None
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Failed to persist scores: {e}")
            return None

    def _framework_to_dict(self, framework: FrameworkScore) -> Dict[str, Any]:
        """Convert FrameworkScore to dict"""
        return {
            "dimension_total": framework.dimension_total,
            "dimensions": [
                {
                    "dimension": d.dimension,
                    "weight": d.weight,
                    "score": d.score,
                    "indicators": [
                        {
                            "name": i.name,
                            "score": i.score,
                            "weight": i.weight,
                            "basis": i.basis,
                            "confidence": i.confidence,
                            "summary": i.summary,
                        }
                        for i in d.indicators
                    ],
                }
                for d in framework.dimensions
            ],
            "version": framework.version,
            "warnings": list(framework.warnings),
        }

    def _bayesian_to_dict(self, bayesian: BayesianResult) -> Dict[str, Any]:
        """Convert BayesianResult to dict"""
        return {
            "prior_p": bayesian.prior_p,
            "market_implied_p": bayesian.market_implied_p,
            "edge": bayesian.edge,
            "posterior_p": bayesian.posterior_p,
            "position_suggestion": bayesian.position_suggestion,
            "stop_conditions": bayesian.stop_conditions,
        }

    def enrich_with_p2_data(
        self,
        stock_code: str,
        raw_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Enrich raw data with P2 data provider data.

        Args:
            stock_code: Stock code
            raw_data: Existing raw data dict

        Returns:
            Enriched raw data dict
        """
        self._init_data_providers()

        enriched = raw_data.copy()

        if self._institutional_provider:
            try:
                inst_data = self._institutional_provider.calculate_institutional_score(
                    stock_code
                )
                if inst_data:
                    enriched["institutional_score"] = inst_data.get("score")
                    enriched["institutional_ratio"] = inst_data.get(
                        "institutional_ratio"
                    )
                    enriched["net_flow"] = inst_data.get("net_flow")
                    logger.debug(
                        f"[ResearchScoringService] Enriched with institutional data: score={inst_data.get('score')}"
                    )
            except Exception as e:
                logger.warning(
                    f"[ResearchScoringService] Failed to get institutional data: {e}"
                )

        if self._northbound_provider:
            try:
                flow_data = self._northbound_provider.calculate_flow_score(stock_code)
                if flow_data:
                    enriched["northbound_score"] = flow_data.get("score")
                    enriched["northbound_flow_20d"] = flow_data.get(
                        "northbound_flow_20d"
                    )
                    logger.debug(
                        f"[ResearchScoringService] Enriched with northbound data: score={flow_data.get('score')}"
                    )
            except Exception as e:
                logger.warning(
                    f"[ResearchScoringService] Failed to get northbound data: {e}"
                )

        return enriched

    def process_with_p2_enrichment(
        self,
        stock_code: str,
        stock_name: str,
        market: str = "cn",
        raw_data: Optional[Dict[str, Any]] = None,
        report_id: Optional[int] = None,
        market_implied_p: float = 0.5,
        lr: float = 1.0,
        strong_negative_evidence: bool = False,
        current_concentration: float = 0.0,
        enrich_with_providers: bool = True,
    ) -> Dict[str, Any]:
        """
        Process scoring pipeline with optional P2 data enrichment.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            market: Market (cn/hk/us)
            raw_data: Raw input data for scoring
            report_id: Associated report ID
            market_implied_p: Market implied probability [0, 1]
            lr: Likelihood ratio for evidence update
            strong_negative_evidence: Whether strong negative evidence exists
            current_concentration: Current sector concentration [0, 1]
            enrich_with_providers: Whether to enrich data from P2 providers

        Returns:
            Dict with framework_score, bayesian_result, and ledger_record
        """
        raw_data = raw_data or {}

        if enrich_with_providers:
            raw_data = self.enrich_with_p2_data(stock_code, raw_data)

        dimension_results = self._score_all_dimensions(raw_data)

        framework_result = self._aggregate_framework(dimension_results)

        bayesian_result = self._calculate_bayesian(
            framework_result.dimension_total,
            market_implied_p,
            lr,
            strong_negative_evidence,
            current_concentration,
        )

        ledger_record = self._persist_scores(
            stock_code=stock_code,
            stock_name=stock_name,
            market=market,
            framework_result=framework_result,
            bayesian_result=bayesian_result,
            report_id=report_id,
        )

        return {
            "framework_score": self._framework_to_dict(framework_result),
            "bayesian_result": self._bayesian_to_dict(bayesian_result),
            "ledger_record": ledger_record,
            "p2_enriched": enrich_with_providers,
        }

    def _init_agents(self) -> None:
        """Lazy initialize P3 agents"""
        if self._agents_initialized:
            return

        self._agents_initialized = True
        self._supply_chain_agent = None
        self._value_agent = None

        try:
            from src.agent.agents import SupplyChainAgent, ValueAgent
            from src.agent.factory import get_tool_registry
            from src.agent.llm_adapter import LLMToolAdapter

            registry = get_tool_registry()
            llm_adapter = LLMToolAdapter()
            common_kwargs: Dict[str, Any] = dict(
                tool_registry=registry,
                llm_adapter=llm_adapter,
            )

            self._supply_chain_agent = SupplyChainAgent(**common_kwargs)
            self._value_agent = ValueAgent(**common_kwargs)
            logger.info("[ResearchScoringService] P3 agents initialized")
        except ImportError as e:
            logger.warning(f"[ResearchScoringService] P3 agents not available: {e}")

    async def analyze_supply_chain_agent(
        self,
        stock_code: str,
        stock_name: str,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Run SupplyChainAgent for supply chain positioning analysis.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            raw_data: Optional pre-fetched data (concept boards, institutional, etc.)

        Returns:
            Dict with agent analysis results
        """
        self._init_agents()

        if self._supply_chain_agent is None:
            logger.warning("[ResearchScoringService] SupplyChainAgent not available")
            return self._get_fallback_supply_chain()

        try:
            from src.agent.protocols import AgentContext

            ctx = AgentContext(query=f"Analyze supply chain for {stock_code}")
            ctx.stock_code = stock_code
            ctx.stock_name = stock_name

            if raw_data:
                if raw_data.get("concept_boards"):
                    ctx.set_data("concept_boards", raw_data["concept_boards"])
                if raw_data.get("institutional_data"):
                    ctx.set_data(
                        "institutional_holdings", raw_data["institutional_data"]
                    )
                if raw_data.get("northbound_data"):
                    ctx.set_data("northbound_flow", raw_data["northbound_data"])

            result = self._supply_chain_agent.run(ctx)

            if result.opinion and result.opinion.raw_data:
                return {
                    "success": True,
                    "analysis": result.opinion.raw_data,
                    "signal": result.opinion.signal,
                    "confidence": result.opinion.confidence,
                    "reasoning": result.opinion.reasoning,
                }
            return {
                "success": False,
                "analysis": {},
                "signal": "hold",
                "confidence": 0.5,
                "reasoning": "Agent did not produce structured output",
            }
        except Exception as e:
            logger.error(f"[ResearchScoringService] SupplyChainAgent failed: {e}")
            return {
                "success": False,
                "analysis": {},
                "signal": "hold",
                "confidence": 0.5,
                "reasoning": f"Agent error: {str(e)}",
            }

    async def analyze_value_agent(
        self,
        stock_code: str,
        stock_name: str,
        raw_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Run ValueAgent for long-term value scenario analysis.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            raw_data: Optional pre-fetched fundamental data

        Returns:
            Dict with agent analysis results
        """
        self._init_agents()

        if self._value_agent is None:
            logger.warning("[ResearchScoringService] ValueAgent not available")
            return self._get_fallback_value()

        try:
            from src.agent.protocols import AgentContext

            ctx = AgentContext(query=f"Analyze value for {stock_code}")
            ctx.stock_code = stock_code
            ctx.stock_name = stock_name

            if raw_data:
                if raw_data.get("realtime_quote"):
                    ctx.set_data("realtime_quote", raw_data["realtime_quote"])
                if raw_data.get("financial_indicators"):
                    ctx.set_data(
                        "financial_indicators", raw_data["financial_indicators"]
                    )

            result = self._value_agent.run(ctx)

            if result.opinion and result.opinion.raw_data:
                return {
                    "success": True,
                    "analysis": result.opinion.raw_data,
                    "signal": result.opinion.signal,
                    "confidence": result.opinion.confidence,
                    "reasoning": result.opinion.reasoning,
                }
            return {
                "success": False,
                "analysis": {},
                "signal": "hold",
                "confidence": 0.5,
                "reasoning": "Agent did not produce structured output",
            }
        except Exception as e:
            logger.error(f"[ResearchScoringService] ValueAgent failed: {e}")
            return {
                "success": False,
                "analysis": {},
                "signal": "hold",
                "confidence": 0.5,
                "reasoning": f"Agent error: {str(e)}",
            }

    def _get_fallback_supply_chain(self) -> Dict[str, Any]:
        """Fallback when SupplyChainAgent fails"""
        return {
            "success": False,
            "analysis": {},
            "signal": "hold",
            "confidence": 0.5,
            "reasoning": "SupplyChainAgent unavailable, using default values",
        }

    def _get_fallback_value(self) -> Dict[str, Any]:
        """Fallback when ValueAgent fails"""
        return {
            "success": False,
            "analysis": {},
            "signal": "hold",
            "confidence": 0.5,
            "reasoning": "ValueAgent unavailable, using default values",
        }

    def enrich_with_agent_analysis(
        self,
        stock_code: str,
        stock_name: str,
        raw_data: Dict[str, Any],
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """
        Enrich raw data with agent analysis results (sync wrapper).

        Args:
            stock_code: Stock code
            stock_name: Stock name
            raw_data: Existing raw data dict
            use_llm: Whether to run LLM agents (may be slow)

        Returns:
            Enriched raw data dict
        """
        enriched = raw_data.copy()

        if not use_llm:
            return enriched

        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            supply_chain_result = loop.run_until_complete(
                self.analyze_supply_chain_agent(stock_code, stock_name, raw_data)
            )
            if supply_chain_result.get("success"):
                enriched["supply_chain_agent_analysis"] = supply_chain_result[
                    "analysis"
                ]
                enriched["supply_chain_signal"] = supply_chain_result.get(
                    "signal", "hold"
                )

            value_result = loop.run_until_complete(
                self.analyze_value_agent(stock_code, stock_name, raw_data)
            )
            if value_result.get("success"):
                enriched["value_agent_analysis"] = value_result["analysis"]
                enriched["value_signal"] = value_result.get("signal", "hold")

        except Exception as e:
            logger.warning(f"[ResearchScoringService] Agent enrichment failed: {e}")
        finally:
            loop.close()

        return enriched

    def get_supply_chain_from_tushare(
        self, stock_code: str, year: Optional[int] = None
    ) -> Dict[str, List[str]]:
        """
        Get supplier/customer data from Tushare Pro.

        Args:
            stock_code: Stock code
            year: Report year (defaults to latest available fiscal year)

        Returns:
            Dict with 'suppliers' and 'customers' lists
        """
        if year is None:
            year = latest_fiscal_year()
        self._init_data_providers()

        if self._tushare_supply_chain is None:
            return {"suppliers": [], "customers": []}

        return self._tushare_supply_chain.get_supplier_customer(stock_code, year)

    def extract_supply_chain_from_annual_report(
        self,
        stock_code: str,
        stock_name: str,
        fundamental_analysis: str,
    ) -> Dict[str, Any]:
        """
        Extract supply chain info from annual report text using LLM.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            fundamental_analysis: Fundamental analysis text

        Returns:
            Dict with supply chain extraction results
        """
        if not fundamental_analysis or len(fundamental_analysis) < 100:
            return {}

        prompt = f"""从以下基本面分析文本中提取供应链信息：

股票：{stock_name}（{stock_code}）

文本：
{fundamental_analysis[:3000]}

请提取以下信息（JSON格式）：
{{
    "chain_position": "公司在产业链中的位置描述（30字内）",
    "upstream_keywords": ["上游关键原材料或组件"],
    "downstream_keywords": ["下游主要应用领域"],
    "chokepoint_type": "主要瓶颈点类型（专利/技术/产能/地理/认证之一）",
    "chokepoint_desc": "瓶颈点描述（20字内）",
    "us_business_ratio": "美国业务占比（如有提及，如'10%'或'约20%'），否则填null",
    "sanction_risk": "制裁风险评估（低/中/高/待观察），基于业务结构判断",
    "substitution_progress": "国产替代进度描述（如有提及）",
    "industry_drivers": ["产业驱动因素1", "产业驱动因素2"]
}}

只输出JSON，不要其他内容。"""

        try:
            from litellm import completion

            response = limited_completion(completion,
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=500,
            )

            response_obj: Any = response
            content_raw: Any = response_obj.choices[0].message.content
            content: str = content_raw if content_raw is not None else ""
            import json
            import json_repair

            result = json_repair.loads(content)
            logger.info(
                f"[ResearchScoringService] LLM extracted supply chain for {stock_code}"
            )
            return cast(Dict[str, Any], result)

        except Exception as e:
            logger.warning(f"[ResearchScoringService] LLM extraction failed: {e}")
            return {}

    def extract_supply_chain_from_llm(
        self,
        stock_code: str,
        stock_name: str,
        fundamental_analysis: str,
    ) -> Dict[str, Any]:
        """
        Extract supply chain info from fundamental analysis using LLM.

        Uses the configured model (from settings) for extraction.
        This is a lightweight version optimized for quick extraction during analysis.

        Args:
            stock_code: Stock code
            stock_name: Stock name
            fundamental_analysis: Fundamental analysis text

        Returns:
            Dict with supply chain extraction results
        """
        if not fundamental_analysis or len(fundamental_analysis) < 50:
            return {}

        for key in [
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ]:
            os.environ.pop(key, None)

        prompt = f"""从以下基本面分析文本中提取供应链信息：

股票：{stock_name}（{stock_code}）

文本：
{fundamental_analysis[:3000]}

请提取以下信息（JSON格式）：
{{
    "chain_position": "公司在产业链中的位置描述（30字内）",
    "upstream": ["上游关键原材料或组件1", "上游关键原材料或组件2"],
    "downstream": ["下游主要应用领域1", "下游主要应用领域2"],
    "chokepoint_type": "主要瓶颈点类型（专利/技术/产能/地理/认证之一）",
    "chokepoint_desc": "瓶颈点描述（20字内）",
    "us_business_ratio": "美国业务占比（如有提及，如'10%'或'约20%'），否则填null",
    "sanction_risk": "制裁风险评估（低/中/高/待观察），基于业务结构判断",
    "substitution_progress": "国产替代进度描述（如有提及）",
    "dual_chain_impact": "中美双链影响评估",
    "industry_drivers": ["产业驱动因素1", "产业驱动因素2"]
}}

只输出JSON，不要其他内容。如果文本中没有相关信息，字段值填null或空数组。"""

        try:
            from litellm import completion

            model = os.environ.get("LITELLM_MODEL", "openai/MiniMax-M3.1-Flash")

            call_kwargs = apply_minimax_reasoning_params(
                {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
                [model],
                get_config().minimax_reasoning_effort,
            )
            response = limited_completion(completion, **call_kwargs)

            response_obj: Any = response
            content_raw: Any = response_obj.choices[0].message.content
            content: str = content_raw if content_raw is not None else ""

            try:
                import json
                import json_repair

                result = json_repair.loads(content)
            except Exception:
                import re

                json_match = re.search(
                    r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content, re.DOTALL
                )
                if json_match:
                    result = json_repair.loads(json_match.group(0))  # type: ignore[union-attr]
                else:
                    logger.warning(
                        f"[ResearchScoringService] Failed to parse LLM response for {stock_code}"
                    )
                    return {}

            logger.info(
                f"[ResearchScoringService] LLM supply chain extraction for {stock_code}"
            )
            return cast(Dict[str, Any], result)

        except Exception as e:
            logger.warning(f"[ResearchScoringService] LLM extraction failed: {e}")
            return {}

    def get_agent_tools_available(self) -> Dict[str, bool]:
        """Check which agent tools are available"""
        self._init_agents()
        return {
            "supply_chain_agent": self._supply_chain_agent is not None,
            "value_agent": self._value_agent is not None,
        }
