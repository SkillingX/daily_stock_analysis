# 深度投研报告模块诊断报告

**诊断时间：** 2026-10-05
**诊断范围：** `reports/deep_research/` 下 18 个 dims JSON，覆盖 7 只股票
**诊断方法：** 解析 `*dims.json` 结构 + 抽查 `*_dim_*.md` 渲染文件 + 代码链路追溯

---

## 一、总体评估

**结论：11/19 维度全局失效，多个维度存在结构性 bug，报告结论可靠性存疑。**

| 指标 | 数字 |
|------|------|
| dims JSON 文件总数 | 18 个 |
| 覆盖股票数 | 7 只 |
| 有效计算维度的股票 | **仅 1 只**（688183） |
| 其他 6 只股票维度得分 | **全部为 0** |

---

## 二、P0 — 阻断性问题

### 🔴 Bug 1：Ownership 研究员输出格式错误 → Pydantic 验证全失败

**现象：** `OwnershipDim` 的 `top_holders`、`executives`、`recent_changes` 三字段均为 `List[str]`，但研究员输出的是 `List[dict]`（dict 含 `name`/`source` 等键），导致 Pydantic 验证抛出 `Input should be a valid string` 错误，**全部 9 个字段均报 type error**。

**影响：** ownership 维度 100% 失效（所有股票该维度 score=None，narrative 为空）。

**根因代码**（`src/schemas/deep_research_dims.py:356-358`）：
```python
top_holders: List[str] = Field(default_factory=list)   # 期望字符串列表
executives: List[str] = Field(default_factory=list)     # 期望字符串列表
recent_changes: List[str] = Field(default_factory=list)
```

研究员（Skill-based）返回：
```python
top_holders: [{"name": "广东生益科...A】", "source": "news"}, ...]  # dict 而非 str
```

**修复方向：** 三选一：
1. **改 schema**：`List[Dict]` + 相应嵌套 Pydantic 模型（推荐，符合数据结构）
2. **改研究员**：输出前提取 `name` 字段拼成字符串
3. **加 adapter**：在研究员输出→schema 校验之间加数据转换层

---

### 🔴 Bug 2：筹码分布数据有值，但 capital 维度未评出分数

**现象：** `chip_summary` 实际有完整数据（获利盘 22.22%、平均成本 120.67 元、成本区间 93.84~135.16），`chip_score = 48.0` 已算出，但 **capital 维度的 top-level `score=None`**。

**根因：** `chip_summary` 被序列化成 JSON **字符串**（双重序列化：`json.dumps` 后再 `json.dumps`），渲染层读不到结构化数据。但 `_score_chip_concentration` 内部评分函数仍能读到，说明评分是在数据序列化为字符串**之前**完成的——**评分函数和数据输出走了两条路径**，数据写 JSON 文件时已经变成了字符串。

**影响：** 筹码分布数据"肉眼可见"但下游渲染无法使用，"筹码分布未启用"是误报。

**证据：**
```json
"chip_summary": "{\"status\": \"ok\", \"asof\": \"2026-09-30\",
  \"profit_ratio\": 0.2222, \"avg_cost\": 120.67, ...}"
  ↑ 这是 json string，不是 dict
```

---

### 🔴 Bug 3：资金流接口全局超时 → 资金流得分 18/18 全为 None

**现象：** 所有 18 个股票 capital 维度的 `flow_score = None`，`flow_summary = "push2his资金流接口超时/不可达"`。

**根因：** `push2his`（财富快车）接口在大陆地区无法访问或超时，研究员无降级策略，直接返回 None。

**影响：** 资金流维度（权重约 15%）对所有股票均无法评分，财务安全维度缺少一项关键验证。

---

## 三、P1 — 数据缺失问题

### 🟡 问题 4：营收增速 / 净利增速 9/18 次缺失

**缺失频率：** `营收增速`、`净利增速`、`资产负债结构` 三个 fundamental 子字段在 18 个股票中出现 **9 次**，覆盖率 50%。

**根因：** Fuyao 年报 API 在 2026-10 时间点，2025 年年报尚未发布（通常在 2026-04 前发布），最新一期可用数据为 2024 年年报，导致 year-over-year 计算缺少最新年度，AkShare 财务指标表数据也因季报粒度与年报口径不匹配而回填失败。

**影响：** 成长质量维度半数股票无法评分，健康分被拖累约 5-10 分。

---

### 🟡 问题 5：板块实时排行数据源缺失

**缺失频率：** 18 次中 **9 次**，覆盖率 50%。

**根因：** `板块实时排行（本期未接数据源）` — AkShare 板块排行接口未接入或当日数据拉取失败，无降级策略。

**影响：** 板块对比维度（sector）完全无法评分，行业内相对表现无法判断。

---

### 🟡 问题 6：ROE / 毛利率 8/18 次缺失

**缺失频率：** 各出现 **8 次**。

**根因：** Fuyao 年报序列最新一期（2024Q3 或更早）无 `gross_margin`/`roe` 字段，AkShare 的 `stock_financial_analysis_indicator` 季报数据与年报口径不一致，回填失败。

---

### 🟡 问题 7：估值 5/18 次缺失

**缺失频率：** 5 次。

**根因：** AkShare 的 PE_TTM 接口在非交易日或个股停牌时返回 None，无兜底数据源。

---

## 四、P2 — 渲染 / 显示问题

### 🟢 问题 8：筹码分布报告"未启用"是误报

**现象：** `reports/report_20261005.md` 中多次出现 `筹码分布未启用或数据源暂不可用`，但 dims JSON 里筹码数据已存在（只是被双重 JSON 序列化了）。

**根因：** 渲染层读到的是字符串，条件判断 `if chip_summary` 永远为 True 但后续渲染失败，输出"未启用"作为兜底提示。

---

### 🟢 问题 9：business 维度 data_gaps 过多（10 项）

**688183** 的 business 维度有 **10 项 data_gaps**，包括"2025 年完整年报原始三大表"、"PCB 按应用领域收入结构明细"等分析师核心数据全部缺失。

**根因：** 这些数据需要 LLM 主动从公开资料（年报 PDF、招股书）提取，但研究员目前无文件级检索能力，仅靠 AkShare 结构化数据无法覆盖。

---

### 🟢 问题 10：render 层 Jinja2 模板渲染 bug（部分字段出现 JSON 转义）

**现象：** `chip_summary` 字段在 Markdown 报告里显示为 `"{\"status\": \"ok\", ...}"` 而非人类可读格式。

**根因：** `chip_summary` 在序列化给 Jinja2 之前已经被 `json.dumps()` 过一次，Jinja2 再次 `json.dumps()` 导致双重编码。

---

## 五、维度得分可用性矩阵

| 维度 | 688183（最新） | 其他 6 只股票 |
|------|---------------|--------------|
| technical（技术面） | ✅ 40.0 | ❌ 全部 None |
| sentiment（情绪面） | ✅ 56.0 | ❌ 全部 None |
| business（基本面） | ✅ 75.0 | ❌ 全部 None |
| us_china（中美关系） | ✅ 72.0 | ❌ 全部 None |
| capital.chip_score | ✅ 48.0 | ❌ 17/17 None |
| capital.flow_score | ❌ None（超时） | ❌ 全部 None |
| ownership（股权） | ❌ None（Pydantic 错误） | ❌ 全部 None |
| fundamental（财务） | ⚠️ 有数据但 score=None | ❌ 全部 None |
| sector（板块） | ❌ None（无数据源） | ❌ 全部 None |
| six_dim/scenarios/conclusion/plan/signal | ❌ score=None（依赖下游） | ❌ 全部 None |

**可用的核心维度（仅 688183）：** technical、sentiment、business、us_china、chip_score — 共 5 个。

**所有股票缺失的维度：** supply_chain、intel、data、phase、history、bayesian、scenarios、conclusion、plan、signal — 共 10 个（这些是研究员编排层的"元维度"，本身不直接评分）。

---

## 六、修复优先级建议

| # | 问题 | 严重度 | 修复成本 | 建议 |
|---|------|--------|----------|------|
| 1 | Ownership Pydantic 类型错误 | 🔴 P0 | 低 | 改 schema 为 `List[Dict]` 或加 adapter |
| 2 | 筹码双重 JSON 序列化 | 🔴 P0 | 低 | 移除多余的 `json.dumps()` |
| 3 | 资金流接口超时 | 🔴 P0 | 高 | 换用 AkShare 主力资金流数据源 |
| 4 | 营收/净利增速缺失（年报未发布） | 🟡 P1 | 高 | 等待 2025 年报发布（2026-04），短期用季报估算 |
| 5 | 板块实时排行数据源缺失 | 🟡 P1 | 中 | 接入 AkShare 板块排行接口 |
| 6 | ROE/毛利率 8 次缺失 | 🟡 P1 | 中 | 扩展 Fuyao 接口字段覆盖 |
| 7 | 估值 5 次缺失 | 🟡 P1 | 低 | 加 AkShare PE_TTM 重试逻辑 |
| 8 | 筹码"未启用"误报 | 🟢 P2 | 低 | 修复双重序列化即可消除 |
| 9 | business 维度 10 项 data_gaps | 🟢 P2 | 高 | 需接入文档检索（RAG）能力 |
| 10 | 其他 6 只股票 0 维度得分 | 🟡 P1 | 高 | 根因在 P0 项修复后自然解决 |
