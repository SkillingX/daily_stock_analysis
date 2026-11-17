# iFinD 与 Choice 接入：当前财务主源选择

2026-10-15 新决策（工单 #23）：在已接通的六项当前财务、主营分产品和个人股东明细内优先 iFinD，Choice 同期补缺和独立核验。下面原规格记录此前 Choice 优先的施工背景；本节主次规则优先生效，原数据资格与验收护栏继续适用。

## 概念归属独立取数与同报告资料复用（issue #51）

个股最新概念归属是独立的轻量数据需求，在阶段 0 与行情、日线一起和整套财务聚合并行装配，不再排在财务聚合链末尾等待剩余预算。为取概念不再重跑整套财务，也不重新发放无限或叠加预算；概念仍受既有单次、阶段、报告时限及取消约束，预算已耗尽或研究已取消时不发起请求，只记录未查询原因。

同一份报告内，证券、实际查询需求、财期、数据契约与刷新标记相同的资料只出站一次：并发请求同一资料共享同一次结果，已知失败及其原因同样供本报告消费，但不写入成功缓存，不阻止新报告重新尝试。一次强刷报告内共享本次结果；不同证券、不同财期、不同刷新标记互不串用。阶段 0 已取得的概念只在证券代码一致时复用，不会把一只股票的概念归属写给另一只。已停止或历史运行的结果不复活。等待方使用自己的阶段剩余时间和既有请求截止，不会被更长的 owner 或报告总截止延长。

可选的主营分产品与股东明细查询失败时保留供应商、操作与原因证据，不再静默丢弃，其错误不抹掉已取得的财务或概念结果。

阶段 0 已取得的概念归属会被确定性采纳进当期报告：数据透视维度（`data`，按报告重建、不读维度缓存）新增可选 `concept_membership` 字段（证券代码、完整概念名列表、来源 `ifind`、时点性质"查询时点最新"、错误码），报告"盘面"小节按该字段列出全部概念名，不截断、不依赖模型复述。日线历史不足降级、模型失败或研究中断的部分报告同样保留该字段；仅有已验证概念名（无行情/历史/财务）也足以让部分报告成文留存，不抬升评分或投资结论；仅有错误码、无概念名不算可用数据。供应商错误以"本次未取得（原因）"披露，不补造日期，也不把概念推断为行业、客户或供应链关系。历史报告的维度产物没有该字段时按缺失读取，不触发任何补查。

共享 MCP 边界在映射丢弃信息前，按 `call_id` 记录一次开始和唯一终态、可获得的 run/维度、是否真正进入 `tools/call`、有效预算、耗时、HTTP 状态、上游 request-id（存在才记）、MCP `isError`、结构化或文本封套中的标量 code/subCode（畸形对象只记类型，不落载荷）与脱敏 msg/subMsg，或异常的 cause/context/ExceptionGroup 链。预算耗尽或取消发生在分派前时仍有这条证据，且 `dispatched` 为假，不记成已经发出。HTTP 429 只证明传输层限流，记为 `rate_limited`；权限不足、登录失败、限流和明确额度证据各自保持独立，不把权限写成登录失败，也不从泛化的 429 推断额度耗尽。业务分类同时读取已返回的 msg 与 subMsg 文本，不从数字 subCode 猜测原因。GET 405 后 POST 正常成功、退出会话时的正常取消，都不误判为业务失败；业务请求实际超时不被会话关闭噪音掩盖。日志失败不改变业务结果。已知调用凭据即使被供应商裸回显也不落日志。智谱套餐搜索复用同一取证边界；MiniMax 搜索沿用既有 HTTP 与业务错误处理，只补 `call_id` 关联证据，不改渠道与计费地址，查询正文不落日志。

没有新增供应商框架、全局缓存服务、重试系统或配置组合。回滚本票提交即可恢复原装配顺序与错误归类；历史报告与退休公开概念探针保持禁用，不重启生产服务。

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

查询当前财务时，iFinD 使用一次明确日期范围的逐期请求，要求每行实际报告期；“最新一期”参数本身不能证明财期。范围由运行日期计算，不以 Choice 的返回日期锁定 iFinD；未披露期的空行不作为选期依据。若供应商仍未提供实际财期，保留候选和缺口，不从请求补造。股东查询不复用这套财务逐期扫描；只有当前研究已经确认、且截至分析时点已披露的财期，才用来构造一次明确股东查询，实际期仍以供应商回传为准。

完整年度财报仍由 Choice 提供，机构目标价在 `choice` 主源与深研外的消费者保持 Choice 出站（iFinD 主源深研 scope 不查询目标价，见下方 issue #73 节）；日线和实时行情沿用原渠道。主营及股东资料保留各自实际财期，不强行跟随财务快照或 Choice 名称表的旧期。混合来源报告按字段标明实际来源。主次策略进入既有缓存身份，切换或强刷不会给新值贴旧核验；历史报告不重标。

## issue #71：iFinD 主源下的当前数据迁移（2026-11）

`FINANCIAL_PRIMARY_SOURCE=ifind` 时，当前财务快照（六字段+同财期资产负债）、当前估值/市值、正式申万一级行业与个股资金 1/5/10 日窗口由 iFinD 主源提供，Choice 降为有界补充；`choice` 主源的既有行为完全不变。

- **同批出站边界**：财务六字段+资产/负债共用一次 `get_stock_financials`；估值/市值与正式行业共用一次 `get_stock_info`。供应商必须回显实际日期——只回显字面「最新」或不回显观测时间的估值读数在源读数上标 `time_unknown`，不参与合格主值，也不得覆盖健康的 Choice 值；请求日期来自本地严格交易日历（`recent_completed_trading_sessions`，不登录 Choice、不用自然日兜底），且不替代返回日期。iFinD 估值候选全部被拒时，真实拒绝原因仍随字段证据保留：拒绝/不合格原因回填独立于是否存在合格主值，缺主值字段按来源合并 iFinD 拒绝原因与既有 Choice `source_errors`，健康 Choice 主值/资格不被被拒候选污染；流通市值（`circ_mv`）的字段记录随估值块投影进入共享上下文、`get_stock_info` 材料与资金研究员市值锚点，可选缺记录不合成占位、不影响三必填 status 口径。
- **资格边界（含 P1 已批准语义）**：供应商仍未回显四比率的调整口径（真实探针证实）。经用户 2026-11-01 明确批准，`FINANCIAL_PRIMARY_SOURCE=ifind` 下 ROE/毛利率/营收同比/归母净利同比的同实际财期公布值可作**单源**财务分析采用：仅当其余资格（证券/定义/单位/合并范围）满足且只缺调整口径时采纳，field_meta 保留 `adjustment_unknown`、`selection_reason=ifind_published_single_source`，质量不得伪装 verified；财务评估显式标注「调整口径未知、未独立核验，不与其他来源混算」，不参与跨源核验/比较（交叉核验证据仍为 not_comparable），不假设重述。资产负债两字段须同财期、合并报表(调整)、同币种齐备才装配，混币种拒绝且不虚构币种标签。PB 仅在表头「最新」且该列参数 `基准日=报表截止日期` 同时成立时按证据认定为 MRQ，不做全局词表映射。估值批次观测日期必须落在最近已完成交易日窗口（严格日历 T/T-1），缺失/字面「最新」/过期/未来日期分别标 `time_unknown`/`stale`/`time_in_future`；行业批次无更低时效下界，仅拒绝晚于请求截至日的未来截止日期（标 `industry_time_in_future`）。拒绝原因随 field_meta/errors 留存且不覆盖健康 Choice 值。
- **资金窗口**：期望交易日只来自严格交易日历；日历不可用按 `calendar_unavailable` 披露，不以自然日/工作日兜底或回退 Choice 登录取日历。周末空行不补零、不前向填充；窗口缺口如实标记。与 Choice 同名指标口径不同（同日金额实测不同），来源标注 ifind，禁止跨源比较或拼接。必要查询之间做剩余预算预留，估值批次超时不吞掉资金窗口的有界机会。资金工具/模型材料的来源说明一律按块内实际 `source` 标注（iFinD 主源窗口不得写成 Choice；iFinD 失败回退 Choice 时实际回退来源与首个 iFinD 失败原因照常保留），不按部署 profile 推断。**标准 HTTP API 补充（第十一轮接入，仅深研执行上下文）**：深研资格（`http_financial_participation_active` 同款判定：开关+个股深研执行上下文+出站资格）时个股资金逐日序列经 `date_sequence` + `ths_net_inflow_amt_stock` API 优先（官方日期序列模板**空参数**、`Days=Tradedays, Fill=Blank`），失败/不可靠解析回退 MCP 原路径；**数据新鲜度边界**——请求起止由本地完成交易日历锚定（enddate=最近已完成交易日），解析层丢弃晚于锚定日的任何行：盘中未定稿值不因非空而伪装收盘完成；**当日 T 定稿防御与原鲜度优先（第十二/十三轮）**：参考日与窗口日历**同一时区语义**（`get_market_now("cn", current_time)`——与日历内部转换同源；不复用 current_time.isoformat()/本地系统时区，跨时区 anchor 不误判历史日）；当日 T（expected[0] ≥ 参考日）场景：T 非空（定稿未证明——官方手册无资金流定稿时效说明、既有双源对照皆为历史日）弃用 API 值并**先以同预算调 MCP 整个窗口**（原鲜度：MCP 的 T 日语义是既有消费契约，不拼两源）；T null 同样先 MCP（MCP 可能有 T 值——旧路径可达鲜度），**MCP 真实失败才**按既有数据质量语义保留 API 的 T-1 完整窗（pending_day 披露 + MCP 真实失败原因 ``ifind:<code>`` 结构化随 result.errors 保留——不伪装正常；不把 T-1 伪装为 T）或按缺口披露（T 非空时；MCP 取消照原语义穿透）；MCP 返回**空序列不构成成功**——按未提供同分流（不虚构失败原因）；历史日（expected[0] 早于参考日）非空照常采纳（四证券×20 交易日双源对照背书）；null 保留为缺值行（停牌/T 缺 → 既有 T-1 回退 + pending_day/window_n_incomplete 披露，不补零）；不可解释形态（errorcode≠0/错股/长度不一致/bool/文本/非有限/同日冲突/空序列/**全 null 零有效数值**）整体拒绝回 MCP——全 null 不能证明「正常无数据」，MCP 可提供完整数据时不得因 API 全空而三窗口全缺；解析层严格请求窗口边界：早于 startdate 的**窗口外旧日行不采信**（与上界对称）；**有效性按 expected 交易日集合判定**（第十四轮：expected 内零有效值——含全 null、或区间内周末等非交易日行非空而 expected 全 null——视同不合格回 MCP，不得跳过兜底；单个 expected 日 null 仍为缺值行不补零）；取消穿透零 MCP 出站、API 超时回退且 MCP 兜底拿正剩余预算；非深研与 HTTP 关闭形态 MCP 原路径零变化。证据边界（样本级声明）：四证券（600519/600031/000001/301269）×20 交易日双源对照 80 日期重合、相对差均 <0.1%（MCP 严格元口径——样本支持 API 数值为元，非供应商书面保证）；停牌 null 样本为 600176 单次停牌窗口，不外推全部停牌形态。资金窗口不进 24h 快照缓存（既有「资金块只随 run 装配」语义不变）。
- **取消保留**：早批已取得的合格资金块取得后即时登记进本报告共享上下文，并在可选公共详情/板块工作之前先落位结果块——之后任一点研究取消（`ResearchStopped`），已取得的三窗口/来源/截至日不因退出路径被空部分块替换；登记沿既有 `stop`/`finalized` 语义（已停止的冻结上下文不接受晚到写入、证券归属不匹配不串写）。取消早于资金研究员启动时，中断报告同样从共享上下文留存资金事实（降级/取消口径，零新增出站、不重跑资格门、不伪造评分）；中断前已取得的合格总市值/流通市值（`field_meta` 既有 `get_stock_info` 记录）经既有 `get_stock_info` 投影随中断资金维度一并留存，资格/来源/实际日期/单位币种原样随行，不因资金块缺失或已存在降级资金维度而丢失（已有维度仅本地追加/去重，不改冻结的共享上下文与 run 维度）。
- **Choice 10001009 止损**：Choice 原生 `10001009`（登录次数达上限）后，同一报告内不再重复尝试 Choice 登录，未分派请求按 `authentication_failed` 如实记录，等锁请求在锁内复检止损；下一份报告照常尝试，不构成跨报告熔断。其他认证错误不做同报告熔断。
- **行业**：只消费「所属申万行业/所属申万行业代码」列；同批矛盾行业整批拒绝，级别/截止日期缺失记录 `industry_metadata_missing`；行业截止日期晚于严格日历请求截至日为供应商未来日期，拒绝采用（`industry_time_in_future` 随 research errors 留存、实际返回日期保留，不回填请求日期、不补造分类/版本），健康 Choice 补充不被覆盖；供应商未回显分类版本则 `classification_version` 保持空，不把请求文本中的版本写成返回事实；概念列不作为行业。共享上下文的行业提示优先级为合格 iFinD 行业 > Choice `HISSW2021` > 板块兜底。
- **缓存**：主源策略已含于 `data_source_contract`（`-primary-ifind-v1`），切换即隔离；本版对受影响研究员/快照缓存做一次性映射版本失效（`FUNDAMENTAL_MAPPING_VERSION` 21 / `source_policy_version`），历史报告不回写。
- **旧库兼容**：`fundamental_snapshot` 缺 `as_of_date` 的部署库由启动期幂等补列（nullable + 索引，无任何历史回填）兼容，读取不再依赖 fail-open。
- **资金采纳证据**：资金研究员模型分析未形成可用评分/正文时，降级载荷带最小脱敏证据（字段存在性、声明类型、采纳门裁决），不含原始回复文本，不据此重试或补造评分。分析正文只认非空字符串——布尔/数字/容器经 `str()` 不构成分析（`str(True)` 不伪装成已完成解读）；选中 `flow_summary` 作为采纳正文时，真实模型分析在其被固定资金事实覆写前落 `narrative` 实际保存。无效评分形状（布尔/非数字字符串/非有限数含原始 NaN）在采纳边界降级且原始值归 None——不进契约解析、不借维度/保存/API 或解析异常文本持久化。`CapitalDim` 仍为唯一评分范围权威：越界等契约拒绝经 capital 解析窄边界保留白名单证据的降级维度（`adoption_evidence.outcome=contract_rejected`，不宣称 adopted、固定脱敏失败原因），证据随维度保存/API/历史链路实际持久化。

本节为中文部署与行为说明；未维护平行英文规格，避免复制两套不断变化的数据契约。英文首页无需扩展。

## issue #73：iFinD 主源深研零 Choice 出站与非估值分析（2026-11）

`DATA_SOURCE_PROFILE=choice` + `FINANCIAL_PRIMARY_SOURCE=ifind` + `ENABLE_IFIND_MCP=true` 的深度投研，在 Choice 完全不可用/无凭据时仍可完成：**深研身份**（深研 run 上下文 + 深研 scope 开关 `include_sector_flows=False` 同时成立，判定见 `ifind_deep_research_scope_active`）内所有路径的 Choice 实际出站为 0；独立专项服务（基本面/板块/财务专项报告）与 `choice` 主源行为不变。

- **深研身份，不是行业开关**：零 Choice 收敛的唯一判定是「深研 run + false-scope 开关 + iFinD 主源」三者同时成立。独立专项服务共用同一 false-scope 入口但无深研 run——保持既有完整聚合（Choice 补充/目标价/公司）与原副作用（无行业榜/股东）逐字不变；`include_sector_flows=False` 本身不构成深研身份。**缓存身份三层一致**：manager 进程内缓存读写键与 single_fetch 需求键、阶段 0 快照缓存键（`_d` 段）都携带同一深研身份——非深研 false-scope（Choice 形状）与深研 false-scope（iFinD 形状）的冷写/热读互不串用（真实 manager 双向回归覆盖），同身份热读命中自身缓存零新增出站。
- **聚合零 Choice**：深研身份内 Choice header/六字段/资产负债/估值/目标价/公司补查/独立核验全部不再发起（不再依赖 10001009 止损兜底）；`research` 块按实际来源标注 `source=ifind` 且不含 `targets` 键——「目标价未查询」是设计语义，与 Choice 失败（`choice:targets:*`）、契约异常、成功空覆盖分别表达，不互相改写。
- **公司资料迁移**：公司名称/主营/主营产品/公司简介/实际控制人及类型由 iFinD 公司资料批次提供（问句为 root 授权探针真实验证过的写法），各字段保留供应商原文值与该列自身截止日期（列未提供日期的字段保持未标注，不补造；不从十大股东表推断控制人、不从概念标签推断产品）。**证券归属只用「证券代码」列**并经既有 `normalize_stock_code` 规范化后精确匹配（叙述文本中的代码字样不构成行归属，外证券行整批拒绝）；列匹配只接受精确语义列名或带认可单位后缀（`（单位：…）`）的同一列名——绝不用类型/比例列补控制人，空/占位单元格保持字段缺失，raw_column 记录实际命中列名。置于必要财务/估值/资金之后同一 deadline 内的有界可选出站，恰一次，失败/取消照常保留 `ifind:company:*` 原因；消费层（业务画像正常装配、失败/取消留存）按块内实际 source 标注来源（不硬编码 Choice），历史 Choice 载荷保持原标注。**标准 HTTP API 补充（第五十九轮接入）**：`company_profile_with_fallback` 统一入口——深研 scope 公司事实 API 五字段优先（profile/main_business/main_products/controller/controller_type；官方模板出参：corp_profile/profile_products 无参、main_businuess/controller/type D=查询基准日），失败/不合格走 MCP 原路径（快照级回退不变）；**部分资料补缺**：API 部分字段+MCP 同字段补缺（字段渠道标注经 raw_column 区分不伪装同源）、MCP errors 随返回对象传播至 base.py company 分支 errors（`ifind:company:*` 同构）；**name 契约边界**：API 无独立名称指标——深研 scope 零 Choice header 下 `stock_name` 仍由 company.NAME 承载、缺失时保持空（既有「不从概念标签补造」语义不变）。
- **来源归因真实**：当前资料批次失败（超时/无效响应）时缺失估值字段的 `source_errors` 保留真实 iFinD 失败原因，估值/财务块来源链默认按实际尝试源标注（深研 scope 为 ifind）——不把未调用的 Choice 填为来源；公共详情/公共补充 helper（公开数据适配器，非 Choice 原生）的超时证据用中性阶段名 `public_details` / `public_supplement`（与既有 source_chain 兜底名及 selection_reason 一致），零 Choice 报告不残留 `choice_` 前缀误导；真实 Choice 失败仍按 `choice:*` 前缀原样保留。PB 缺合格读数时区分 `wrong_indicator_variant`（回执含已知中证/% 误映射变体）与 `no_qualified_reading`，随字段证据保留；快照/研究的请求日期与实际观测日期保持区分，不从请求补造 as_of。
- **PB 定向补查（每报告至多一次）**：聚合批次未取得合格 PB（含中证/% 变体）时，用 root 已捕获验证过的单指标定向问句（`市净率(PB,最新)`、`基准日=报表截止日期`、单位为倍、明确不使用中证发布/不把百分比当倍数）做一次有界补全：同绝对预算（不新增预算，资金窗口预算先行预留）、既有 single_fetch 每报告共享（含已知失败，无重试框架），不覆盖健康合格值、不放宽任何解析资格。补查返回的不合格读数（stale/future/口径或单位未知等）不采纳，真实拒绝原因经既有 `source_errors` 边界保留（`directed_pb:<reason>`）；**补查处取消不是供应商失败**——不写拒绝原因，已完成的财务/估值/行业/资金/research 事实沿既有聚合装配照常保留，停止后不再发起新出站，停止原因由 run 自身状态承载。
- **核验投影不丢事实**：`financial_snapshot` 六项已采纳事实的投影不再依赖 `cross_validation` 块存在——单源资格如实标注（`single_source`，不合成 verified 记录）；渲染层「当前财务快照」表按存在实际读数渲染，全部字段无读数时不渲染空表。
- **真实缺口列明**：真实缺 PB 时字段级缺口（`市净率PB`）进入 `data_gaps`，不再出现「财务未完整而缺口 0 项」假象；财务完整门槛不降低。
- **主营分产品财期与正常空覆盖（v5-v7，四出口实际期资格）**：初查 items 实际期须单一非未来——等于研究期正常采用；严格早于研究期可零额外出站直接作较早财期参考（`earlier_period_reference=True`、真实期保留、不伪造本期无覆盖）；晚于研究期/未来/混期拒绝（`initial_period_after_research`/`initial_period_future`/`initial_period_mixed`）。初查 no_data 仅回显期与请求研究期一致才认定本期正常空覆盖（否则 `period_mismatch` 不回查）。候选 items 实际期单一、严格早于研究期、非未来才作历史参考（`candidate_period_not_earlier`/`_future`/`_mixed` 拒绝）；候选 no_data 的实际回显期必须匹配该次候选查询（否则 `products_prior:period_mismatch`，保留原空事实、不发下一候选）。失败/已尝试状态共享与全部上述自动选择语义**仅 iFinD 主源深研身份**（`deep_identity`）生效——非深研消费者维持既有失败语义与 latest_annual 重试资格。空覆盖分类结构资格：合法排名（≥1，不可解析/非法排名列不静默忽略）且名称与数值结构**可按同一合法 rank 关联**、结构行须有财期证据。
- **产品失败不跨报告固化（v8，已批准方案）**：初查与候选的真实产品失败（timeout/权限/额度/invalid_response/资格拒绝/错期）完整传播到结构化 `product_segments.errors`（候选失败带 `prior:` 前缀，保留本期合法 no_data 事实与公司/财务/资金；取消/预算未派发不伪造成供应商失败）；business 维度缓存读写两侧复用同一结构化失败资格（适用策略复用既有 `_mapping_version_scoped("business")` 三配置判定，深研 orchestrator 与独立 fundamentals_service 共享缓存统一收紧）——携带失败的结果不写入、同版本旧坏记录读取 miss，下一报告有真实重取机会；健康当前明细/合格较早财期参考/有证据正常空覆盖照常缓存命中，business.status 与完成标准不变，Choice 主源/legacy 及其他维度资格不变；不新增缓存版本（既有 21→25 失效已覆盖已知旧缓存，QA 丢失 metadata 的不可识别边界如实保留）。独立专项服务的**取数/显式财期查询/默认补查/完成与评分标准等原路径不变**——本 v8 收紧的仅是其与深研共用的 business 维度缓存对携带产品失败记录的读写资格（错误缓存 miss 是明确的行为变化）。
- **资金失败不回退**：深研身份内 iFinD 资金供应商失败按缺口如实披露（失败原因随块保留，来源标签仍为实际 iFinD），不回退 Choice 补窗；非深研消费者既有有界回退保持。阶段 0 资金块复用与资金核验回填路径同样禁回退。
- **非估值分支**：iFinD 主源深研不查询机构目标价，与 Choice 成功空覆盖一样可在充分证据下完成定性情景/结论/观察信号（`valuation_basis=none`、无目标价/EV/估值驱动买卖评级）。F1 整维 status gate 的豁免**仅限 iFinD 主源深研策略**且要求四个非估值财务评分输入（ROE/毛利率/营收同比/归母净利同比）为实际 iFinD 合格读数、F1 不完整只来自 PE/PB 估值输入——Choice 主源/legacy 维持基线整维 gate；核心字段缺失/不合格、资产负债等非估值缺口、资金未完成或证据空壳仍不放行，F1 自身保持 degraded 并列明真实缺口；情景/结论/信号既有实质内容谓词与六维/贝叶斯校验不变。
- **受控纵向验收**：非估值成功链路与核心不足降级链路各有一条真实编排→SQLite 保存→API detail/download→SSE 唯一 done 的受控回归（仅外部传输受控，编排/资格/保存为真实代码路径），全链路零 Choice 原生出站断言。
- **缓存**：阶段 0 快照缓存键按深研身份分段（`_d0`/`_d1`），深研（零 Choice）与独立服务完整聚合快照互不串用；受影响阶段 0/研究员快照按 `FUNDAMENTAL_MAPPING_VERSION` 25 一次性失效重算（21→25 各版本随 #71/#73 v5-v7 各语义变更递增）；历史报告不回写、不重标来源。
- **回滚**：revert 本票提交并把 `FUNDAMENTAL_MAPPING_VERSION` 回退即可恢复原行为；无数据库结构变更、无历史报告删除/回填。

## issue #86：合格 iFinD 现金分红前复权日线接入（2026-11）

`DATA_SOURCE_PROFILE=choice` + `ENABLE_IFIND_MCP=true` + 有效 endpoint/token/超时（同一既有启用条件，不新增配置）时，沪深 A 股普通股「截至当前最新已完成交易日」的现金分红前复权日线优先来自 iFinD，经既有 `IfindFetcher.query` MCP 边界与 `DataFetcherManager` 候选链：合格成功时本次请求不再调用东财等后续候选；任一失败按既有候选相对次序兜底并保留真实原因。已验证口径（600519/600031 各 260 严格交易日）：现金分红前复权 OHLC 按分位四舍五入后与东财逐日一致、成交额逐日一致；泛「前复权」曾产生显著价格差，必须逐价格字段回显「前复权(现金分红)」。

- **资格边界**：证券身份在 `normalize_stock_code` 之前判定（`000001.SH` 指数不得归为 `000001.SZ` 银行，`399001.SZ`/`399006.SZ` 等深市指数不出站）；仅已确认普通股代码段（沪 6 开头、深 000/001/002/003/300/301）合格，ETF/指数/B 股/北交所/港美股不出站、不计供应商故障；显式历史目标在任何 iFinD 调用前跳过并把真实截止日传给原源（显式 baseDate 实测被忽略，不支持固定历史基点）；严格交易日历不可用即跳过，不退 weekday。窗口整窗合格：四个价格字段独立现金分红复权元数据、成交量单位股、成交额单位元（万/亿倍率 Decimal 换算）、请求窗口内所有严格交易日的六字段齐备且合法（开高低收跨字段不变式契约保证）；上市前占位/停牌量额缺失/IPO 泛复权/冲突重复/截断均整窗拒绝并兜底，非交易日占位按严格日历剔除，不造零量或前填 bar。
- **完整导出**：超过预览上限必须取完整 CSV 导出（截断提示存在时内联表不作为全量）；一次完整尝试（MCP+CSV GET+解码+解析+最终采纳）共享同一个有限绝对 deadline。导出使用本机 `curl` 子进程（参数向量、`-q` 为第一个参数、不继承代理/curlrc/netrc/cookie/MCP 凭据、正常 TLS、不跟随重定向、不重试）。父进程在启动前、轮询（≤20ms）、退出后和资格判定前复查绝对 deadline 与停止信号；取消或失败时终止并回收该子进程。`--max-filesize` 只是补充上限，父进程自己限制响应头与正文实际字节，并拒绝错误状态、非 `text/csv`/`application/csv`、非 identity 编码、矛盾或超限 Content-Length 与截断正文。缺少 curl 或当前平台无法有界排空管道时，该可选源明确失败并走既有兜底，不声称 Windows 已验证。外层无 deadline 时按「MCP 配置超时 + 导出阶段余量」自建并为后续源预留 fallback 份额。仅允许 `o.thsi.cn` 的已验证结果缓存 CSV 路径；唯一允许 HTTP→HTTPS 协议升级（仅改协议、绝不先访问 HTTP），拒绝 userinfo/端口/查询串/片段/相似域/其他路径与协议。
- **同报告复用与缓存**：活跃 ResearchRun 的日线槽是该报告内唯一权威窗口（同时存在股票作用域时也不再分叉）；没有 run 时，当前股票任务作用域在同一把锁里采纳、标记已消费并返回实际采用帧。启用范围内不采纳持久 DB 命中：断点续传、趋势、今日/昨日上下文、Agent 历史与深研阶段 0 都从这一帧读取；扩窗整窗重取，同源且重叠一致才替换，已消费窗口不换源——换源或重叠值冲突的扩窗对该次需求显式失败（拒绝原因随既有错误路径到达 Agent 工具、取数入口与阶段 0 限制注记，已采用帧保留给已满足的更短需求复用；最终报告在复用保留帧的同时如实保留该未满足扩窗注记，仅在 run 内记录的同一需求被后续一致采纳实际覆盖（行数或请求跨度起点）后清除——该状态的记录、消解与快照统一在 run 既有锁内（迟到拒绝按当前权威帧复核、不复活已解决状态，并发消解幂等）；同源一致短窗是真实可得历史如 IPO，不当作拒绝）。停止与外层 deadline 在复用、发布、落库和工具返回前复查，不把取消或超时收成缺失数据。跨报告/进程重新取全窗是本票明确代价。任何模式下读取范围含本票遗留 iFinD 行（含混合）的 DB 窗口都被拒绝并整窗走原链重取——该资格边界统一覆盖断点续传、趋势、普通/Agent 今日昨日上下文、Agent 预取与工具入口；启用范围内共享窗口获取失败时不以旧 DB 行复活；纯旧源混存缓存保持既有规则；不删除历史报告、不重写全局缓存最小行数。窗口记录与派生上下文统一携带真实日线来源（`data_source`）、iFinD 窗口的价格口径（前复权(现金分红)）与窗口实际截止日（不冒充供应商回传 baseDate），报告上下文包按实际来源标注。每一张匹配表在采纳其行之前独立校验量/额实际单位，后表不能因前表缺单位而跳过。
- **契约与回滚**：日线口径纳入 `data_source_contract`（`-ifind-daily-cashqfq-v1` 段），旧成功报告不沿用新日线窗口。回滚不是只关一个开关：若当前 `FINANCIAL_PRIMARY_SOURCE=ifind`，必须同时设 `FINANCIAL_PRIMARY_SOURCE=choice` 与 `ENABLE_IFIND_MCP=false`，并保持 `DATA_SOURCE_PROFILE=choice` 后按现有入口重启。这一对设置也会把既有财务 iFinD 切回 Choice，不是日线单独回滚；只关 `ENABLE_IFIND_MCP` 会因既有财务配置契约无法启动。缓存读取仍拒绝含 iFinD 遗留行的不合格窗口，纯旧源缓存行为保留，不删除历史报告。板块目录/成员/指数/排行、公告、分钟行情、其他市场与北交所未获资格，保持原路由。

## issue #94：扩窗需求的同源来源约束（2026-11）

前置研究（[Issue #89](https://github.com/SkillingX/daily_stock_analysis/issues/89)）记录的真实场景：iFinD 长窗口失败后候选链真实请求东财日线，但换源候选注定被既有跨源整窗一致性保护拒绝，出站完全浪费且该扩窗需求仍未满足。启用范围内已采用窗口未被替换前，扩窗需求（含强制刷新）把已采用来源作为 `DataFetcherManager.get_daily_data` 新增的 `constraint_source` 约束传入——只有同名数据源出站（iFinD 预置尝试仅当约束即 `IfindDailyFetcher` 时参与），其余候选跳过并在错误摘要中如实记录「未出站」，不冒充供应商故障；约束在不适用市场显式失败而非静默忽略。

保护与语义不变：换源/重叠冲突的拒绝路径（`_expansion_rejection`）原样保留作为兜底，失败原因、真实缺项、超时/停止复查、整窗资格校验、候选相对次序全部不变；不传约束的调用方（普通分析、告警、回测等）行为不变，不新增配置。约束使换源候选不再产生「成功取数后被拒」的结果，因此同源真实失败（manager 错误、源级超时、空窗口、截止裁剪失败）改经 run owner 方法 `record_failed_daily_expansion` 进入同一份持久未满足记录：锁内按当前权威帧复核——无已采纳帧的首取失败不误标（通用缺项语义不变）、已覆盖需求不记录、取消照常传播不误标；披露以锁内帧来源包装成与换源拒绝同形的诚实原因。失败原因统一脱敏限长（issue #94 复审收敛）：manager 异常串会经 loader 三个失败分支进日志、失败 memo、直接返回值与 run 持久未满足，再进报告 limitations——loader 在原因构造处统一走既有诊断脱敏规范 `run_diagnostics.sanitize_diagnostic_text`（与供应商诊断同一标准，保留非敏感端点 URL 等安全上下文，限长 300），保留可诊断错误类型名；unmet 两个 owner 写入方法（`record_unmet_daily_window`/`record_failed_daily_expansion`）在持久边界再次入库前脱敏，报告历史装配注记两个入口（`_safe_history` 的缺失原因注记与装配异常注记）按同一规范入库；合成密钥全链路测试与无敏感数据普通错误回归钉住该语义。报告侧 `sync_unmet_history_notes` 的清除/重建闭环照常工作：短窗恢复（复用保留帧）不丢失长窗未满足注记，后续真正长窗同源采纳在采纳锁内按既有覆盖消解机制清除。标准 HTTP CPS=6 同口径整窗候选仅在已验证的证券范围内可选启用，见下方 issue #91；扩大范围仍需供应商单位契约或充分可重复的资格证据。iFinD 实时快照（时间/量单位未合格）与深研产业链行业涨跌排行（同分类来源均为东财，非东财分类不同不可冒充）保持原路由，未覆盖项如实披露。

预算与诊断同样按受约束后的真实候选链计算，不为被排除候选预留或报告：约束为 `IfindDailyFetcher` 时通用链不存在任何同源候选，iFinD 预置尝试不再为幻影 fallback 预留尝试预算（否则仅剩 iFinD 可尝试且外层剩 20 秒时，预留 `min(8, 20×0.3)`=6 秒会把本可在 16 秒完成的请求提前截断到约 14 秒），失败诊断的 `fallback_to` 也不再报告将被跳过的来源；约束为某个普通候选时，该候选之后没有同源候选，同样不预留预算、不报告虚假 fallback、不出现指向被跳过源的「数据源切换」日志，「预算耗尽剩余 N 个数据源未尝试」的 N 只计入真实可尝试候选。无约束路径的上述预算与诊断逐位保持既有行为（非末位候选预留、末位候选不预留、fallback 链照旧）。

## issue #91：标准 HTTP CPS=6 同口径整窗日线候选（2026-11，默认关闭，沪深普通股资格段，仅个股深研上下文）

MCP 日线（issue #86）预置尝试失败后，`IfindHttpDailyFetcher` 作为第二预置候选在既有候选链（东财优先）之前出站，减少**个股深度投研**日线对东财公开接口的依赖。**默认关闭**：不配置时零出站、既有链路行为逐位不变。**参与范围限定个股深研执行上下文**（第二十二轮）：预警（`alert_service`）、回测、罗盘、定时任务、stock_service 等非深研调用保持既有候选顺序与 DB 路径，HTTP 零出站、读取范围不由 HTTP 贡献（MCP 语义独立保持原样）；判定复用 `current_research`（ResearchRun 上下文，个股深研编排经 `run.bind()` 设置，板块分析的 set 在 LLM 线程内与日线获取线程隔离），扩窗换源约束只发生在深研上下文内、无「非深研 + 显式约束」生产调用。实现与验收的过程记录（含各轮审查反例与修复）存于 `.claude/reviews/ifind-http-daily-implementation-20261114.md`，本节只保留用户需要的最终契约。

### 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `ENABLE_IFIND_HTTP_API` | `false` | 总开关；启用要求 `DATA_SOURCE_PROFILE=choice` + 有效凭据 + 有限正超时，缺任一项启动校验显式失败 |
| `IFIND_HTTP_REFRESH_TOKEN` | （空） | 唯一凭据（长期有效；网页版超级命令「账号详情」可获取）。进程内自动获取/缓存 access_token，过期或被 401/403 拒绝时经官方端点续用（不调用 update access token、不改变 refresh_token） |
| `IFIND_HTTP_TIMEOUT_SECONDS` | `30` | 单次尝试预算（经绝对 deadline 由 curl `--max-time` 硬性承载） |

- 凭据与 MCP token 独立互不通用、互不充当对方启用条件；凭据只进请求 header 与 0600 临时文件，不落日志/仓库/报告；设置页/registry 掩码显示；首次配置经私有 `.env`/部署秘密注入。
- 配额（官方权限页）：正式/试用行情 1.5 亿条/周；双请求成本每窗口约 `2×6×N` 单元格（250 日约 3000 条）。历史行情取值年限：试用/免费账号仅最近 5 年，更长窗口按供应商拒绝走兜底。

### 路由范围（沪深普通股资格段，代表样本验证，仅个股深研上下文）

- HTTP 候选对 `qualified_daily_identity` 的**全部沪深普通股资格段**路由（第十九轮段级方案，第十一轮 8 股白名单已删除）：沪 6 开头 + 深 000/001/002/003/300/301；ETF（沪 51/52/56/58、深 15/16/18）、B 股（900/200）、指数（含 `000001.SH` 显式身份）、北交所、港美股、格式错误仍在既有资格层拒绝——返回 None、保持原有候选（MCP/东财兜底），不出站、不计供应商故障。
- **参与范围限定个股深研执行上下文**（第二十二轮）：manager 预置与读取范围的 HTTP 贡献都要求 `current_research` 活跃——非深研调用（预警/回测/罗盘/定时任务/stock_service）HTTP 零出站、保持既有候选顺序与 DB 路径；读取作用域判定规则不变（MCP/HTTP 各自资格求并集），仅 HTTP 贡献加深研门（MCP 启用时的读取范围语义原样，非深研由 MCP 支撑）。深研内资格内标的按共享窗口权威语义走（跳过旧 DB 命中、窗口缺失不回退旧 DB、不走断点续传捷径），非资格证券保持原有候选与旧 DB 行为。深研扩窗换源约束（issue #94）只发生在深研上下文内，行为不变；非深研即使显式约束点名 HTTP 也不绕过门（约束源不可用时按既有约束语义显式失败、零出站）。
- **证据是代表样本级，不是全市场或接口契约证明**：官方对 THS_HQ 量额单位零声明（手册/FAQ/权限页均无）。量纲绝对单位（股/元）由交易所官方公开数据对照验证——沪市 `query.sse.com.cn` 官方概况接口 603259@2024-07-03 单股单日精确锚（量额差 ±50 容差内）+ 10 只×2 个交易日量纲锚；深交所个股页快照 4 只（003816/003021/301269/301308）amount 粗量纲锚（亿元两位小数，偏差 −0.014%~−0.172%）；此前 16 只与东财的逐日量额对照（600519/600031 各 250 日 + 其余各 47 日）。样本覆盖已采样段 600/601/603/605/688 与深 000/001/002/003/300/301——**沪市资格实现是「6 开头」，覆盖 689（CDR）等未采样前缀，未采样前缀与未采样标的均不称作已验证**；交易所概况/快照与供应商行情存在 0.01%~0.2% 的系统统计差（成因未定源），量级对照不宣称精确等价，也不宣称长期恒定，更不构成供应商对全市场的官方保证。
- **范围外保持原有候选**：资格段外的证券不出站；资格段内的证券每个窗口仍须通过量额自洽校验（见下），任一不满足整窗拒绝走兜底——「尽量减少东财调用」以逐窗口量额自洽校验（同 iFinD 双 CPS）与代表样本跨源对照承载，不逐股认证。

### 财务快照八项（标准 API 优先，第三十一轮；深研门为本轮审查收敛）

- `ENABLE_IFIND_HTTP_API` 开关扩展为标准 HTTP API 两类能力：日线候选（仅个股深研上下文，语义不变）+ **财务快照八项**（营业收入/归母净利润/加权ROE/销售毛利率/营收同比/归母同比/资产总计/负债合计，`financial_snapshot` 八字段）。**参与范围（本轮审查收敛，与日线候选同一授权边界）：标准 HTTP 财务快照仅「实际出站资格 + 个股深研执行上下文」同时成立时出站（判定单一来源 `http_financial_participation_active`，含 profile/开关/refresh_token/有效 timeout，与缓存身份段完全同判定）**——iFinD 主源财务与 Choice 主源交叉验证两个调用点一致生效；非深研（普通基本面专项等无深研 run 的聚合）完全不因该开关改变**调用**（iFinD 主源财务走 MCP 快照直调=HTTP 接入前旧行为、Choice 主源交叉验证段保持旧行为：MCP 开→MCP 直调、MCP 关→段不进零出站）、**预算**（交叉验证 HTTP 份额仅深研计入，非深研可用预算不虚占）与**缓存**（`financial_cache_contract` 不含 HTTP 段，开关翻转不失效非深研缓存；深研进程内缓存键在参与形态附加 `-http-fin-v1` 段，写入与读取两侧同判定）。开关关闭 = 两类均不参与，MCP 与既有路径原样；MCP 启用时的既有行为不因本收敛改变（深研下 fallback 的 API 优先/MCP 兜底/补缺语义原样）。
- 接入形态（第三十二轮收敛）：`financial_snapshot_with_fallback` 统一入口——`period=None` 主源多期语义经 API **字段级「有值的最新期」合并**（第三十九轮对齐 `select_current_financials` protected 契约、第四十一轮修正：从最近季度末往回逐期请求、每字段在最先遇到的有值期定格——**资产/负债单侧候选按真实期入快照**（第四十四轮定稿：不为其旧期配对额外扫描——同主期装配契约（`_qualified_ifind_balance` 同财期）裁决其采纳，不满足则缺口如实披露走既有 MCP/Choice 同主期兜底）；**逐字段最新承诺**（第四十五轮定稿）：缺失字段在候选期与预算线内继续探测有值最新期（探测优先于 MCP 补缺），自然缺项探测后仍缺则安全降级走 MCP 补缺/缺口披露不假称等价；全字段齐提前完成（通常 1-3 次出站，实测单期 ~0.6s）；后续期非取消失败时保留已合并合格字段（不整体丢弃）转交补缺/披露；通常 1-3 次出站）；具体报告期走单期入口。每字段按官方 `get_indicator_params` 模板出参（roe 仅 TD、毛利率/同比/披露日仅 D、四指标 D+C——不给单参指标传多余参数）；API 部分缺失由 MCP 补缺，财期契约**按调用形态区分**（第四十轮修正第三十九轮的过宽合并）：具体期（Choice 次源/单期契约）MCP 候选经归一化比较等于请求期才拼（指定 2026-09-30 不得填入 2025-12-31）；period=None 主源多期为 protected 语义（MCP 候选携带自身财期真实标注补入——不同财期不混称同一期）；每字段来源渠道/家族标注保持真实；API 整体失败/不合格快照级回退 MCP；MCP 失败按既有 `SourceReadError` 传播走 Choice 等兜底。401/403 鉴权强制续用重试一次（与日线 authorized 模式一致）；**预算预留按实际可用计算与候选能力**（第三十八/四十六轮：choice 主源核验预留按真正合格候选——只含实际开启且合格的候选（MCP 按开关；HTTP 按开关+凭据+资格，关闭或资格不满足不虚占默认 timeout）而不仅 enable_ifind_mcp——MCP 关/HTTP 开时慢 Choice 与可选研究不得占满总 deadline 使 HTTP 补充无时间；`available=deadline-now`、`budget=min(cap+解析余量, available)`、`reserve=min(8, budget×30%)`——短预算下 reserve 不被外层 deadline 截断，HTTP 用满 API 份额后 MCP 兜底仍有正剩余；bool 值不是财务数字显式拒绝，不伪装合格、不阻止 MCP 同期补缺）；MCP 兜底/补缺参与按**显式开关+实际配置**同时判定（用户关闭 MCP 但遗留 endpoint/token 时不得错误出站；关闭即 API 失败按实际类别上抛——超时→timeout、401/403→authentication_failed、429→rate_limited、其他 http→http_XXX 具体状态、资格→permission_denied、凭据缺失→invalid_configuration——诊断文本在脱敏日志、异常链保留，走既有 Choice 兜底）；错误层级收敛——用户取消（`ResearchStopped` 是 `TimeoutError` 子类）显式穿透不回退不消耗 MCP 出站，curl/token 的 deadline `TimeoutError` 与网络/认证/格式失败同按既有单源失败契约回退 MCP（MCP 复用同一绝对 deadline，不延长原预算）。
- 资格语义（第三十二轮）：缺失不伪装（自然缺项不进快照、不补零不推算）；`disclosed_at` 只取披露日指标实际值；**采纳前按分析时点校验披露时点**——披露日缺失（未披露期，供应商可能回退旧值）或晚于今日 → 该期拒绝（`invalid_response`，细分原因进日志）走 MCP 兜底；读数携带 `statement_scope="consolidated"` + `adjustment="restated"` + `period_basis`（年报 annual/其余 YTD）——经既有采纳门（`_eligible_current_financial`）判定最终业务字段合格。来源标注（第三十三轮修正）：`source="ifind"` + `source_family` 不显式设置由 AnchorReading 自动推导 `"ths"`（与 MCP 读数同供应商家族——同花顺 HTTP 与 MCP 是同一来源，不得被交叉验证独立性判定误判为独立来源；传输渠道区分仅存在于回退警告日志，日志经 refresh token 二次脱敏），caliber 对齐 MCP 规范值（weighted_roe/operating_revenue/parent_net_profit/gross_margin/balance_sheet_total 等）。
- 缓存（本轮审查收敛，替代第三十四轮形态；三层身份按实际依赖如实区分）：`financial_cache_contract` **不含 HTTP 开关段**——标准 HTTP 财务快照仅个股深研执行上下文出站（参与判定单一来源 `http_financial_participation_active` = 实际出站资格 `ifind_http_daily_enabled`【choice profile+开关+refresh_token+有效 timeout】+ 个股深研执行上下文，与调用/预算门一致）。三层：①**财务 manager 进程内键**（`financial_cache_contract` 无 HTTP 段 + 参与段 `-http-fin-v1`）——非深研调用/预算/缓存不因该开关或凭据补齐撤去改变（参与段随资格收敛，凭据切换两方向不串）；②**阶段 0 持久快照键**与③**维度持久缓存键**以完整 `data_source_contract`（含 HTTP 开关段）为基底——这两层**随开关翻转一次性失效重取**（非深研亦然，24h TTL/重建语义内有界），叠加参与段（阶段 0 `_h{0/1}`、维度 `_httpfin`/`_plain` 双形态段）隔离深研/普通与凭据切换；旧版本无段文件对两种新形态均不命中（迁移隔离，第八轮）。深研报告/维度评分键继续使用完整 `data_source_contract`。回滚语义不变：身份过期的旧产物保留可读不命中，一次性重取。来源标注：字段级仅供应商家族可见（`source`="ifind"、`source_family` 自动推导 `"ths"`——同花顺双渠道同家族，不破坏交叉验证独立性）；**渠道（API/MCP）不进入字段级契约**（AnchorReading 不承载渠道身份），渠道可观测性=日志级（API 快照采纳 info、回退 warning、MCP 补缺 info/warning——成功与回退均可验收分辨）。期参数以 **canonical 期次（YYYYMMDD）为单一事实来源**（第三十六轮）：入口 `_canonical_period` 接受消费方 Choice 形态（YYYY-MM-DD）与官方形态并统一规范化（两格式实测均被服务端接受且值一致），请求、解析、期间基数（annual/YTD）与业务资格全部以 canonical 为准——带/不带横线的同一期间产出完全等价的读数与资格（带横线年报不再误标 YTD）；非法形态/非法日历日期显式拒绝（`invalid_configuration`，回退语义下走 MCP 原路径），不静默透传依赖服务端宽松。

### 量额资格与能力边界

- 每个窗口采纳前做两笔请求（CPS=6 取数 + CPS=1 不复权对照），要求：两响应日期集合一致、量额逐日精确相等（不随复权变动）、未复权成交均价 `amount/volume` ∈ 当日**未复权** `[low, high]`（一字板闭区间成立）。任一不满足整窗拒绝走兜底；价格比较只用未复权行。
- **能力边界**：把量额以同一倍率缩放（如各 ×100，百股/百元量纲）VWAP 不变、判据照样通过——双请求证明的是「量额互一致、未复权、比率正确」，**不能证明绝对单位是 股/元**，该同倍率缩放不可被当前运行时校验检测（测试锁定），绝对单位证据来自交易所官方对照与供应商间一致性（段级代表性），不虚构拦截。价格侧已知差异：605499 除息（2026-09-24）前后 17/47 条 CPS=6 前复权价与第三方存在约 −0.03 元的未定源差异（交易所证实除息、算法根因未证实）——复权价无运行时判据，如实披露。

### 失败 / 取消 / 超时

- **传输**：本机 curl 子进程（复用 MCP 导出的可中断传输模式）——`--max-time` 限制请求总时间（配置预算与剩余 deadline 取小）；父进程每 20ms 检查停止信号与 deadline，取消（`ResearchStopped`）时终止并回收子进程；字节边界由父进程自数。子进程环境只继承 PATH（无代理/curlrc/netrc/cookie），正常 TLS、不跟随重定向、不重试；Windows 明确失败走既有兜底。
- **token 生命周期**：access_token 官方有效期 7 天（`expired_time` 防御解析，缺失按 6 天回退、60 秒提前刷新）；被 401/403 拒绝时强制续用并重试同一请求一次，仍被拒则错误指向 refresh_token 有效性/账号权限；刷新后的 5xx/网络/超时保留真实原因，首次换取成功后的业务失败原因保真不重试。token 等待锁分段轮询（每 0.1s 响应停止/deadline），并发换取单飞（每凭据一次出站），并发强制刷新按换取时间去重。
- **熔断与诊断**：健康熔断按 `daily_data:cn:IfindHttpDailyFetcher` 独立计数，取号（探测名额）只发生在真正出站；`fallback_to` 是诊断观察时点的下一候选（纯读观察），**不是并发保证**——观察与实际出站之间熔断状态可被并发请求改变，以实际出站结果为准。预算与 fallback 语义与 MCP 预置尝试同构（约束生效时不为被排除来源预留或报告）。
- **缓存身份**：`data_source_contract` 含 `-ifind-http-daily-v1` 段（仅 HTTP 启用时）——MCP×HTTP 四象限身份互不相同，HTTP 开启后普通非强刷不复用关闭期旧报告/维度缓存（重新获取让新源出站）。**财务缓存身份（本轮审查收敛，替代第三十四轮形态；三层按实际依赖如实区分）**：标准 HTTP 财务/公司快照仅个股深研执行上下文出站（非深研保持 MCP/Choice 原路径），参与判定单一来源 `http_financial_participation_active`（第八轮收敛为实际出站资格 `ifind_http_daily_enabled` + 个股深研执行上下文——凭据补齐/撤去身份段实时收敛）。①财务 manager 进程内键：`financial_cache_contract`（不含 HTTP 段）+ 参与段 `-http-fin-v1`——非深研缓存不随开关/凭据改变；②阶段 0 持久快照键：完整 `data_source_contract`（含 HTTP 段，随开关一次性失效重取）+ `_d` 段（零 Choice 收敛身份）+ `_h{0/1}` 参与段；③维度持久缓存键：完整 contract + 双形态段（参与 `_httpfin`/不参与 `_plain`——旧版本无段文件对两新形态均不命中，迁移隔离）。深研报告/维度评分键继续使用完整 `data_source_contract`。双向隔离真实持久层回归：深研先写（HTTP 形态）普通专项不复用、普通先写深研不复用、同身份命中零新取数、开关翻转重取、凭据切换两方向不串。见上方缓存段与回滚节。**仍随完整契约翻转的范围（第二十五轮核实，不声称全部非深研缓存零影响）**：供给链工具的进程内 TTL 缓存（`supply_chain_tools` 股票信息缓存，秒级 TTL + 并发单飞）与阶段 0 基本面快照持久缓存（`deep_research_dims/context.py`，24h TTL，深研与普通基本面专项报告共用）仍以完整 `data_source_contract` 为键——两者内容（产业链行情/财务资金快照）均不依赖日线口径，开关翻转的实际影响是有界的一次性重新出站（前者与 TTL 自然过期等价、后者 24h TTL 本会过期重建）；保留完整契约理由：进程内 TTL 与 24h 快照翻转代价有界且拆分无正确性收益，阶段 0 快照是深研维度缓存的上游输入，拆出财务身份会引入与维度缓存双层身份的语义解释成本，不做机械拆分。
- **共享 `StockDaily` 表契约（第二十四轮）**：标准 HTTP 日线行**不落跨业务共享日线表**——`save_daily_data` 在存储层统一过滤 `IfindHttpDailyFetcher` 来源（精确匹配，判定单一来源 `is_ifind_http_daily_source`），覆盖深研工具、主流程、Agent 预取等全部现有与后续写库入口。深研窗口只在内存共享作用域（run 槽位/股票任务作用域）内复用与扩窗，深研读取路径不读该表，因此不入库不影响深研任何行为；旧源（东财兜底等）与 MCP 来源行照常按既有 UPSERT 语义写库，普通链路读库始终命中的是未被覆盖的旧源行（含混合来源语义不变）。真实 SQLite 库完整序列回归（旧源行→深研首取/复用/扩窗→普通读库→直接读表→HTTP 失败回退→全关对照→并发旧源写入）见 `tests/test_ifind_http_daily_shared_db.py`。

### 回滚

1. **先将 `ENABLE_IFIND_HTTP_API` 设为 `false`** 并重启——恢复既有链路（MCP/东财兜底）。注意不能只移除 `IFIND_HTTP_REFRESH_TOKEN`：开关仍为 true 时缺凭据会被启动校验拒绝。
2. 确认回滚稳定后，可按需移除 `IFIND_HTTP_REFRESH_TOKEN` 并再次重启。
3. 缓存身份：HTTP 启用期生成的成功报告/维度缓存带 `-ifind-http-daily-v1` 身份段，回滚关闭后**保留可读但不再作为当前身份命中**（同日需按原身份重新获取或等次日）；HTTP 引入前的历史报告命中行为不变；无数据库迁移。财务报告/财务分析/manager 财务聚合缓存随 `financial_cache_contract`（本轮收敛后不含 HTTP 段）——非深研普通基本面专项的身份与缓存不随该开关翻转改变；标准 HTTP 财务快照仅个股深研执行上下文出站（深研形态参与段随资格收敛，凭据补齐/撤去实时隔离），身份过期的旧产物保留可读不命中、一次性重取。

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
| `LLM_STREAM_RESPONSE_TIMEOUT` | 既有单次模型响应上限，未统一改默认值（最小钳制 10 秒） | 本机 GLM 验收用 `180` 秒，依据实测返回耗时；三个超时维度的生产对照建议在 120–180 秒范围做一次有界验证（首选 180 候选），需单独授权后执行；线上按实际模型设置，不机械照搬 |
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

默认不开启；显式启用必须同时给 endpoint/token 和有限正超时。配置在设置页只读展示，token 为敏感项；修改后重启实际服务进程。仍使用 `DATA_SOURCE_PROFILE=choice`，不改成 legacy，也不恢复其他停用渠道。若 `FINANCIAL_PRIMARY_SOURCE=ifind`，停用 iFinD 必须同时设 `FINANCIAL_PRIMARY_SOURCE=choice` 与 `ENABLE_IFIND_MCP=false` 并重启：财务回到 Choice，日线回到既有候选链（东财等原链），不是「Choice 日线」；历史报告保留生成时证据。

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

收入、成本、毛利金额保留各自单位；没有明确单位的产品毛利率显示原始数值及“单位未知”，不推断比例/百分点。明细始终标“部分覆盖”，不汇总、不把产品毛利率替代公司毛利率。披露日/原文未返回时如实说明；既有Choice简介保留。模型调用失败、输出未通过严格字段校验，或阶段0之后提前中断时，共用事实恢复逻辑保留已经取得的公司、产品和股东资料；该章节仍标未完成，不生成缺失评分。基本面与深度投研一致，Choice/iFinD 模式的业务降级报告不作为当日成功缓存，后续可重新生成；legacy 模式保留原有同日复用与显式刷新语义。模型不能制造或改写结构化明细。

资料复用同一启用、客户端、现有快照缓存与刷新标记；业务研究使用阶段0传入的研究资料，不为补主营重取六项财务。基本面强制刷新覆盖财务及主营获取；主营错误不命中同日报告缓存，恢复后可重试。空响应及非法排名披露为供应商错误，保留其他有效来源资料，已取得事实进入基本面和深度投研的现有章节。

基本面与深度投研共用分产品模板，每条记录保留独立 Markdown 表格行；正文、历史读取和下载均应完整显示已取得的产品，不能仅以结构化数据中的记录数量作为展示验收。

## 股东明细接入（#18）

现有股权章节按需取得前十大股东明细。查询使用当前研究里已经确认、且不晚于分析时点的财期，并要求供应商返回实际截止日期；没有财期时不逐期扫描。披露日晚于分析时点的财期不用于查询。“最新”或与请求不一致的返回仍记为财期未知或不匹配，不用请求日期或财务快照日期补成股东实际期。名称表和数值表仅按返回的证券、明确财期和排名连接。十个唯一排名均有合格股数及百分比才标名单完整，否则显示缺项/部分覆盖；这不是独立双源核验，也不表示整个股权研究完成。不能把合计比例赋给个体，不能从持股数据推导实控人、关联关系或高管背景。Choice现有控制人和股东信息保留。

股数的万/亿倍率和表头%按各自单位解析，字段名“比例”不等于原值是0—1比例。零值保留；数量为负、比例超范围、单位矛盾不作为合格持股数值。模型失败仍保留已取得明细，披露日和原文链接未返回时明确列缺项。与主营共用同一客户端、既有缓存/刷新/期限处理，不增加历史股东系统或新页面。

资金研究可复用阶段0完整当期名单，再用现有入口有界补查上一个季度末；只在两期实际财期、10个唯一排名/股东和百分比数值都合格时，计算两期各自十大股东集中度变化（百分点）。名单可以变化，不将退出前十解释成清仓，不将集中度变化映射成全体机构持仓或净买卖。补查共享已有超时、同报告去重、缓存与强刷；失败保留资金流解释并如实降级。缺披露日/原文链接仍明确说明，不能据此证明历史时点可得性，不增加配置项。机构持仓变化仍需独立满足其统计范围与期间资格，不能用该集中度结果代替。
