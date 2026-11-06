# -*- coding: utf-8 -*-
"""板块分析模块 Pydantic v2 Schema（Type-Contract-Data Defense Layer 3）。

参考 `docs/type-contract-data-defense.md` 第 4 节「Pydantic 配置模式」，
对齐 `src/schemas/compass.py` 的「strict + frozen + validate_assignment + extra=forbid」基类。

v2 载荷（issue #81）：新报告不再沿用旧综合总分/分档/β 含义/权重——旧评分
字段**保留路径但显式退役（恒为 None）**，非评分事实旧路径经最小兼容投影
（``identification``/``policy``/``base_rate``/``prosperity``）保留可读；
首版「未评分」与「资料不足」用独立 Literal 状态精确表达。存量旧记录经
列表/详情端点以自由 dict 返回（analysis_json 不经过本文件严格校验），
旧字段可读，不做破坏性迁移。

issue #82 追加 ``sector_trend``/``sector_peers``；issue #83 追加
``sector_catalysts``（行业催化，统一准入+严格日期资格+去重）与
``sector_synthesis``（证据化综合研判，登记表引用+模型输出纯定性，
数字/日期事实由服务端从登记材料渲染）——均为可选默认键的追加兼容边界。

字段约束原则：
- Literal 用于有限枚举（subject_status / payload status / lean）
- Field(ge/le/min_length/max_length) 用于数值范围和字符串长度
- extra="forbid" 防止未知字段污染
- frozen=True 防止下游误改
- validate_assignment=True 防止运行时赋值绕过校验
"""

from __future__ import annotations

from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------

class _StrictBase(BaseModel):
    """板块分析所有 schema 的基类：strict + frozen + validate-on-assign + extra=forbid。

    与 compass._StrictBase 同源；保持一致以便后续合并 / 抽取。
    """

    model_config = ConfigDict(
        strict=True,
        frozen=True,
        validate_assignment=True,
        extra="forbid",
    )


# ---------------------------------------------------------------------------
# Literal 枚举
# ---------------------------------------------------------------------------

PolicyLean = Literal["supportive", "neutral", "restrictive"]
"""政策倾向三档（与 src.services.sector_analysis_service._POLICY_LABEL 对齐）。"""

SubjectStatus = Literal["confirmed", "unverified", "ambiguous", "missing"]
"""主行业身份状态（贯穿载荷/正文/页面/历史，不静默取近似名）：
- confirmed：东财板块锁定（精确名称+板块代码）且成分表含目标股
- unverified：板块已锁定但成分验证失败或不含目标股（归属未确认）
- ambiguous：行业名存在但板块身份无法唯一锁定（无精确命中/多候选/表不可用）
- missing：无行业数据
"""

PayloadStatus = Literal["unscored", "insufficient_data"]
"""报告资料状态（正常「未评分」与「资料不足」语义分开）：
- unscored：有可研究主体，首版板块专项不产出综合评分（可带缺口）
- insufficient_data：主行业身份缺失或无法唯一锁定，核心问题无法回答
"""

Completeness = Literal["complete", "partial"]
"""报告资料完整性（与评分政策分开：未评分是评分政策，完整性是资料状态）。
判定依据**有效动态事实**（issue #81 第二轮），独立于评分政策：
- complete：confirmed + 当日行情同键命中 + 核心动态（涨跌幅/动量）有效；
  部分可选维度缺口由 gaps 如实标注，不机械降级
- partial：insufficient_data（主体缺失/歧义）、当日行情未命中、行情行存在但
  核心动态未取得（行存在 ≠ 数据取得）、或成分 unverified（归属未确认）
"""

PayloadVersion = Literal[2]
"""板块报告载荷版本。"""


_GapList = List[Annotated[str, Field(min_length=1, max_length=200)]]



# ---------------------------------------------------------------------------
# 身份子结构
# ---------------------------------------------------------------------------

class SWChain(_StrictBase):
    """申万一/二/三级行业参考链（成分验证产物；口径单列，不与东财成分混算）。"""

    l1: Optional[str] = Field(default=None, max_length=64)
    l2: Optional[str] = Field(default=None, max_length=64)
    l3: Optional[str] = Field(default=None, max_length=64)
    l3_code: Optional[str] = Field(default=None, max_length=32)
    gaps: _GapList = Field(default_factory=list, max_length=10)


class MembershipInfo(_StrictBase):
    """东财板块成分验证结果（同板块成分表含目标股才算 confirmed）。"""

    verified: bool = False
    member_count: Optional[Annotated[int, Field(ge=0, le=10000)]] = None


class BoardIdentity(_StrictBase):
    """主行业身份（识别阶段产物；同一东财行业板块口径）。"""

    industry_em: Optional[str] = Field(default=None, max_length=64)
    industry_cninfo: Optional[str] = Field(default=None, max_length=64)
    business_scope: Optional[str] = Field(default=None, max_length=200)
    board_name: Optional[str] = Field(default=None, max_length=64)
    board_code: Optional[str] = Field(default=None, max_length=16)
    subject_status: SubjectStatus
    membership: MembershipInfo = Field(default_factory=MembershipInfo)
    observed_at: Optional[Annotated[str, Field(max_length=32)]] = None
    """本系统采集时点（ISO 8601）。"""
    vendor_as_of: Optional[Annotated[str, Field(max_length=32)]] = None
    """供应商数据截止时点；供应商未回显时保持 None，不用采集时间顶替。"""
    classification_system: Optional[Annotated[str, Field(max_length=32)]] = None
    """分类体系名称（本报告锁定口径=东财行业板块）；无行业数据时 None。"""
    classification_level: Optional[Annotated[str, Field(max_length=32)]] = None
    """供应商返回的分类层级；未返回恒为 None，如实未知不推算。"""
    classification_version: Optional[Annotated[str, Field(max_length=32)]] = None
    """供应商分类版本；未返回恒为 None，如实未知不推算。"""
    sw_chain: SWChain = Field(default_factory=SWChain)
    sector_name: Optional[str] = Field(default=None, max_length=64)
    gaps: _GapList = Field(default_factory=list, max_length=10)


class ConceptItem(_StrictBase):
    """相关概念条目（仅列示，不作为行业归属依据）。"""

    name: Annotated[str, Field(min_length=1, max_length=64)]
    source: Literal["ifind"]
    as_of_nature: Optional[Annotated[str, Field(max_length=16)]] = None


class ConceptsBlock(_StrictBase):
    """相关概念列表（仅列示；入口未启用或失败记缺口，不拖垮主报告）。

    超过展示上限（50 条）时 ``items`` 截取展示，``total_returned`` 保留
    供应商原始总量、``truncated=True`` 且缺口注明「展示截取，非供应商故障」。
    """

    items: List[ConceptItem] = Field(default_factory=list, max_length=50)
    source: Literal["ifind"] = "ifind"
    total_returned: Annotated[int, Field(ge=0, le=10000)] = 0
    """供应商原始返回条数（截取前的总量；入口未启用/失败为 0）。"""
    truncated: bool = False
    """items 是否因展示上限被截取。"""
    gaps: _GapList = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# 背景支柱子结构
# ---------------------------------------------------------------------------

class PolicyPillar(_StrictBase):
    """政策倾向支柱输出（行业 DNA 库·静态规则背景，非近期新闻事件）。"""

    lean: Optional[PolicyLean]
    label: Optional[str] = Field(default=None, max_length=32)
    gaps: _GapList = Field(default_factory=list, max_length=10)


class BaseRateRow(_StrictBase):
    """基率表单行（all_rates 透出用）。"""

    keyword: Annotated[str, Field(min_length=1, max_length=32)]
    base_rate: Annotated[float, Field(ge=0.0, le=1.0)]
    note: Optional[str] = Field(default=None, max_length=200)


class BaseRatePillar(_StrictBase):
    """行业基率支柱输出（启发式先验背景，非实测胜率）。"""

    base_rate: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    basis: Optional[str] = Field(default=None, max_length=64)
    hit_keyword: Optional[str] = Field(default=None, max_length=32)
    is_default: bool = False
    table_version: Annotated[str, Field(min_length=1, max_length=16)]
    table_updated: Annotated[str, Field(min_length=1, max_length=32)]
    table_industry_count: Annotated[int, Field(ge=0, le=10000)]
    percentile: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    all_rates: List[BaseRateRow] = Field(default_factory=list, max_length=100)


class IntradayRow(_StrictBase):
    """当日行情行数据（东财行业板块表单行；板块代码与身份锁定同键）。"""

    board_name: Optional[str] = Field(default=None, max_length=64)
    board_code: Optional[str] = Field(default=None, max_length=16)
    change_pct: Optional[float] = None
    turnover_rate: Optional[float] = None
    total_market_cap: Optional[float] = None
    amount: Optional[float] = None
    up_count: Optional[float] = None
    down_count: Optional[float] = None
    leader: Optional[str] = Field(default=None, max_length=32)
    leader_change_pct: Optional[float] = None


class IntradayPillar(_StrictBase):
    """当日行情支柱输出（价格强弱与活跃度，非产业景气、非资金净流入）。"""

    sector_found: bool = False
    momentum: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    activity: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    amount_activity: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    """成交额活跃度（成交额/全表中位）——相对水平，不是资金净流入。"""
    breadth: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    rank_of: Optional[int] = Field(default=None, ge=1)
    total_boards: Optional[int] = Field(default=None, ge=1)
    row: IntradayRow = Field(default_factory=IntradayRow)
    gaps: _GapList = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_rank(self) -> IntradayPillar:
        if self.rank_of is not None and self.total_boards is not None and self.rank_of > self.total_boards:
            raise ValueError("Board rank cannot exceed the observed board population")
        return self


class BackgroundBlock(_StrictBase):
    """背景依据（三类来源性质不同，正文与页面分别命名，不混同）。"""

    policy: PolicyPillar
    base_rate: BaseRatePillar
    intraday: IntradayPillar


# ---------------------------------------------------------------------------
# 近期趋势（issue #82）：严格日历 20 个已完成交易日 + 沪深300 同日期对比
# ---------------------------------------------------------------------------

class TrendObservation(_StrictBase):
    """单个已完成交易日的实际合格日线观测（issue #82；无发明值）。

    ``close`` 是供应商日线收盘值经有限正 Decimal 资格后的边界序列化（float）；
    排序/收益计算全程 Decimal，此处仅为 I/O 边界类型，精度上限为上游数值。
    板块侧收盘为板块指数点位，基准侧为指数点位（供应商口径原样，不换算元）。
    """

    date: Annotated[str, Field(min_length=10, max_length=10)]
    close: Annotated[float, Field(gt=0.0)]


class TrendBenchmark(_StrictBase):
    """比较基准子块（沪深300，与板块完全相同的期望交易日集与合格性规则）。

    ``period_return_pct`` 是基准自身 20/20 合格时的区间收益（19 个间隔复合）；
    日期集不齐或数据不足时为 None 并由 gaps 说明，不与板块侧凑对。
    ``observations`` 是基准侧**实际合格观测**（升序、无发明值）：部分合格时
    保留已取得部分与 ``qualified_sessions`` 实际数量，缺口注明缺失/无效日期，
    供正文与存档独立于板块侧展示（issue #82 rework）。
    """

    name: Annotated[str, Field(min_length=1, max_length=32)] = "沪深300"
    symbol: Annotated[str, Field(min_length=1, max_length=16)] = "sh000300"
    source: Annotated[str, Field(min_length=1, max_length=32)] = "东财指数日线"
    start_date: Optional[Annotated[str, Field(max_length=10)]] = None
    end_date: Optional[Annotated[str, Field(max_length=10)]] = None
    qualified_sessions: Optional[Annotated[int, Field(ge=0, le=20)]] = None
    """基准侧实际对齐期望窗口的合格观测数（20/20 才有区间收益；部分合格如实<20）。"""
    observations: List[TrendObservation] = Field(default_factory=list, max_length=20)
    """基准侧实际合格日线观测（升序；缺失/无效日期不占位，由 gaps 说明）。"""
    period_return_pct: Optional[float] = None
    gaps: _GapList = Field(default_factory=list, max_length=10)


class SectorTrendBlock(_StrictBase):
    """板块近期趋势块（issue #82；主行业 confirmed 才有资格执行与给结论）。

    ``period_return_pct`` 由 20 个收盘价形成的 **19 个间隔**复合得出，起止日
    由 ``start_date``/``end_date`` 明确标注（不用「20日收益」冒充）；板块与
    基准可各自保留有资格部分，``relative_return_pp`` 仅在两源对同一期望窗口
    均 20/20 合格时输出，单位为**百分点**（区间收益差，不是相对收益率）。
    ``expected_dates`` 是严格交易日历期望窗口（升序、唯一真源），数据不足时
    缺口注明具体缺失日期，不补自然日、不取交集凑 20。
    ``observations``/``qualified_sessions`` 是板块侧实际合格观测与覆盖数
    （issue #82 rework）：部分合格时保留已取得部分（无发明值），与区间收益
    资格分开表达；展示与资格共用同一份观测结果，不重复判定逻辑。
    """

    eligible: bool = False
    """身份资格（subject_status == confirmed）才为 True；False 时未执行出站。"""
    window_completed_sessions: Literal[20] = 20
    interval_count: Literal[19] = 19
    start_date: Optional[Annotated[str, Field(max_length=10)]] = None
    end_date: Optional[Annotated[str, Field(max_length=10)]] = None
    expected_dates: List[Annotated[str, Field(min_length=10, max_length=10)]] = Field(
        default_factory=list, max_length=20,
    )
    qualified_sessions: Optional[Annotated[int, Field(ge=0, le=20)]] = None
    """板块侧实际对齐期望窗口的合格观测数（20/20 才有区间收益；部分合格如实<20）。"""
    observations: List[TrendObservation] = Field(default_factory=list, max_length=20)
    """板块侧实际合格日线观测（升序；缺失/无效日期不占位，由 gaps 说明）。"""
    period_return_pct: Optional[float] = None
    benchmark: Optional[TrendBenchmark] = None
    relative_return_pp: Optional[float] = None
    source: Optional[Annotated[str, Field(max_length=32)]] = None
    observed_at: Optional[Annotated[str, Field(max_length=32)]] = None
    gaps: _GapList = Field(default_factory=list, max_length=10)


class PeerItem(_StrictBase):
    """同业样本条目（同一成分快照内的成交活跃代表，非市值/基本面龙头）。"""

    code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    amount: Optional[float] = None
    """快照观测的当日成交额（单位见块级 ``amount_unit``）；缺失/无效为 None
    + 块级缺口（不补造）；成交额无效的成员保留成员资格但不参与成交额排名。"""
    change_pct: Optional[float] = None
    """快照观测的当日涨跌幅；缺失为 None，不影响样本成员资格。"""
    is_target: bool = False


class SectorPeersBlock(_StrictBase):
    """同业比较块（issue #82；同一已验证成分快照内选取，资格同趋势块）。

    选取规则固定：同一成分快照按**成交额前五 + 目标股去重**（「成交活跃代表」，
    不称市值/基本面龙头，不逐股补市值、不混用其他分类权重）。排序指标为快照
    成交额（原始值经 Decimal 判定后降序、并列按代码升序）；成员资格（代码
    有效且无重复冲突）与成交额可排名资格分开：目标股成交额缺失/无效时仍
    保留成员并如实标注（``target_in_amount_top5=None``），不误报「非成员」。
    ``target_change_pct_rank`` 是目标股在样本内按**当日涨跌幅**降序的位置
    （名次 = 严格更高的个数 + 1，并列同名次）；样本内存在涨跌幅缺失的同行时
    不输出全样本名次（withhold + 有效比较范围缺口），不把缺失同行默默排后。
    ``amount_unit``/``amount_provenance`` 随载荷固化成交额单位及判定依据
    （issue #82 rework）。
    """

    eligible: bool = False
    selection_rule: Annotated[str, Field(max_length=120)] = (
        "同一成分快照按成交额前五 + 目标股去重（成交活跃代表，非市值/基本面龙头）"
    )
    items: List[PeerItem] = Field(default_factory=list, max_length=6)
    target_in_amount_top5: Optional[bool] = None
    """目标股是否落在成交额前五内；成交额缺失/无效无法判定时 None（成员仍保留）。"""
    target_change_pct_rank: Optional[Annotated[int, Field(ge=1, le=6)]] = None
    amount_unit: Literal["元"] = "元"
    """快照成交额计量单位（判定依据见 ``amount_provenance``）。"""
    amount_provenance: Annotated[str, Field(max_length=240)] = (
        "成交额单位为元：已装 AkShare 成分接口与全市场快照接口同为东财 clist "
        "f6 字段且 fltt=2、原样映射「成交额」；官方行情文档标注全市场快照成交额"
        "为元，板块接口文档未标注单位，按同字段同接口参数推断"
    )
    """成交额单位的判定依据（诚实标注推断来源，不冒称板块文档明示）。"""
    snapshot_rows: Annotated[int, Field(ge=0, le=10000)] = 0
    """成分快照观测总行数（资格过滤前；与 rankable_rows 一起暴露实际范围）。"""
    rankable_rows: Annotated[int, Field(ge=0, le=10000)] = 0
    """成交额可排名行数（代码有效、无重复冲突且成交额有限正）；与观测总数分开。"""
    sample_size: Annotated[int, Field(ge=0, le=6)] = 0
    snapshot_observed_at: Optional[Annotated[str, Field(max_length=32)]] = None
    source: Optional[Annotated[str, Field(max_length=48)]] = None
    gaps: _GapList = Field(default_factory=list, max_length=14)


# ---------------------------------------------------------------------------
# 行业催化（issue #83）：统一准入（source→相关性→日期→同一事件资格/去重登记）
# ---------------------------------------------------------------------------

class CatalystItem(_StrictBase):
    """已证实近期催化条目：发布日期在有效窗口内 + 行业相关 + 去重后。

    ``published_date`` 经共享语义归一化（ISO 日期），只是窗口资格依据——
    事件日期未知即为未知，不用发布日期顶替。``date_basis`` 区分供应商发布
    字段与文章元数据日期口径；``date_basis_detail`` 保存 article 口径的
    可识别原始依据（实际 meta 标签），无依据不升级。``same_event_urls``
    是同一事件的跨域转载链接（保守显式规则合并：同题+明确转载标记），
    明确非独立佐证——域名数量不冒充独立确认。``final_url`` 是 article
    口径实际取得响应的终态 URL（重定向后的实际文章地址；日期证据冲突
    判定与溯源域名以它为准），``url`` 保留原始引用身份；provider 口径或
    无重定向时与 ``url`` 一致/为 None。
    """

    evidence_id: Annotated[str, Field(min_length=1, max_length=16)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    url: Annotated[str, Field(min_length=1, max_length=512)]
    source_domain: Annotated[str, Field(min_length=1, max_length=253)]
    """实际文章 URL 的完整解析 hostname（DNS 全名上限 253；来源身份不截断——
    共享 URL 准入对超限 host 本地拒绝，绝不用截断段当同源判定身份）。"""
    snippet: Optional[Annotated[str, Field(max_length=500)]] = None
    published_date: Annotated[str, Field(min_length=10, max_length=10)]
    date_basis: Literal["provider_published", "article_metadata"] = (
        "provider_published"
    )
    """日期口径：供应商发布字段 / 文章 HTML 出版元数据（禁 URL 启发式，
    有 raw meta 依据才接受）。"""
    date_basis_detail: Optional[Annotated[str, Field(max_length=200)]] = None
    """article_metadata 口径的原始依据（meta 标签 repr）；provider 口径 None。"""
    same_event_urls: List[Annotated[str, Field(min_length=1, max_length=512)]] = Field(
        default_factory=list, max_length=4,
    )
    """同一事件的其他转载链接（保留溯源；不作独立佐证计数）。"""
    final_url: Optional[Annotated[str, Field(min_length=1, max_length=512)]] = None
    """article 口径实际取得响应的终态 URL（重定向后的实际文章地址；日期
    证据与 source_domain 按它判定）；追加兼容字段（默认 None），provider
    口径 None，无重定向时与 url 相同。"""


class BackgroundCatalystItem(_StrictBase):
    """无日期或降级背景线索（明确限制：最多作背景，不参与已证实近期催化判断）。

    ``published_date`` 仅在「历史回顾被降级」时保留实际发布日期（不发明
    事件日期）；``downgrade_reason`` 记录降级依据（标题声明旧事件年月、
    文章元数据日期在窗口外、URL-only 等）。``final_url`` 在文章探查已到达
    实际终态时保留该地址与实际 hostname（``source_domain`` 即实际文章
    域名；日期冲突/历史回顾/同文重复别名/安全失败背景同样不丢 provenance），
    追加兼容字段（默认 None；探查未到达终态或 provider 口径为 None）。
    """

    evidence_id: Annotated[str, Field(min_length=1, max_length=16)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    url: Annotated[str, Field(min_length=1, max_length=512)]
    source_domain: Annotated[str, Field(min_length=1, max_length=253)]
    snippet: Optional[Annotated[str, Field(max_length=500)]] = None
    published_date: Optional[Annotated[str, Field(min_length=10, max_length=10)]] = None
    downgrade_reason: Optional[Annotated[str, Field(min_length=1, max_length=200)]] = None
    final_url: Optional[Annotated[str, Field(min_length=1, max_length=512)]] = None
    """文章探查已到达的实际终态 URL（重定向后文章地址）；未到达终态的
    失败（传输/redirect 边界/出站前拒绝）为 None。"""


class SectorCatalystsBlock(_StrictBase):
    """行业催化块（issue #83；confirmed 资格前置，同趋势/同业块模式）。

    - ``eligible`` 只表达资格（subject_status == confirmed）；搜索入口未配置、
      供应商失败、等待超预算时仍 eligible=True 并在 gaps 如实说明（资料未取得
      与资格不足分开表达）。
    - ``items`` 是已证实近期催化；``background_items`` 是无日期或降级条目的
      有界背景线索（明确限制标记，不参与已证实催化、不作为模型重要结论的
      唯一事实依据）。已证实与背景条目经过**同一**准入序列（来源验证→相关性
      →日期→事件资格/去重登记）；文章日期探查升级同样完整重过事件资格。
    - ``dropped_*`` 是过滤前逐类计数；供应商失败响应 ≠「无新闻」（gaps 文本
      分开，诊断经 sanitize 后入库）。
    """

    eligible: bool = False
    query: Optional[Annotated[str, Field(min_length=1, max_length=200)]] = None
    window_days: Optional[Annotated[int, Field(ge=1, le=365)]] = None
    window_start: Optional[Annotated[str, Field(max_length=10)]] = None
    window_end: Optional[Annotated[str, Field(max_length=10)]] = None
    observed_at: Optional[Annotated[str, Field(max_length=32)]] = None
    provider: Optional[Annotated[str, Field(max_length=32)]] = None
    items: List[CatalystItem] = Field(default_factory=list, max_length=5)
    background_items: List[BackgroundCatalystItem] = Field(
        default_factory=list, max_length=3,
    )
    dropped_duplicate_url: Annotated[int, Field(ge=0, le=1000)] = 0
    dropped_syndicated: Annotated[int, Field(ge=0, le=1000)] = 0
    dropped_offwindow: Annotated[int, Field(ge=0, le=1000)] = 0
    dropped_offtopic: Annotated[int, Field(ge=0, le=1000)] = 0
    dropped_invalid_source: Annotated[int, Field(ge=0, le=1000)] = 0
    """URL/来源无效条目计数（本地供应商数据错误；不进入载荷、不截断伪造）。"""
    dropped_invalid_field: Annotated[int, Field(ge=0, le=1000)] = 0
    """畸形候选行总数 = 供应商 raw 侧逐行拒绝（``SearchResponse.malformed_rows``
    结构化计数，含 fallback 尝试中已见的；MiniMax 适配器入口类型契约）+
    本地准入拒绝（title/url 非空字符串、可选 snippet 字符串——不 str(...)
    把结构/字节/数字伪合法成标题/摘要；剩余有效行全部保留）。两类作用对象
    不相交（供应商侧拒绝的行不会进入本地准入），无双重计数。追加兼容字段
    （默认 0）。"""
    source: Optional[Annotated[str, Field(max_length=48)]] = None
    gaps: _GapList = Field(default_factory=list, max_length=14)


# ---------------------------------------------------------------------------
# 证据化综合研判（issue #83）：既有模型路由 + 登记表引用/纯定性校验；
# 无评分/分档/行动建议
# ---------------------------------------------------------------------------

class SynthesisClaim(_StrictBase):
    """单条研究结论：有界**纯定性**文本 + 必须引用登记表内实际证据 ID。

    文本不得含数字/日期/价格/比率/代码编号，也不得含明确倍数词（翻倍/
    翻番——半角/全角数字、常见中文数量表述（含简繁变体）与倍数词都在
    装配边界拒绝）；数值由服务端从所引证据渲染到既有数字表格。"""

    kind: Literal["interpretation", "counterevidence", "risk"]
    text: Annotated[str, Field(min_length=1, max_length=500)]
    evidence_refs: List[Annotated[str, Field(min_length=1, max_length=16)]] = Field(
        default_factory=list, max_length=8,
    )


class SectorSynthesisBlock(_StrictBase):
    """证据化综合研判块（issue #83；confirmed 资格前置 + 合格催化事实前置）。

    - ``eligible`` 只表达资格；模型未配置/失败/输出畸形/无合格催化分别是
      不同 ``status``，合格事实保留、缺口如实，不用空泛模型文字补完整叙事。
    - ``claims[].evidence_refs`` 已在装配边界全量校验为**实际登记**证据 ID
      集的子集；陈述文本纯定性（不含数字/日期/价格/比率/代码编号，也不含
      翻倍/翻番等明确倍数词——定量/倍数陈述整条拒绝并记 sanitized 原因），
      数字事实由服务端从**所引条目**材料渲染到既有数字表格；行动指令整条
      拒绝。claims 限定为催化剂含义/目标相关性/条件风险（市场数值关系由
      服务端拥有，见 ``summary_origin``），单独列示；校验仅覆盖结构/引用/
      纯定性边界，**语义准确性不由此保证**——模型观点可能仍有误读，
      核对请以表格与所引原始材料为准。
    - ``summary``/``uncertainty_note``：新报告由服务端从已采纳事实程序化
      生成（``summary_origin="server_facts"``），不消费模型 claims 文本；
      旧记录为历史 validated-claim 拼接（默认 origin），原样读回不改写。
      无评分/分档/行动/价格目标；``model``/``provider`` 透出实际路由
      （不写死模型）。模型诊断经 sanitize 后入库。
    """

    eligible: bool = False
    status: Literal[
        "ok", "model_unavailable", "model_error", "malformed_output",
        "no_qualified_facts", "skipped_identity",
    ] = "skipped_identity"
    summary: Optional[Annotated[str, Field(min_length=1, max_length=1000)]] = None
    claims: List[SynthesisClaim] = Field(default_factory=list, max_length=6)
    uncertainty_note: Optional[Annotated[str, Field(max_length=500)]] = None
    summary_origin: Literal["server_facts", "legacy_model_claims"] = (
        "legacy_model_claims"
    )
    """摘要来源判别器（追加兼容字段，默认 legacy；无版本迁移/新端点）。

    新生成的报告 ``summary``/``uncertainty_note`` 由服务端从已采纳事实
    程序化生成（``server_facts``，不消费模型 claims 文本）；旧载荷（无此
    键）读取默认 ``legacy_model_claims``（历史 validated-claim 拼接），
    原样读回，渲染与审计据此诚实区分来源，不把 legacy 派生摘要误标为
    服务端事实摘要。"""
    rejected_count: Annotated[int, Field(ge=0, le=24)] = 0
    """模型输出中被整条拒绝的结论计数（status=ok 的混合输出 > 0）。

    追加兼容字段（默认 0）：携带拒绝规模供编排完整性判定与前端展示——
    合格结论与全部服务端事实保留，但混合输出完整性=partial，不呈现
    「全部合格研究」。不解析 gap 文本；旧载荷（无此键）读取默认 0。"""
    provider: Optional[Annotated[str, Field(max_length=64)]] = None
    model: Optional[Annotated[str, Field(max_length=120)]] = None
    observed_at: Optional[Annotated[str, Field(max_length=32)]] = None
    gaps: _GapList = Field(default_factory=list, max_length=14)


# ---------------------------------------------------------------------------
# 资料状态
# ---------------------------------------------------------------------------

class AssessmentBlock(_StrictBase):
    """资料状态：首版不产出综合评分（score 恒为 None，显式未评分不伪填）。

    status（评分政策）与 completeness（资料完整性）分开：confirmed 但当日
    行情全缺时 status=unscored 而 completeness=partial 且 reasons 给出原因。
    """

    status: PayloadStatus
    completeness: Completeness = "complete"
    score: None = None
    reasons: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list, max_length=10,
    )


# ---------------------------------------------------------------------------
# v1 兼容投影（追加，非替换）：旧非评分事实路径保留可读
# ---------------------------------------------------------------------------

class LegacyIdentification(_StrictBase):
    """旧 ``analysis.identification`` 路径兼容投影（事实自 v2 identity 投影）。"""

    industry_em: Optional[str] = Field(default=None, max_length=64)
    industry_cninfo: Optional[str] = Field(default=None, max_length=64)
    business_scope: Optional[str] = Field(default=None, max_length=200)
    sector_name: Optional[str] = Field(default=None, max_length=64)
    sw_chain: SWChain = Field(default_factory=SWChain)
    gaps: _GapList = Field(default_factory=list, max_length=10)


class LegacyPolicy(_StrictBase):
    """旧 ``analysis.policy`` 路径兼容投影。

    ``score`` 恒为 None（退役）：v2 不产出政策档位分，政策 DNA 只保留方向
    标签作为静态规则背景；旧调用者读到的 None 即显式退役语义。
    """

    lean: Optional[PolicyLean] = None
    label: Optional[str] = Field(default=None, max_length=32)
    score: None = None
    gaps: _GapList = Field(default_factory=list, max_length=10)


class LegacyProsperityRow(IntradayRow):
    """旧 ``analysis.prosperity.row`` 兼容投影：v2 行数据 + 退役列显式 None。

    ``volume``/``company_count`` 为旧 v1 字段，现供应商口径不再返回，恒为
    None（退役语义，不为新增字段造事实）。
    """

    volume: None = None
    company_count: None = None


class LegacyProsperity(_StrictBase):
    """旧 ``analysis.prosperity`` 路径兼容投影（事实自 v2 intraday 投影）。

    ``capital`` 恒为 None（退役）：旧「资金」维度实为成交额相对分位，
    v2 已更名 ``amount_activity`` 并在正文标注「非资金净流入」；把成交额
    活跃度回填 capital 会重新引入净流入误读，故显式退役。
    """

    sector_found: bool = False
    momentum: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    activity: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    capital: None = None
    breadth: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    rank_of: Optional[int] = Field(default=None, ge=1)
    total_boards: Optional[int] = Field(default=None, ge=1)
    row: LegacyProsperityRow = Field(default_factory=LegacyProsperityRow)
    gaps: _GapList = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# 主契约：compose_report 返回值（v2 载荷 + v1 兼容投影）
# ---------------------------------------------------------------------------

class SectorReportPayload(_StrictBase):
    """compose_report v2 载荷契约（generate 响应 analysis 字段）。

    追加兼容（非整体换形）：v2 字段之外保留旧路径最小兼容投影——
    ``identification``/``policy``/``base_rate``/``prosperity`` 为旧非评分
    事实路径（既有调用者仍可读）；评分字段 ``sector_score``/``band``/
    ``beta_meaning``/``weights``/``prosperity_score`` 恒为 None（显式退役，
    新报告不恢复旧综合总分、旧分档或行动结论，也不以「数据不足」代指
    正常未评分）。存量 v1 记录经列表/详情端点以自由 dict 原样透出。
    """

    payload_version: PayloadVersion
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Annotated[str, Field(min_length=1, max_length=64)]
    generated_at: Annotated[str, Field(min_length=10, max_length=32)]
    identity: BoardIdentity
    background: BackgroundBlock
    concepts: ConceptsBlock
    assessment: AssessmentBlock
    # ---- v2.1 追加（issue #82）：近期趋势与同业。compose 总是产出；
    # Optional 默认 None 仅是追加兼容边界——已验收 #81 形状 payload（无此键）
    # 仍通过严格校验；存量旧记录经详情端点以自由 dict 透出，缺新键仍可读 ----
    sector_trend: Optional[SectorTrendBlock] = None
    sector_peers: Optional[SectorPeersBlock] = None
    # ---- v2.2 追加（issue #83）：行业催化与证据化综合研判。同上追加兼容
    # 边界；已验收 #81/#82 形状 payload（无此键）仍通过严格校验 ----
    sector_catalysts: Optional[SectorCatalystsBlock] = None
    sector_synthesis: Optional[SectorSynthesisBlock] = None
    # ---- v1 兼容投影（事实路径）----
    identification: LegacyIdentification
    policy: LegacyPolicy
    base_rate: BaseRatePillar
    prosperity: LegacyProsperity
    # ---- 评分退役（显式 None 语义）----
    sector_score: None = None
    band: None = None
    beta_meaning: None = None
    weights: None = None
    prosperity_score: None = None
    gaps: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=20,
    )


# ---------------------------------------------------------------------------
# API I/O 边界
# ---------------------------------------------------------------------------

class SectorAnalysisRequest(_StrictBase):
    """POST /api/v1/sector-analysis/generate 请求体。

    FastAPI 自动从 query 参数解析，所以这里仅供内部调用与测试使用。
    """

    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None


class SectorAnalysisGenerateResponse(_StrictBase):
    """POST /api/v1/sector-analysis/generate 响应（仅 success 终态走 200）。

    timeout → 504、保存失败 → 500，均不落入本模型，不产生伪成功响应。
    """

    report_id: Annotated[str, Field(min_length=1, max_length=32)]
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None
    markdown: Annotated[str, Field(min_length=1)]
    analysis: SectorReportPayload


class SectorAnalysisReportItem(_StrictBase):
    """报告元数据 + 可选 markdown 全文。

    list 端点：只填充核心字段（id/code/name/created_at/score），md_path/markdown/analysis_json 均为 None。
    详情端点：填充所有字段，包括从 .md 文件读取的 markdown 全文。
    sector_score 为存量旧记录的综合分；v2 新记录恒为 None（不产综合分）。
    analysis_json 为自由 dict：存量 v1 记录与新 v2 载荷都原样透出，不破坏历史。
    """

    id: Annotated[str, Field(min_length=1, max_length=32)]
    stock_code: Annotated[str, Field(min_length=6, max_length=6, pattern=r"^\d{6}$")]
    stock_name: Optional[Annotated[str, Field(max_length=64)]] = None
    created_at: Annotated[str, Field(min_length=10, max_length=32)]
    """ISO 8601 datetime 字符串（storage 层返回 `datetime.isoformat()` 格式）。

    不用 `datetime` 类型是因为 _StrictBase 强制 strict=True，不接受 ISO 字符串自动解析；
    显式用 str + ISO 字符串格式约束，与 storage 契约一致。
    """
    md_path: Optional[Annotated[str, Field(min_length=1, max_length=512)]] = None
    """md_path：详情端点有，列表端点无（列表只展示元数据）。"""
    sector_score: Optional[float] = Field(default=None, ge=0.0, le=100.0)
    analysis_json: Optional[Dict[str, Any]] = Field(default=None)
    """分析 JSON。storage 层以 JSON 字符串持久化，详情端点反序列化后填充。"""
    markdown: Optional[Annotated[str, Field(min_length=1)]] = None
    """markdown 全文：详情端点从 md_path 读取后填充；列表端点不返回（前端按需 GET markdown 端点）。"""


class SectorAnalysisListResponse(_StrictBase):
    """GET /api/v1/sector-analysis/reports 响应。"""

    success: bool = True
    data: List[SectorAnalysisReportItem] = Field(default_factory=list, max_length=200)
    total: Annotated[int, Field(ge=0)]


class SectorAnalysisDetailResponse(_StrictBase):
    """GET /api/v1/sector-analysis/reports/{id} 响应。"""

    success: bool = True
    data: SectorAnalysisReportItem


class SectorAnalysisDeleteResponse(_StrictBase):
    """DELETE /api/v1/sector-analysis/reports/{id} 响应。

    删除是「DB 记录 + 存档文件」两步：DB 已删而文件 unlink 失败（OSError）
    时如实部分失败——``success`` 仍为 True（记录已删除、不可再访问），
    ``file_cleanup="failed"`` 且 ``message`` 给出可理解状态，不静默假成功。
    """

    success: bool = True
    deleted: Annotated[str, Field(min_length=1, max_length=32)]
    file_cleanup: Literal["ok", "failed"] = "ok"
    """存档文件清理结果；failed 表示记录已删除但文件可能残留。"""
    message: Optional[Annotated[str, Field(min_length=1, max_length=200)]] = None
    """file_cleanup=failed 时的可理解部分失败说明；正常删除为 None。"""
