# 模型调用优化：实施规格与阶段边界

> 2026-10-26；基于 dev `285078f8` 的代码及已保存线上样本。本文规定目标、约束与阶段验收；阶段状态和后续施工关联统一维护在 [GitHub 主 Issue #53](https://github.com/SkillingX/daily_stock_analysis/issues/53)，不在本页复制进度。文档描述待实施行为，不表示功能已上线。

## Problem Statement

用户生成深度投研报告时，需要获得有来源、有完整分析、能解释缺口且等待可预期的结果。当前多位研究员重复组织取数和上下文，在模型实际并发为 1 时竞争同一名额；报告成功保存不表示研究完整，调整超时或补充提示文案无法消除重复工作与预算竞争。

已保存的线上报告 `300260_202610261122` 总耗时 616.8 秒，15 个维度正常、4 个不完整。可关联模型请求 18 次，模型占用 343.664 秒，最大单次排队 215.343 秒。其中 10 次不超过 5 秒的请求合计 24.776 秒，其余 8 次合计 318.888 秒。删除短调用不能据此承诺速度翻倍；两个额外排队超时缺少运行归属，不能算作这份报告的失败数。该报告跳过了经理终稿三波，不能把它的主要耗时归于这三波。

同窗口观察到同一股票的完全相同财务查询 4 次、同一主营和股东查询各 3 次，反映同报告有效资料复用不足。另有报告 token 总量归集断点，数值为 0 不能解释为没有用量或免费。当前缺少足够归因，难以持续比较优化前后的模型工作量、质量和用户等待。

本规格聚焦模型调用编排及直接关联的资料复用、计量和生命周期，不把供应商真实缺失、账号限额、SDK 授权及所有取数故障混为模型问题。

## Solution

以“代码有界取数和计算 → 共享可追溯事实 → 各维度结构化分析 → 必要时有界定向补查 → 规则派生与校验 → 集中成文 → 原有模板输出”为目标。

保留现有 19 个报告维度及八个研究员的输出契约和失败隔离。把已知如何执行的常规取数收回现有代码入口，让模型专注分析而非反复决定是否取得同一批资料。先通过技术、资金两个维度证明单轮分析可保持质量，再扩大范围。开放研究保留明确证据缺口驱动的追查能力，不无条件取消深度研究。

并发上限是安全护栏，不等于工作流调度。后续在现有报告入口落实清晰、有界的等待和取消语义；API 与 Scheduler 保持独立进程及模型分流，不引入跨进程通信。每一阶段只有质量和模型占用的对照证据通过，才能扩大范围。

## User Stories

1. As a report reader, I want the selected research dimensions to remain available, so that faster generation does not silently remove analysis.
2. As a report reader, I want numerical facts to retain their source, report period and units, so that I can assess the basis of the analysis.
3. As a report reader, I want an existing valid financial snapshot to be reused within my report, so that repeated research does not waste time fetching the same facts.
4. As a report reader, I want technical analysis to interpret the available indicators, so that a model does not repeatedly request the same daily history.
5. As a report reader, I want capital analysis to distinguish valid amounts from missing or unqualified inputs, so that fluent text does not conceal unreliable data.
6. As a report reader, I want important unresolved questions to receive targeted follow-up, so that reducing model calls does not make the research superficial.
7. As a report reader, I want a true evidence gap to remain visible, so that a model is not asked to invent missing vendor data.
8. As a report reader, I want the report's summary and chapters to use the same validated conclusions, so that repeated writing does not introduce contradictions.
9. As a report reader, I want executable prices and conditions to remain rule-generated, so that model prose cannot replace the accepted trading plan.
10. As a report reader, I want a failed dimension to preserve other valid results, so that a local failure does not erase the whole report.
11. As a report reader, I want waiting, research and model execution to be distinguishable, so that I understand the actual progress of my request.
12. As a report reader, I want bounded waiting and explicit overload handling, so that an accepted request does not remain hidden in an indefinite queue.
13. As a report reader, I want cancellation to stop queued work from starting later, so that my cancelled report does not continue consuming model capacity.
14. As a report reader, I want a disconnected browser to be handled consistently with the existing lifecycle, so that a network interruption is not mistaken for my cancellation.
15. As a report reader, I want the page, history and downloaded artifacts to agree, so that I can trust the saved outcome.
16. As an operator, I want every model attempt to have an attributable purpose and terminal outcome, so that queue failures and upstream failures can be diagnosed separately.
17. As an operator, I want known usage and missing usage to be distinguished, so that a displayed zero does not imply zero consumption without evidence.
18. As an operator, I want actual outbound concurrency to respect the effective process limit, so that optimization does not evade account constraints.
19. As a maintainer, I want staged comparisons against frozen evidence, so that performance changes are accepted only when research quality is preserved.
20. As a maintainer, I want a single progress tracker and explicit stage gates, so that unfinished research directions are not mistaken for delivered features.

## Implementation Decisions

### 共同约束

1. 复用现有报告服务、研究员注册与解析、共享上下文、运行控制、模型适配、用量记录、报告模板和生命周期入口。不新建平行 Agent 引擎、供应商框架、通用监控平台或缓存服务。
2. 模型需要的标准事实由代码按所选维度组织。相同 run、股票、实际财期、观测时点和查询身份的有效结果可共享；不同财期、单位、口径、时点或新鲜度要求不能混用。正在取得的相同事实复用同次结果，不能并发重复发送。
3. 数据源失败与成功空值、解析失败、尚未执行、取消分别保留；不把失败缓存成成功，也不因辅助查询失败丢弃已取得的合格字段。跨报告缓存沿用既有资格和刷新语义，本规格不重写全局缓存。
4. 保持主数据源/补充数据源和独立核验语义；同一来源家族或单源补值不能写成已独立核验。不复活退役数据渠道、模型字段、评分和缺失提示。
5. 交易计划、执行价位及触发条件继续由规则产生；模型只能解释已有事实。金融公式与跨字段不变式、边界数据校验继续遵守现有三层防御，不以减少调用移除校验。
6. API 模型、Scheduler 专用模型及本地套餐替代的路由不变；不切换按量计费入口。当前配置的推理强度作为对照，本规格不全局关闭 thinking，也不自动提高生产并发。
7. 正常路径的长期目标为约八次专题分析加一次集中成文；必要追查、参数恢复或异常修复须独立归因，不隐藏实际请求数。该数量是评测目标，不是删维度、强制截断或放宽质量要求的硬配额。

### 阶段一：当前可施工边界——调用归因、直接复用与技术/资金试点

1. 接通报告、维度与真实模型请求之间的计量链。至少能关联运行、调用标识、业务入口、维度、目的、实际模型、是否出站、等待/执行耗时和终态。排队未出站不能记录为模型已调用，兼容恢复/备用尝试不能隐去额外请求。
2. 复用供应商 usage 与现有逐次用量记录；补齐报告归集，覆盖研究、已有补分和实际执行的成文请求，避免双计。同次请求的输入、输出、推理和缓存 token 只在供应商确实提供时记录；缺失须标为未知。混合已知/未知 usage 的报告保留可确认合计并标为不完整，不能声称完整总量。保留旧数值接口的兼容性，必要的完整性说明采用追加方式；未知不得冒充确认的零消耗。
3. 不另建供应商取数层。技术试点使用本次共享行情、日线、已计算指标及现有结构分析；资金试点使用现有有界资金/机构资料。确定需要的固定资料由原代码入口准备，不先请求模型来决定是否调同一工具。
4. 在资料充分、模型正常返回的路径上，技术和资金各执行一次无工具循环的结构化分析，并走原维度契约、资格校验与规则消费。其他研究员暂不迁移；现有报告维度与评分定义不变。
5. 该维度完全没有可解释的有效资料时不为凑内容启动模型；部分资料有效时保留其解释机会，真实缺口独立披露。局部字段缺失不能错误表述为整套资料无效。
6. 如正常请求外实际发生兼容恢复或既有错误处理尝试，单独记录原因、用量和失败范围；本阶段不顺手改写全局兼容恢复/备用模型策略。
7. 本阶段模型并发及报告接单行为保持现状。必须用两报告重叠测试保护当前全局模型上限与取消行为，不能提前把阶段三的队列功能夹带进来。

阶段一退出门槛：

- **A1**：同一冻结输入下，两个试点在正常路径各一条真实出站分析请求；资料准备不需要模型规划调用，已取得相同资料不因两个试点各自重新发送查询。
- **A2**：充分、部分缺失及供应商失败样本均保留原有合格事实、来源/财期/单位与规则边界。输出通过原契约；不能少写分析或丢掉证据制造通过。
- **A3**：可控供应商给定的 usage 在逐次记录和报告合计中一致，成功、失败及恢复尝试不双计；未知/部分未知与确认零用量可区分。
- **A4**：实际在途模型数不超过有效配置；排队失败、模型失败和取消有可关联终态；取消后排队请求不再出站，实际在途资源按现有清理契约释放。
- **A5**：同模型、同推理强度、同事实、同维度、同缓存条件完成旧/新对照。两个试点的模型请求数应从原实际基线减少（原本已一轮者保持一轮），合计模型占用中位数不高于基线；质量门槛全部通过。若只减少短调用而耗时无明确改善，只能宣称删除冗余请求，不宣称整体提速。
- **A6**：页面、历史、Markdown/PDF、维度数据的状态与有效内容一致；整个19维报告完成一次真实验收，逐项解释仍存在的真实缺口。供应商故障不能作为代码验证已通过的替代证据。

### 阶段二：阶段一通过后，逐步扩大分析与成文收敛

1. 把其他研究员逐个迁移为共享证据后的单轮分析；保持独立输出和失败隔离，不直接合并成少数大 schema。
2. 新闻、产业链、业务、股权等开放研究按明确待核问题决定是否补查：必须说明缺少的证据、为何影响判断及允许的查询范围，代码执行有界批次，复核仅针对受影响结论。一次补查可以包含多个相关查询，不默认给每个维度增加一轮；继续沿用报告总预算和现有工具超时，不重置完整预算。
3. 主观评分与依据尽可能由所属分析同次返回，已有合格评分不被另一次模型调用覆盖；无依据则保留缺口，不能用“补分”代替取证。金融计算/规则派生不迁回模型。
4. 收敛终读总结和经理成文的重叠职责，集中生成必要综述，不再生成最后必被规则覆盖的字段。保留章节所需分析和降级回退，模型成文失败不能丢弃已验证研究。
5. 输入按相关字段和证据有界选择，保持完整合法结构，披露省略范围；不能直接截字符串把 JSON 或关键证据截断。不能只传模型摘要而抹掉可追溯原始依据。
6. 推进前的施工工单必须列出具体迁移维度、必要证据、固定取数与补查边界、输出预算及可验证反例。阶段一结果未通过时不得按长期目标全量迁移。

阶段二退出门槛：沿用A2—A6的质量与生命周期要求；充分样本常规路径不再为已知标准取数发模型规划，补查由明确缺口触发；完整路径仅一轮集中成文且部分报告不得借成文越过证据门槛。以实际请求/占用/质量对照证明收益，不用“约九次”的目标替代验收。

### 阶段三：后续容量与用户可见等待

1. 在实际容量仍为1时，以同一API进程内一次执行一份重型深研报告为初始方向；其他请求在现有报告入口有界等待或在超过能力时明确拒绝。底层模型限制继续覆盖同进程各模块；不同时建设请求FIFO、优先级或第二套业务队列。
2. 等待不提前启动研究线程或消耗局部探索窗口；用户提交到终态的总等待必须有限。开始研究时依剩余总时限判断是否足够执行，不足时不能以大量缺项的“成功”报告替代未执行任务。
3. 等待、研究、收尾和取消明确可见；待启取消后永不出站。浏览器断开不自动取消；同股票去重与在途清理约束保留。新状态与错误需要API/SSE、页面、状态查询和取消一致消费，旧客户端兼容策略必须同步说明。
4. 本阶段还需要把等待上限、等待容量、满载响应和状态兼容确定成一张完整生命周期工单，再经reader testing后施工。当前规格只确定上述边界，不授权施工者自行选择数值或先搭建队列平台；阶段一、二可独立实施。
5. MiniMax账号有效并发上限尚未被证实等于1；本地智谱限制不套用线上账号。真实套餐的小范围并发1/2对照是本阶段独立评测项，不因通用RPM/TPM文档自动增容。任何生产配置、重启和测试安排遵守单独授权；Scheduler仍独立路由。

阶段三退出门槛：排队生命周期规格已确定并验收，正常/满载/取消/断线/预算耗尽与实际出站一致；不发生无限等待、待启任务偷跑或退役渠道复活。提供实际容量对照及最终配置建议，不能将准入顺序改善称为吞吐增加。

## Testing Decisions

1. **已由用户确认的主验收边界**：沿用现有报告生成API/SSE、状态和取消入口，接入本地可控模型/数据源，验证单报告、两报告重叠、取消及真实在途并发；核对页面、历史和下载；最后以同一组证据进行真实模型质量与耗时对照。
2. 不断言锁类型、线程数或内部函数调用序列。通过实际HTTP请求数、在途峰值、返回报告、事件终态和供应商可控usage证明行为。保留真实编排、模型准入、传输、解析、保存和渲染，只替换外部供应商；不mock掉被测预算或并发层。
3. 优先复用已有模型并发/真实HTTP服务测试、报告生命周期与取消测试、输入一致性测试和最终渲染回归。少量纯输入边界/金融契约案例留在原测试层；不为本计划新增另一套测试框架。
4. 固定至少三类资料：充分、关键资料真实缺失、新闻/治理证据矛盾。冻结股票、观测时点/报告期、原始事实、来源家族、单位及规则输入；冷取数与缓存命中分开测。缺失样本的正确结果可以是部分报告，不能编造数据追求全部正常。
5. 输出评价使用既有必需字段和已确认事实清单：事实/来源/财期/单位正确，数值与规则计划一致，关键理由、证据与真实限制保留。语序和文风不做逐字快照；独立语义复核不能只靠另一模型打分或“每章有字”。
6. 阶段一先录旧代码基线，再在同一可控服务上比较；真实模型对照固定模型、推理强度、资料、缓存条件和并发，按旧/新交错顺序至少各3次，报告每次值和中位数。冻结资料试验与真实在线端到端是两类证据，不混算；供应商波动或样本不足时标为待验收，不推断已提速。
7. 正常试点只发一次分析请求；技术/资金共享资料、重复查询抑制、部分字段有效、出站前预算耗尽、上游超时/拒绝、无效JSON/截断、用量未知及已知零用量均应有反例。
8. 两报告重叠及其他模块争用时用服务端观测验证上限；覆盖排队取消、流式中断/显式关闭释放、模型失败释放与后续请求可继续执行。可控模型故意迟于响应上限返回，验证上限真实作用于传输/读取；报告总时限和取消不能被内部补查或恢复重置。
9. 对规则事实输入不变的样本，保留数值、来源与交易方案的确定性比较。API与Scheduler既有模型分流及套餐入口保持回归保护，不在真实验收中切换计费入口。
10. 文档阶段不运行付费模型、供应商或生产验收；后续施工复用既有凭据和授权范围。截图及一次性原始证据放非入库位置或工单附件；维护者可在主Issue查看版本、配置摘要、缓存条件、对照结果、未通过项和阶段结论。

## Out of Scope

- IPC、跨进程共享额度、Redis/Celery、独立模型网关、通用任务平台或Agent框架迁移；跨进程事项继续由既有独立Issue跟踪。
- 一次巨型模型请求包办全报告，未经评测把八个研究员合并为三四个大Agent，删除19维产品范围、金融护栏或严格校验。
- 全局关闭thinking、更换模型/供应商、切换计费入口、增设多模型挑选体系、自动把生产并发调成2/5/8。
- 将供应商无数据、权限/额度、SDK激活、Choice单登录冲突或所有来源日志工程纳入本次模型计划；直接影响试点时单独定位与关联依赖，不以模型补造供应商数据。
- 通用日期混型等独立代码故障、来源提示词退休残留及组合风险旧入口的大范围清理；这些属于先前调查的独立问题，不夹带进模型优化提交。
- 改写历史报告、全局缓存/数据库迁移、持久任务恢复、轮询机器人或定期自动跑付费报告。持续跟踪采用GitHub状态与阶段验收记录，不自动创建定时任务。
- 把prompt caching、strict structured output或异步Batch作为本阶段前置条件；未验证供应商能力不承诺命中、格式正确性或加速比例。

## Further Notes

### 阶段推进与关闭规则

GitHub主Issue是工作状态真源，记录当前阶段、关联施工Issue/PR、验收版本、对照条件、结果和遗留问题。初始只开放阶段一施工；阶段二需引用阶段一退出证据再明确迁移批次，阶段三需先确定完整生命周期契约。后续施工按每票可独立验证的完整行为拆分，数量保持克制。

`ready-for-agent`表示当前下一步——阶段一试点——已明确，不表示后续队列数值或所有研究员迁移方案已确定。主Issue不能因阶段一CI通过或代码合并而关闭；完成全部已纳入阶段的验收，或维护者明确将剩余阶段迁出并关联后，才评估关闭。

本地规格维护产品约束和验收定义；不另设可变状态清单。每次阶段交付在主Issue追加精简记录：执行阶段、版本、真实请求/重复查询/模型占用/用量完整性、质量反例、完整报告结果、未验收项与下一步。若结果与预期不符，停止扩大改造范围，先解释数据与语义差异。

### 起源与资料

- 代码研究基线：dev `285078f8`；线上样本版本 `e613e9c8`，报告 `300260_202610261122`。该样本不是优化后的性能承诺；后续真实对照仍需完整充分样本。
- 主源/补源和独立核验边界沿用仓库领域术语及已接受ADR。
- 既有[跨进程并发后续事项 #26](https://github.com/SkillingX/daily_stock_analysis/issues/26)保持独立，不重开到本批。
- [Anthropic：workflow与agent的取舍](https://www.anthropic.com/research/building-effective-agents)用于架构原则参考；不据旧文章引入新框架。
- [OpenAI：延迟优化](https://developers.openai.com/api/docs/guides/latency-optimization)用于减少无效工作、重复输出和请求的原则；不是当前套餐能力或加速比例证明。
- [Python：Semaphore](https://docs.python.org/3/library/threading.html#threading.Semaphore)不保证唤醒顺序；既有护栏不能直接称为公平队列。
- [MiniMax：通用限流说明](https://platform.minimax.io/docs/guides/rate-limits)不能代替实际套餐并发测试与控制台证据。

本文是中文工程规格；未新增英文镜像，因为当前没有对应的英文专题规格，也未改变双语产品能力或配置文档。后续实现涉及用户可见能力、配置或契约时须同步相应文档和变更记录。
