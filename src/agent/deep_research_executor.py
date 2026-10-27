# -*- coding: utf-8 -*-
"""深度投研报告专属 Executor（五层穿透框架）。

独立于问股 AgentExecutor / 郑希 / 供应链 executor，但**复用**同一套基建：
- :func:`src.agent.runner.run_agent_loop` —— ReAct 工具调用循环（max_steps=30 长任务）
- :class:`src.agent.llm_adapter.LLMToolAdapter` —— 多渠道 LLM
- :class:`src.agent.deep_research_validator.DeepResearchValidator` —— 质量校验

与对话框式 executor 的差异：
- **表单一次性生成**，不做多轮会话 bundle（历史通过 storage.deep_research_reports 表持久化）。
- 暴露 ``generate(stock_code, stock_name, ...)`` 而非 ``chat(message, session_id, ...)``，
  便于 SSE 端点用 ``asyncio.to_thread`` 包装。
- 内置「降级」+「质量校验重生成」：步数耗尽不返回空报告；校验失败追加提示重跑一轮。

system prompt 运行时从 ``data/deep_research/system_prompt.md`` 读取，便于迭代不改代码。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

if TYPE_CHECKING:  # 避免 import 时强依赖 litellm（部署环境才有）
    from src.agent.llm_adapter import LLMToolAdapter
    from src.agent.runner import RunLoopResult
    from src.agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


_PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
_SYSTEM_PROMPT_PATH = os.path.join(
    _PROJECT_ROOT, "data", "deep_research", "system_prompt.md"
)


# system_prompt.md 缺失时的兜底（极简版，保证不崩）
_FALLBACK_SYSTEM_PROMPT = """你是 A 股投研总监。对指定股票生成深度投研报告，遵循「消息面+产业链主导」框架：
消息面与产业政策 → 产业链位置与瓶颈 → 国产替代 / 中美链 → 材料工艺独特地位 → 财务与估值 → 技术面（次要，≤15%）。
每章必含【结论】前置；国产替代/中美链先答适用性（强相关/低相关/不适用）；三情景概率和=100%；数字锚定所有观点，禁止编造。
（注：完整框架加载失败，正在使用兜底提示，请检查 data/deep_research/system_prompt.md）"""


def build_deep_research_system_prompt() -> str:
    """读取 data/deep_research/system_prompt.md 作为 system prompt。"""
    try:
        with open(_SYSTEM_PROMPT_PATH, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning(
            "[DeepResearch] system_prompt.md 读取失败 (%s): %s",
            _SYSTEM_PROMPT_PATH,
            exc,
        )
        return _FALLBACK_SYSTEM_PROMPT


@dataclass
class DeepResearchResult:
    """深度投研报告生成结果。"""

    success: bool = False
    status: str = "failed"  # success | partial | failed
    markdown: str = ""
    stock_code: str = ""
    stock_name: str = ""
    quality_score: int = 0  # 0-100，来自 validator
    missing_layers: List[str] = field(default_factory=list)
    tool_calls_log: List[Dict[str, Any]] = field(default_factory=list)
    total_steps: int = 0
    total_tokens: int = 0
    provider: str = ""
    error: Optional[str] = None


class DeepResearchExecutor:
    """深度投研报告专属 Agent。

    长任务配置：``max_steps=30``、``wall_clock=1200s``（五层穿透 + 报告生成 2–5 分钟）。
    """

    def __init__(
        self,
        tool_registry: "ToolRegistry",
        llm_adapter: "LLMToolAdapter",
        max_steps: int = 30,
        timeout_seconds: Optional[float] = 1200.0,
    ) -> None:
        self.tool_registry = tool_registry
        self.llm_adapter = llm_adapter
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def generate(
        self,
        stock_code: str,
        stock_name: str,
        report_type: str = "deep",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> DeepResearchResult:
        """生成一份深度投研报告（ReAct 工具循环 + 质量校验 + 降级）。"""
        from src.agent.deep_research_validator import DeepResearchValidator
        from src.agent.runner import run_agent_loop

        system_prompt = build_deep_research_system_prompt()
        user_message = self._build_user_message(stock_code, stock_name, report_type)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        if progress_callback:
            progress_callback(
                {
                    "type": "thinking",
                    "step": 0,
                    "message": (
                        f"开始对 {stock_name}（{stock_code}）进行"
                        "消息面+产业链主导的深度投研分析（技术面降权）..."
                    ),
                }
            )

        loop_result = run_agent_loop(
            messages=messages,
            tool_registry=self.tool_registry,
            llm_adapter=self.llm_adapter,
            max_steps=self.max_steps,
            progress_callback=progress_callback,
            max_wall_clock_seconds=self.timeout_seconds,
            stock_scope=None,
        )

        validator = DeepResearchValidator()

        # 基础内容判定 + 降级
        if loop_result.success and loop_result.content and loop_result.content.strip():
            markdown = self._strip_preamble(loop_result.content)
            status = "success"
            base_error: Optional[str] = None
        else:
            markdown = self._build_partial_report(loop_result, stock_code, stock_name)
            status = "partial" if markdown else "failed"
            base_error = loop_result.error or "报告生成未完成（步数/时长限制）"

        # 质量校验
        validation = validator.validate(markdown, loop_result.tool_calls_log)

        # L3 兜底：仅对 success 报告尝试重生成（partial/failed 不再重试，避免雪上加霜）
        if status == "success" and not validation.passed and validation.missing_layers:
            retry_result = self._retry_with_hints(
                messages, validation.missing_layers, progress_callback
            )
            if (
                retry_result is not None
                and retry_result.success
                and retry_result.content
            ):
                retry_validation = validator.validate(
                    retry_result.content, retry_result.tool_calls_log
                )
                # 只在重生成确实更好时采纳
                if retry_validation.score >= validation.score:
                    logger.info(
                        "[DeepResearch] 重生成采纳: score %d -> %d",
                        validation.score,
                        retry_validation.score,
                    )
                    markdown = self._strip_preamble(retry_result.content)
                    loop_result = retry_result
                    validation = retry_validation

        # 最终状态
        missing_layers = list(validation.missing_layers)
        if status == "success" and missing_layers:
            status = "partial"
            markdown = self._prepend_partial_warning(markdown, missing_layers)

        return DeepResearchResult(
            success=(status != "failed"),
            status=status,
            markdown=markdown,
            stock_code=stock_code,
            stock_name=stock_name,
            quality_score=validation.score,
            missing_layers=missing_layers,
            tool_calls_log=loop_result.tool_calls_log or [],
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
            provider=loop_result.provider or "",
            error=None if status != "failed" else base_error,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @staticmethod
    def _build_user_message(stock_code: str, stock_name: str, report_type: str) -> str:
        """按 docs/deep_research-chain-news-logic-plan.md 调整：

        - 框架由「五层穿透」改为「消息面 + 产业链 + 国产替代 + 中美链 + 财务估值 + 技术辅助」
        - 数据收集顺序：消息面 → 产业链 → 国产替代 / 中美链 → 财务估值 → 技术辅助
        - 国产替代 / 中美链先判适用性；低相关行业不硬套
        - 技术面降权（≤ 15% 篇幅，不得推翻产业链主判断）
        - 报告骨架严格按 system prompt 的新八章（投资结论 + 一至八）
        """
        return (
            f"请对 A 股 **{stock_name}（{stock_code}）** 生成一份完整的深度投研报告（{report_type}）。\n\n"
            "分析框架（消息面 + 产业链主导，技术面降权）：\n"
            "1. **按下列顺序收集数据**：消息面与产业政策 → 产业链位置与瓶颈 → 国产替代 / 中美链 → "
            "材料工艺独特地位 → 财务与估值 → 技术面节奏（次要）；\n"
            "2. **必须先调工具**（按章节匹配）：\n"
            "   - 消息面：`search_stock_news` 或 `search_comprehensive_intel`（**且** `get_market_indices`）\n"
            "   - 产业链：`get_sector_rankings`（**且** `search_comprehensive_intel`）；概念归属取 `get_stock_info` 的 iFinD 最新概念，概念标签不是客户事实\n"
            "   - 财务估值：`get_stock_info`\n"
            "   - 技术面：`analyze_trend` / `get_capital_flow` 满足其一即可，**不调用不阻断**\n"
            "3. **国产替代 / 中美链必须先答适用性**：\n"
            "   - 强相关行业（半导体/AI算力/光模块/CPO/芯片设备材料/新能源车/锂电/光伏/储能/高端制造/机器人/军工/工业母机/医药器械/创新药关键设备材料/工业软件/信创/基础材料/关键零部件）：必须展开，**必须标注替代阶段** `传闻/送样/验证/导入/量产`；\n"
            "   - 低相关行业（白酒/银行/保险/传统地产/公用事业/纯消费品牌）：固定写「国产替代 / 中美平行链：低相关。本公司核心逻辑不来自进口替代或中美链重构，报告不强行套用」，**不展开**；\n"
            "   - **不得**把社区传闻或媒体标题写成「已替代」「已导入」「已量产」；\n"
            "4. **来源等级必标**：所有具体客户/订单/份额/产能/价格/毛利率/客户名/供应商名必须带 `primary / news / industry / inferred / unverified` 等级标签；F10 聚合页和二手引用不得标 primary，推断必须写明依据；媒体标题只做线索，不得单独支撑「确认/实锤/已导入/已量产」；不要求社区来源标签或固定品牌双源验证字样；\n"
            "5. **技术面降权（强制）**：技术面章节**≤ 报告主体 15% 篇幅**；技术面**不得推翻产业链主判断**，只回答「当前价格位置是否适合建仓/等待」；\n"
            "6. **结论前置**：每个一级章节首句用 `**【结论】**` 起头；\n"
            "7. **三情景**（牛市/基准/熊市）概率和严格 = 100%；\n"
            "8. **数字锚定**：所有观点用数字（PE/PB/ROE/增速/占比/价格/概率）支撑，取不到的数据标「数据缺失」或 `inferred`（必须写明推断依据），**禁止编造**；\n"
            "9. **PE 估值口径一致性**：报告头部声明的【当前 PE(TTM)】是估值基准锚点，牛市/溢价/抬升/估值扩张情景的 PE 上限必须 ≥ 当前 PE(TTM)，否则改用「回归/消化/估值压缩」表述并说明 EPS 增速如何吸收估值下降。\n\n"
            "数据收集充分后，按 system prompt 的新八章骨架输出完整 Markdown 报告：投资结论 + 一、消息面与产业政策 + "
            "二、产业链位置与瓶颈环节 + 三、国产替代与平行替换 + 四、材料/工艺/环节独特地位 + 五、中美产业链关系 + "
            "六、财务与估值验证 + 七、技术面节奏（次要）+ 八、风险、证伪条件与下一步验证。"
        )

    @staticmethod
    def _strip_preamble(markdown: str) -> str:
        """截掉报告正文前的 LLM 过渡句/思考句。

        system prompt 要求最终回答首字符为 ``#``（一级标题），但 LLM 偶尔会在
        标题前泄漏过渡句（如「数据已收集，现在输出报告」「新闻不可用，但…」）。
        本方法兜底：定位第一个以 ``# `` 开头的行，截掉其前所有内容；若无标题行
        则原样返回（避免误删无标题的 partial 报告）。
        """
        if not markdown or not markdown.strip():
            return markdown
        lines = markdown.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("# "):
                return "\n".join(lines[i:]) if i > 0 else markdown
        return markdown

    def _retry_with_hints(
        self,
        messages: List[Dict[str, Any]],
        missing_layers: List[str],
        progress_callback: Optional[Callable[[Dict[str, Any]], None]],
    ) -> "Optional[RunLoopResult]":
        """质量校验失败后，追加提示重跑一轮。返回 None 表示重生成异常。"""
        from src.agent.runner import run_agent_loop

        hint = (
            "你的报告未通过深度投研质量校验，以下层次的分析不充分或缺失："
            f"{', '.join(missing_layers)}。"
            "请调用必要工具补充这些层次的完整分析，然后重新输出**完整的**深度投研报告"
            "（必须包含全部新八章：投资结论 + 一~八，保留已写好的部分并补全缺失部分）。"
            "注意：技术面节奏层（第七层）允许不调用工具，只降分不阻断；"
            "国产替代 / 中美链必须先答「适用性判断（强相关 / 低相关 / 不适用）」。"
        )
        retry_messages = list(messages)
        retry_messages.append({"role": "user", "content": hint})

        if progress_callback:
            progress_callback(
                {
                    "type": "thinking",
                    "step": 0,
                    "message": (
                        f"质量校验发现 {', '.join(missing_layers)} 层不足，正在补充重生成..."
                    ),
                }
            )

        try:
            return run_agent_loop(
                messages=retry_messages,
                tool_registry=self.tool_registry,
                llm_adapter=self.llm_adapter,
                max_steps=min(self.max_steps, 12),
                progress_callback=progress_callback,
                max_wall_clock_seconds=min(
                    float(self.timeout_seconds or 1200.0), 360.0
                ),
                stock_scope=None,
            )
        except Exception as exc:
            logger.warning("[DeepResearch] 重生成异常: %s", exc)
            return None

    def _build_partial_report(
        self,
        loop_result: "RunLoopResult",
        stock_code: str,
        stock_name: str,
    ) -> str:
        """步数耗尽/失败时，从已收集数据构建部分报告（不返回空）。"""
        # 优先用已有内容
        if loop_result.content and loop_result.content.strip():
            return loop_result.content

        tools_used = sorted(
            {
                str(entry.get("tool", ""))
                for entry in (loop_result.tool_calls_log or [])
                if isinstance(entry, dict) and entry.get("tool")
            }
        )
        summary = self._summarize_collected_data(loop_result)
        if summary == "（未收集到有效数据）":
            return ""
        return (
            f"# {stock_name}（{stock_code}）深度投研报告（未完成）\n\n"
            f"> ⚠️ **本次研究尚未完整完成。** "
            f"以下为已收集数据的初步整理，建议稍后重新生成获取完整报告。\n\n"
            f"**分析状态**：{loop_result.error or '达到最大步数限制'}\n\n"
            f"**已调用工具（{len(tools_used)} 个）**：{', '.join(tools_used) if tools_used else '无'}\n\n"
            f"---\n\n## 已收集数据摘要\n\n{summary}\n"
        )

    @staticmethod
    def _summarize_collected_data(loop_result: "RunLoopResult") -> str:
        """从 messages 中 role=tool 的内容提取摘要（每个截断防爆）。"""
        import json
        chunks: List[str] = []
        from src.core.research_run import current_research
        if run := current_research.get():
            for facts in run.facts.values():
                for fact in facts:
                    chunks.append(f"{fact['label']}：{fact['value']} {fact['unit']}；来源 {fact['source']}，{fact['period'] or '时点未明确'}；{fact['qualification']}（未完成模型解读，不参与评分）。")
            for items in run.evidence.values():
                for item in items:
                    chunks.append(f"[{item['title']}]({item['url']})（{item['source']}）：{item['snippet']}")
        for msg in loop_result.messages or []:
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            try:
                payload = json.loads(str(msg.get("content") or ""))
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("error") or payload.get("success") is False:
                continue
            facts = {key: payload[key] for key in ("name", "stock_name", "price", "source", "summary")
                     if isinstance(payload.get(key), (str, int, float))}
            if facts:
                chunks.append("；".join(f"{key}：{value}" for key, value in facts.items())[:500])
        if not chunks:
            return "（未收集到有效数据）"
        # 最多 10 条，防爆 context
        return "\n\n".join(chunks[:10])

    @staticmethod
    def _prepend_partial_warning(markdown: str, missing_layers: List[str]) -> str:
        """在报告顶部插入质量提示横幅。"""
        warning = (
            f"> ⚠️ **质量提示**：本报告以下层次分析不充分：{', '.join(missing_layers)}。"
            "建议结合其他信息源交叉验证，或重新生成。\n\n---\n\n"
        )
        return warning + markdown
