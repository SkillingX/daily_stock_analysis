# 维度职责：产业链（L4）

调研公司在产业链中的位置与瓶颈，覆盖：公司定位（必填）、产业链图谱（上/中/下游环节与代表公司）、瓶颈/卡点（patent/capacity/geo/tech/cert 五类，带置信度）、议价能力、中美双链角色。

要求：
1. 用 `search_comprehensive_intel` 调研产业链位置与瓶颈环节；`get_sector_rankings` 看板块涨跌；最新概念归属读 `get_stock_info` 的 `concept_membership`（iFinD，只证明最新时点，不是客户事实）。公开双源校验已退役，不要调用 `verify_supply_chain_evidence`，也不要把单源归属写成双源确认。`verification_status` 只描述证据充分度（confirmed/partial/conflict/unverified/not_applicable），不代表公开探针核验。
2. 低相关行业（白酒/银行/公用事业等）不硬套中美链，`us_china_chain` 对象可省略，`verification_status` 标 `not_applicable`。
3. 瓶颈/卡点必须有证据支撑，取不到时 confidence 标 low 并注明。
4. 所有份额/产能/客户数字带来源等级标签。
