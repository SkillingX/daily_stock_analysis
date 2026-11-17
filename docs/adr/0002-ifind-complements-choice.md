---
status: superseded-in-part
---

# iFinD 与 Choice 的有限双源协作

2026-11-13 修订（[Issue #94](https://github.com/SkillingX/daily_stock_analysis/issues/94)）：启用范围内已采用日线窗口未被替换前，扩窗需求把已采用来源作为 `DataFetcherManager` 来源约束传入——只尝试同源候选，其余数据源（含 iFinD 预置尝试，除非约束即 iFinD）不再出站，消除前置研究（[Issue #89](https://github.com/SkillingX/daily_stock_analysis/issues/89)）记录的「iFinD 长窗失败后东财真实出站、候选却被跨源整窗一致性保护拒绝」的注定冗余出站。换源/重叠冲突的拒绝语义、失败原因、超时/停止复查与整窗资格校验全部保留，候选相对次序不变；约束不适用市场显式失败而非静默忽略。尝试级预算预留与 fallback_to/切换/剩余未尝试计数诊断按受约束后的真实候选链计算，不为被排除候选预留预算或报告虚假 fallback。同源真实失败（manager 错误、源级超时、空窗口、裁剪失败）经 run owner 方法 `record_failed_daily_expansion` 进入与换源拒绝同一份持久未满足记录（锁内按权威帧复核，首取失败与取消不误标），失败原因在 loader 原因构造处、unmet 持久边界与报告历史装配注记入口按既有诊断脱敏规范统一脱敏限长（保留可诊断错误类型与安全上下文），报告注记与后续真正长窗采纳的锁内消解照常工作。标准 HTTP CPS=6 同口径整窗候选因供应商量单位契约尚未取得（两股样本仅支持换算假设，见 [Issue #90](https://github.com/SkillingX/daily_stock_analysis/issues/90)/[#91](https://github.com/SkillingX/daily_stock_analysis/issues/91) 复审澄清）不启用，保留既有 MCP、合格非东财与必要东财兜底；实时主行情与深研产业链行业涨跌排行按前置资格结论保持原路由。语义与边界详见[接入说明](../ifind-mcp-integration.md) issue #94 一节。

2026-11-08 修订（[Issue #86](https://github.com/SkillingX/daily_stock_analysis/issues/86)）：日线 K 线范围更新——前文「行情沿用原渠道」对 A 股日线不再完整。`DATA_SOURCE_PROFILE=choice` + `ENABLE_IFIND_MCP=true` 且配置有效时，沪深普通股「截至当前最新已完成交易日」的现金分红前复权日线优先经 iFinD MCP 获取（`IfindDailyFetcher` 复用既有 MCP 客户端/预算/单飞）：四个价格字段逐字段现金分红复权元数据、量（股）/额（元）单位、严格交易日历整窗六字段齐备才采纳；IPO 泛复权、停牌量额缺失、截断、冲突重复整窗拒绝并按既有候选相对次序兜底。显式历史目标与未资格证券（含指数/ETF/B 股/北交所）在任何 iFinD 出站前走原路由。完整 CSV 导出与 MCP 共享同一次尝试的有限绝对 deadline，仅允许 `o.thsi.cn` 已验证结果缓存路径（唯一 HTTP→HTTPS 协议升级），使用禁用 ambient 凭据发现的隔离传输。启用范围内持久 DB 不作为分析读缓存（当前股票任务共享窗口 + 深研 ResearchRun 槽位复用完整同源窗口）；任何模式下含本票遗留 iFinD 行（含混合）的 DB 窗口被拒绝走原链，纯旧源混存缓存保持既有规则。实时行情、分钟线与其他市场不在本修订范围。关闭日线 iFinD 不能只关 `ENABLE_IFIND_MCP`：若 `FINANCIAL_PRIMARY_SOURCE=ifind`，必须同时改回 `choice` 再关闭开关并重启，该组合也会把既有财务 iFinD 切回 Choice。语义与边界详见[接入说明](../ifind-mcp-integration.md) issue #86 一节。

2026-11-02 修订（[Issue #73](https://github.com/SkillingX/daily_stock_analysis/issues/73)）：iFinD 主源深度投研在深研身份（深研 run 上下文 + 深研 scope 开关）内实现零 Choice 出站——Choice header/六字段/资产负债/估值/目标价/公司补查/独立核验与资金失败回退全部不再发起，公司事实与公司名称由 iFinD 提供，目标价以「未查询」语义走获准的非估值分析；非深研消费者与 `choice` 主源的 Choice 补充/目标价/公司行为不变。语义与边界详见[接入说明](../ifind-mcp-integration.md) issue #73 一节。

2026-11-01 修订（[Issue #71](https://github.com/SkillingX/daily_stock_analysis/issues/71)）：`FINANCIAL_PRIMARY_SOURCE=ifind` 时，当前财务快照（六字段+同财期资产负债）、当前估值/市值、正式申万一级行业与个股资金 1/5/10 日窗口迁移至 iFinD 主源，Choice 降为有界补充；完整年报与机构目标价仍由 Choice 提供，交易日历以本地严格日历为准（不以 Choice 登录兜底）。语义与边界详见[接入说明](../ifind-mcp-integration.md) issue #71 一节。

2026-10-15 修订：用户确认当前六项财务可显式选择 iFinD 优先，Choice 同期补值/核验；主营和个人股东明细优先保留 iFinD。完整年报、估值、资金与机构目标价仍由 Choice 提供，行情沿用原渠道。以最新有效实际财期建快照，在同一期按主源优先；未知调整口径不放宽，旧期不能填新期。复用现有共享上下文、核验和缓存，不建设供应商框架。配置与回退见[接入说明](../ifind-mcp-integration.md)，新工作状态见 [Issue #23](https://github.com/SkillingX/daily_stock_analysis/issues/23)。下文保留原决策背景，其主源顺序由本次修订替代。

2026-10-14，用户确认保留 Choice 为 A 股常规财务/行情主源，恢复 iFinD 的当前财务快照六项指标补值/独立核验、主营分产品和前十大股东资料能力。此决策仅调整 [ADR 0001](0001-choice-native-data-policy.md) 对 iFinD 的停用范围；MX、旧 Choice MCP、Fuyao 继续停用，不通过 legacy 绕过隔离。iFinD 四年完整年报替代、资金流窗口、行情主源切换、目标价及新闻/宏观接入不在本批范围。

选择有限补充而非全量双源互替，是因为现有报告消费契约和真实样本已提供复用基础，但尚未证明 iFinD 覆盖 Choice 的完整能力。两家来源只有同财期、单位和口径等身份条件满足时才独立核验；补值不等于核验通过，冲突不取平均或静默覆盖。单独缺披露日/原文链接不丢弃可展示的有效资料，计算与核验仍遵守数据资格要求，不能据此证明历史时点可得性。

启用 iFinD 后，包含财务快照的报告默认核验本批六个核心指标，即使 Choice 已有值；同一批返回兼顾补值和核验，复用有效缓存并遵守强制刷新。主营与股东资料按需补充；核验失败保留 Choice 有效内容并披露原因，等待受报告总预算约束。

本决策记录已确认的产品边界与默认行为，不表示运行时已启用，也不授权部署。实施与验收依据见[正式规格 Issue #15](https://github.com/SkillingX/daily_stock_analysis/issues/15)及[接入方案](../ifind-mcp-integration.md)；复用现有来源、缓存和质量机制，不建设通用供应商框架。

2026-10-14 施工补充：用户明确约定本批 iFinD A 股标为“元”的金额按人民币处理。保留供应商原始币种缺项并在报告披露部署约定；报表/调整口径仍需实际返回或已验证指标契约证明，不扩展为其他市场或其他缺失元数据的默认值。
