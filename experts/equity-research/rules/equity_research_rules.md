---
description: Equity research core rules - financial data timeliness and reporting standards. Use when conducting equity research and investment analysis.
alwaysApply: true
enabled: true
updatedAt: 2026-08-02T00:00:00.000Z
provider: 
---

## 财务数据时效性原则

获取基本面数据时，必须根据当前日期判断目标公司最新可获取的报告期次，而不是默认拉取年报。不同市场的财报披露节奏不同，请据此动态选择：

- **中国 A 股**：一季报（4月底前）、中报（8月底前）、三季报（10月底前）、年报（次年4月底前）
- **美股**：10-Q 按季披露（财季结束后 40-45 天）、10-K 年报（财年结束后 60-90 天）
- **港股**：中报（9月底前）、年报（次年3月底前）

**核心逻辑**：先确认"此刻能拿到的最新一期财报是什么"，再去获取数据。

**行情与多源资料**：交付前以最新数据日期为准，当日数据不全时标注实际采用日期与缺口，不得静默回退到更早日期；综合多份带时间戳的文档时，以最新且未被推翻的结论为锚，并注明出处日期。

## 数据源优先级

行情、财务、估值、一致预期、公告与新闻等金融数据按以下顺序获取，前一级拿不到或缺字段时再用下一级：

1. **用户已连接的金融类 MCP**（行情、财务、资讯类连接器）：结构化数据以它为准
2. **agentic search**
3. **WebSearch**

各子技能里出现的 web search、Bloomberg / FactSet / EDGAR 等，指资料类型或引用出处，实际取数一律按本顺序。取自第 2、3 级的关键数值标注来源与日期；财务明细仍以公司正式披露为准，检索到的转述只作线索。

## Important Notes

- All financial outputs should be reviewed by qualified professionals
- Research outputs are analytical frameworks and should not constitute investment advice
- Always disclose: "本报告仅供研究参考，不构成个人投资建议"
