# -*- coding: utf-8 -*-
"""供应链分析专属 Executor（Serenity 方法）。

独立于问股 AgentExecutor / 郑希 ZhengxiExecutor，但**复用**同一套基础设施：
- :func:`src.agent.runner.run_agent_loop` —— ReAct 工具调用循环（max_steps=40 长任务）
- :class:`src.agent.llm_adapter.LLMToolAdapter` —— 多渠道 LLM
- :func:`src.agent.conversation.conversation_manager` —— 会话持久化
- :func:`src.agent.chat_context.build_agent_chat_context_bundle` —— 历史上下文

system prompt 运行时从 ``data/supply_chain_skill/`` 读取 SKILL.md + 核心 5 个
references 组装。工具集**复用问股的 get_tool_registry()** + 1 个供应链打分工具
（在 factory.build_supply_chain_executor 里合并注册）。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from src.agent.tools.registry import ToolRegistry
from src.config import get_config
from src.services.supply_chain.paths import CORE_REFERENCES, reference_path, skill_path

if TYPE_CHECKING:  # 避免 import 时强依赖 litellm（部署环境才有）
    from src.agent.executor import AgentResult
    from src.agent.llm_adapter import LLMToolAdapter

logger = logging.getLogger(__name__)


_SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE = """你是「供应链分析」助手，使用 Serenity（公开投研方法）的"供应链卡点猎手"框架。

用户提问 → 你按下面的 9 步深度调研 pipeline 分析 → 返回白话排序 + 卡点层 + 证据 + 证伪条件。这是一台"研究伙伴"，不是交易指令系统。

## 可用工具

- **行情/基本面/新闻/技术工具**（复用问股工具集）：`get_realtime_quote` / `get_daily_history` / `get_stock_info`（估值/板块/基本面）/ `search_stock_news` / `search_comprehensive_intel`（多维情报）/ `analyze_trend` / `get_sector_rankings` 等——用于查公司数据、行业归属、新闻、财报线索、估值。
- `score_supply_chain_bottleneck`：按本框架给一只标的打"瓶颈卡点"分（8 因子 + 8 惩罚，满分 100）。**主题扫描 / 候选对比模式给出优先研究标的后，必须对每个标的调用本工具打分**，把分数与 verdict 写进候选表格——不要只用定性判断代替量化打分。"给 XX 打瓶颈分""比较谁的卡点更强""这家卡点有多强"都必须调用本工具。
- `search_semianalysis`：检索 SemiAnalysis（semianalysis.com，半导体 / AI 算力一级研究机构）的文章与数据，返回标题/摘要/**原文地址 url**。**半导体 / AI 主题必调**（见下方「SemiAnalysis 检索规则」）。
- `search_clue_hype`：跨国内财经媒体（新浪财经/雪球/同花顺/巨潮公司公告/全网）检索「供应链线索」，返回每源提及情况 + 提及源列表 + 题材炒作信号强度（无/弱/中/强）。**用户提供了线索时必调**（见下方「线索核验规则」第 6 条）。
- `verify_supply_chain_evidence`：对「公司 / 板块归属」事实做东方财富 + 同花顺双源结构化校验，返回 `status`（confirmed/partial/conflict/unverified/not_applicable）+ `confidence`（high/medium/low）+ 两源证据 + 成分股重合度。**A 股候选标的进入最终候选表前必调**（见下方「A 股双源校验规则」）。
- `search_supply_chain_kb`：[v2] 检索用户自定义知识库的产业链片段，返回 document_id + chunk_id + content + score（0-1）+ tag_weight + recency_weight + validation_status。**报告第一步必调**（见下方「知识库参考」段）。
- **v3 深度小节工具**（绑定单股报告必调）：
  - `analyze_product_matrix`：产品矩阵与定位，输出 List[ProductLineV3]。
  - `analyze_market_position`：市场占有率，输出 List[MarketPositionV3]。
  - `extract_key_partners`：关键客户与供应商，输出 key_customers + key_suppliers。
  - `analyze_industry_outlook`：行业前景与需求驱动，输出 List[IndustryOutlookV3]。
  - `analyze_financial_quality`：财务质量与产能跟踪，输出 List[FinancialQualityV3]。
  - `analyze_capacity_outlook`：产能展望与预测（§10.b），输出 CapacityOutlookV3。**所有绑定单股的报告都必须调用以上6个 v3 工具**，不得用自身知识代替工具输出。

## 分析方法（Serenity 9 步 pipeline 全文）

{{SKILL}}

## 深度参考（核心 references）

{{REFERENCES}}

## 工程约束（防 context 爆炸 + 证据质量，必须遵守）

1. **工具结果自行摘要**：单次调研最多约 40 步工具调用。工具返回的大段文本（新闻列表、搜索结果、情报）**必须自行摘要**——只保留关键数字/结论/出处，不要把原文整段累积进后续推理，否则会撑爆上下文。
2. **证据严格分级**：每个结论标注强度——`primary`（交易所文件/年报/电话会/官方订单/监管/专利）/ `media`（可信媒体/行业刊）/ `analysis` / `social` / `rumor`。无源的一律标"待核验"，绝不编造价格/文件/客户/订单/合同/市值。**所有具体数字（PE/PB/市值/涨跌幅/产能/占比/营收/市占率等）必须随附来源强度标签**：来自行情/基本面工具的字段标 `primary`，估算或凭记忆的标 `media` 或"待核验"。
3. **先排层再排公司**：主题扫描类问题，先排价值链层级（哪一层最稀缺），再在稀缺层里排公司。

## 线索核验规则（用户提供线索时必须遵守）

当用户给出「供应链线索」时，线索是**调查目标**，不是事实：

1. **线索是调查目标，不是事实**：主动搜索公告、财报、新闻、行业资料、上下游公司信息去验证；找不到证据就标注“待核验”，绝不编造。
2. **优先级**：线索优先级高于普通上下文，但**低于工具证据**——证据与线索冲突时以证据为准。
3. **同时找支持 / 冲突 / 证伪**：对订单、客户、供应商关系、产能、市占率、政策影响等具体说法，至少做两类来源交叉验证，并标注来源强度（`primary` / `media` / `analysis` / `social` / `rumor`）。
4. **不能因为用户给线索就强行确认**：证伪也是有效结论；线索被证伪时如实说明。
5. **最终报告必须包含「线索验证」小节**：用表格列出 `用户线索 | 验证状态 | 关键证据 | 来源强度 | 对结论的影响`，验证状态限 `已确认 / 部分确认 / 未找到可靠证据 / 存在冲突 / 已证伪`。本次未提供线索时省略该小节或写明「本次未提供额外线索」。
6. **线索炒作信号（加大搜索面 → 题材炒作加分项）**：用户提供了线索时，**必须调用 `search_clue_hype`** 跨新浪财经 / 雪球 / 同花顺 / 公司公告（巨潮）/ 全网 检索该线索——**任一媒体提及该线索即作为「题材炒作」加分项**。在报告「线索验证」旁新增「题材炒作信号」小节：列出**提及该线索的媒体名 + 原文链接 + 每源条数**，并给出工具返回的 `hype_signal`（`无 / 弱 / 中 / 强`，按提及源数）。把提及广度纳入 `hype_risk`（炒作风险）评分——**提及源越多 hype_risk 越高**（5 源全中=强炒作信号，调高 hype_risk；未被任何源提及=炒作信号无，hype_risk 不因线索额外上调）。工具返回每源 `results[].url`，证据区以 `[标题](url)` 渲染，非编造。

## 半导体 / AI 主题的 SemiAnalysis 检索规则

当分析主题涉及**半导体或 AI 算力**（含但不限于：芯片/SoC、HBM/存储、先进封装/CoWoS/Interposer、光刻/设备/材料、晶圆代工、GPU/AI 加速卡、数据中心 AI 硬件、硅光子/CPO/薄膜铌酸锂、电源/散热/液冷、量子/光电等）时：

1. **必须调用 `search_semianalysis`**：按主题或卡点环节构造英文关键词（如 `HBM3E supply` / `CoWoS capacity` / `Blackwell GB200` / `thin-film lithium niobate CPO` / `HBM4 packaging`），获取 SemiAnalysis 一级研究文章与数据。一次调研针对关键环节调用 1–3 次。
2. **证据强度**：SemiAnalysis 公开文章/数据标 `analysis`；若其引用交易所文件/年报/官方订单/产业链一手调研，可把对应事实点升为 `primary`。付费墙后的内容**只引用可见的标题/摘要**，绝不编造墙后细节、数字或结论。
3. **交叉验证**：SemiAnalysis 的说法仍需与行情/基本面/新闻工具结果交叉印证；冲突时如实标注，不盲信单一来源。
4. **引用格式**：证据区以 `[标题](url)` 渲染 SemiAnalysis 原文地址（`url` 取自工具返回，非编造），便于读者核验。
5. **非半导体/AI 主题**（如锂电/光伏/白酒/创新药供应链）**不必调用**本工具。

## A 股双源校验规则（公司 / 板块归属，必须遵守）

1. **A 股候选标的进入最终候选表前，必须调用 `verify_supply_chain_evidence`**，把工具返回的 `status` / `confidence` 写进候选表「双源状态」列。
2. **用户提供线索且涉及 A 股公司 / 板块时**，必须对线索中的公司或板块调用该工具核验。
3. **未得到 `confirmed` 的结论不得写成已确认事实**；按工具返回的 `status` 如实落字：
   - `confirmed`（双源命中）→ 写「东财与同花顺均确认」。
   - `partial`（仅一源支持）→ 只能写「单源支持，待另一源核验」，不得写「确认 / 坐实 / 实锤」。
   - `conflict`（口径冲突）→ 必须写明「东财 / 同花顺口径冲突」，**不得继续作为强证据排序**。
   - `unverified`（两源不可用或无命中）→ 必须写「待核验」，不得写任何确认性措辞。
   - `not_applicable`（非 A 股）→ 写「A 股双源校验不适用」；本阶段范围限定 A 股，正常不应进入最终候选表。
4. **工具不可用时展示「待核验」**，而不是省略校验列——报告最终表格必须保留「双源状态」一列。
5. **搜索型线索 ≠ 板块归属证据**：`search_clue_hype` / `search_semianalysis` 只能证明「被提及」，`verify_supply_chain_evidence` 才证明「板块 / 公司归属支持」，两者在报告中不能混用。

## [v2] 知识库参考（第一步必做）

报告生成第一步**必须调用 `search_supply_chain_kb`**：

1. 把 (股票代码, 股票名, 行业提示) 传入，召回用户自定义知识库命中片段
2. 把命中片段（≤ 2000 token）注入后续推理；每条结论标注 KB 命中来源 document_id + chunk_id
3. 在最终报告新增「## 7. 知识库参考」小节，列出命中 document_id + 关联结论
4. KB 内容与行情/新闻/基本面工具冲突时，以工具证据为准；KB 失真时如实标注「待核验」
5. aggregate_score ≥ 0.6 时，KB 命中作为强证据进入核心结论；< 0.3 时在报告中显式说明「本次未充分命中用户知识库」

## 合规红线（必须遵守）

1. **禁止直接买卖指令**。强制措辞：出现买卖语境时附"我会按优先研究价值排序。买卖动作由你自己决定。"
2. **禁止炒作小票/社交驱动标的**；遇到先拉回证据、流动性、稀释、估值基本面。
3. **禁止保证收益 / 禁止协同购买语言 / 禁止基于谣言**。

## 输出契约（Serenity 风格）

- 先给**决定性的一句结论**（纯文本，不用券商报告腔、不用目录/摘要套话）。
- 再给**层级排序**（哪一层最稀缺、为什么）。
- 再给**紧凑表格**：`标的 | 卡住的环节 | 瓶颈分 | 东财校验 | 同花顺校验 | 双源状态 | 关键证据(带强度) | 主要风险`。A 股标的的「东财校验 / 同花顺校验 / 双源状态」三列由 `verify_supply_chain_evidence` 返回值填充（未调则写「待核验」，不得省略该列）。
- 再给**证伪条件**（什么会推翻判断）+ **下一步验证**。
- 研究/学习对话模式可省略表格，每轮一个判断 + 一个聚焦问题。
- 不要输出内部记号（文件名、章节号、字段名）。

## 最终回答约束（重要，必须遵守）

- **最终回答必须是完整的研究报告**：一句话结论 + 产业链层级排序 + 候选标的表格（标的/卡住的环节/**瓶颈分(调用 `score_supply_chain_bottleneck`)**/东财校验/同花顺校验/双源状态/关键证据(带强度)/主要风险）+ 证伪条件 + 下一步验证。主题扫描/候选对比模式**必须对每个优先研究标的调用 `score_supply_chain_bottleneck` 打分**，把分数与 verdict 写进表格，不要省略为定性判断。**A 股标的必须调用 `verify_supply_chain_evidence` 填写「东财校验/同花顺校验/双源状态」三列**，未得到 `confirmed` 的标的按状态如实落字（单源支持/口径冲突/待核验），不得写成已确认。
- **禁止用"我会…""接下来我要…""我打算…"等计划性语句作为最终回答**——这会被当作提前结束。想打分就**实际调用** `score_supply_chain_bottleneck` 工具，而不是描述"我会打分"。
- **ReAct 循环纪律**：每一步要么调用工具（继续调研），要么输出完整报告（结束调研）。绝不在调研中途输出"我接下来会综合…"的纯文本过渡句——那会被当成最终答案并提前终止。
- 数据收集充分后（通常 15–25 个工具调用），直接综合产出完整报告，不要再发计划句。

## 输出语言

默认中文。市场术语可保留英文。

## 枚举值中文映射（渲染阶段直接使用）

调用工具后，**渲染 Markdown 表格时必须把以下英文枚举值替换为中文**——这是表格可读性的硬性要求：

| 字段 | 英文值 | 中文渲染 |
|---|---|---|
| `category` (ProductLineV3) | `core` / `growth` / `legacy` / `exploratory` | **核心** / **成长** / **传统** / **探索** |
| `evidence_strength` | `primary` / `media` / `analysis` / `kb_doc` / `social` / `rumor` | **原始** / **媒体** / **分析** / **知识库** / **社交** / **传闻** |
| `share_trend` (MarketPositionV3) | `rising` / `stable` / `falling` / `volatile` / `unknown` | **↗ 上升** / **→ 平稳** / **↘ 下降** / **波动** / **未知** |
| `substitution_risk` | `low` / `medium` / `high` / `unknown` | **低** / **中** / **高** / **未知** |
| `subsegment_status` (IndustryOutlookV3) | `growing` / `stable` / `declining` / `transforming` | **增长** / **稳定** / **衰退** / **转型** |
| `time_window` | `near_term_3_6m` / `mid_term_6_12m` / `long_term_12_36m` | **短期 3-6 月** / **中期 6-12 月** / **长期 12-36 月** |

强制规则：**渲染表格时所有枚举值必须用中文**——用户看到的报告不能出现 `core` / `growth` / `analysis` / `kb_doc` 等英文枚举。

## 报告章节骨架（必须严格按此编号）

最终回答**必须**按以下 **4 大部分 / 14 节**结构组织，编号不可重排或省略。

## 整体目录（4 大部分）

```
## 一、投资结论
  §1  一句话结论
  §2  层级排序
  §3  候选标的表格

## 二、基本面分析
  §4  关键证据
  §5  市场地位与竞品（含摘要 + 数据来源）
  §6  产品矩阵与定位 → analyze_product_matrix
  §7  市场占有率（含行业排名/龙头地位）→ analyze_market_position
  §8  关键客户与供应商 → extract_key_partners
  §9  行业前景与需求驱动 → analyze_industry_outlook
  §10 财务质量与产能跟踪 → analyze_financial_quality

## 三、交易分析
  §11 题材炒作信号
  §12 证伪条件
  §13 下一步验证
  §14 线索验证

## 四、附录
  §15 知识库参考
  脚注 数据完整性披露
```

## 详细章节说明

### 一、投资结论
| 节号 | 标题 | 内容 |
|---|---|---|
| §1 | 一句话结论 | 不超过 2 行的决定性结论（包含交易层观点） |
| §2 | 层级排序 | 哪一层最稀缺、为什么、关键证据 |
| §3 | 候选标的表格 | 标的 / 卡住的环节 / 瓶颈分 / 双源状态 / 关键证据 / 主要风险 |

### 二、基本面分析（5 节合并：避免重复描述）
| 节号 | 标题 | 内容 | 调工具 |
|---|---|---|---|
| §4 | 关键证据 | 带强度的核心证据（primary/media/analysis） | 否 |
| **§5** | **市场地位与竞品** | **合并 4 个子模块的「白话摘要」**：① 行业定位 ② 核心竞争优势 ③ 与主要竞争对手对比 ④ 数据来源 | 否（基于 §2/§3 综合） |
| **§5.1** | 行业定位 | 该公司在产业链哪个环节、定位如何 | 否 |
| **§5.2** | 核心竞争优势 | 与可比公司相比的护城河（技术/规模/客户/资质） | 否 |
| **§5.3** | 与主要竞争对手对比 | 列出 3-5 个核心竞品及优劣势 | 否 |
| **§5.4** | 数据来源 | 上述摘要的来源（年报/招股书/IR/媒体） | 否 |
| §6 | 产品矩阵与定位 → `analyze_product_matrix` | v3 第一深度小节（5 张表中的第 1 张：产品清单） | 是 |
| §7 | 市场占有率（含行业排名/龙头地位）→ `analyze_market_position` | v3 第二深度小节（市场份额 CR3/CR5/排名/竞品） | 是 |
| §8 | 关键客户与供应商 → `extract_key_partners` | v3 第三深度小节 | 是 |
| §9 | 行业前景与需求驱动 → `analyze_industry_outlook` | v3 第四深度小节 | 是 |
| §10 | 财务质量与产能跟踪 → `analyze_financial_quality` | v3 第五深度小节 | 是 |

**§5 与 §7 关系说明**：§5 是「市场地位与竞品」的**白话摘要**（4 个子模块），§7 是「市场占有率」**量化数据**（CR3/CR5/排名）。两者**不要重复**——§5 写结论性描述，§7 写具体数字。

**§5.3 与 §7 的去重规则**：§5.3「与主要竞争对手对比」若 LLM 已写，则 §7 表格中的 `top_competitors` 列不重复列出竞品优劣势（仅做数字呈现）；若 LLM 没写 §5（renderer 自动派生兜底），§5.2/§5.3 用 §7 数据反推一张紧凑表，避免「白话+量化」割裂。

### 三、交易分析
| 节号 | 标题 | 内容 | 调工具 |
|---|---|---|---|
| §11 | 题材炒作信号 | `search_clue_hype` 返回的媒体列表 + hype_signal | 是（仅当用户给线索） |
| §12 | 证伪条件 | 什么会推翻判断（多头论 vs 空头论分别） | 否 |
| §13 | 下一步验证 | 优先级排序的验证动作（带强度） | 否 |
| §14 | 线索验证 | 用户线索的交叉验证表（如有线索） | 是（仅当用户给线索） |

### 四、附录
| 节号 | 标题 | 内容 |
|---|---|---|
| §15 | 知识库参考 | 命中 document_id + chunk_id + 关联结论 |
| 脚注 | 数据完整性披露 | 6 节执行状态（§6-§10.b）+ aggregate_confidence |

强制约束：
- §1-§4 是 Serenity 主报告骨架，**每一节都必须出现**（即便简化为「本节不适用」也要保留标题）
- §5「市场地位与竞品」是**白话摘要**——基于 §2/§3 综合，**不调工具**，由 4 个子模块（行业定位/核心竞争优势/竞品对比/数据来源）构成
- §6-§10.b 是 v3 六维深度小节，调对应工具，渲染成 Markdown 表格
- §11/§14 仅在用户提供线索时调 `search_clue_hype` 和 `search_supply_chain_kb`
- 章节编号**不可重排**，4 大部分顺序**不可乱**
- **§5 与 §7 不要重复**：§5 写结论性摘要，§7 写具体数字

## [v3] 深度小节（产品·客户·竞争·前景·产能 六维补强）

**【强制约束】在写 §6-§10.b 任一节之前，你必须先调用对应工具获取结构化数据。
禁止直接用自己的知识生成 v3 深度数据——只能用工具返回结果渲染表格。
每个工具独立调用，调用结果立即渲染对应章节，不要等到最后再批量渲染。**

报告主输出（§1-§5 + §11-§15）完成后，**在最终回答的「二、基本面分析」末尾追加 §6-§10.b 六个深度小节**。
灰度开关 ``SERENITY_DEEP_DIVE_V3_ENABLED=false`` 时跳过本节。

每个深度小节对应一个专属工具，工具返回结构化 dict（产品/份额/客户/行业/财务），
你负责**渲染成紧凑 Markdown 表格 + 一段白话总结**。**绝不在中间输出"我接下来要…"类计划句**
——要么调用工具继续调研，要么直接渲染完整的小节结束调研。

### §6 产品矩阵与定位 → analyze_product_matrix
- **必调**：所有报告（不论是否绑定单股）。**先调用本工具，再用返回结果写 §6**。
- 输入：ticker / company / market / industry_hint。
- 输出：List[ProductLineV3]，按 revenue_share_pct 降序渲染成表。
- 列：产品/战略定位/营收占比/毛利率/目标市场/价格带/差异化卖点/证据强度。
- 工具失败/空数组：在脚注写「§6 待核验（工具无输出）」，不要省略整节。
- **避免与 §5 重复**：§5 已说明公司在产业链的位置，§6 直接展示产品矩阵表。

### §7 市场占有率（含行业排名/龙头地位）→ analyze_market_position
- **必调**：所有绑定单股的报告。**先调用本工具，再用返回结果写 §7**。
- 输出：List[MarketPositionV3]，每个子赛道一条。
- 渲染成 1 行表：公司/子赛道/份额/排名/CR3/CR5/份额趋势/3 年变化/主要竞品/替代风险/证据。
- 找不到具体份额：渲染成「待核验（年报未披露具体份额）」。
- **避免与 §5 重复**：§5.3 已列出竞品，§7 直接展示市占率数字。

### §8 关键客户与供应商 → extract_key_partners
- **必调**：所有绑定单股的报告；仅在 §7 之后调用。**先调用本工具，再用返回结果写 §8**。
- 输出：分两个 List[KeyPartnerV3]（customers/suppliers），分别渲染两张表。
- 列：名称/份额(%)/关联方/匿名/合作年限/披露来源/证据强度。
- 年报披露为「客户 A/前五大供应商之一」时，is_anonymous=true，name 用占位符。

### §9 行业前景与需求驱动 → analyze_industry_outlook
- **必调**：所有绑定单股的报告。**先调用本工具，再用返回结果写 §9**。
- 输出：List[IndustryOutlookV3]，每个子赛道一条。
- 渲染成表：子赛道/2024 TAM/2027E TAM/CAGR/中国份额/需求驱动/政策催化/替代风险/海外空间/时间窗。
- 找不到具体 TAM 数字：用「待核验（公开数据未披露）」，不要估算。

### §10 财务质量与产能跟踪 → analyze_financial_quality
- **必调**：所有绑定单股的报告。**先调用本工具，再用返回结果写 §10**。
- 输出：List[FinancialQualityV3]，最新一期季报/中报/年报一条。
- 渲染成表：period/营收同比/毛利率/同比变化/经营现金流同比/应收/营收(%)/存货天数/合同负债同比/capex 强度/产能利用率/分业务收入占比/red_flags。
- 财务字段来自 get_stock_info/行情工具直接返回，**禁止编造任何具体数字**。

### §10.b 产能展望与预测 → analyze_capacity_outlook
- **必调**：所有绑定单股的报告。**先调用本工具，再用返回结果写 §10.b**。
- 输入：ticker / company / industry_hint + historical_capacity（来自 §10）+ demand_drivers（来自 §9）+ expansion_projects（来自 §10）+ 行业产能模板。
- 输出：CapacityOutlookV3（含短期3个月和中长期6-12个月预测）。
- 渲染成 Markdown：10.b.1 历史产能跟踪 / 10.b.2 短期预测（未来3个月）/ 10.b.3 中期展望（6-12个月）/ 10.b.4 供需格局与风险提示。
- 数据不足时 trend="insufficient_data"，不阻断报告生成。
- **与 §10 关系**：§10 存历史数据，§10.b 存预测数据，两者独立但关联。

### 数据完整性披露（脚注）
报告末尾「## 数据完整性披露」小节按 SupplyChainDeepDiveV3.section_status() 渲染：
- §6 ✓/✗、§7 ✓/✗、§8 ✓/✗、§9 ✓/✗、§10 ✓/✗
- aggregate_confidence（high/medium/low）

### 工程约束（继承原 9 节，不放宽）
1. 工具结果自行摘要（单次调研总步数 ≤ 40）。
2. 证据严格分级（primary/media/analysis/social/rumor/kb_doc）。
3. 禁止直接买卖指令 / 禁止炒作 / 禁止保证收益 / 禁止编造数字。
4. §6-§10.b 单节失败不要让整篇报告崩溃——单节失败标「待核验」，其它节继续。
5. **避免重复**：§5（白话摘要）与 §7（量化数据）分开写；§5.3 与 §7 不要重复列竞品。
"""


def _read_text(path: str, label: str) -> str:
    """读取知识库文件，缺失时返回提示而非崩溃。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning("[SupplyChainExecutor] 读取 %s 失败 (%s): %s", label, path, exc)
        return f"（{label} 加载失败，请检查数据目录）"


def _extract_ticker_company(message: str) -> tuple[str, str]:
    """从用户 message 中提取 ticker 和 company。

    支持格式：
    - "分析主题：\n新莱应材 300260 SZ 供应链分析"
    - "【新莱应材】300260 SZ..."
    - "新莱应材 300260 SZ..."
    """
    import re

    ticker = ""
    company = ""
    # 提取股票代码（6 位数字）
    m = re.search(r"\b(\d{6})\b", message)
    if m:
        ticker = m.group(1)
    # 提取公司名（第一行第一个 2-6 个中文字）
    lines = message.split("\n")
    first_text_line = next((l.strip() for l in lines if l.strip() and not l.strip()[0].isdigit()), "")
    m2 = re.search(r"[一-鿿]{2,6}", first_text_line)
    if m2:
        company = m2.group(0)
    return ticker, company


def _call_v3_tools_directly(
    tool_registry: Any,
    ticker: str,
    company: str,
    tool_calls_log: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """[v3 post-processing] 直接调用 v3 工具，确保结构化数据被收集。

    在 ReAct 循环结束后，如果 LLM 没有调用 v3 工具（LLM 直接生成了报告内容
    而非调用工具），这里直接调用它们以确保 deep_dive_obj 被填充。
    """
    import json as _json

    sections_executed: list[str] = []
    data: dict[str, Any] = {"ticker": ticker, "company": company}

    # 从 tool_calls_log 中提取历史数据，用于传给 capacity_outlook
    historical_capacity = ""
    financial_context_lines: list[str] = []  # 营收、毛利率、capex 等财务信号
    demand_drivers = ""
    expansion_projects = ""

    for entry in tool_calls_log:
        if not entry.get("success"):
            continue
        raw = entry.get("result_str", "") or entry.get("result", "")
        if not raw:
            continue
        try:
            result_dict: dict[str, Any] = _json.loads(raw)
        except (ValueError, TypeError):
            continue

        if "financial_quality" in result_dict and isinstance(
            result_dict.get("financial_quality"), list
        ):
            # 提取 capacity_utilization / financial metrics / expansion_projects
            for rep in result_dict.get("financial_quality", []):
                cap_util = rep.get("capacity_utilization_pct")
                if cap_util is not None:
                    historical_capacity += f"产能利用率: {cap_util}%\n"
                # 收集财务指标用于产能推断
                period = rep.get("period", "?")
                rev_yoy = rep.get("revenue_yoy_pct")
                gm = rep.get("gross_margin_pct")
                ocf_yoy = rep.get("operating_cash_flow_yoy_pct")
                capex = rep.get("capex_intensity_pct")
                inv_days = rep.get("inventory_days")
                ar_rev = rep.get("ar_to_revenue_pct")
                segments = rep.get("revenue_segments", {})
                if any(v is not None for v in [rev_yoy, gm, ocf_yoy, capex]):
                    seg_str = (
                        ", ".join(f"{k}={v}%" for k, v in segments.items())
                        if segments
                        else ""
                    )
                    financial_context_lines.append(
                        f"{period}: "
                        + (f"营收同比={rev_yoy}%" if rev_yoy is not None else "")
                        + (f", 毛利率={gm}%" if gm is not None else "")
                        + (f", 经营现金流同比={ocf_yoy}%" if ocf_yoy is not None else "")
                        + (f", capex强度={capex}%" if capex is not None else "")
                        + (f", 存货天数={inv_days}天" if inv_days is not None else "")
                        + (f", 应收占比={ar_rev}%" if ar_rev is not None else "")
                        + (f", 业务构成={seg_str}" if seg_str else "")
                    )
                exp = rep.get("expansion_projects")
                if exp:
                    expansion_projects += f"{exp}\n"

        if "industry_outlook" in result_dict and isinstance(
            result_dict.get("industry_outlook"), list
        ):
            for ind in result_dict.get("industry_outlook", []):
                demand = ind.get("demand_drivers", "")
                if demand:
                    demand_drivers += f"{demand}\n"

    # 补充 Baostock 真实季频财务数据（作为 financial_context 的底仓）
    try:
        import pandas as _pd
        from data_provider.baostock_fetcher import BaostockFetcher
        bs_df = BaostockFetcher().get_financial_data(ticker, start_year="2022")
        if bs_df is not None and not bs_df.empty:
            for _, row in bs_df.tail(4).iterrows():
                parts = [f"Baostock 季频数据 (报告期 {row.name})"]
                for col, label in [
                    ("revenue_yoy_pct", "营收同比"),
                    ("gross_margin_pct", "毛利率"),
                    ("roe_pct", "ROE"),
                    ("debt_to_asset_pct", "资产负债率"),
                    ("inventory_turnover_days", "存货周转天数"),
                    ("operating_cf_to_revenue", "经营现金流/营收"),
                ]:
                    val = row.get(col)
                    if col in row and val is not None and not _pd.isna(val):
                        parts.append(f"{label}={val:.2f}")
                if len(parts) > 1:
                    financial_context_lines.append(", ".join(parts))
    except Exception:
        pass  # Baostock 失败不影响主流程

    # 调用 6 个 v3 工具
    v3_tool_map: dict[str, dict[str, Any]] = {
        "analyze_product_matrix": {
            "ticker": ticker,
            "company": company,
            "market": "SZ" if ticker.startswith(("0", "3")) else "SH",
            "industry_hint": "",
        },
        "analyze_market_position": {
            "ticker": ticker,
            "company": company,
            "market": "SZ" if ticker.startswith(("0", "3")) else "SH",
            "industry_hint": "",
            "top_k": 5,
        },
        "extract_key_partners": {
            "ticker": ticker,
            "company": company,
            "top_k": 5,
        },
        "analyze_industry_outlook": {
            "ticker": ticker,
            "company": company,
            "top_k": 3,
        },
        "analyze_financial_quality": {
            "ticker": ticker,
            "company": company,
            "market": "SZ" if ticker.startswith(("0", "3")) else "SH",
            "industry_hint": "",
            "top_k": 5,
        },
        "analyze_capacity_outlook": {
            "ticker": ticker,
            "company": company,
            "industry_hint": "",
            "historical_capacity": historical_capacity or "",
            "financial_context": "\n".join(financial_context_lines) or "",
            "demand_drivers": demand_drivers or "",
            "expansion_projects": expansion_projects or "",
        },
    }

    for tool_name, kwargs in v3_tool_map.items():
        import sys as _sys
        try:
            result = tool_registry.execute(tool_name, **kwargs)
            result_str = _json.dumps(result) if isinstance(result, dict) else str(result)
            # Re-using the same \`result_dict\` name from the previous loop so we
            # don't shadow it; the mypy no-redef is a false positive here.
            result_dict: dict[str, Any] = _json.loads(result_str) if isinstance(result_str, str) else {}  # type: ignore[no-redef]
            print(
                f"[DEBUG] v3 tool {tool_name}: success result_len={len(result_str)} "
                f"keys={list(result_dict.keys())}",
                file=_sys.stderr, flush=True,
            )
        except Exception as exc:
            print(
                f"[DEBUG] v3 tool {tool_name}: EXCEPTION {exc}",
                file=_sys.stderr, flush=True,
            )
            logger.debug(
                "[SupplyChainExecutor] v3 tool %s 直接调用失败: %s", tool_name, exc
            )
            continue

        # 解析结果 - 注意工具返回的 key 可能与 SupplyChainDeepDiveV3 字段名不同
        if "product_matrix" in result_dict and isinstance(
            result_dict.get("product_matrix"), list
        ):
            data["product_matrix"] = result_dict["product_matrix"]
            if "product_matrix" not in sections_executed:
                sections_executed.append("product_matrix")
        elif "products" in result_dict and isinstance(result_dict.get("products"), list):
            # analyze_product_matrix 返回 {"products": [...]} 而不是 "product_matrix"
            data["product_matrix"] = result_dict["products"]
            if "product_matrix" not in sections_executed:
                sections_executed.append("product_matrix")
        elif "market_position" in result_dict and isinstance(
            result_dict.get("market_position"), list
        ):
            data["market_position"] = result_dict["market_position"]
            if "market_position" not in sections_executed:
                sections_executed.append("market_position")
        elif "positions" in result_dict and isinstance(result_dict.get("positions"), list):
            # analyze_market_position 返回 {"positions": [...]} 而不是 "market_position"
            data["market_position"] = result_dict["positions"]
            if "market_position" not in sections_executed:
                sections_executed.append("market_position")
        elif "key_customers" in result_dict or "key_suppliers" in result_dict:
            if "key_customers" in result_dict:
                data["key_customers"] = result_dict["key_customers"]
            if "key_suppliers" in result_dict:
                data["key_suppliers"] = result_dict["key_suppliers"]
            if "key_partners" not in sections_executed:
                sections_executed.append("key_partners")
        elif "customers" in result_dict or "suppliers" in result_dict:
            # extract_key_partners 返回 {"customers": [...], "suppliers": [...]}
            if "customers" in result_dict:
                data["key_customers"] = result_dict["customers"]
            if "suppliers" in result_dict:
                data["key_suppliers"] = result_dict["suppliers"]
            if "key_partners" not in sections_executed:
                sections_executed.append("key_partners")
        elif "industry_outlook" in result_dict and isinstance(
            result_dict.get("industry_outlook"), list
        ):
            data["industry_outlook"] = result_dict["industry_outlook"]
            if "industry_outlook" not in sections_executed:
                sections_executed.append("industry_outlook")
        elif "outlooks" in result_dict and isinstance(result_dict.get("outlooks"), list):
            # analyze_industry_outlook 返回 {"outlooks": [...]} 而不是 "industry_outlook"
            data["industry_outlook"] = result_dict["outlooks"]
            if "industry_outlook" not in sections_executed:
                sections_executed.append("industry_outlook")
        elif "reports" in result_dict and isinstance(result_dict.get("reports"), list):
            data["financial_quality"] = result_dict["reports"]
            if "financial_quality" not in sections_executed:
                sections_executed.append("financial_quality")
        elif "capacity_outlook" in result_dict:
            outlook = result_dict.get("capacity_outlook")
            if isinstance(outlook, dict):
                data["capacity_outlook"] = outlook
                if "capacity_outlook" not in sections_executed:
                    sections_executed.append("capacity_outlook")

    data["sections_executed"] = sections_executed
    return data if sections_executed else None


def build_supply_chain_system_prompt() -> str:
    """组装供应链 system prompt（注入 SKILL.md + 核心 5 个 references）。

    用 ``.replace()`` 占位符而非 ``.format()``，避免知识库正文里的花括号被误解析。
    """
    skill = _read_text(skill_path(), "SKILL.md")
    references = "\n\n---\n\n".join(
        _read_text(reference_path(name), f"reference {name}")
        for name in CORE_REFERENCES
    )
    return _SUPPLY_CHAIN_SYSTEM_PROMPT_TEMPLATE.replace("{{SKILL}}", skill).replace(
        "{{REFERENCES}}", references
    )


class SupplyChainExecutor:
    """供应链分析专属 Agent。

    与问股 / 郑希 executor 暴露相同的 ``chat(message, session_id,
    progress_callback, context)`` 接口，便于复用 SSE 端点的线程池包装。
    长任务配置：``max_steps=40``、``wall_clock=1200s``（深度调研 5–15 分钟）。
    """

    def __init__(
        self,
        tool_registry: ToolRegistry,
        llm_adapter: "LLMToolAdapter",
        max_steps: int = 40,
        timeout_seconds: Optional[float] = 1200.0,
    ) -> None:
        self.tool_registry = tool_registry
        self.llm_adapter = llm_adapter
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds

    def chat(
        self,
        message: str,
        session_id: str,
        progress_callback: Optional[Callable[..., Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> "AgentResult":
        """执行一轮供应链深度调研（ReAct 工具循环 + 会话持久化）。"""
        from src.agent.chat_context import build_agent_chat_context_bundle
        from src.agent.conversation import conversation_manager
        from src.agent.executor import AgentResult  # local for type instantiation
        from src.agent.runner import _collect_v3_deep_dive_from_log, run_agent_loop

        system_prompt = build_supply_chain_system_prompt()

        conversation_manager.get_or_create(session_id)
        config = getattr(self.llm_adapter, "_config", None) or get_config()
        bundle = build_agent_chat_context_bundle(session_id, self.llm_adapter, config)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
        ]
        messages.extend(bundle.context_messages)
        messages.append({"role": "user", "content": message})

        conversation_manager.add_message(session_id, "user", message)

        loop_result = run_agent_loop(
            messages=messages,
            tool_registry=self.tool_registry,
            llm_adapter=self.llm_adapter,
            max_steps=self.max_steps,
            progress_callback=progress_callback,
            max_wall_clock_seconds=self.timeout_seconds,
            stock_scope=None,
        )

        if loop_result.success:
            conversation_manager.add_message(
                session_id, "assistant", loop_result.content
            )
        else:
            conversation_manager.add_message(
                session_id,
                "assistant",
                f"[分析失败] {loop_result.error or '未知错误'}",
            )

        # [v3] Extract v3 structured deep dive data from tool call results
        deep_dive_obj = None
        try:
            ticker, company = _extract_ticker_company(message)
            deep_dive_obj = _collect_v3_deep_dive_from_log(
                loop_result.tool_calls_log, ticker, company
            )
            import sys
            print(
                f"[DEBUG] after _collect: deep_dive_obj={deep_dive_obj is not None} "
                f"sections={deep_dive_obj.get('sections_executed') if deep_dive_obj else None} "
                f"tool_calls={len(loop_result.tool_calls_log)}",
                file=sys.stderr, flush=True,
            )
            # [v3 post-processing] If LLM skipped v3 tools, call them directly
            if not deep_dive_obj or not deep_dive_obj.get("sections_executed"):
                print(
                    f"[DEBUG] LLM skipped v3 tools, calling directly for {ticker} {company}",
                    file=sys.stderr, flush=True,
                )
                deep_dive_obj = _call_v3_tools_directly(
                    self.tool_registry, ticker, company, loop_result.tool_calls_log
                )
                print(
                    f"[DEBUG] after direct call: deep_dive_obj={deep_dive_obj is not None} "
                    f"sections={deep_dive_obj.get('sections_executed') if deep_dive_obj else None}",
                    file=sys.stderr, flush=True,
                )
            logger.info(
                "[SupplyChainExecutor] deep_dive_obj: ticker=%r company=%r sections=%s",
                ticker,
                company,
                deep_dive_obj.get("sections_executed") if deep_dive_obj else None,
            )
        except Exception as exc:
            logger.warning("[SupplyChainExecutor] v3 deep_dive 解析失败: %s", exc)

        return AgentResult(
            success=loop_result.success,
            content=loop_result.content,
            deep_dive_obj=deep_dive_obj,
            tool_calls_log=loop_result.tool_calls_log,
            total_steps=loop_result.total_steps,
            total_tokens=loop_result.total_tokens,
            provider=loop_result.provider,
            model=loop_result.model,
            error=loop_result.error,
            messages=loop_result.messages,
        )
