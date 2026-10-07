---
name: earnings-analysis
description: "A股业绩分析报告生成器。适用场景：季报/年报发布后分析、业绩预告/快报解读、与预期对比的 earnings beat/miss 分析。当用户要求「业绩分析」「季报解读」「年报分析」「业绩超预期/不及预期」「分析[股票]财报」时使用。"
---

# A 股业绩分析报告生成器

基于 AkShare 实时数据 + Fuyao LLM API，生成符合机构研究标准的业绩分析报告。

## 核心原则（来自 Anthropic financial-services 数据完整性规则）

> **中间文件是唯一真相来源。** 所有数据必须先写入文件，报告生成阶段只读文件，不依赖内存中的数据。

## 工作流

### Step 1: 确定分析范围

1. **股票代码** — 如未提供，通过 `get_stock_info` 解析
2. **报告期** — Q1/Q2/Q3/Q4/年度，如未提供默认最新一期
3. **Audience** — 默认「散户/个人投资者」，如需机构版本请告知

### Step 2: 数据采集 Query Plan

> 每一步查询后**立即写入**中间文件，不要等所有查询完成后再写。

建立工作目录：
```bash
mkdir -p /tmp/earnings-analysis/
```

**Query 1 — 公司基本信息 + 最新行情**
调用 `get_stock_info` → 写入 `/tmp/earnings-analysis/company-profile.txt`

**Query 2 — 财务报表（利润表 + 资产负债表 + 现金流量表）**
调用 `get_financials`（从 financial_analysis_service 获取）→ 写入 `/tmp/earnings-analysis/financials.csv`

**Query 3 — 业绩预告/快报（如有）**
调用 `search_service`（Tavily）搜索「[股票名] 业绩预告 [年份]」→ 写入 `/tmp/earnings-analysis/earnings-guidance.txt`

**Query 4 — 一致预期（分析师预期）**
调用 AkShare 或 search_service → 写入 `/tmp/earnings-analysis/consensus.csv`

**Query 5 — 机构持仓变化**
调用 `get_stock_info` 的 `institution_holding` → 写入 `/tmp/earnings-analysis/institutions.txt`

### Step 3: 计算派生指标

读取所有中间文件，**仅做计算，不做查询**：

```python
# 毛利率 = 毛利 / 营收
gross_margin = gross_profit / revenue

# 净利率 = 净利润 / 营收
net_margin = net_profit / revenue

# YoY 增长率 = (本期 - 上期) / |上期|
yoy_growth = (current - prior) / abs(prior)

# ROE = 净利润 / 股东权益
roe = net_profit / equity

# 经营现金流 / 净利润（盈利质量）
cash_quality = operating_cash_flow / net_profit
```

验证所有算术：
- [ ] 毛利率 × 营收 ≈ 毛利（误差 < 1%）
- [ ] 净利率 × 营收 ≈ 净利润（误差 < 1%）
- [ ] YoY 增长率与报表附注一致
- [ ] 各科目占比之和 ≈ 100%（误差 < 2%）

**算术验证失败** → 尝试从原始组件重新计算 → 仍失败则标记为「N/A」而非错误数字

结果写入 `/tmp/earnings-analysis/calculations.csv`：
```
metric,value,formula,components
gross_margin_fy2024,31.2%,gross_profit/revenue,"3250/10420"
revenue_yoy_fy2024,12.3%,(current-prior)/prior,"10420/9280"
```

### Step 4: 生成报告

读取中间文件，生成 Markdown 报告，写入 `reports/earnings/`

**报告结构：**

```markdown
# [公司名] [报告期] 业绩分析报告

> 报告日期 | 股票代码 | 报告期

## 一、业绩概览

| 指标 | 本期 | 上年同期 | YoY | 一致预期 | 超出预期 |
|------|------|----------|-----|----------|----------|
| 营收（亿）|  |  |  |  |  |
| 毛利（亿）|  |  |  |  |  |
| 净利润（亿）|  |  |  |  |  |
| 毛利率 |  |  |  |  |  |
| 净利率 |  |  |  |  |  |
| ROE |  |  |  |  |  |
| EPS（元）|  |  |  |  |  |

## 二、业绩点评

### 2.1 营收端
- 分析要点...

### 2.2 毛利率变化
- 分析要点...

### 2.3 费用率分析
- 分析要点...

### 2.4 现金流质量
- 分析要点...

## 三、业绩与预期对比

| 指标 | 实际 | 预期 | 差值 | 超出幅度 |
|------|------|------|------|----------|
| 营收 |  |  |  |  |
| 净利润 |  |  |  |  |

## 四、业绩前瞻

基于最新一期财报，展望下期业绩趋势...

## 五、风险提示

- ...
```

## 数据完整性规则

1. **AkShare API 是财务数据唯一来源**，不要用训练数据补充缺失字段
2. **标注拿不到的数据**：用「N/A」或「未披露」而不是留空
3. **日期必须标注**：注明财报期和发布日，不假设自然年 = 财年
4. **不混用报告期**：FY2023 营收和 LTM EBITDA 要分别标注
5. **一致预期注明来源**：Wind/同花顺/自行估算需标注
6. **永远使用合并报表数据**：母公司数据需特别说明

## 算术验证规则

所有派生指标必须从原始数据计算，验证通过后才写入报告：

| 验证项 | 公式 | 通过条件 |
|--------|------|----------|
| 毛利率一致性 | 毛利/营收 vs 报表毛利率 | 误差 < 0.5% |
| 净利率一致性 | 净利润/营收 vs 报表净利率 | 误差 < 0.5% |
| 营收 YoY | (本期-上年)/上年 | 与报表附注一致 |
| 盈利质量 | 经营现金流/净利润 | > 0 为高质量 |
| ROE 合理性 | 净利润/股东权益 | 0 < ROE < 100% |

验证失败的指标标记为「N/A」，不写错误数字。

## 中间文件规范

| 文件 | 格式 | 内容 |
|------|------|------|
| `/tmp/earnings-analysis/company-profile.txt` | Key-Value | 公司名、代码、行业、总股本、市值、PE、PB |
| `/tmp/earnings-analysis/financials.csv` | CSV | 报告期、科目、值、来源 |
| `/tmp/earnings-analysis/earnings-guidance.txt` | Text | 业绩预告/快报内容 |
| `/tmp/earnings-analysis/consensus.csv` | CSV | 指标、预期值、来源 |
| `/tmp/earnings-analysis/institutions.txt` | Text | 机构持仓变化 |
| `/tmp/earnings-analysis/calculations.csv` | CSV | 指标名、计算值、公式、原始数据 |

## 与 financial-services 差距说明

| 模块 | financial-services | 本项目现状 | 行动计划 |
|------|-------------------|-----------|----------|
| MCP 数据源 | Kensho/S&P Capital IQ | AkShare | 接入更多 AkShare 财务接口 |
| 文档格式 | DOCX（docx-js） | Markdown | 后续扩展 DOCX 导出 |
| 一致预期 | FactSet/Bloomberg | 有限 | 接入东方财富一致预期 |
| 机构持仓 | S&P 数据 | 部分 | 增强机构持仓数据 |
