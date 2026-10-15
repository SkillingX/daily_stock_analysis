# iFinD 与 Choice 接入：当前财务主源选择

2026-10-15 新决策（工单 #23）：在已接通的六项当前财务、主营分产品和个人股东明细内优先 iFinD，Choice 同期补缺和独立核验。下面原规格记录此前 Choice 优先的施工背景；本节主次规则优先生效，原数据资格与验收护栏继续适用。

## 当前部署配置

本机根目录私有 `.env` 或生产部署配置使用：

```dotenv
DATA_SOURCE_PROFILE=choice
ENABLE_IFIND_MCP=true
FINANCIAL_PRIMARY_SOURCE=ifind
IFIND_MCP_ENDPOINT=<供应商提供的地址>
IFIND_MCP_TOKEN=<供应商提供的授权值>
IFIND_MCP_TIMEOUT_SECONDS=30
```

配置修改后重启实际后端/分析/调度进程，无需每次启动临时注入。主源默认 `choice`，保持已有部署兼容；选择 `ifind` 必须同时开启上述策略和必要配置。回退设置 `FINANCIAL_PRIMARY_SOURCE=choice`；可进一步关闭 iFinD，停用 MX、旧 Choice MCP 和 Fuyao 的部署策略不变。

当前快照按两源实际返回选最新有效累计/年度财期，不由请求日期补造。同期内合格 iFinD 值优先，Choice 同期补位；更新财期有缺项时不倒退用旧期填充。单独缺披露日保留有效资料，但不因此声称历史时点可得。四个比率缺调整口径时仍不默认重述后，可比性与规则资格继续按字段证据判断。冲突保留已采用主值与两家证据，限制相应确定性评分/动作。

查询当前财务时，iFinD 使用一次明确日期范围的逐期请求，要求每行实际报告期；“最新一期”参数本身不能证明财期。范围由运行日期计算，不以 Choice 的返回日期锁定 iFinD；未披露期的空行不作为选期依据。若供应商仍未提供实际财期，保留候选和缺口，不从请求补造；这一请求方式不扩展到语义不同的股东截止日期。

完整年度财报、PE/PB/总市值、资金流和机构目标价仍由 Choice 提供；日线和实时行情沿用原渠道。主营及股东资料保留各自实际财期，不强行跟随财务快照或 Choice 名称表的旧期。混合来源报告按字段标明实际来源。主次策略进入既有缓存身份，切换或强刷不会给新值贴旧核验；历史报告不重标。

本节为中文部署与行为说明；未维护平行英文规格，避免复制两套不断变化的数据契约。英文首页无需扩展。

## 从 Choice 单源版本升级的配置变更

代码升级不会自动开启 iFinD、切换模型或覆盖部署凭据。本次取消/证券校验修复不新增配置项；整批版本相对 Choice 单源版本的配置变化如下。生产使用部署平台的环境变量和凭据配置，本机使用被忽略的根 `.env`；已有进程环境变量优先于文件。

| 配置 | 兼容默认 / 变化 | 本次部署的设置 |
| --- | --- | --- |
| `DATA_SOURCE_PROFILE` | 沿用既有策略，示例默认 `legacy` | 本部署保持 `choice`，不要通过 `legacy` 恢复退休渠道 |
| `ENABLE_IFIND_MCP` | 默认 `false`，仅残留 Token 不会启用 | 使用本批能力设为 `true` |
| `FINANCIAL_PRIMARY_SOURCE` | 新增，默认 `choice` | 本次 iFinD 优先设为 `ifind` |
| `IFIND_MCP_ENDPOINT` / `IFIND_MCP_TOKEN` | 沿用供应商地址和授权值；服务器不需 iFinD SDK/设备激活 | 两项都必须由部署配置提供，不提交到仓库或镜像 |
| `IFIND_MCP_TIMEOUT_SECONDS` | 默认由 `8` 秒调整为 `30` 秒 | 可保持 `30`；旧部署显式配置不会被自动改写，仍受阶段总预算限制 |
| `CODING_PLAN_SEARCH_PROVIDER` | 新增，默认 `minimax` | 线上保留 `minimax`；本地替代原 MiniMax 搜索位置时设 `zhipu` |
| `LLM_ZHIPU_API_KEY` | 复用已有套餐 Key，仅选智谱搜索时必需 | 本机提供，不另申请搜索 Key；线上继续使用原 MiniMax 配置 |
| `TAVILY_API_KEYS` | 本版本停用 Tavily，遗留值不再触发调用 | 无需补配；可从部署凭据中移除，其余搜索工具不变 |
| `DEEP_RESEARCH_LLM_CONCURRENCY` | 默认 `3`；行为改为限制同一进程所有模块的实际模型请求 | 当前共享 Coding Plan 建议显式设 `1`；独立后端、CLI、调度进程分别计数 |
| `DEEP_RESEARCH_TIMEOUT_SECONDS` | 有限报告总预算默认 `2400` 秒，包含取数、排队、研究与收尾 | 可保持默认；结束预算后保存有效资料，在途资源清理不被伪装成已完成 |
| `LLM_STREAM_RESPONSE_TIMEOUT` | 既有单次模型响应上限，未统一改默认值 | 本机 GLM 验收用 `180` 秒，依据实测返回耗时；线上按实际模型设置，不机械照搬 |
| `CHOICE_FORCE_LOGIN` | 新增，默认 `false` | 共享试用账号错峰且明确允许挤掉另一端时才设 `true`；不增加同时登录容量 |
| `CHOICE_USERNAME` / `CHOICE_PASSWORD`、`ENABLED_MARKETS` | 沿用既有配置 | Choice 保留原登录方式和 SDK；本部署保持 `ENABLED_MARKETS=cn` |

模型仍由原 `LITELLM_CONFIG` / 渠道配置决定。线上 MiniMax 模型入口不因本次升级改变；本机智谱保持套餐入口 `https://open.bigmodel.cn/api/coding/paas/v4`，模型与推理参数见现有 YAML。搜索选择不自动切换模型，失败也不改走智谱普通按量 API；路由本身不作为账单归属证明。套餐搜索细节见[搜索配置](coding-plan-search.md)，模型执行边界见[深度投研说明](deep-research.md)。

上线顺序：先提供上述部署配置及现有 Choice SDK/登录条件，更新代码或镜像，再重启实际 API、分析及调度进程；不要直接复制本机 `.env`。核对生效的主次/搜索/并发配置时只输出非敏感值，Token 和账号密码保持遮罩。启动成功后强刷普通企业及金融行业报告，检查采用源、实际财期、核验状态、有效明细、取消后无新请求及历史/下载一致；Choice 回退可用不等于 iFinD 主源正向验收通过。

优先保留新代码，只回退配置：设 `FINANCIAL_PRIMARY_SOURCE=choice`、`ENABLE_IFIND_MCP=false` 并重启；智谱搜索可独立恢复 `CODING_PLAN_SEARCH_PROVIDER=minimax`。完整代码回滚会恢复旧版 Tavily 入口，必须保留 Tavily 退休变更，或先停用所有运行环境及 Actions 中的 `TAVILY_API_KEYS` 注入，再 revert 对应 PR 并重启；不能假定残留 Key 在旧代码中仍无效。无数据库迁移，原报告与其他凭据不清空。共享账号的另一端会话不会因代码回滚自动恢复。

---

# iFinD 补充 Choice 与核心财务独立核验规格

状态：四项产品决策已确认；两名独立 reader 均通过，正式规格已发布为 [Issue #15](https://github.com/SkillingX/daily_stock_analysis/issues/15)（`ready-for-agent`），已开始施工；各票验收证据见实施记录。实施分支 `feat/ifind-integration` 从本地 `dev`（`1e27128`）创建。以 GitHub Issue 跟踪工作状态，本文保留规格正文；规格修改应同步，不能维护互相竞争的工单状态。

术语采用项目领域词表，来源决策依据 ADR 0001 与 ADR 0002。该规格只有限恢复 iFinD，不代表当前运行时已启用。

实施子工单：[六项财务核验 #16](https://github.com/SkillingX/daily_stock_analysis/issues/16)、[主营分产品 #17](https://github.com/SkillingX/daily_stock_analysis/issues/17)、[股东明细 #18](https://github.com/SkillingX/daily_stock_analysis/issues/18)。每票一次提交；#17、#18 均以 #16 验收完成为前置，彼此不阻塞。工单正文和原生依赖以 GitHub 为准。

## Problem Statement

报告读者需要财务数据完整、可追溯，并能知道不同供应商是否相互印证。当前部署以 Choice 为主源，旧 iFinD 路径被隔离；主源缺项时无法利用本次获准的 iFinD 服务补充，已有值也不能获得同花顺来源的独立核验。主营分产品及股东明细存在补充需求。

已有 iFinD 代码可以复用，但真实 demo 已证明原样打开并不可用：多表格结果可能漏读，表头“元”与数值“万/亿”的组合会被误判为单位冲突，部分日期只在指标参数中。供应商返回本身也存在“表格明确日期、参数写最新一期”、公告日有时缺失等语义问题。请求成功、返回有数和报告正确是三个不同结论。

## Solution

保留 Choice 为常规财务/行情主数据源，以 iFinD 补充当前财务快照六项核心指标、主营分产品和前十大股东资料。启用后，即使 Choice 数据齐全，也默认核验这六项；同一批返回兼顾缺项补值与已有值核验，复用有效缓存，资料按需补充。

六项指标为：营业收入、归母净利润、加权 ROE、销售毛利率、营业收入同比、归母净利润同比。它们必须对应同一实际财期及相容的报表、调整、币种和计量口径。供应商冲突、不可比、单源、未完成核验分别表达，不以平均值或静默覆盖掩盖差异。

证券、数值、单位和实际财期等足够明确的资料，不因单独缺公告日或原文链接整块丢弃；报告披露缺项，参与补值、计算或独立核验仍须满足各自的数据资格。失败和超时保留有效 Choice 内容，报告等待受既有总预算约束。

## User Stories

1. As a report reader, I want Choice to remain the primary source, so that existing report behavior remains predictable.
2. As a report reader, I want six core financial metrics independently checked even when populated, so that completeness is not mistaken for correctness.
3. As a report reader, I want eligible iFinD values to fill missing fields, so that a partial primary response does not unnecessarily remove useful facts.
4. As a report reader, I want a supplemented value to retain its actual source, so that supplementation is not presented as independent verification.
5. As a report reader, I want the actual reporting period displayed, so that older data cannot silently substitute for the current period.
6. As a report reader, I want annual, year-to-date and single-quarter values distinguished, so that incompatible financial periods are not compared.
7. As a report reader, I want parent-attributable profit and weighted ROE identified correctly, so that nearby but different metrics cannot replace them.
8. As a report reader, I want units, currencies and numeric scales preserved, so that values differing by ten thousand are not treated as equivalent.
9. As a report reader, I want zero and negative values retained, so that legitimate business results are not mistaken for missing data.
10. As a report reader, I want conflicting supplier readings disclosed, so that I can see uncertainty without losing the primary facts.
11. As a report reader, I want dependent scores and conclusions to respect data quality, so that an attractive narrative cannot override a conflict.
12. As a report reader, I want unavailable verification distinguished from agreement, so that timeouts and permission errors never look like successful checks.
13. As a report reader, I want usable facts retained when a disclosure date or original link is missing, so that one missing metadata field does not erase a whole section.
14. As a report reader, I want missing disclosure dates explicitly identified, so that a financial period end cannot masquerade as a publication date.
15. As a report reader, I want supplier traceability distinguished from an original filing citation, so that the evidence is not overstated.
16. As a report reader, I want product-level revenue, cost and gross profit presented with their period and coverage, so that business composition is understandable.
17. As a report reader, I want product margins distinguished from the company margin, so that segment data cannot distort the overall financial assessment.
18. As a report reader, I want shareholder names, quantities and percentages joined by identity and period, so that one shareholder's values cannot appear against another.
19. As a report reader, I want incomplete shareholder or product coverage disclosed, so that a partial table is not presented as exhaustive.
20. As a report reader, I want fundamental, financial and deep-research views to preserve the same facts and quality, so that changing pages does not change the evidence.
21. As a report reader, I want generated, reread and downloaded reports to agree, so that saved evidence remains trustworthy.
22. As a report reader, I want a failed secondary source to leave valid primary content available, so that a bounded failure does not destroy the report.
23. As a report reader, I want forced refresh to update values and evidence together, so that new values cannot inherit stale verification.
24. As a report reader, I want effective caching and shared acquisition, so that repeated consumers do not duplicate supplier work unnecessarily.
25. As an operator, I want iFinD enabled explicitly, so that leftover credentials cannot unexpectedly restore retired providers.
26. As an operator, I want deployments without iFinD to retain Choice behavior, so that the integration remains optional.
27. As an operator, I want safe errors without credentials, so that troubleshooting does not disclose secrets.
28. As a maintainer, I want real supplier fixtures exercised through the existing report pipeline, so that tests cover the failures readers actually encounter.
29. As a maintainer, I want an entire local deep-research report verified, so that a successful isolated demo is not mistaken for finished integration.
30. As an operator, I want a bounded rollback to Choice-only behavior, so that recovery does not reactivate other retired channels or rewrite history.

## Implementation Decisions

1. **单一启用语义。** 保留 Choice 主源策略，增加一个默认关闭的 iFinD 显式启用项，复用现有 endpoint、token、timeout 配置。该项在 Choice 策略内统一控制本批补值、资料补充和默认核验；不能再被某个旧的深度投研专用开关静默关闭。旧部署及 legacy 策略保持既有语义，不新增各页面开关。缺必要配置时校验明确失败，远端鉴权/额度失败按供应商错误处理。配置示例、设置页、服务/CLI 实际行为同步说明；Choice 策略下默认关闭且仅残留旧密钥时零 iFinD 调用；此约束不改写 legacy 已有渠道规则。
2. **复用供应商适配边界。** 沿用现有 MCP 客户端和指标读数契约。以证券身份及表头选择相关表，保留空单元格位置、字段标签、原始数值、倍率、单位、币种、财期、披露日及供应商工具信息。处理 MCP 与业务层错误，不把空表、错误文本或无资格值伪装成功。仅按实测必要范围处理响应差异，不建设通用 Markdown 数据库或插件框架。
3. **元数据证据。** 请求参数仅表达意图，不能自动成为返回事实。表中明确日期可作为相应数据的实际日期证据；参数中的“最新一期”等动态默认标签不是另一个确定财期。若不同位置有互相矛盾的明确日期或单位，标为不可采用，不能择一掩盖。币种、合并/母公司、调整口径由返回或已经验证的供应商指标契约证明，不能仅因 A 股或查询提示词默认补齐。
4. **六项快照完整接通。** 现有 Choice 快照只有本批六项中的四个比率；必须通过既有原生财务调用补入同期营收和归母净利，并让它们连同字段证据进入共享财务上下文与报告。复用已有指标定义，不拿上一年年报数额与当前季度比率拼接，不扩展为四年年报替代。个股财务报告以最小的当前快照证据区块承载本批数值和核验状态，年度序列及其评分口径保持不变。
5. **字段采纳与独立核验。** 先保留 Choice 有效值，再将 iFinD 合格缺项按字段补入。Choice 已有值必须保留为核验主读数；只有跨来源家族且同证券、实际财期、期间口径、币种、单位、报表/调整口径可比时使用现有容差判断。单源补值不标通过，只有旧财期候选不补当前快照。主源无财期时，候选必须自行建立明确且一致的实际财期，不能放宽资格要求。
6. **资格与最终结论一致。** 可展示资料与规则输入分开。缺公告日/原文链接不一律删除资料，但不能宣称公告原文核验或历史时点可得性；依赖这些元数据的计算继续受限。冲突保留主源展示值、候选证据和原因，阻断依赖冲突的确定性评分与行动结论；不删除无关有效章节，经理总结或模型改写不能重新引入已被规则禁止的结论。本批六个核验指标不等于既有 F1 的六个评分字段，不替换评分字段、不新增评分公式；六项核验通过也不代表 PE/PB 或整份报告已独立核验。
7. **主营及股东窄适配。** 沿用已有研究资料与报告章节，补充产品收入、成本、毛利金额/毛利率及股东名称、股数、持股比例。只采用实际存在的字段；分项不重复求和，不把部分覆盖标为全集。股东名称表与数值表按证券、实际财期、排名连接；前十大合计比例不可分配给每位股东。资料块可有自己的真实财期并清晰标注，不能因为拼进同一报告就冒称与财务快照同一期。
8. **一次采集、多处消费。** 在既有共享财务上下文组织本批数据，相关页面、Agent 工具、质量投影与报告消费它；同一报告不各自再建供应商调用链。六项默认核验与补值复用同批结果，资料按需取。复用现有超时和总预算，为核验分配有限预算，避免 Choice 已耗完总时限后名义上启用核验；无缓存整报告耗时需实测，不拿单次 demo 延迟作承诺。
9. **缓存与刷新。** 扩展既有缓存身份以区分渠道策略、数据契约、证券、实际财期及必要口径；值、元数据和质量同步更新。强制刷新贯穿相关缓存，失败不固化为成功报告；可保留既有历史版本，但不能将旧核验贴到新值上。关闭 iFinD 或切换策略后不得继续展示来自另一策略缓存的已核验状态。无必要不新增缓存服务。
10. **兼容与回滚。** 在既有数据载荷上追加必要证据，保持已有字段与历史报告可读；API、持久化、页面和下载使用同一来源/质量语义。回滚恢复本批前 Choice 行为及配置，MX、Fuyao、旧 Choice MCP 仍关闭。生产部署、账户购买和 Git 提交/推送不是本规格发布授权的一部分。

## Testing Decisions

### 主验收边界

用户已确认沿用现有报告入口和本机完整报告验收。以“实际报告生成 → 持久化 → API 重读 → Markdown 下载 → 页面展示”为主要外部行为边界，复用现有报告检查脚本与 API/SSE 测试。断言读者看到的数值、来源、财期、质量和结论资格，不测试私有调用步骤或精确方法次数。

供应商 transport 是必要的受控替换边界：回放本轮真实 MCP 返回，让生产解析器、质量判断、共享上下文和真实报告编排继续执行，不能 mock 掉它们以证明自己。单位/财期等金融不变式沿用现有契约测试，作为少量精确反例补充；不为每层建立新测试框架。

已有先例包括 Choice transport 到共享上下文的集成测试、数值与金融身份契约、质量冲突穿透 Agent 工具的投影测试、财务刷新与历史缓存测试，以及研究资料通过真实报告服务/SSE/持久化重读的测试。扩展这些现有测试类型，不复制平行实现。

### 必须覆盖的行为

| 编号 | 场景 | 验收结果 |
| --- | --- | --- |
| A1 | Choice 六项有效，iFinD 已启用 | 即使无缺项也执行默认核验或复用有效证据；可比且一致才通过，营收/归母净利确实到达消费端 |
| A2 | Choice 部分字段缺失，iFinD 同期合格 | 仅补缺项，字段标真实来源；iFinD 单源值不标独立核验通过 |
| A3 | 实际财期不同、累计/单季不同、币种或口径不明 | 不混期、不比较、不用请求期补造返回期，保留不可采用原因 |
| A4 | 表头元且数值万/亿，百分比、零值、负数、多表和空单元格 | 正确缩放且不重复乘倍率，字段不错位；异常输入不会变成合格数值 |
| A5 | 已有有效值与 iFinD 可比但超容差冲突 | 主值与冲突保留到报告/API/下载，依赖冲突的评分和行动结论受到限制 |
| A6 | 缺公告日或原文链接 | 有效资料仍可读、缺项清楚；不能冒称原文核验或历史可得性，计算资格不被绕过 |
| A7 | 超时、远端鉴权/额度错误、协议异常 | 总预算内结束、保留 Choice 内容、正确披露未完成核验；无无限后台重试或失败成功缓存 |
| A8 | 股东名/数分表、排名缺项、分产品只有部分覆盖 | 不错配财期与排名，不把合计比例当个体比例，不把产品毛利率当整体毛利率 |
| A9 | 缓存命中、刷新、策略切换与失败后再试 | 值/证据/质量一致更新，未启用不能继承已核验状态，历史版本保持原始内容 |
| A10 | Choice 策略下 iFinD 未启用但存在旧密钥；或显式启用但必需配置缺失 | 前者保持 Choice-only 且退休渠道零调用，后者配置失败明确，不静默关闭；legacy 维持既有规则 |
| A11 | 模型失败或经理重写 | 原始合格资料保留，规则已禁止的评分/动作不能被改写恢复 |
| A12 | 实际生成、重读、下载与页面展示 | 同一报告事实、来源、财期、质量一致，显示正常；历史报告兼容 |

### 本机真实验收与完成门槛

- 真实供应商样本至少包含 300260 和一只金融行业样本，明确区分“不适用”“供应商未提供”和“实现漏读”。当前 demo 只有普通企业有限样本，不能冒充金融行业已验收。
- 至少取得一组真实同证券、同财期、同口径的 Choice/iFinD 核心财务证据并完成核验路径，同时展示一次实际有效补充资料；只展示全部降级或使用合成数据，不能证明本批能力已交付。缺少供应商元数据时继续核实其指标契约，不能填默认值过关；确实无法得到时报告具体未验收项。
- 生成本机基本面、个股财务及至少一次全维度深度投研报告，检查受影响章节和页面/API/历史/下载一致。深度报告保留既有 Choice 年报、行情、资金等能力，不把全维度验收扩大成 iFinD 的全能力替代。受影响板块路径按实际消费做回归，未受影响页面不重构。
- 执行适用的后端 gate、类型/契约检查及受影响 Web 验证。代码检查、真实接口、本机完整报告三类证据分别列出，不能互相替代。截图放在 PR 附件或外部验收位置，不提交一次性截图。
- 业务侧确实缺失允许在报告明确降级；实现错误、未接通消费者、预算使核验从未执行、全样本始终不可比不能以“降级正常”关闭交付。报告记录样本、实际财期、取数/生成时间、来源状态及剩余限制。

## Out of Scope

- iFinD 全功能替代 Choice、双主源自动切换或整份报告无条件互备。
- iFinD 四年完整年报补齐、资金流窗口替代、行情主源切换、机构目标价、新闻/宏观工具接入；本批不改变现有 Choice 相应能力。
- 港美股及其他市场扩展、回测或历史时点可得性系统。
- 通用供应商框架、新数据平台、新缓存服务、全量双跑或各页面独立开关。
- 商务有效期、套餐、团队/报告展示授权谈判与生产发布；这些不作为当前开发测试前置。
- 根目录工作区已有的 Choice 登录等未提交改动，不自动包含在本规格交付中。

## Further Notes

- 发布前由两名独立 reader 分别检查可理解性、可施工性、范围、复杂度与现有代码复用，均判定可进入 `ready-for-agent`，无阻断项。已吸收 Choice/legacy 启用边界、六项核验与 F1 评分区别、财务专项最小证据展示三项澄清；这只代表规格可施工，不代表实现或供应商验收完成。

- 用户已确认双源职责、首批范围、元数据缺失展示原则和默认核验时机，无待决产品问题。父规格发布阶段只创建 #15；随后用户批准拆为 #16、#17、#18 三张子工单，发布链接与依赖见本文开头。父 Issue 中“不额外拆票”描述此前阶段，不限制这次已批准的拆分；本轮未改写父 Issue 正文。
- demo 成功完成 MCP 初始化、工具枚举与财务/股东/资金请求，五份保留结果耗时约4–11秒。返回是 JSON 中的表格及指标参数，不是完整固定输出 schema；部分参数仍写最新一期，披露日仅部分查询返回，未取得原始公告链接/标准指标代码。只证明候选能力，不证明全证券覆盖或业务验收。
- 旧 iFinD 适配器在2026-06-25的提交中已存在；协议/工具可复用不意味着旧解析器可原样启用。当前 Choice 消费路径中的旧核验禁用、零预算和固定单源标记必须一并收敛。
- 官方参考：[接入准备](https://mcp.51ifind.com/#/docs/guide-prepare)、[数据范围](https://mcp.51ifind.com/#/domains)、[登录后工具目录](https://mcp.51ifind.com/#/profile)。公开能力说明与账号实测证据分别对待；发布正文不包含凭据或私有账号信息。
- 领域依据：[术语表](../GLOSSARY.md)、[初始 Choice 决策](adr/0001-choice-native-data-policy.md)、[有限恢复 iFinD 决策](adr/0002-ifind-complements-choice.md)。

## 实施配置与财务快照（#16）

Choice profile 下在本机私有 `.env` 或部署环境配置：

```dotenv
ENABLE_IFIND_MCP=true
IFIND_MCP_ENDPOINT=<官方A股专用Streamable HTTP地址>
IFIND_MCP_TOKEN=<账号生成的Authorization值>
IFIND_MCP_TIMEOUT_SECONDS=30
```

默认不开启；显式启用必须同时给 endpoint/token 和有限正超时。配置在设置页只读展示，token 为敏感项；修改后重启实际服务进程。仍使用 `DATA_SOURCE_PROFILE=choice`，不改成 legacy，也不恢复其他停用渠道。关闭 `ENABLE_IFIND_MCP` 即回到 Choice-only；历史报告保留生成时证据。

持续运行应使用持久配置来源。本机把上述四项放在项目根目录被 Git 忽略的 `.env`，现有 CLI/API 启动入口会自动加载，无需每次手工 `export`、从浏览器取 token 或借助临时重启工具。指定 `ENV_FILE` 时改读该文件；启动时已有的环境变量优先于文件。原有 Choice 账密和模型配置继续使用各自的现有入口。

生产环境通过部署平台的环境变量或凭据配置提供同名项；现有 Docker Compose 的 `env_file` 会将部署侧配置传给应用，不把本机私有 `.env` 或 token 合入仓库或镜像。iFinD MCP 本身不需要在服务器安装 SDK 或进行设备激活；Choice 原生 SDK 的安装和登录按其独立部署说明准备。配置变更后须按部署入口重新加载并验证实际服务的取数结果，不能仅凭进程存活认定接入成功。

一次当前财务调用同时供补值和六项核验。Choice 快照增加同期营收/归母净利润；基本面、深度投研和财务专项报告追加当前快照证据，不改既有年度评分和 F1 公式。报告保留主值、对照原值、来源、财期、单位、质量及失败原因。共享上下文和 Agent 复用管理器已资格筛选的补值，旧财期或财期未知的候选仅保留在对照证据中，不在下游重新补进当前快照。失败不作为成功缓存，强制刷新更新值和证据。即使 Choice 规则输入有效、深度报告总体成功，快照存在核验源错误时仍拒绝日缓存读写，恢复后可重试。

Choice SDK 设备登录授权过期（如错误码10001020、要求reactivate）属于鉴权失败；设备模式须恢复授权并重新激活，也可按[Choice 部署配置](choice-quantapi-integration.md)改用账号密码登录。这不等于 iFinD 接入失败。iFinD 合格补值可以继续展示，但不能冒称双源核验，也不能代替 Choice 的年报、行情和资金能力。

2026-10-14 用户追加明确部署约定：iFinD 本批 A 股返回中标为“元”的金额按人民币处理。这是本部署约定，原始返回缺币种的事实仍保留在 `raw_currency` 中；不是供应商明确返回币种，也不适用于 legacy/非 A 股/未知单位。显式返回其他币种或矛盾单位仍拒绝比较，不因该约定覆盖供应商证据。

[同花顺官方 FAQ](https://quantapi.51ifind.com/gwstatic/static/ds_web/quantapi-web/help-center/faq.html)区分首次披露的合并报表和后续前期调整/差错更正的合并调整数据。解析器只把实际返回“合并报表(调整)”映射为 consolidated/restated；请求中的重述字样不作为返回事实。响应缺失的元数据可由下述已确认的指标契约补足；尚无证据的调整口径仍保持不可比，不猜默认值。

真实 300260 的 2026-06-30 样本已取得六项值；同期 Choice/iFinD 的营收与归母净利可在上述明确人民币约定下正向核验。四个比率真实返回缺失的合并范围按下述确认处理，调整口径仍未知，保留 Choice 数值、披露不可比原因；不能宣称六项均通过或整报告通过。

2026-10-14 供应商经用户转述确认：加权 ROE、销售毛利率及两项同比基于合并报表。当前 A 股批次可据此补足缺失的报表范围，但不是调整口径确认；四个比率在调整口径未明确前仍保留不可比状态。显式返回母公司等其他口径时保留实际返回，不以契约覆盖冲突。

2026-10-15 供应商经用户转述进一步说明：MCP 数据来自 iFinD 终端数据浏览器，浏览器的 ROE 加权指标没有合并报表参数，故 MCP 也不会输出该参数。这解释了该指标的参数缺省，不代表 ROE 数值获取失败，也没有否定前述合并报表口径确认。无需继续要求接口输出不存在的参数；沿用已确认的报表范围、保留实际 ROE 数值，仍不把参数缺失推断为调整前或重述后。该回复没有确认公布值在财报更正后是否回溯更新，因而不单独解除调整口径的可比性限制，也不扩展为其他三个比率具有相同参数限制。

## 主营分产品接入（#17）

现有主营章节按需查询最近完整年度的分产品资料（可有独立于当前快照的财期），按实际返回的证券、财期与排名连接名称和金额。缺名称关联、跨期或重复身份不得拼接。默认请求上一年年末仅表达查询意图，采纳仍依赖返回的明确财期。

收入、成本、毛利金额保留各自单位；没有明确单位的产品毛利率显示原始数值及“单位未知”，不推断比例/百分点。明细始终标“部分覆盖”，不汇总、不把产品毛利率替代公司毛利率。披露日/原文未返回时如实说明；既有Choice简介保留。模型失败时资料由现有研究员编排直接保留，模型不能制造或改写结构化明细。

资料复用同一启用、客户端、现有快照缓存与刷新标记；业务研究使用阶段0传入的研究资料，不为补主营重取六项财务。基本面强制刷新覆盖财务及主营获取；主营错误不命中同日报告缓存，恢复后可重试。空响应及非法排名披露为供应商错误，保留其他有效来源资料，已取得事实进入基本面和深度投研的现有章节。

基本面与深度投研共用分产品模板，每条记录保留独立 Markdown 表格行；正文、历史读取和下载均应完整显示已取得的产品，不能仅以结构化数据中的记录数量作为展示验收。

## 股东明细接入（#18）

现有股权章节按需取得前十大股东明细；名称表和数值表仅按返回的证券、明确财期和排名连接。十个唯一排名均有合格股数及百分比才标名单完整，否则显示缺项/部分覆盖；这不是独立双源核验，也不表示整个股权研究完成。不能把合计比例赋给个体，不能从持股数据推导实控人、关联关系或高管背景。Choice现有控制人和股东信息保留。

股数的万/亿倍率和表头%按各自单位解析，字段名“比例”不等于原值是0—1比例。零值保留；数量为负、比例超范围、单位矛盾不作为合格持股数值。模型失败仍保留已取得明细，披露日和原文链接未返回时明确列缺项。与主营共用同一客户端、既有缓存/刷新/期限处理，不增加历史股东系统或新页面。
