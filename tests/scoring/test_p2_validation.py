# -*- coding: utf-8 -*-
"""
P2 产业链数据 - 完整验证测试脚本

测试内容:
1. 数据 Provider (概念板块、机构持仓、北向资金)
2. Prompt 模板
3. LLM 主观评分服务
4. 评分服务集成
5. 前端组件
"""

import sys
import json
from datetime import datetime
from typing import Dict, Any, List, Tuple

sys.path.insert(0, "/home/yu/pythonProjects/daily_stock_analysis")


class TestRunner:
    """测试运行器"""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results: List[Dict[str, Any]] = []

    def record(self, name: str, passed: bool, message: str = "", details: str = ""):
        self.results.append(
            {
                "name": name,
                "passed": passed,
                "message": message,
                "details": details,
            }
        )
        if passed:
            self.passed += 1
            print(f"  ✅ {name}")
        else:
            self.failed += 1
            print(f"  ❌ {name}: {message}")
        if details:
            print(f"     详情: {details}")

    def summary(self) -> Tuple[int, int]:
        return self.passed, self.failed


def test_concept_board_provider(runner: TestRunner):
    """公开概念板块 Provider 已退役；最新概念归属由 iFinD 适配器提供。"""
    from data_provider import supply_chain
    from src.agent.tools.supply_chain_tools import ALL_SUPPLY_CHAIN_TOOLS

    print("\n" + "=" * 60)
    print("1. 公开概念板块 Provider 已退役")
    print("=" * 60)

    runner.record(
        "ConceptBoardProvider 未从生产包导出",
        not hasattr(supply_chain, "ConceptBoardProvider"),
        "iFinD adapter owns latest concept membership",
    )
    runner.record(
        "公开概念校验工具未注册",
        "verify_supply_chain_evidence"
        not in {tool.name for tool in ALL_SUPPLY_CHAIN_TOOLS},
        "no Eastmoney/THS public concept validation tool",
    )


def test_institutional_provider(runner: TestRunner):
    """测试机构持仓数据 Provider"""
    from data_provider.supply_chain import InstitutionalHoldingsProvider

    print("\n" + "=" * 60)
    print("2. 机构持仓数据 Provider (InstitutionalHoldingsProvider)")
    print("=" * 60)

    provider = InstitutionalHoldingsProvider()

    # 测试 Mock 数据
    mock_data = provider._get_mock_shareholders("600519")
    runner.record(
        "Mock 数据返回字典", isinstance(mock_data, dict), f"类型: {type(mock_data)}"
    )

    runner.record(
        "Mock 数据包含股东列表",
        "holders" in mock_data and isinstance(mock_data["holders"], list),
        f"股东数: {len(mock_data.get('holders', []))}",
    )

    runner.record(
        "Mock 数据包含机构持股比例",
        "institutional_ratio" in mock_data,
        f"比例: {mock_data.get('institutional_ratio', 0):.2f}%",
    )

    # 测试 Provider 实例化
    runner.record(
        "Provider 实例化成功", provider is not None, "InstitutionalHoldingsProvider()"
    )

    # 测试 calculate_institutional_score
    score_data = provider.calculate_institutional_score("600519")
    runner.record(
        "机构得分计算返回数据",
        isinstance(score_data, dict) and "score" in score_data,
        f"得分: {score_data.get('score', 'N/A')}",
    )

    # 测试得分范围
    if score_data:
        score = score_data.get("score", 0)
        runner.record("机构得分范围 0-100", 0 <= score <= 100, f"得分: {score}")


def test_northbound_provider(runner: TestRunner):
    """测试北向资金数据 Provider"""
    from data_provider.supply_chain import NorthboundFlowProvider

    print("\n" + "=" * 60)
    print("3. 北向资金数据 Provider (NorthboundFlowProvider)")
    print("=" * 60)

    provider = NorthboundFlowProvider()

    # 测试 Mock 数据
    mock_flows = provider._get_mock_northbound_flow()
    runner.record(
        "Mock 数据返回列表",
        isinstance(mock_flows, list) and len(mock_flows) > 0,
        f"类型: {type(mock_flows)}, 长度: {len(mock_flows)}",
    )

    runner.record(
        "Mock 数据包含必要字段",
        all(k in mock_flows[0] for k in ["date", "net_inflow"])
        if mock_flows
        else False,
        f"字段: {list(mock_flows[0].keys()) if mock_flows else 'N/A'}",
    )

    # 测试 Provider 实例化
    runner.record(
        "Provider 实例化成功", provider is not None, "NorthboundFlowProvider()"
    )

    # 测试 calculate_flow_score
    flow_score = provider.calculate_flow_score("600519", days=20)
    runner.record(
        "流量得分计算返回数据",
        isinstance(flow_score, dict) and "score" in flow_score,
        f"得分: {flow_score.get('score', 'N/A')}",
    )

    # 测试得分范围
    if flow_score:
        score = flow_score.get("score", 0)
        runner.record("流量得分范围 0-100", 0 <= score <= 100, f"得分: {score}")


def test_prompt_templates(runner: TestRunner):
    """测试 Prompt 模板"""
    from src.agents.prompts.supply_chain_prompts import (
        build_supply_chain_prompt,
        SUPPLY_CHAIN_ANALYSIS_PROMPT,
    )

    print("\n" + "=" * 60)
    print("4. 产业链 Prompt 模板")
    print("=" * 60)

    # 测试 Prompt 模板存在
    runner.record(
        "SUPPLY_CHAIN_ANALYSIS_PROMPT 存在",
        SUPPLY_CHAIN_ANALYSIS_PROMPT is not None
        and len(SUPPLY_CHAIN_ANALYSIS_PROMPT) > 0,
        f"长度: {len(SUPPLY_CHAIN_ANALYSIS_PROMPT)}",
    )

    # 测试 Prompt 包含关键字段
    key_fields = [
        "stock_code",
        "stock_name",
        "concept_board_data",
        "institutional_data",
    ]
    runner.record(
        "Prompt 包含关键占位符",
        all(f"{{{field}}}" in SUPPLY_CHAIN_ANALYSIS_PROMPT for field in key_fields),
        f"字段: {key_fields}",
    )

    # 测试 build_supply_chain_prompt 函数
    prompt = build_supply_chain_prompt(
        stock_code="600519",
        stock_name="贵州茅台",
        industry="白酒",
        main_business="茅台酒生产销售",
    )

    runner.record(
        "build_supply_chain_prompt 返回字符串",
        isinstance(prompt, str) and len(prompt) > 0,
        f"长度: {len(prompt)}",
    )

    runner.record("Prompt 包含股票代码", "600519" in prompt, "股票代码嵌入成功")

    runner.record("Prompt 包含股票名称", "贵州茅台" in prompt, "股票名称嵌入成功")

    # 测试带数据的 Prompt
    prompt_with_data = build_supply_chain_prompt(
        stock_code="600519",
        stock_name="贵州茅台",
        concept_boards=[
            {"name": "白酒", "change_pct": 2.5},
            {"name": "超级品牌", "change_pct": 1.8},
        ],
        institutional_data={
            "institutional_ratio": 61.26,
            "holders": [{"name": "茅台集团", "hold_ratio": 54.06}],
        },
    )

    runner.record(
        "Prompt 包含概念板块数据",
        "白酒" in prompt_with_data and "2.5" in prompt_with_data,
        "概念数据嵌入成功",
    )


def test_llm_service(runner: TestRunner):
    """测试 LLM 主观评分服务"""
    from src.services.llm_subjective_service import (
        LLMSubjectiveScoringService,
        get_llm_subjective_service,
    )

    print("\n" + "=" * 60)
    print("5. LLM 主观评分服务")
    print("=" * 60)

    # 测试服务实例化
    service = LLMSubjectiveScoringService()
    runner.record(
        "LLMSubjectiveScoringService 实例化成功",
        service is not None,
        "LLMSubjectiveScoringService()",
    )

    # 测试单例获取
    singleton = get_llm_subjective_service()
    runner.record(
        "get_llm_subjective_service 返回实例", singleton is not None, "单例模式正常"
    )

    # 测试 fallback 方法
    fallback_sc = service._get_fallback_supply_chain()
    runner.record(
        "_get_fallback_supply_chain 返回数据",
        isinstance(fallback_sc, dict) and "chain_position" in fallback_sc,
        f"chain_position: {fallback_sc.get('chain_position')}",
    )

    fallback_val = service._get_fallback_value()
    runner.record(
        "_get_fallback_value 返回数据",
        isinstance(fallback_val, dict) and "value_score" in fallback_val,
        f"value_score: {fallback_val.get('value_score')}",
    )

    # 测试 JSON 提取
    test_json = '{"chain_position": "bottleneck", "score": 85}'
    parsed = service._extract_json(test_json)
    runner.record(
        "_extract_json 正确解析", parsed == test_json, f"解析结果: {parsed[:50]}..."
    )

    # 测试解析带 markdown 的 JSON
    test_markdown = '```json\n{"chain_position": "midstream", "score": 60}\n```'
    parsed_md = service._extract_json(test_markdown)
    runner.record(
        "_extract_json 处理 markdown",
        "chain_position" in parsed_md,
        "Markdown JSON 解析成功",
    )


def test_scoring_integration(runner: TestRunner):
    """测试评分服务集成"""
    from src.services.research_scoring_service import ResearchScoringService

    print("\n" + "=" * 60)
    print("6. 评分服务 P2 集成")
    print("=" * 60)

    service = ResearchScoringService()

    # 测试 enrich_with_p2_data 方法存在
    runner.record(
        "enrich_with_p2_data 方法存在",
        hasattr(service, "enrich_with_p2_data"),
        "方法存在",
    )

    # 测试 process_with_p2_enrichment 方法存在
    runner.record(
        "process_with_p2_enrichment 方法存在",
        hasattr(service, "process_with_p2_enrichment"),
        "方法存在",
    )

    # 测试数据增强
    enriched = service.enrich_with_p2_data("600519", {})
    runner.record(
        "enrich_with_p2_data 返回数据",
        isinstance(enriched, dict),
        f"增强字段数: {len(enriched)}",
    )

    runner.record(
        "增强包含机构得分",
        "institutional_score" in enriched,
        f"institutional_score: {enriched.get('institutional_score')}",
    )

    runner.record(
        "增强包含概念板块得分",
        "concept_performance" in enriched or "concept_boards" in enriched,
        "概念数据增强成功",
    )

    # 测试完整流程
    result = service.process_with_p2_enrichment(
        stock_code="600519",
        stock_name="贵州茅台",
        market="cn",
        raw_data={
            "chain_position": "downstream",
            "moat_type": "brand",
            "moat_strength": "strong",
        },
        enrich_with_providers=True,
    )

    runner.record(
        "process_with_p2_enrichment 返回结果",
        isinstance(result, dict) and "framework_score" in result,
        "返回完整结果",
    )

    runner.record(
        "结果包含 P2 增强标记",
        "p2_enriched" in result,
        f"p2_enriched: {result.get('p2_enriched')}",
    )

    framework = result.get("framework_score", {})
    runner.record(
        "框架评分包含维度",
        "dimensions" in framework,
        f"维度数: {len(framework.get('dimensions', []))}",
    )


def test_frontend_component(runner: TestRunner):
    """测试前端组件"""
    import os

    print("\n" + "=" * 60)
    print("7. 前端组件验证")
    print("=" * 60)

    component_path = "/home/yu/pythonProjects/daily_stock_analysis/apps/dsa-web/src/components/report/ResearchFrameworkPanel.tsx"

    # 检查文件存在
    runner.record(
        "ResearchFrameworkPanel.tsx 文件存在",
        os.path.exists(component_path),
        f"路径: {component_path}",
    )

    if os.path.exists(component_path):
        with open(component_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 检查组件导出
        runner.record(
            "包含组件导出",
            "export const ResearchFrameworkPanel" in content
            or "export default" in content,
            "组件导出正确",
        )

        # 检查 Props 接口
        runner.record(
            "包含 ResearchFrameworkData 接口",
            "ResearchFrameworkData" in content,
            "数据类型定义存在",
        )

        # 检查关键 UI 元素
        ui_elements = ["六维总分", "贝叶斯分析", "仓位建议", "先验概率", "Edge"]
        found_elements = [elem for elem in ui_elements if elem in content]
        runner.record(
            "包含关键 UI 文本",
            len(found_elements) >= 3,
            f"找到 {len(found_elements)}/{len(ui_elements)} 个关键文本",
        )

        # 检查颜色映射函数
        runner.record(
            "包含得分颜色函数",
            "getScoreColor" in content and "getEdgeColor" in content,
            "颜色映射函数存在",
        )

        # 检查紧凑模式支持
        runner.record("支持紧凑模式", "compact" in content.lower(), "compact mode 支持")


def test_e2e_scenario(runner: TestRunner):
    """端到端场景测试"""
    print("\n" + "=" * 60)
    print("8. 端到端场景测试")
    print("=" * 60)

    from data_provider.supply_chain import InstitutionalHoldingsProvider, NorthboundFlowProvider
    from src.services.research_scoring_service import ResearchScoringService

    # 场景: 分析贵州茅台
    stock_code = "600519"
    stock_name = "贵州茅台"

    # Step 1: 获取数据
    ih = InstitutionalHoldingsProvider()
    nf = NorthboundFlowProvider()

    shareholders = ih._get_mock_shareholders(stock_code)
    flows = nf._get_mock_northbound_flow()

    runner.record(
        f"Step 1: 数据获取 - {len(shareholders.get('holders', []))} 个股东, {len(flows)} 天资金流",
        True,
        "数据获取成功",
    )

    # Step 2: 计算得分
    inst_score = ih.calculate_institutional_score(stock_code)
    flow_score = nf.calculate_flow_score(stock_code)

    runner.record(
        f"Step 2: 得分计算 - 机构{inst_score.get('score', 'N/A')}, 流量{flow_score.get('score', 'N/A')}",
        inst_score.get("score", 0) > 0 and flow_score.get("score", 0) > 0,
        "得分计算成功",
    )

    # Step 3: 完整评分流程
    service = ResearchScoringService()
    result = service.process_with_p2_enrichment(
        stock_code=stock_code,
        stock_name=stock_name,
        market="cn",
        raw_data={
            "chain_position": "downstream",
            "moat_type": "brand",
            "moat_strength": "strong",
            "us_china_risk": "low",
        },
        enrich_with_providers=True,
    )

    framework = result.get("framework_score", {})
    bayesian = result.get("bayesian_result", {})

    runner.record(
        f"Step 3: 评分结果 - 总分{framework.get('dimension_total', 'N/A')}, Edge{bayesian.get('edge', 'N/A')}",
        framework.get("dimension_total", 0) > 0,
        "评分流程成功",
    )

    # Step 4: 验证结果合理性
    dimension_total = framework.get("dimension_total", 0)
    edge = bayesian.get("edge", 0)

    runner.record(
        "Step 4: 维度总分范围合理 (0-100)",
        0 <= dimension_total <= 100,
        f"总分: {dimension_total}",
    )

    runner.record("Step 4: Edge 范围合理 (-1 to 1)", -1 <= edge <= 1, f"Edge: {edge}")

    runner.record(
        "Step 4: 仓位建议有效",
        bayesian.get("position_suggestion") in ["0-1%", "1-3%", "3-5%", "5-8%"],
        f"仓位: {bayesian.get('position_suggestion')}",
    )


def main():
    print("\n" + "=" * 70)
    print("P2 产业链数据 - 完整验证测试")
    print("=" * 70)
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    runner = TestRunner()

    # 执行所有测试
    test_concept_board_provider(runner)
    test_institutional_provider(runner)
    test_northbound_provider(runner)
    test_prompt_templates(runner)
    test_llm_service(runner)
    test_scoring_integration(runner)
    test_frontend_component(runner)
    test_e2e_scenario(runner)

    # 汇总
    print("\n" + "=" * 70)
    print("验证结果汇总")
    print("=" * 70)

    passed, failed = runner.summary()
    total = passed + failed

    print(f"\n总计: {total} 项测试")
    print(f"通过: {passed} 项 ✅")
    print(f"失败: {failed} 项 ❌")
    print(f"通过率: {passed / total * 100:.1f}%")

    # 失败详情
    if failed > 0:
        print("\n失败项详情:")
        for r in runner.results:
            if not r["passed"]:
                print(f"  ❌ {r['name']}: {r['message']}")

    # 结论
    print("\n" + "=" * 70)
    if failed == 0:
        print("🎉 P2 产业链数据验证全部通过！")
    else:
        print(f"⚠️  有 {failed} 项验证失败，请检查。")
    print("=" * 70)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
