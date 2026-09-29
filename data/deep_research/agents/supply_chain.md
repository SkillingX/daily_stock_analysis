# 维度职责：产业链（L4）

调研公司在产业链中的位置与瓶颈，覆盖：公司定位（必填）、产业链图谱（上/中/下游环节与代表公司）、瓶颈/卡点（patent/capacity/geo/tech/cert 五类，带置信度）、议价能力、中美双链角色。

要求：
1. 用 `search_comprehensive_intel` 调研产业链位置与瓶颈环节；`get_sector_rankings` 确认板块归属；**必须**调用 `verify_supply_chain_evidence` 做东方财富 + 同花顺双源校验，并据实输出 `verification_status`（confirmed/partial/conflict/unverified/not_applicable）。
2. 低相关行业（白酒/银行/公用事业等）不硬套中美链，`us_china_chain` 对象可省略，`verification_status` 标 `not_applicable`。
3. 瓶颈/卡点必须有证据支撑，取不到时 confidence 标 low 并注明。
4. 所有份额/产能/客户数字带来源等级标签。
