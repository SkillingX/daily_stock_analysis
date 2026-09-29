# 深度分析（深度投研报告）双轨多 Agent 化 · 需求文档

- 状态：草案 v2（经深度审计修订，变更见文末附录 A）
- 日期：2026-09-28
- 关联文档：`docs/deep-research.md`、`docs/deep-research-chain-news-logic-plan.md`、`docs/type-contract-data-defense.md`
- 涉及模块：`src/agent/`、`src/scoring/`、`src/schemas/`、`src/services/deep_research_service.py`、`api/v1/endpoints/deep_research.py`、`apps/dsa-web/src/pages/DeepResearchPage.tsx`

---

## 1. 背景与目标

### 1.1 现状

深度投研报告（路由 `/deep-research`）当前是**单 Agent 长循环**架构：一个 ReAct 循环（`run_agent_loop`，max_steps=30 / 1200s）承载全部章节，system prompt（`data/deep_research/system_prompt.md`）+ 单条 9 指令 user message 驱动，产出后经 `DeepResearchValidator` 七层校验、失败重生成一轮，SSE 推前端 + Markdown/PDF 落盘。

问题：

1. **单一 prompt 承载全部维度**：消息面/产业链/财务/技术面挤在一个上下文里，互相抢注意力，越深度的维度（产业链、六维评分）越容易被压缩。
2. **无「短线 vs 长线」双轨结构**：每日个股分析（`src/analyzer.py` + `src/schemas/report_schema.py`）已形成「短线六件套 × 长线五段式」双轨 + 规则决策护栏 + 六维评分锚的成熟结构，深度投研只有新八章单轨。
3. **维度质量不可独立度量/重试**：校验器只能整报告打分，无法定位"哪个维度没做透"。
4. **规则资产未复用**：`src/scoring/`（六维评分引擎 + 贝叶斯映射 + 六个规则指标评分器）和 `analyzer.py` 的决策护栏（`_downgrade_*`/`_stabilize_*`）在深度投研里完全没用上。
5. **贝叶斯与估值口径缺机器定义**：`market_implied_p` 无计算公式、证据 LR 无标定表、亏损股（PE≤0）无估值口径切换——LLM 手填概率的"贝叶斯"是装饰性的，不是可审计的。

### 1.2 目标

把深度分析升级为：

> **「短线六件套（信号/数据/情报/计划/阶段/历史）× 长线五段式（结论/产业链/情景/贝叶斯/六维）」双轨结构，中间夹一层规则决策护栏，六维评分（产业链 25% + 基本面 25% 占一半权重、技术面 10%）为长线轨量化锚。每个分区维度由专门的后台深入分析代码负责，按维度性质分配 LLM 角色（探索循环 / 单轮叙述 / 无 LLM 纯规则）；全部维度完成后产出详细投研报告，用户可直接下载（PDF + Markdown + 维度数据 JSON）查看细节。**

### 1.3 成功标准（验收锚）

1. 一份深度报告包含 11 个维度分区的独立产出，每区有可溯源的结构化数据（工具调用记录 + 评分依据 + `as_of` 时点）。
2. 长线轨必含六维评分明细表（六维 × 指标 × 权重 × 打分 × rule/llm 依据）+ 贝叶斯证据链（LR、后验、止损条件），LR 与先验全部由机器按标定表计算。
3. 顶层结论经过规则护栏校正（降级/收敛可解释，报告"护栏事件表"逐条列出）。
4. 报告 PDF 可下载，正文含全部维度细节；维度 JSON 产物随报告持久化，可单独下载复算。
5. 单维度失败可独立降级/重试，不拖垮整份报告；降级维度在报告中显式标注缺失原因。
6. 报告头部声明数据快照时点，尾部附免责声明（AI 生成、不构成投资建议）。

---

## 2. 名词约定

| 名词 | 含义 |
|------|------|
| 维度分区（dimension） | 双轨共 11 个：短线 6 + 长线 5，每区一套后台分析代码 + 按性质分配的 LLM 角色 |
| 后台深入分析代码 | 非 LLM 的规则/计算/数据获取层（评分器、校验器、数据装配器），Agent 的工具与输入由它供给 |
| 决策护栏 | 纯规则层，对 Agent 产出的结论做降级/收敛/否决，介于维度产出与最终报告之间 |
| 六维评分锚 | `src/scoring/` 的六维加权总分，经 `map_prior` 映射为贝叶斯先验，是长线轨量化锚点 |
| 探索型维度 | 需要 ReAct 工具循环检索的维度（情报、产业链） |
| 叙述型维度 | 后台规则已算出结论，LLM 仅单轮写成人类可读叙述（1 次调用） |
| 纯规则维度 | 完全无 LLM，后台代码直出结构化结果 |

---

## 3. 总体架构

```
POST /api/v1/deep-research/generate/stream (SSE，现有端点保留)
  └── DeepResearchOrchestrator（新编排器，参照 src/agent/orchestrator.py 模式扩展）
        ├── 阶段 0：共享上下文装配（行情/基本面快照/历史报告/日线数据，一次取齐，记录 as_of）
        ├── 波次 1：S2 数据 / S3 情报 / S5 阶段 / S6 历史 / L1 六维 / L4 产业链 / L5 情景（并发 ≤4）
        ├── 波次 2：S4 计划（依赖 S2,S3）/ L2 贝叶斯（依赖 L1）
        ├── 波次 3：L3 结论（依赖 L1,L2,S2,S3）→ S1 信号（依赖 S2,S3,L3）
        ├── 阶段 3：规则决策护栏（纯代码，无 LLM）
        ├── 阶段 4：报告成文——默认 Jinja 模板直灌（无 LLM）；
        │           可选 LLM 成文 Agent 只做叙述润色，数字一律模板变量注入
        └── 落盘：Markdown + PDF + 维度 JSON 产物（md2pdf 与现有落盘复用）
```

关键设计原则：

1. **按维度性质分配 LLM 角色，不整齐划一上 ReAct**（v2 修订，见 §4.3）：11 个维度里只有 2 个真正需要工具探索循环；其余走单轮叙述或纯规则。预计单次报告 LLM 调用 10–20 次，**总成本不高于甚至低于现状单循环**（现状 10–30 步全包且注意力被稀释）。
2. **数字全部机器算，LLM 只写叙述**：六维、贝叶斯、点位、概率校验、护栏判定零 LLM 参与；LLM 成文 Agent 禁用改写数字（prompt 硬约束 + 模板变量注入双重防呆）。
3. **后台分析代码先行，Agent 后至**：每个维度的规则计算/数据获取先以纯函数实现（可单测），Agent 在其上调用工具与补全叙述。
4. **复用现有基建**：`run_agent_loop`、`LLMToolAdapter`、独立 ToolRegistry 模式（`factory.py` 先例）、SSE 队列/心跳/看门狗、PDF 惰性生成、SQLite+文件落盘全部沿用。
5. **三层防御**：维度输出契约全部 Pydantic v2 strict（Layer 3）；护栏规则、贝叶斯计算、评分映射加 icontract（Layer 2，金融场景强制）；全量类型注解（Layer 1），遵守 `docs/type-contract-data-defense.md`。
6. **灰度与回滚**：新旧引擎并存于配置开关之后（§7.1），出问题一键回 legacy。

---

## 4. 维度分区定义（11 区）

每个维度统一四件套：**后台分析代码（模块）+ LLM 角色 + 输出契约（Pydantic strict）+ 独立校验**。"复用"列指可直接搬用的现有代码。

### 4.1 短线轨（六件套）

| # | 维度 | 类型 | 职责 | 后台深入分析代码（新建/复用） | 输出契约要点 |
|---|------|------|------|------|------|
| S1 | 信号 signal | 叙述型 | 评级、目标价、信号类型、置信度 | 新建信号合成器：输入 S2/S3/L3 输出，复用 `src/schemas/decision_action.py` 动作枚举 + S1↔L3 一致性映射表 | `signal_type`、`rating`、`target_price`、`confidence`、`one_sentence` |
| S2 | 数据 data | 纯规则 | 趋势/价格位置/量能/筹码四类硬数据 | 复用 `data_provider/` + `analyze_trend`/`get_chip_distribution`；新建数据透视装配器（`report_schema.py` 的 `DataPerspective` 同构） | `trend_status`/`price_position`/`volume_analysis`/`chip_structure` + `as_of` |
| S3 | 情报 intel | **探索型** | 新闻/公告/风险警报/催化剂/业绩展望/情绪 | 复用 `search_stock_news`/`search_comprehensive_intel`；新建来源等级标注器（旧 prompt §一.3 七级标签机器化） | `Intelligence` 同构 + **每条情报带来源等级标签** + `unverified` 计数 |
| S4 | 计划 plan | 叙述型 | 狙击四点位、仓位策略、行动清单 | 新建：**ATR/波动率规则计算点位**（理想买=支撑+0.5×ATR 类规则）+ `src/schemas/risk_check.py` 风控 skill（已有 icontract 范式）算仓位；LLM 仅叙述 | `SniperPoints`/`PositionStrategy`/`action_checklist` + 点位计算参数 |
| S5 | 阶段 phase | 纯规则 | 行动窗口/立即行动/观察条件/下次检查 | 新建：复用 `src/core/trading_calendar.py`（交易日历）+ 盘后/盘中时段判定（analyzer `_phase_aware_quote_labels` 思路） | `PhaseDecision` 同构 |
| S6 | 历史 history | 纯规则 | 历次报告对比、观点漂移、证据时间线 | 新建：读 SQLite 历史报告 + `storage.deep_research_reports`，做观点一致性 diff | `history_compare` 表 + `drift_flags` |

### 4.2 长线轨（五段式）

| # | 维度 | 类型 | 职责 | 后台深入分析代码（新建/复用） | 输出契约要点 |
|---|------|------|------|------|------|
| L1 | 六维 six_dim | 叙述型 | 六维×指标×权重×打分 | **复用 `src/scoring/` 全套**：`engine.py` + `indicators/{supply_chain,fundamental,capital,technical,sentiment,macro}.py` + `weights.py`；LLM 只写维度叙述与主观键值 | `ResearchFramework` 同构（`scoring_version` 必填）+ 指标级 `evidence` |
| L2 | 贝叶斯 bayesian | 纯规则 | 先验→证据 LR→后验→止损条件 | **复用 `src/scoring/bayesian.py`**：`map_prior`（六维总分→先验）、`calculate_edge`、后验更新；**LR 标定表机器校验**（§5.4） | `BayesianFramework` 同构 + `StopConditions` |
| L3 | 结论 conclusion | 叙述型 | 行动（六选一）+ 1/3/5 年价值区间 | 新建：规则合成 L1/L2 + 六维总分，套用护栏后定稿；LLM 仅写 rationale | `InvestmentConclusion` 同构（`action` 为 6 值 Literal） |
| L4 | 产业链 supply_chain | **探索型** | 供应链地图/定位/瓶颈/中美双链 | 复用 `data_provider/supply_chain/` + `industry_dna_loader.py` + `verify_supply_chain_evidence` 双源校验 | `SupplyChain` 同构 + 双源校验状态 |
| L5 | 情景 scenarios | 叙述型 | 产业空间、乐观/中性/悲观三情景、价值锚 | 新建：情景概率机器校验（和=100%）+ **期望目标价 EV=Σ(概率×目标价) 强制计算** + 与当前估值口径交叉校验（§5.4） | `ValueScenarios` 同构 + `expected_value` |

### 4.3 LLM 角色分配与成本模型（v2 修订）

| 类型 | 维度 | LLM 调用预算 | 说明 |
|------|------|-------------|------|
| 探索型（ReAct） | S3、L4 | 各 3–8 步 | 唯一需要 `run_agent_loop` 的维度，独立 max_steps=8 |
| 叙述型（单轮） | S1、S4、L1、L3、L5 | 各 1–2 次 | 输入=后台代码产出的结构化结果，输出=该维度叙述文本，契约校验失败重试 1 次 |
| 纯规则（无 LLM） | S2、S5、S6、L2 | 0 | 后台代码直出，失败即维度降级 |

**全报告 LLM 预算：10–20 次调用 + 0–16 步探索**，与现状单循环（10–30 步全包）相当或更低，但每维度注意力不再被稀释。额度不足时降级顺序：先砍探索型的步数上限，再砍叙述型（降级为模板默认句），纯规则维度永不降级。

### 4.4 每个维度的 Prompt 规范

- **维度 prompt 独立文件**：`data/deep_research/agents/<dim_id>.md`，运行时读取（沿用 system_prompt.md 模式），只写本维度职责、工具、输出契约。
- **共享"宪法"文件**：`data/deep_research/agents/_constitution.md`（全局硬约束：来源七级标签、诚实标注、数字锚定、不得编造），注入全部维度 prompt，防止 12 份 prompt 各自漂移（v2 新增）。
- **工具子集**：探索型维度从 11 工具总表精选（`factory.py` 模式）；叙述型/纯规则维度不挂工具，杜绝越权取数。
- **输出**：强制 JSON 走 Pydantic strict 校验（`extra="forbid"`），畸形重试 1 次，再失败记 `degraded` 不阻断全局。

---

## 5. 双轨结构、决策护栏与量化锚

### 5.1 双轨关系

- 短线轨回答"当下怎么操作"，长线轨回答"值不值得长期持有"；两轨共享阶段 0 上下文与 S2 数据，减少重复取数。
- 报告成文时短线六件套在前、长线五段式在后，交叉引用（S1 信号必须注明与 L3 长线行动是否冲突，冲突由护栏裁决）。

### 5.2 依赖与并发（v2 修订：修正波次依赖）

```
波次 1（并发 ≤4）：S2 数据 / S3 情报 / S5 阶段 / S6 历史 / L1 六维 / L4 产业链 / L5 情景
波次 2：S4 计划（←S2,S3）        L2 贝叶斯（←L1）
波次 3：L3 结论（←L1,L2,S2,S3）  → S1 信号（←S2,S3,L3）
```

**修订说明**：v1 把 S1 信号放在波次 1 与其它维度并行，这是设计错误——信号是综合产物（需要数据、情报与长线结论共同决定），提前产出会导致评级与长线 action 互相矛盾。v2 将 S1 移到依赖链末端，并新增 S1↔L3 一致性护栏（§5.3 规则 7）。

并发上限 4 个 LLM 调用（配置项），防单_provider 额度打满；`try_submit_long_task` 全局池继续兜底。

### 5.3 决策护栏规则表（v2 扩至 10 条）

纯规则、可单测、触发即写入报告"护栏事件表"（`guardrail_report: List[GuardrailEvent]`：规则 id、触发维度、处置、理由），随报告持久化并随 done 事件下发前端。

| # | 规则 | 动作 | 来源参照 |
|---|------|------|---------|
| 1 | 买入/增持信号但资金流显著净流出 | 信号降一级 + 注明 | analyzer `_downgrade_buy_without_capital_flow` |
| 2 | 长线 action=建仓/加仓但 edge ≤ 0 | 降级为观察 + 注明 | analyzer 决策稳定化思路 |
| 3 | 长线 action=建仓/加仓但六维总分 < 55 | 降级为观察 + 注明（评分锚与结论背离） | v2 新增 |
| 4 | 情景概率和 ≠ 100% | 机器归一化后复核，仍不通过打回 L5 | validator 概率和容差 |
| 5 | 溢价情景 PE 上限 < 当前 PE(TTM) | 打回 L5 改写为回归/消化表述 | validator PE 矛盾检测 |
| 6 | 三情景目标价与 L3 价值锚偏离 > 30% | 标记内部矛盾，降置信度 | 新建 |
| 7 | S1 评级与 L3 action 方向冲突（如 rating=买入 但 action=减仓） | 按映射表收敛到较保守侧 + 注明 | v2 新增（S1↔L3 映射表冻结于契约） |
| 8 | 情报含 unverified 等级却支撑确定性结论 | S3 打回重筛 | prompt §一.3/4 机器化 |
| 9 | L2 证据条数 < 3 或证据日期全部早于 90 天 | L2 置信度上限"低"，L3 行动不得高于"观察" | v2 新增（证据链新鲜度） |
| 10 | LLM 主观六维键值与规则分背离 > 20 分 | 不静默平均，标记为护栏事件并列展示两者 | v2 新增（防止主观分漂移吞掉规则锚） |

### 5.4 六维评分锚与贝叶斯机器定义（v2 重点补強）

v1 只说了"复用 scoring"，但投研上缺三个机器定义，LLM 手填会让贝叶斯装饰化。v2 冻结如下：

1. **权重**：冻结 `DEFAULT_DIMENSION_WEIGHTS`（产业链 25 / 基本面 25 / 资金面 15 / 技术面 10 / 情绪 15 / 宏观 10），本期不改。
2. **LR 标定表**（`EvidenceItem.lr` 的允许区间，机器校验，越界即打回）：
   | strength | LR 区间 |
   |----------|---------|
   | strong_positive | [2.0, 5.0] |
   | weak_positive | [1.2, 2.0) |
   | neutral | [0.9, 1.1] |
   | weak_negative | (0.5, 0.9) |
   | strong_negative | [0.2, 0.5] |
3. **market_implied_p 定义**：机器计算，LLM 不得手填——`industry_dna_loader` 的行业基率表（该行业过去 N 年龙头胜率/平庸率基线）按当前估值分位插值；取不到行业 DNA 时回退板块中性值 0.5 并标记 `inferred`。
4. **亏损股估值口径切换**：当前 PE(TTM) ≤ 0 或缺失时，自动切换 PB/PS 口径，PE 矛盾检测（规则 5）跳过并标注口径；报告头部声明所用口径。
5. **规则分与 LLM 主观分合并**：同维度两者背离 > 20 分触发护栏规则 10；未背离时按 schema 的 `basis` 字段并列展示，不做无记录的平均。
6. **证据新鲜度**：L2 每条证据带 `date`，全部早于 90 天触发护栏规则 9。
7. **数据源风险（v2 新增）**：北向资金实时数据自 2024-08 起已停止披露，`indicators/capital.py` 的北向类指标需先确认数据源仍可用；不可用则降级为融资余额 + 主力资金净流入 + 股东户数三项，并在维度 `data_limitations` 注明。

---

## 6. 综合投研报告与下载

### 6.1 报告骨架（Markdown，严格章节）

```
# {名称}（{代码}）深度投研报告
> 数据截至：{快照 as_of}（阶段0装配时点） | 报告生成：{时间}
> 评级/目标价(含EV)/当前价/核心逻辑/催化剂/核心风险（由 S1+L3 合成）
> 护栏说明：本报告触发 N 条决策护栏（详见附录事件表）

## 短线六件套
### 一、信号   ### 二、数据透视   ### 三、情报（含来源等级与 unverified 计数）
### 四、作战计划（点位含 ATR 参数说明 / 仓位 / 清单）
### 五、阶段决策   ### 六、历史对比与观点漂移

## 长线五段式
### 七、投资结论（先验/市场隐含/Edge/行动/1·3·5年价值区间）
### 八、产业链解读（含双源校验状态）
### 九、长期价值与情景（三情景表：概率/目标价/EV，概率和=100%）
### 十、贝叶斯证据链（证据/LR(标定区间)/后验/止损条件）
### 十一、六维评分明细（六维×指标×权重×打分×rule/llm 依据×evidence）

## 附录：数据假设、可比公司、护栏事件表、维度降级标记与原因、免责声明
（免责声明：本报告由 AI 生成，不构成投资建议。）
```

**成文方式（v2 修订）**：默认 **Jinja 模板直灌**——数字与表格全部由阶段 3 后的结构化数据直接注入，零 LLM 参与，确定性 100% 可回归；LLM 成文 Agent 仅作为可选润色层（仅叙述段、数字仍模板注入），默认关闭。v1 设"综合 Agent 唯一允许写长文"会让最长文处最不可控，且与每日分析的模板化渲染路线不一致。

### 6.2 下载与产物（v2 扩充）

- **维度 JSON 产物落盘**（v2 新增）：11 个维度的结构化输出按 `{report_id}/dim_<id>.json` 落盘，与 md 同目录；供复算、回归对比与前端钻取。
- 复用现有链路：Markdown 落盘 `reports/deep_research/` + SQLite 元数据 + `GET /reports/{id}/pdf` 惰性生成（md2pdf）。
- PDF 端点补上限流（Semaphore）——顺手修复现状 docstring 与实现不符的问题。
- 新增 `GET /reports/{id}/markdown` 直接下载 .md；新增 `GET /reports/{id}/dims` 返回维度 JSON 列表（v2）。
- **PDF 宽表渲染验收**（v2 新增）：六维×指标表列多，xhtml2pdf 易溢出——P1 验收必须实测该表 PDF 排版（必要时拆为六张单维表或缩字号），未达标不交付 P2。

---

## 7. API 与前后端改造

### 7.1 后端

| 改动 | 说明 |
|------|------|
| 新增 `src/agent/deep_research/orchestrator.py` | 编排器：波次调度、依赖图、降级收集、护栏调用 |
| 新增 `src/agent/deep_research/dim_*.py` ×11 | 维度定义（prompt 路径、类型、工具子集、契约、后台代码入口） |
| 新增 `src/deep_research_dims/` | 各维度后台分析代码（装配器/评分/校验/ATR 点位/LR 标定），纯函数可单测 |
| 新增 `src/schemas/deep_research_dims.py` | 11 个维度输出契约（Pydantic v2 strict/frozen）+ GuardrailEvent |
| 新增 `data/deep_research/agents/` | `_constitution.md` + 11 个维度 prompt + （可选）成文润色 prompt |
| 改造 `deep_research_executor.py` | legacy 引擎保留于 `DEEP_RESEARCH_ENGINE=legacy` 分支内，作为回滚路径 |
| 改造 `deep_research_validator.py` | 改为两层：逐维度契约校验（各维度独立）+ 成文后结构校验（十一章骨架） |
| system_prompt.md | 拆分为维度 prompt 目录；旧文件仅 legacy 引擎使用 |
| 配置项 | `DEEP_RESEARCH_ENGINE`（`dual_track`/`legacy`，**默认 `dual_track`（P2 已翻转）**，`legacy` 保留回滚）；`DEEP_RESEARCH_DIM_MAX_STEPS`（探索型，默认 8）。已决策（§10）：LLM 润色层不实现，`DEEP_RESEARCH_SYNTH_NARRATIVE` 取消 |

### 7.2 前端（DeepResearchPage）

1. 生成中进度按维度分组展示（11 个维度徽章：运行中/完成/降级 + 波次信息），替代现在的平铺步骤流。
2. 报告展示页分区渲染（短线六件套 / 长线五段式 / 护栏说明折叠区），复用 `ReportMarkdownBody`。
3. 新增"下载 Markdown"与"维度数据 JSON"入口（现有 PDF 保留）。
4. 历史列表展示改为"完成维度数/11 + 护栏触发数"。
5. 报告头部展示"数据截至"时间戳（配合 §8 快照时点声明）。

### 7.3 兼容

- 端点路径、report_id 格式、鉴权、SSE 事件协议全部不变；`done` 事件 payload 追加 `dimensions`、`guardrail_events`、`dims_degraded` 字段（增量，旧前端可忽略）。
- legacy 引擎下 `done` payload 不带新字段，前端按缺省渲染（与现状一致）。

---

## 8. 非功能需求

1. **时长**：整份报告 P95 ≤ 10 分钟（波次并发后预期 3–8 分钟，留观测项，实测后回填）。
2. **成本**：LLM 调用预算见 §4.3；额度超支时按"探索型步数 → 叙述型降级为模板句"顺序降级，纯规则维度不受影响。
3. **稳定性**：单维度失败/超时只降级该维度（报告内显式标注原因）；阶段 0 数据装配失败才整票失败。
4. **缓存（v2 细化 TTL）**：维度产物按 `{code}_{date}_{dim}` 缓存，但 TTL 分档——S3 情报 4 小时（盘中易变）、S2 数据 1 个交易日、L1/L2/L4/L5 长线维度 5 个交易日、S5/S6 纯规则不缓存；同票重复生成默认复用未过期维度，用户可"强制刷新"。
5. **数据时延诚实性（v2 新增）**：报告生成需 3–8 分钟，阶段 0 快照到成文存在时延——报告头部必须声明快照 as_of，S2/S3 维度附各自 as_of；合成阶段禁止引用未声明时点的价格。
6. **安全**：维度 prompt 注入面与白名单（report_id 防穿越）维持现状强度；社区情报仍需来源等级标签。
7. **可观测**：每维度记录 steps/tokens/provider/dim_type/as_of 入 SQLite 元数据（现有字段扩展 + `dims` JSON 列）。

---

## 9. 里程碑

| 期 | 内容 | 验收 |
|---|------|------|
| P0 | 本需求冻结；11 维度契约 schema + 10 条护栏规则表 + LR 标定表 + S1↔L3 映射表定稿；orchestrator 骨架（空跑通 SSE）；`DEEP_RESEARCH_ENGINE` 配置开关 | 契约测试过，骨架返回空报告，开关可切 legacy |
| P1 | 先行打通 L1/L2/S3/S2 四维度验证成本模型；其余维度跟进；护栏层上线；Jinja 模板成文；维度 JSON 落盘；PDF 宽表排版实测 | 真实票端到端产出双轨报告，PDF（含六维表）排版合格，成本 ≤ 预算 |
| P2 | 前端分区渲染 + 维度徽章进度 + MD/JSON 下载；validator 两层化；引擎默认值翻转为 `dual_track` | Playwright E2E + 截图证据 |
| P3 | 缓存 TTL 体系；质量基准回归（与 legacy 报告做信息覆盖对照）；LLM 润色层评估；通知渠道接入评估 | 成本报告 + 回归对照表 |

**P3 执行记录（2026-09-29）**：维度子集（`dims` 参数 + 依赖闭包 + skipped 态 + 前端预设面板）、缓存 TTL 分档（`dim_cache`，派生维度不缓存防固化过期依赖）、行业基率表（`industry_base_rates.json` → `market_implied_p` 机器锚点）三项已实现并测试（39/39）；PDF 六维宽表排版验收结论——md2pdf CSS 已具备换行防护（`table-layout:auto` + `overflow-wrap:anywhere`，六维节为每维独立小表），本机缺 pango 无法实测（weasyprint 优雅降级返回 None 符合契约），生产 Docker（fonts-noto-cjk）为首验环境。

---

## 10. 风险与开放问题

| 风险/问题 | 级别 | 缓解/待决策 |
|-----------|------|------------|
| 探索型维度（S3/L4）成本不可控 | 高 | 独立 max_steps=8 + P1 先跑四维度成本模型，超预算即降级叙述 |
| 北向资金数据源已停止实时披露 | 中 | §5.4-7 降级三指标方案；P0 先验证 `indicators/capital.py` 数据源 |
| xhtml2pdf 渲染六维宽表溢出 | 中 | P1 验收实测；兜底拆六张单维表 |
| LLM 润色层引入数字漂移 | 中 | 默认关；开启时数字仍模板注入 + 成文后数字回归校验（与结构化数据比对） |
| 12 份 prompt 长期漂移 | 中 | `_constitution.md` 共享宪法 + 契约测试锁定输出形状 |
| **待确认（已决策 2026-09-28）**：`DEEP_RESEARCH_ENGINE` 默认翻转时点 | — | **已决策**：本期默认 `legacy`，E2E 验证通过后于 P2 翻转；开关已上线 |
| **待确认（已决策 2026-09-29）**：是否允许用户自选维度子集生成（省钱模式） | — | **已实现（P3）**：`dims` 数组参数 + 依赖闭包 + skipped 态 + 前端预设面板 |
| **待确认（已决策 2026-09-28）**：LLM 成文润色层是否值得常开 | — | **已决策**：不实现润色层，Jinja 模板直灌为唯一成文路径；`DEEP_RESEARCH_SYNTH_NARRATIVE` 配置项相应取消 |

---

## 附录 A：v2 审计修订记录（2026-09-28 深度审计）

| # | 审计意见 | 严重度 | 处置 |
|---|---------|--------|------|
| A1 | **S1 信号放在并行波次 1 是依赖设计错误**：信号是数据+情报+长线结论的综合产物，提前产出必然与 L3 矛盾 | P1 设计错误 | §5.2 改为波次 3；新增护栏规则 7（S1↔L3 映射表） |
| A2 | **贝叶斯装饰化**：market_implied_p 无定义、LR 无标定、亏损股无口径切换，LLM 手填概率不是可审计的量化 | P1 金融正确性 | §5.4 冻结 LR 标定表、market_implied_p 机器定义、PB/PS 口径切换 |
| A3 | **11 个维度全上 ReAct 是过度设计**：成本翻倍且多数维度不需要工具探索 | P1 成本 | §4.3 三分类（探索 2 / 叙述 5 / 纯规则 4），LLM 预算降到 10–20 次 |
| A4 | **"综合 Agent 唯一写长文"与每日分析模板化路线冲突**，最长文处最不可控 | P2 | §6.1 改为 Jinja 模板直灌默认 + LLM 润色可选默认关 |
| A5 | **缺期望目标价 EV、证据新鲜度、评分背离检测**三个投研刚需 | P2 | §5.3 规则 9/10、§5.4-6、L5 增加 EV 强制计算 |
| A6 | **缺灰度回滚路径**：新架构直接替换生产引擎风险大 | P2 | §7.1 增加 `DEEP_RESEARCH_ENGINE` 开关，P1 默认 legacy |
| A7 | **快照时延诚实性**：5–10 分钟生成期行情已变，报告须声明 as_of | P2 | §6.1 头部声明 + §8-5 |
| A8 | **北向数据源 2024-08 起停止实时披露**，资金面指标可能静默失效 | P2 | §5.4-7 降级方案 |
| A9 | **缓存 TTL 一刀切**对情报维度不合理 | P3 | §8-4 分档 TTL + 强制刷新 |
| A10 | **PDF 六维宽表渲染风险**、prompt 漂移风险 | P3 | §6.2 排版验收、§4.4 宪法文件 |
