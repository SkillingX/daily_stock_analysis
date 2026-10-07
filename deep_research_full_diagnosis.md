# 深度投研报告数据质量完整诊断报告

**诊断时间：** 2026-10-05
**覆盖范围：** `reports/deep_research/` 下 7 只股票共 18 个 dims JSON 文件
**分析方法：** 解析 `*dims.json` 结构 + 代码链路追踪 + 根因定位

---

## 一、总体结论

| 维度类别 | 有数据 | 缺失 | 备注 |
|---------|--------|------|------|
| researcher 维度（technical/capital/ownership/sentiment/us_china/business） | **仅 688183** | **6/7 只股票** | 根因：调度路径问题 |
| system 维度（six_dim/bayesian/conclusion/signal/plan） | **全部无分** | **7/7 只股票** | 根因：依赖 researcher 数据 + six_dim.score=None |
| fundamental | **有结构，无数值** | **全部 7 只** | 根因：年报数据仅到 2024，2025 未发布 |
| sector | **有结构，无排行** | **全部 7 只** | 根因：板块排行数据源未接入 |

---

## 二、P0 — 调度路径错误：6只股票从未跑研究员

### 问题

7 只股票中，**只有 688183** 有 researcher 维度（technical/capital/ownership/sentiment/us_china/business）数据。其余 6 只股票的 dims JSON 里这些类别**全部为空字典** `{}`，说明研究员从未被调用。

### 根因

代码里有两条调度路径：

**路径 A（完整研究员）** → `deep_research_service.py` → `run_dual_track(dims_filter=None)` → **全部 11 个维度包括 researcher** ✅（688183 走这条）

**路径 B（轻量 subset）** → `longtrack_bridge.py` → `run_dual_track(dims_filter=set(LONG_TRACK_SUBSET))` → **只跑 system 维度，跳过所有 researcher** ❌（其他 6 只走这条）

```python
# longtrack_bridge.py:25
LONG_TRACK_SUBSET = frozenset({
    "data", "phase", "history", "six_dim", "bayesian",
    "scenarios", "conclusion", "plan", "signal", "supply_chain"
    # ← 注意：没有 technical/capital/ownership/sentiment/us_china/business
})
```

其他 6 只股票（300209/300260/300623/300911/600176/600519）通过 `longtrack_bridge` 调度，导致研究员 agent 根本未被执行。

### 修复方案

路径 B 的 `LONG_TRACK_SUBSET` 需要补全 researcher 维度：

```python
LONG_TRACK_SUBSET = frozenset({
    # system 维度
    "data", "phase", "history", "six_dim", "bayesian",
    "scenarios", "conclusion", "plan", "signal", "supply_chain",
    # ← 补全以下 researcher 维度
    "technical", "capital", "ownership", "sentiment", "us_china", "business",
})
```

---

## 三、P1 — 6 个研究员维度的具体问题（以 688183 为例）

### 3.1 ownership（已修复，等待验证）

- **状态：** `degraded`（Pydantic 验证错误）
- **原因：** `List[str]` → `List[HolderInfo]` 类型不匹配
- **修复：** 已在本地修复（H olderInfo 模型 + generic_parse 转换）
- **待验证：** 需重新跑一只股票确认 score 有值

### 3.2 capital

| 字段 | 值 | 状态 |
|------|-----|------|
| flow_score | None | push2his 接口超时（大陆访问不稳定） |
| institution_score | None | top10_holder_change 字段为 null |
| chip_score | **48.0** ✅ | 有数据 |
| score（维度总分） | **None** ❌ | chip 有分但维度总分 None |

**根因：** `capital_dim()` 函数在 `flow_score=None` 和 `institution_change_score=None` 时返回 None，即便 `chip_score=48.0` 有值。`build_six_dim` 调用 `capital_payload` 为 None 时无法使用 chip 数据。

**修复方案：** 在 `six_dim.py` 的 `build_six_dim` 中，当 `capital_payload` 为 None 但 `ctx.chip` 有数据时，传入完整 capital payload（包含 chip_score）。

### 3.3 fundamental

| 字段 | 值 | 状态 |
|------|-----|------|
| profitability.roe | **25.64** ✅ | 有数据 |
| profitability.gross_margin | 有数据 ✅ | — |
| growth_quality.revenue_yoy | **None** ❌ | 数据缺失 |
| growth_quality.net_profit_yoy | **None** ❌ | 数据缺失 |
| valuation_detail | **None** ❌ | 估值数据缺失 |

**根因：** Fuyao 年报 API 最新只到 2024 年报（2025-04-30 发布），而 2025 年报需 2026-04 左右才发布，所以 2025 年的营收/净利增速无法计算。AkShare 季报口径与 Fuyao 年报不一致，无法直接替换。

### 3.4 sector

| 字段 | 值 | 状态 |
|------|-----|------|
| base_rate | 0.45 ✅ | 行业基率有值 |
| sector_score | 57.0 ✅ | 有数据 |
| rankings | **None** ❌ | 板块实时排行数据源未接入 |
| policy_lean | **None** ❌ | 政策倾向未接入 |

**根因：** `sector_dim` 需要 AkShare 板块排行接口，目前未接入，`rankings` 字段为 None。

### 3.5 technical / sentiment / us_china / business

- **状态：** 有数据 ✅（688183）
- **备注：** 这 4 个维度在其他 6 只股票上完全没有，因为路径 B 跳过了研究员调用

---

## 四、P2 — system 维度全无分

### 根因分析

`six_dim.score`（六维总分）在所有 7 只股票都是 None。这不是因为数据缺失，而是 **`SixDimDim` schema 本身没有 top-level `score` 字段**——总分存在 `framework.total` 里，不在 `score` 字段。

`bayesian/conclusion/signal/plan` 依赖 `six_dim` 的输出，它们的 `score=None` 是因为各自的 `build_*` 函数在缺少必要输入时返回 None 而非中性默认值。

---

## 五、数据缺失汇总

| 缺失项 | 影响股票数 | 根因 | 可修复？ |
|--------|-----------|------|---------|
| researcher 维度（6只股票） | 6/7 | LONG_TRACK_SUBSET 缺维度 | ✅ 可修复 |
| ownership（Pydantic 错误） | 1/7（688183） | List[dict] 类型不匹配 | ✅ 已修复 |
| capital.score（有 chip 无总分） | 1/7（688183） | capital_dim() 逻辑问题 | ✅ 可修复 |
| 营收/净利增速 | 全部 7 只 | 2025 年报未发布 | ⚠️ 需等财报 |
| 板块实时排行 | 全部 7 只 | 数据源未接入 | ⚠️ 需接入接口 |
| six_dim.score=None | 全部 7 只 | schema 设计问题 | ⚠️ 需确认是否需要 |
| sentiment 社区数据 | 全部 7 只 | 雪球热帖返回 0 条 | ⚠️ 需换数据源 |

---

## 六、修复优先级建议

| 优先级 | 修复项 | 工作量 | 预期效果 |
|--------|--------|--------|---------|
| 🔴 P0 | LONG_TRACK_SUBSET 补全 researcher 维度 | 改 1 行 | 6 只股票获得完整研究员维度 |
| 🔴 P0 | capital.score 有 chip 分但维度为 None | 改 build_six_dim | capital 维度有总分 |
| 🟡 P1 | ownership 重新跑验证 | 跑一只股票 | 确认 Pydantic 修复生效 |
| 🟡 P1 | 接入 AkShare 板块排行数据 | 需开发接口 | sector.rankings 有数据 |
| 🟡 P1 | 资金流换 AkShare 备用源 | 需开发接口 | flow_score 有值 |
| 🟢 P2 | six_dim.score=None 确认是否需要修复 | 确认需求 | — |
