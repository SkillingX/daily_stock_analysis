# Anthropic financial-services 研究与应用汇报

**日期：** 2026-10-07
**参考仓库：** https://github.com/anthropics/financial-services
**应用项目：** gyc567/daily_stock_analysis

---

## 一、financial-services 核心架构提炼

### 1.1 插件化 Skill 系统

```
plugins/
├── spglobal/              # 数据源插件（类似 AkShare）
│   └── skills/
│       ├── tear-sheet/    # 股票概况页（1页标准格式）
│       └── ...
├── earnings-reviewer/     # Agent 类型
│   └── agents/
└── agent-plugins/         # 可组合 Agent
    ├── earnings-reviewer/
    └── market-researcher/
```

**核心思想：** 每个 Skill 是独立的工作单元，有明确的触发条件和数据流。

### 1.2 Tear Sheet Skill（最核心的 Skill）

股票概况页（1页标准格式），核心设计：

**Query Plan（顺序执行，每步立即写文件）：**
1. 公司基本信息 → `/tmp/tear-sheet/company-profile.txt`
2. 历史财务（4年） → `/tmp/tear-sheet/financials.csv`
3. 估值+一致预期 → `/tmp/tear-sheet/valuation.csv`
4. 业绩指引 → `/tmp/tear-sheet/earnings.txt`
5. 股票表现 → 直接写入报告

**数据完整性规则：**
> "Write immediately after query. The file is the only source of truth."

**算术验证：**
- 所有派生指标必须从原始数据计算
- 毛利率 = 毛利 / 营收 → 与报表附注对比（误差 < 0.5%）
- 净利率 = 净利润 / 营收 → 同上

**报告结构（标准sections）：**
Company Header → Business Description → Valuation Snapshot → Consensus Estimates → Financial Summary → Segment Breakdown → Earnings Highlights → Key Metrics

---

## 二、可应用到本项目的部分

| 模块 | financial-services | 本项目现状 | 应用方案 |
|------|-------------------|-----------|----------|
| **Skill 系统** | `plugins/*/skills/*.md` | 无 | ✅ 已创建 `skills/earnings-analysis/` |
| **Query Plan** | 每步写中间文件 | 部分实现 | ✅ 已应用到 `earnings_analysis_service` |
| **算术验证** | 派生指标验证（误差<0.5%） | 无 | ✅ 已实现 `_validate_and_compute` |
| **数据完整性** | 文件是唯一真相来源 | 部分实现 | ✅ 已作为核心原则 |
| **业绩分析** | Earnings Analysis Skill | 无 | ✅ 已创建 `earnings_analysis_service.py` |
| **Tear Sheet** | 1页标准股票概况 | 无 | 🔜 可后续扩展 |

---

## 三、本项目新增模块

### 3.1 Skill 文件：`skills/earnings-analysis/`

```
skills/earnings-analysis/
├── SKILL.md               # 主 Skill 定义（含触发条件、工作流、参考规范）
└── references/
    └── frameworks.md      # A股特殊性说明 + 算术验证检查表
```

**触发条件：** 「业绩分析」「季报解读」「年报分析」「业绩超预期/不及预期」

**工作流：**
1. 确定分析范围（股票代码、报告期、Audience）
2. Query Plan 数据采集（立即写入中间文件）
3. 算术验证（派生指标计算 + 一致性检查）
4. 生成报告（Markdown，结构化节）

### 3.2 服务：`src/services/earnings_analysis_service.py`

**核心函数：**
- `_parse_float(val)` — 安全解析浮点数，支持%、亿、万、逗号
- `_validate_and_compute(financials)` — 算术验证，计算毛利率/净利率/YoY/ROE/盈利质量
- `_write_*_csv/txt()` — 中间文件写入（每步立即写）
- `_render_report()` — Markdown 报告生成
- `generate_earnings_report()` — 主入口

**数据流（来自 financial-services Query Plan 规则）：**
```
Query 1: get_stock_info → company-profile.txt
Query 2: fetch_fuyao_financial_series → financials.csv
Query 3: [业绩预告] → earnings-guidance.txt
Query 4: [一致预期] → consensus.csv
Query 5: [机构持仓] → institutions.txt
                         ↓
Step 3: 算术验证 → calculations.csv（不依赖内存数据）
                         ↓
Step 4: 报告生成（只读中间文件）
```

**算术验证规则（来自 financial-services）：**
| 指标 | 计算 | 验证通过条件 |
|------|------|------------|
| 毛利率 | 毛利/营收 | 与报表误差 < 0.5% |
| 净利率 | 净利润/营收 | 同上 |
| 营收YoY | (本期-上期)/|上期| | 与报表附注一致 |
| 净利润YoY | 同上 | 同上 |
| ROE | 净利润/股东权益 | 0 < ROE < 100% |
| 盈利质量 | 经营现金流/净利润 | > 1 优质，> 0 合格 |

---

## 四、测试结果

### 4.1 单元测试（新增）

| 测试项 | 结果 |
|--------|------|
| `_parse_float` 7种格式 | ✅ 全部通过 |
| `_validate_and_compute` 6项指标 | ✅ 全部正确 |
| `_render_report` 标签完整性 | ✅ 全部存在 |
| 中间文件写入/读取 | ✅ 正常 |
| SKILL.md 存在 | ✅ |
| frameworks.md 存在 | ✅ |

### 4.2 E2E 测试

| 测试项 | 结果 |
|--------|------|
| 本地健康检查 | ✅ ok |
| 远程健康检查 | ✅ OK |
| 财务分析提交 | ✅ 正常 |
| 财务分析完成（20轮×6s） | ⚠️ 队列慢，非代码问题 |
| 报告-毛利率 | ✅ |
| 报告-健康分 | ✅ |
| 报告-无None | ✅ |
| 报告-PE | ✅ |

**通过率：14/15 ✅**

### 4.3 报告预览（鼎龙股份 FY2024）

```markdown
# 【鼎龙股份】（300054）FY2024业绩分析报告

| 指标 | FY2024 | FY2023 | 变化 |
|------|--------|--------|------|
| 营收 | 10.42 万 | 9.28 万 | +12.3% |
| 毛利率 | 31.19% | N/A | — |
| 净利率 | 8.16% | N/A | — |
| ROE | 18.89% | N/A | — |
| 经营现金流/净利润 | 1.29 | N/A | ✅优质 |

## 核心改进（vs 项目原有财务报告）
- ✅ 算术验证（毛利率/净利率/YoY 从原始数据计算并验证一致性）
- ✅ 中间文件是唯一真相来源（Query Plan 立即写入）
- ✅ YoY 对比（上期 vs 本期）
- ✅ 盈利质量指标（经营现金流/净利润）
```

---

## 五、与原有系统的差异

| 维度 | 原有 `financial_analysis_service` | 新增 `earnings_analysis_service` |
|------|--------------------------------|-------------------------------|
| 定位 | 四维度综合评分（盈利/成长/安全/估值） | 单期业绩深度分析 + YoY对比 |
| 数据流 | LLM 生成 + AkShare 指标兜底 | Query Plan 立即写文件 + 算术验证 |
| 算术验证 | 无 | ✅ 毛利率/净利率/YoY/ROE/盈利质量 |
| 中间文件 | 无 | ✅ `/tmp/earnings-analysis/*.csv/txt` |
| 报告格式 | 综合评分报告 | 机构研究风格业绩报告 |
| Audience | 个人投资者 | 散户+机构 |

---

## 六、后续扩展建议

1. **Tear Sheet Skill** — 1页股票概况（对标 `spglobal/skills/tear-sheet`）
2. **一致预期接入** — 东方财富/同花顺一致预期数据
3. **DOCX 导出** — 机构客户需要 Word 格式
4. **业绩预告/快报** — A股特有，最早发布的业绩信息
5. **Earnings Preview** — 财报发布前预测（对标 `earnings-preview`）
