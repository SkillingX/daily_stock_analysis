# -*- coding: utf-8 -*-
"""板块研究准入与证据契约（issue #83）：纯判定逻辑，无网络/预算/模型出站。

职责边界（单一职责，编排与预算见 sector_analysis_service）：
1. 催化候选统一准入：source 验证（公共 HTTP(S) URL 结构契约）→ 行业相关性
   → 日期获得 → **同一个**事件资格/去重登记步骤（provider-dated 与
   article-dated 两条日期分支只在「日期获得」一步不同，事件资格完整共享
   ——背景条目不存在绕过事件资格的 append→upgrade 捷径）。
2. 文章发布日期证据：真实 newspaper 提取逻辑，禁用 URL 启发式
   （0.2.8 ``get_publishing_date`` 是 URL→metadata 两级策略；空 URL 即只剩
   可识别的 HTML 出版元数据），并要求可保存的 raw meta 依据；URL-only、
   URL/meta 冲突均不作日期证据。
3. 证据登记表：实际取得的合格事实按 evidence_id 注册（材料原文 +
   provenance）。具体业务量值（价格/估值/涨跌幅/分位/排名/收益/财务规模）
   及其主体、指标、时点、单位由服务端从登记材料渲染；模型陈述承担定性
   解读，不新增/重述/计算/错配具体数值（不存在任何"数字归属/量纲匹配"
   的 token 锚定通道）。
4. 综合输出校验：结构/引用资格/背景唯一支撑/行动词/**有限明确量值拒绝**
   全量前置，任一违规整条拒绝（不清洗、不截断伪合法）。普通中文含数量形
   汉字不因字形拒绝——判定范围见 ``quantitative_statement_reason`` 的
   有限合同（issue #85）。

本模块不 import akshare/newspaper/LLM 运行时；日期证据提取函数接受已下载
的有界 HTML（网络读取由 service 层包裹预算与字节上限后调用），保证判定
逻辑可纯函数测试。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 有界常量（与 schema 上限对齐；不做配置项）
# ---------------------------------------------------------------------------

CATALYST_SEARCH_LIMIT = 10   # 过滤前请求条数
CATALYST_DISPLAY_CAP = 5      # 已证实催化展示上限（模型输入同界）
CATALYST_BACKGROUND_CAP = 3   # 无日期/降级背景线索上限
CATALYST_ARTICLE_PROBE_CAP = 3  # 无日期条目的有界文章日期探查上限
CATALYST_ARTICLE_MAX_BYTES = 1_000_000  # 单篇文章响应字节上限（读取期间强制）
"""文章日期探查只需要 HTML 头部出版元数据；1MB 显式上限在读取期间强制
（stream 逐块累计，超限立即停止并关闭响应），不是下载完成后再检查。
已装 newspaper 0.2.8 ``Article.download`` 是非 stream 全量
``response.text``（实测 8,389,344 bytes 全进 parser），因此探查不走它。"""

SYNTHESIS_MAX_TOKENS = 2000
SYNTH_CLAIMS_RAW_CAP = 24     # 模型输出 claims 原始条数上限（超出整体拒绝）
CLAIMS_MAX = 6
CLAIM_TEXT_MAX = 500
CLAIM_REFS_MAX = 8
CATALYST_URL_MAX = 512
CATALYST_TITLE_MAX = 200
CATALYST_SNIPPET_MAX = 500
SAME_EVENT_URLS_MAX = 4

# ---------------------------------------------------------------------------
# 合同正则（显式有界子集，非通用语义判定）
# ---------------------------------------------------------------------------

ACTION_INVEST_RE = re.compile(
    r"(买入|卖出|加仓|减仓|清仓|建仓|止盈|止损|目标价|建议持有)"
)
"""行动/价位指令词：claim 文本命中即整条拒绝（无投资建议契约）。"""

STOCK_ONLY_EVENT_RE = re.compile(
    r"(运动会|年会|团建|周年庆|生日会|表彰大会|联谊|招聘会|员工体检)"
)
"""个股活动合同：标题含目标股名 + 此类活动词 → 仅个股事件，不冒充行业催化。"""

SYNDICATION_SOURCE_RE = re.compile(
    r"(来源[：:]\s*\S|转自|转载[自：:]|【[^】]{2,24}】|\S{2,12}(?:通讯社|报社|电)记者)"
)
"""明确转载/通稿标记：跨域同题 + 命中 → 同一事件合并（保守显式规则）。"""

_TITLE_EVENT_DATE_RE = re.compile(r"(20\d{2})年(?:(\d{1,2})月(?:(\d{1,2})日)?)?")
"""标题声明的事件年(/月(/日)?)：历史回顾降级判定（标题是文章对主题的最强
声明；正文引用历史比较不动——新政策正文提到旧年份不降级）。"""

# ---------------------------------------------------------------------------
# 有限明确业务量值判定（issue #85；替换旧「任何数量字符一律拒绝」逐字禁令）
# ---------------------------------------------------------------------------

_NUMERAL_CHARS = "[〇零一二两俩倆三四五六七八九十百千万亿拾佰仟萬億兩幾壹贰貳叁參肆伍陆陸柒捌玖参数双半几]"
"""封闭中文数字字符集（简繁/合成字/带 Numeric 属性的 Lo 汉字）：仅这些
字符参与结构层判定，不再按字形一律拒绝（参考/參考/陸續/陆续/一致/统一/
一般/一度等普通词不受影响——参/參/陸/陆 均为 Lo 类别、带 Numeric 属性，
旧 isnumeric 视图曾把它们误当字面数字）。"""

_METRIC_TERMINATOR = (
    r"(?=$|[\s，。、；：！？…—（）【】《》·,.;:!?'\"（）%％]|以上|以下|左右|上下)"
)
"""数值词段完结边界：量纲词后的数字段必须以上述终点收束才判为量值；
否则视为普通词内部数字形（利润一般/股价一度/一直/一流/十大）放行。
不给普通词加豁免——边界是位置结构规则。"""

_METRIC_SEP = r"[ \t：:]*"
"""量纲词与系词桥/数字段之间的有限分隔（空格/冒号；不含句读，不跨句）：
「市盈率为 十」「市盈率为：十」「市盈率：十」与「市盈率为十」同判。"""

_METRIC_BRIDGE = (
    r"(?:为|為|是|达|達|於|在|约|約|超|过|過|近|低|高|低于|低於|高于|高於|"
    r"达到|僅为|僅為|仅为|僅有|仅有|超过|不到|不足|第)?"
)
"""量纲词与数字段之间的系词桥（简繁；含序数「第」与比较方向词）。"""

_METRIC_WORDS = (
    r"(?:市盈率|市净率|市占率|估值|价格|股价|单价|营收|收入|利润率|利润|"
    r"涨幅|涨跌幅|跌幅|收益率|回报率|增速|增幅|排名|分位|负债率|毛利率|"
    r"净利率|市值|成交额|换手率|规模|份额)"
)
"""量纲词封闭集合（业务数值的指标名；非行业词库、非增长词表）。"""

_QUANT_STRUCTURE_PATTERNS = (
    re.compile(_METRIC_WORDS + _METRIC_SEP + _METRIC_BRIDGE + _METRIC_SEP
               + r"(?!百分|分位)" + _NUMERAL_CHARS + "+" + _METRIC_TERMINATOR),
    # 数字段 + 货币（含双字货币）；单位即终点
    re.compile(_NUMERAL_CHARS + r"+(?:美元|港元|港币|日元|欧元|元)"),
    re.compile(r"百分之" + _NUMERAL_CHARS + "+"),
    re.compile(_NUMERAL_CHARS + r"+分之" + _NUMERAL_CHARS + "+"),
    # 函数词 + X成（占比声明；无函数词裸成数不判——见已知漏检披露）
    re.compile(r"(?:约|約|近|超|达|達|占|漲|涨|跌|升|降|增|回落|高达|超過|"
               r"超过|不足|仅为|僅為|仅有|僅有|只有|跌至|升至|占到|高于|高於|"
               r"低于|低於)" + _NUMERAL_CHARS + r"+成"),
    re.compile(_NUMERAL_CHARS + r"+倍"),
    re.compile(r"翻(?:了|个)?" + _NUMERAL_CHARS + r"*[倍番]"),
    re.compile(r"翻倍|翻番|倍增"),
    re.compile(_NUMERAL_CHARS + r"点" + _NUMERAL_CHARS + "+"),
    re.compile(_NUMERAL_CHARS + r"+个?百分点"),
    # 序数 + 名次/时点终点（「次」不在终点集：第一次属普通叙述）
    re.compile(r"第" + _NUMERAL_CHARS + r"+个?(?:名|位|月|日|天|周|季度|年|轮)"),
    re.compile(r"前" + _NUMERAL_CHARS + r"+(?:名|位)"),
    # 时长量纲；「年」需 ≥2 位数字段（半年/一年放行）
    re.compile(_NUMERAL_CHARS + r"+个?(?:月|日|天|周|季度)"),
    re.compile(_NUMERAL_CHARS + r"{2,}年"),
)
"""明确量值结构（封闭集合）：量纲词+系词桥+完结数字段、货币、百分之/分之、
函数词+成、数字+倍、翻X倍/番+倍增（词级倍数封闭三词）、口语小数、百分点、
序数+名次/时点终点、前X名次、时长量纲。非通用 NLP 语义判定。"""

_QUANT_REASON_PREFIX = "quantitative_statement"


def _quant_literal_reason(text: str) -> Optional[str]:
    """字面数字层（Unicode 类别制）：Nd/Nl/No 原码点与百分号 → 拒绝原因。

    仅真字面数字字形（阿拉伯/全角/罗马/上标/分数/阿拉伯-印度等）逐字拒绝；
    中文数量形汉字（Lo 类别，含带 Numeric 属性的参/參/陸/陆）不在本层——
    它们由结构层判定。无名称/上下文豁免（名称含字面数字仍拒，身份事实由
    服务端 identity 字段保留、模型正文中性指称）。"""
    for ch in text:
        if ch in ("%", "％"):
            return f"{_QUANT_REASON_PREFIX}:{ch}"
        if unicodedata.category(ch) in ("Nd", "Nl", "No"):
            return f"{_QUANT_REASON_PREFIX}:{ch}"
    return None


def _quant_structure_reason(text: str) -> Optional[str]:
    """结构层：明确量值结构 → 拒绝原因；普通词内部数字形 → None。"""
    for pattern in _QUANT_STRUCTURE_PATTERNS:
        m = pattern.search(text)
        if m:
            return f"{_QUANT_REASON_PREFIX}:{m.group(0)}"
    return None


def quantitative_statement_reason(text: str) -> Optional[str]:
    """有限明确业务量值判定：明确量值陈述 → 拒绝原因；自然行文 → None。

    四步双视图（任一命中即整条拒绝；输出原文不改写，折叠仅是检测视图，
    折叠命中原因记 ``nfkc:`` 前缀）：
    1. 原码点字面层：``category ∈ {Nd, Nl, No}`` 与百分号（名称含字面数字
       仍拒——无名称豁免；Roman 数字在原码点视图即命中，不依赖 NFKC，
       NFKC 会把 Ⅱ 归一成 II/X 丢数值属性）；
    2. 原码点结构层：封闭量值结构（见 ``_QUANT_STRUCTURE_PATTERNS``）；
    3. NFKC 折叠字面层：兼容符号折叠出字面数字（㋀→1月、㍘→0点）即拒；
    4. NFKC 折叠结构层：康熙部首类码点（⼆→二）折叠后命中量值结构。

    已知保守拒绝（如实披露，不豁免）：名称含字面数字（白酒Ⅱ）、序数时点
    叙述（第一天）、口语时间（八点半）、夸张修辞（百倍努力）、含「数字+元」
    成语（一元复始）、数+月模糊时长（筹备数月）。
    已知漏检（如实披露，语义层归独立证据审读，不逐个补词）：裸规模计数
    （数十家公司/成交额破万亿）、模糊时长（一段时间）、无函数词裸成数
    （九成公司）、无名次终点（前三）、单字数字+年（持续一年）、非常规桥接
    （市值上到千亿）、口语「个点」（涨了五个点）。这些是有限语法未覆盖的
    数量/持续表达——放行不等于「不是数量」或「无需证据支持」。
    """
    text_or_empty = text or ""
    reason = _quant_literal_reason(text_or_empty)
    if reason is not None:
        return reason
    reason = _quant_structure_reason(text_or_empty)
    if reason is not None:
        return reason
    folded = unicodedata.normalize("NFKC", text_or_empty)
    if folded != text_or_empty:
        reason = _quant_literal_reason(folded)
        if reason is not None:
            return f"{_QUANT_REASON_PREFIX}:nfkc:{reason.split(':', 1)[1]}"
        reason = _quant_structure_reason(folded)
        if reason is not None:
            return f"{_QUANT_REASON_PREFIX}:nfkc:{reason.split(':', 1)[1]}"
    return None


def _declared_title_dates(title: str) -> List[Tuple[int, Optional[int], Optional[int]]]:
    """标题声明的（年, 月?, 日?）序列（按声明精度，缺省为 None）。"""
    return [
        (
            int(m.group(1)),
            int(m.group(2)) if m.group(2) else None,
            int(m.group(3)) if m.group(3) else None,
        )
        for m in _TITLE_EVENT_DATE_RE.finditer(title or "")
    ]


def _declared_topic_bound(year: int, month: Optional[int],
                          day: Optional[int]) -> Optional[date]:
    """单个标题声明的**保守上界时点**（该声明精度下可能的最晚时刻）。

    不发明缺失精度：完整且有效的年月日 → 该日期；月有效但日缺失/无效 →
    该月最后一整日（整月都早于窗口才可能回顾）；月无效 → 该年年末。
    返回 None 仅当年分量畸形（正则已约束为 20xx，实际不触发）。
    """
    import calendar

    if not (2000 <= year <= 2099):
        return None
    if month is None or not (1 <= month <= 12):
        return date(year, 12, 31)
    last_day = calendar.monthrange(year, month)[1]
    if day is None or not (1 <= day <= last_day):
        return date(year, month, last_day)
    return date(year, month, day)


def is_retrospective_title(title: str, window_start: date) -> bool:
    """标题主题时点早于窗口起始 → 历史回顾（背景降级）。

    标题是文章对主题的最强声明：取各声明的**保守上界时点**（见
    ``_declared_topic_bound``——缺失精度不发明：年月取整月上界、仅年取
    年末上界）中**最晚**者作为主题时点，早于实际 ``window_start`` 即回顾：
    - 「回顾2020年白酒行情」（上界 2020-12-31）→ 回顾
    - 11 月窗口内「2026年1月政策回顾」（上界 2026-01-31 < 2026-11-04）→ 回顾
    - 「白酒行业2020年与2019年销量对比」（今天发布）→ 回顾
      （历史销量对比是历史背景，不是今天的事件；无豁免词表）
    - 「2026年新政 与2018年版对比」（上界 2026-12-31 = 窗口年后）→ 不降级
    - 「白酒行业2026年11月1日政策回顾」（窗口 11-04 起，完整日期 11-01
      早于窗口起始）→ 回顾（声明日参与判定，不降精度为月）
    - 「白酒行业2026年11月5日研讨」（窗口 11-04..11-06）→ 不降级
    标题无声明年份 → 不降级；本判定不看正文（正文旧年份不触发——新政策
    正文引用历史日期不因旧年份而降级）。供应商标题的无效日期分量（13 月/
    2 月 30 日）本地按低一级精度保守处理，不产生错误。歧义时保守降级为
    明确受限背景，不发明当前事件。
    """
    bounds = [
        b for b in (
            _declared_topic_bound(year, month, day)
            for year, month, day in _declared_title_dates(title)
        )
        if b is not None
    ]
    if not bounds:
        return False
    return max(bounds) < window_start


# ---------------------------------------------------------------------------
# URL 归一化 / 公共 HTTP(S) 来源边界（去重键与统一准入共用）
# ---------------------------------------------------------------------------

def urlsplit_host(url: str) -> str:
    """URL → host（stdlib 解析的 hostname；source 为空时保溯源）。

    用 ``urlsplit(...).hostname`` 而非按 ``:`` 切分 netloc——IPv6 literal
    （``[2606:4700:4700::1111]``）按冒号切分会得到 ``[2606`` 这样的损坏
    host，两个不同的公网 IPv6 源会被误判为同源合并。stdlib hostname 取
    brackets 内完整地址、自动去端口并小写化；非法 URL/无 host 返回空串。
    """
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


_PUBLIC_HOSTNAME_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$"
)
"""名义主机名字符契约（小写化后）：字母数字/连字符标签、点分隔、无空标签；
punycode（xn--）属于字母数字形态天然通过。非通用 DNS 全量校验，
只是来源准入的保守结构边界。"""


def validate_public_http_url(url: Any) -> Tuple[bool, str]:
    """公共 HTTP(S) 来源 URL 结构契约（准入与文章出站同一入口）。

    全部通过才允许注册或出站；任何失败返回稳定拒绝类别（调用方本地计数/
    降级，不字符串化伪合法、不截断伪造、不转用户 400 或最终 500）：
    - 字段必须是字符串且非空（供应商字段类型错误本地拒绝，不 str(...)）
    - 长度 ≤ CATALYST_URL_MAX（超长拒绝，不截断后接受）
    - urlsplit 可解析、scheme ∈ {http, https}
    - 无 userinfo（@/username/password）；显式端口必须 1-65535
    - hostname 完整保留为来源身份（DNS 全名上限 253 字符）：超限本地拒绝
      （``source_host_too_long``），**绝不截断后当身份**（两个合法长域名
      前 120 字符相同会被截断伪同域合并）；IPv6 literal（brackets 内地址）
      远小于该上限不受影响
    - literal IP 必须 global 且非 multicast（私网/回环/链路本地/保留/
      未指定全部拒绝）；名义主机拒绝 localhost/.localhost/.local，
      其余须通过主机名字符契约
    """
    import ipaddress
    from urllib.parse import urlsplit

    if not isinstance(url, str):
        return False, "source_not_string"
    if not url.strip():
        return False, "source_empty"
    if len(url) > CATALYST_URL_MAX:
        return False, "source_too_long"
    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "source_unparseable"
    if parts.scheme not in ("http", "https"):
        return False, "source_scheme_not_http"
    netloc = parts.netloc or ""
    if parts.username is not None or parts.password is not None or "@" in netloc:
        return False, "source_userinfo"
    hostname = parts.hostname
    if not hostname:
        return False, "source_no_host"
    if len(hostname) > 253:
        return False, "source_host_too_long"
    try:
        port = parts.port
    except ValueError:
        return False, "source_invalid_port"
    if port is not None and not (1 <= port <= 65535):
        return False, "source_invalid_port"
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        addr = None
    if addr is not None:
        if not addr.is_global or addr.is_multicast:
            return False, "source_non_public_host"
        return True, ""
    name = hostname.lower()
    if (name == "localhost" or name.endswith(".localhost")
            or name.endswith(".local")):
        return False, "source_non_public_host"
    if not _PUBLIC_HOSTNAME_RE.match(name):
        return False, "source_invalid_hostname"
    return True, ""


_TRACKING_PARAM_RE = re.compile(
    r"^(utm_|spm$|from$|fr$|share_|vd_|t_[a-z]|scm|scene|clickid|tt_|"
    r"ref$|refer$|source$|sh_top|zx)"
)


def normalize_catalyst_url(url: Any) -> str:
    """URL 归一化（去重键）：host 小写、剥跟踪参数/fragment、路径尾斜杠统一。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(raw)
    except ValueError:
        return ""
    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING_PARAM_RE.match(k.strip().lower())
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                       urlencode(kept), ""))


# ---------------------------------------------------------------------------
# 文章发布日期证据（真实 newspaper 提取逻辑；输入是有界 HTML，零网络）
# ---------------------------------------------------------------------------

class ArticleParseFailure(Exception):
    """有界 HTML 无法被已装 newspaper parser 解析为文档。

    对应 ``Parser.fromstring`` 的**显式 None 契约**（0.2.8 源码：invalid
    string 时 log warn 并返回 None）——这是预期解析数据错误类别，调用方
    按文章数据错误降级背景。程序/契约错误（TypeError/AssertionError 等）
    不使用此类，原样传播。"""


@dataclass(frozen=True)
class ArticleDateEvidence:
    """单篇文章的发布日期证据（可识别、可保存依据，或明确无证据）。"""

    metadata_date: Optional[date]
    """仅 HTML 出版元数据提取的日期（空 URL 调 newspaper get_publishing_date，
    URL 启发式已禁用）；无法提取为 None。"""
    url_declared_date: Optional[date]
    """URL 自身声明的日期（命中 newspaper STRICT_DATE_REGEX 并解析成功时）；
    仅作冲突判定，不冒充文章声明日期。"""
    raw_basis: Optional[str]
    """可保存的原始依据：与 metadata_date 匹配的实际 meta 标签
    （如 ``<meta property="article:published_time" content="...">``）；
    无法定位时 None（保守拒绝日期证据）。"""

    @property
    def url_only(self) -> bool:
        """URL 含日期且无任何元数据日期：仅 URL 证据，不作文章声明日期。"""
        return self.url_declared_date is not None and self.metadata_date is None


def extract_article_date_evidence(html: str, url: str) -> ArticleDateEvidence:
    """有界 HTML + 实际 URL → 发布日期证据（真实 newspaper parser）。

    - URL 启发式判定与解析用 newspaper 自己的 ``urls.STRICT_DATE_REGEX``
      （与已装版本行为一致，不另造正则）；该日期只参与冲突判定。
    - metadata-only 日期：``ContentExtractor.get_publishing_date("", doc)``
      空 URL 使 STRICT_DATE_REGEX 不命中——已装 0.2.8 该方法仅 URL→
      PUBLISH_DATE_TAGS 元数据两级策略，因此返回值即元数据日期
      （不消费 ``Article.publish_date``：它不区分来源，不是证据）。
    - raw basis：在同一 doc 上定位 content 可解析且等于该日期的实际 meta
      标签并保存其 repr；定位失败 → raw_basis=None（调用方保守拒绝）。
    异常契约：``Parser.fromstring`` 返回 None（newspaper 显式 invalid
    string 契约）→ 抛 :class:`ArticleParseFailure`（预期数据错误）；空
    HTML 返回无证据；其余异常（lxml/dateutil 内部、程序错误）原样传播，
    由调用方按显式类别处理——本函数不吞异常、不做宽泛包装。
    """
    from newspaper import Config, urls
    from newspaper.extractors import ContentExtractor
    from newspaper.parsers import Parser

    trimmed = (html or "").strip()
    url_declared = probe_url_declared_date(url, urls)
    if not trimmed:
        return ArticleDateEvidence(None, url_declared, None)
    extractor = ContentExtractor(Config())
    doc = Parser.fromstring(trimmed)
    if doc is None:
        raise ArticleParseFailure(
            "newspaper parser rejected the bounded html (fromstring None contract)"
        )
    metadata_dt = extractor.get_publishing_date("", doc)
    metadata_date = metadata_dt.date() if metadata_dt is not None else None
    raw_basis: Optional[str] = None
    if metadata_date is not None:
        raw_basis = _locate_publish_meta_basis(doc, extractor, metadata_date)
    return ArticleDateEvidence(metadata_date, url_declared, raw_basis)


def _locate_publish_meta_basis(doc: Any, extractor: Any,
                               metadata_date: date) -> Optional[str]:
    """在 doc 中定位产生 metadata_date 的实际出版元 meta 标签（保存原始依据）。

    扫描覆盖 HTML 出版元数据的标准命名集合（与 newspaper 0.2.8
    PUBLISH_DATE_TAGS 一致的标准 meta 属性名）；content 经同一 dateutil
    解析等于 metadata_date 才是依据；找不到返回 None（保守拒绝）。
    """
    from dateutil import parser as date_parser

    tag_specs = (
        ("property", "rnews:datePublished"), ("property", "article:published_time"),
        ("name", "OriginalPublicationDate"), ("itemprop", "datePublished"),
        ("property", "og:published_time"), ("name", "article_date_original"),
        ("name", "publication_date"), ("name", "sailthru.date"),
        ("name", "PublishDate"), ("pubdate", "pubdate"),
    )
    for attr, value in tag_specs:
        tags = extractor.parser.getElementsByTag(doc, attr=attr, value=value)
        if not tags:
            continue
        content = extractor.parser.getAttribute(tags[0], "content") or ""
        try:
            parsed = date_parser.parse(str(content))
        except (ValueError, OverflowError, AttributeError, TypeError):
            continue
        if parsed.date() == metadata_date:
            return f'<meta {attr}="{value}" content="{str(content)[:80]}">'
    return None


def qualifies_article_declared_date(evidence: ArticleDateEvidence,
                                    window_start: date, window_end: date,
                                    ) -> Tuple[Optional[date], str]:
    """文章日期证据 → (合格声明日期|None, 判定说明)。

    接受条件（全部满足）：元数据日期存在 + raw basis 可保存；URL 声明日期
    与元数据日期同时存在且冲突 → provenance 冲突保守拒绝（一致则两源互证）。
    合格日期须在窗口内（含端点）。
    """
    if evidence.metadata_date is None:
        return None, ("url_only" if evidence.url_only else "no_date_evidence")
    if not evidence.raw_basis:
        return None, "metadata_date_without_raw_basis"
    if (evidence.url_declared_date is not None
            and evidence.url_declared_date != evidence.metadata_date):
        return None, "url_meta_date_conflict"
    if not (window_start <= evidence.metadata_date <= window_end):
        return None, "article_date_offwindow"
    return evidence.metadata_date, "article_metadata"


def probe_url_declared_date(url: str, urls_module: Any = None) -> Optional[date]:
    """URL 命中 newspaper STRICT_DATE_REGEX 时的声明日期（解析失败 None）。

    仅作与元数据日期的冲突判定；URL 日期单独不作任何日期证据。
    """
    try:
        from dateutil import parser as date_parser

        if urls_module is None:
            from newspaper import urls as urls_module
        match = re.search(urls_module.STRICT_DATE_REGEX, url or "")
        if not match:
            return None
        return date_parser.parse(match.group(0)).date()
    except (ValueError, OverflowError, AttributeError, TypeError, ImportError):
        return None


# ---------------------------------------------------------------------------
# 统一事件资格（provider-dated 与 article-dated 共享的唯一一步）
# ---------------------------------------------------------------------------

def qualifies_event(published: date, window_start: date, window_end: date,
                    ) -> Optional[str]:
    """发布日期 → 事件资格（窗口判定）。返回 None=合格；否则拒绝原因。

    板块窗口不含未来日（共享全局搜索过滤的 +1 未来容忍不照搬到 board 口径）；
    窗口外（过去/未来）一律不合格——发布日期只作窗口资格，不发明事件日期。
    """
    if published > window_end:
        return "future"
    if published < window_start:
        return "past"
    return None


# ---------------------------------------------------------------------------
# 证据登记表（实际合格事实 keyed by evidence_id；qualitative 契约的真源）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceEntry:
    """单条证据：ID + 资格类别 + 实际材料文本（含来源/日期等 provenance）。

    数值语义不进模型权威：材料仅供服务端渲染与引用资格判定，模型陈述
    不含明确业务量值（见 ``validate_synthesis_claim`` 有限合同）——不存在
    numeric token 锚定字段（token 集合并集无法表达字段归属/量纲）。"""

    evidence_id: str
    kind: str  # "identity" | "fact" | "catalyst" | "background"
    title: str
    material: str

    @staticmethod
    def build(evidence_id: str, kind: str, title: str,
              payload: Dict[str, Any]) -> "EvidenceEntry":
        material = json.dumps(payload, ensure_ascii=False, default=str)
        return EvidenceEntry(
            evidence_id=evidence_id, kind=kind, title=title,
            material=material,
        )


class EvidenceRegistry:
    """实际合格证据登记表：缺失事实不注册（模型不可引用）；
    引用资格按实际注册 ID 校验，陈述文本不含明确业务量值（有限合同）。"""

    def __init__(self) -> None:
        self._entries: Dict[str, EvidenceEntry] = {}
        self._order: List[str] = []

    def register(self, entry: EvidenceEntry) -> None:
        if entry.evidence_id in self._entries:
            raise ValueError(f"evidence_id 重复注册: {entry.evidence_id}")
        self._entries[entry.evidence_id] = entry
        self._order.append(entry.evidence_id)

    def get(self, evidence_id: str) -> Optional[EvidenceEntry]:
        return self._entries.get(evidence_id)

    def ids(self) -> List[str]:
        return list(self._order)

    def facts(self) -> List[EvidenceEntry]:
        return [self._entries[i] for i in self._order if self._entries[i].kind != "background"]

    def backgrounds(self) -> List[EvidenceEntry]:
        return [self._entries[i] for i in self._order if self._entries[i].kind == "background"]

    def non_market_facts_json(self) -> str:
        """非市场类事实材料 JSON（identity/catalyst：身份/新闻/出处原样）。

        市场类证据（MARKET_FACT_IDS）的 raw magnitudes 不进模型投影，由
        ``market_observation_relations`` 的定性观察关系替代（root-decision：
        原始值保留在公开载荷/存档，模型不再重新解读数值水平/顺序/持续）。"""
        return json.dumps(
            [{"evidence_id": e.evidence_id, "kind": e.kind, "title": e.title,
              "material": json.loads(e.material)}
             for e in self.facts() if e.evidence_id not in MARKET_FACT_IDS],
            ensure_ascii=False, default=str,
        )

    def backgrounds_json(self) -> str:
        return json.dumps(
            [{"evidence_id": e.evidence_id, "kind": e.kind, "title": e.title,
              "material": json.loads(e.material)}
             for e in self.backgrounds()],
            ensure_ascii=False, default=str,
        )


# ---------------------------------------------------------------------------
# 综合输出校验（全量前置；任一违规整条拒绝）
# ---------------------------------------------------------------------------

VALID_CLAIM_KINDS = ("interpretation", "counterevidence", "risk")


def validate_synthesis_claim(raw_claim: Any, registry: EvidenceRegistry,
                             ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """单条 claim 校验 → (合格结论|None, 拒绝原因|None)。

    校验顺序（任何一项违规整条拒绝并给出原因；不清洗、不截断保留）：
    结构（dict/kind/text 原始长度 ≤500）→ 行动词拒绝 → **有限明确量值拒绝**
    （明确量值/倍数陈述整条拒绝——数值与日期事实由服务端从所引证据渲染；
    普通中文含数量形汉字不因字形拒绝，判定范围见
    ``quantitative_statement_reason``）→ 引用资格（refs 是 1..8 个
    非空字符串且 ⊆ registry 实际 ID）→ 背景唯一支撑拒绝（bg-* 只能附加引用）。
    """
    if not isinstance(raw_claim, dict):
        return None, "not_an_object"
    kind = raw_claim.get("kind")
    if kind not in VALID_CLAIM_KINDS:
        return None, "invalid_kind"
    text = raw_claim.get("text")
    if not isinstance(text, str):
        return None, "text_not_string"
    stripped = text.strip()
    if not (1 <= len(stripped) <= CLAIM_TEXT_MAX):
        return None, "text_length_out_of_range"
    if ACTION_INVEST_RE.search(stripped):
        return None, "action_word"
    quant_reason = quantitative_statement_reason(stripped)
    if quant_reason is not None:
        return None, quant_reason
    refs = raw_claim.get("evidence_refs")
    if not isinstance(refs, list) or not refs or len(refs) > CLAIM_REFS_MAX:
        return None, "refs_shape"
    clean_refs: List[str] = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            return None, "refs_shape"
        clean_refs.append(ref.strip())
    known = set(registry.ids())
    unknown = [r for r in clean_refs if r not in known]
    if unknown:
        return None, f"unknown_evidence_ref:{unknown[0]}"
    if all(ref.startswith("bg-") for ref in clean_refs):
        return None, "background_only_support"
    return {"kind": kind, "text": stripped, "evidence_refs": clean_refs}, None


def validate_synthesis_output(parsed: Any, registry: EvidenceRegistry,
                              ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """模型输出 claims 全量校验 → (合格结论, 拒绝原因列表)。

    原始条数超上限返回特殊原因「raw_count_exceeded」（整体拒绝）；
    合格条数超展示上限按超出数记「display_cap_exceeded」。
    """
    rejected: List[str] = []
    if not isinstance(parsed, dict):
        return [], ["output_not_object"]
    raw_claims = parsed.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        return [], ["claims_missing"]
    if len(raw_claims) > SYNTH_CLAIMS_RAW_CAP:
        return [], [f"raw_count_exceeded:{len(raw_claims)}"]
    accepted: List[Dict[str, Any]] = []
    for raw_claim in raw_claims:
        if len(accepted) >= CLAIMS_MAX:
            rejected.append("display_cap_exceeded")
            continue
        verdict, reason = validate_synthesis_claim(raw_claim, registry)
        if verdict is None:
            rejected.append(reason or "unknown")
            continue
        accepted.append(verdict)
    return accepted, rejected


# ---------------------------------------------------------------------------
# 综合输入组装（immutable 事实 + 催化证据 + 明确限制背景）
# ---------------------------------------------------------------------------

SYNTH_SYSTEM_PROMPT = (
    "你是 A 股板块研究助手。只基于用户消息中的【已确认事实】与【市场事实观察】"
    "（含证据 ID；【背景线索】仅按其限制附加）撰写板块定性研判，供报告存档；"
    "市场数值关系的摘要与观察限制由服务端从已采纳事实程序化生成（含精确数值），"
    "从不取自模型输出——你不得输出 summary 或 uncertainty_note 字段。\n"
    "硬性约束（违反任何一条的内容都会被整条拒绝）：\n"
    "1. 写定性研判：方向、结构、因果、相对强弱与不确定性用文字表达；不得新增、"
    "重述、计算或错配具体业务量值——价格、估值、涨跌幅、分位、排名、收益、"
    "财务规模等精确数值及其主体、指标、时点、单位由服务端从证据渲染"
    "（明确量值或倍数表述，如 X元/百分之X/X倍/翻倍，会被整条拒绝）；"
    "正常中文表述（如一致、统一、一方面、二手设备、半年报等）照常使用，"
    "不需要回避数量形汉字。目标相关性以【已确认事实】中的 identity 证据"
    "为界：identity 只确立目标股的行业归属，不提供企业地域、品牌力、合规"
    "能力、业务结构、市场地位等公司属性——此类属性不得以常识、行业惯例或"
    "名称联想补进陈述，龙头企业/分类断言不用于目标描述；仅当所引材料明确"
    "提供了可确认属于目标主体的事实，才可依材料原意陈述（以实际 refs 指向"
    "来源）。行业层面的作用以条件式关联表达（如「若目标股业务涉及该催化剂"
    "领域，则可能受益/承压」），不直接指派给目标股。陈述文本一律用中性指称（如「该板块」「目标股」"
    "「同业」），含数字字符的行业名称与级位后缀（如 Ⅱ/Ⅲ 类后缀）同样用中性"
    "指称指代——不要重印行业分类后缀、板块或股票代码、纯数字名称、带日期或"
    "编号的标题词以及季度/年份 label，这些身份与出处事实保留在输入材料、"
    "服务端表格与引用标题中，指向关系用 evidence_refs 表达。输出 JSON 前"
    "逐条自检每个 text 字段：发现具体量值或量值倍数表述，改写成有证据支持"
    "的等价定性表述；无等价支持的句子直接省略，不发明替代说法，也不为显得"
    "完整而删除真实的风险或反面证据。\n"
    "2. 不得虚构行业身份、行情走向、同业名单或新闻事件；不得出现买入/卖出/加仓/"
    "减仓/清仓/建仓/止盈/止损/目标价等操作指令或价位；不产出评分或分档。\n"
    "3. 每条 claims 的 evidence_refs 必须引用用户消息给出的证据 ID，不得引用集外 ID；"
    "证据引用一律走 evidence_refs 字段，不要把证据 ID 写进陈述文本；"
    "每条至少引用一个非背景证据（cat-*、identity、intraday、trend、peers、target）；"
    "bg-*（无日期或降级背景）只能作为附加引用，不得单独支撑结论。\n"
    '4. 只输出严格 JSON，结构：{"claims": [{"kind": '
    '"interpretation|counterevidence|risk", "text": 字符串(≤500字), '
    '"evidence_refs": ["证据ID"]≤8个}]≤6条}；claims 至少 1 条；'
    "当前任务：输出**一条**整合段落结论（目标篇幅 200–300 字，硬上限 500 字"
    "不变），只围绕**最主要的一组催化剂**（多项催化指向同一主题时合并为一句"
    "陈述，不逐条复述、不为凑满篇幅铺开全部条目）：①该催化的行业层面含义"
    "（以所引材料实际陈述为界）；②对目标股的条件式相关性与主要不确定性；"
    "③所引材料中与最终判断相关的反面证据或条件风险——材料性冲突不得省略，"
    "无论其属于哪个主题；取舍只作用于详略与无关泛化背景，不为缩短篇幅删除"
    "冲突（市场事实概览与观察限制由服务端事实摘要承载，不要复述市场观察、"
    "表格数值、报告免责声明或通用政策背景）。kind 按段落主导内容从现有"
    "枚举选择；材料性反面证据或已采纳来源之间的冲突必须在同一段落内以"
    "实际 refs 保留；无证据支持的反面陈述不要编造来填充。\n"
    "5. 反面证据与不确定性真实存在时，如实纳入上述整合段落"
    "（counterevidence 或 risk 类按主导内容），不粉饰、不省略材料性冲突；"
    "证据不足以支持的判断不要写。\n"
    "6. 市场事实已由服务端以定性观察关系提供（原始数值不在你的输入中）："
    "你的陈述不得对数值水平、顺序或持续时间做重新解读（如偏高/偏低/居前/"
    "靠后/仍/持续/转强类断言），不得发明龙头/资金流向/投资者偏好等分类或"
    "因果；claims 限定为已引用催化剂（cat-*/bg-*）的含义、目标相关性与"
    "条件风险——依据观察关系与新闻材料陈述可能影响与不确定性，保留真实"
    "风险与反面证据。观察关系只作为背景条件与不确定性来源，不作为因果、"
    "归因或市场效率判断的证据——不得从涨跌方向、同业分歧等单期观察推断"
    "催化剂的定价程度、市场认可、资金态度或偏好迁移；claims 不复述观察"
    "关系本身，此类推断一律不写，不粉饰、不补造。"
)


def build_synthesis_user_text(registry: EvidenceRegistry) -> str:
    """登记表 → 模型 user 消息（root-decision 所有权边界）。

    - 非市场事实（identity/catalyst）原样投影（title/snippet/date/
      provenance/身份不变，immutable）；背景单列。
    - 市场类证据（intraday/trend/peers/target）**不投影 raw material**：
      由 ``market_observation_relations`` 以服务端确定性定性观察关系呈现
      （符号/已存相对关系/样本选取含义/单期范围），原始 magnitudes 留在
      公开载荷/存档与服务端表格——模型不再承担数值水平/顺序/持续的重新
      解读。
    - 引用集 = registry 实际注册 ID：缺失的块根本不出现在材料里，模型无法
      引用它们补造。模型陈述不新增/重述具体业务量值（系统约束第 1 条）。
    """
    lines = [
        "【已确认事实】（immutable，直接采用，不得改写数值/日期/名单；"
        "未列出的证据 ID 不存在、不可引用也不得补造；你的陈述文本不新增/"
        "重述具体业务量值）",
        registry.non_market_facts_json(),
        "",
    ]
    market_obs = market_observation_relations(registry)
    if market_obs:
        lines += [
            "【市场事实观察】（服务端从已采纳数据推导的确定性定性关系；"
            "原始数值不在本消息中，报告的服务端表格承载精确值；你的陈述"
            "不得对这些关系做进一步方向/序数/持续性解读，也不得发明龙头/"
            "资金流分类）",
            json.dumps(market_obs, ensure_ascii=False),
            "",
        ]
    backgrounds = registry.backgrounds()
    if backgrounds:
        lines += [
            "【背景线索】（bg-*：无可靠发布日期或历史回顾降级，仅背景参考；"
            "不得作为已证实事实，不得单独支撑结论；limitation 字段为具体限制）",
            registry.backgrounds_json(),
            "",
        ]
    lines += [
        "请输出严格 JSON 定性研判（结构见系统约束）：一条简洁的整合段落结论，"
        "覆盖主要催化剂含义、对目标股的相关性与材料性反面证据/条件风险；"
        "市场概览与观察限制由服务端事实摘要承载，不要复述。研判对象以上方"
        "【已确认事实】中的 identity 证据为准：identity 只确立行业归属，"
        "目标相关性用条件式表达；所引材料明确提供的目标事实可依材料陈述；"
        "陈述围绕最主要的催化剂主题，目标篇幅见系统约束；叙述用中性指称"
        "（见系统约束），不要把行业分类后缀、板块/股票代码或数字名称写进"
        "陈述文本。",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 服务端拥有的市场事实观察与程序化摘要（root-decision 所有权边界）
# ---------------------------------------------------------------------------

MARKET_FACT_IDS = ("intraday", "trend", "peers", "target")
"""市场类证据 ID：raw material（分位/排名/价格/收益 magnitudes）不进入模型
投影，仅以服务端定性观察关系呈现；原始值完整保留在公开载荷与存档。"""


def _sign_word(value: Any) -> Optional[str]:
    """已采纳数值的符号关系词（精确零比较；无阈值/无算术/无形容词）。"""
    if value is None:
        return None
    if value > 0:
        return "上涨"
    if value < 0:
        return "下跌"
    return "持平"


def market_observation_relations(registry: EvidenceRegistry) -> List[Dict[str, str]]:
    """市场类证据 → 服务端拥有的确定性定性观察关系（模型投影）。

    每个关系单一 owner（现有装配缝）：只呈现已采纳值的符号关系、已存的
    板块对基准相对关系、样本选取含义与单期范围——不投影 raw percentile/
    rank/价格/收益 magnitudes 供模型重新解读；原始值逐字保留在公开载荷/
    存档与报告服务端表格。缺失块省略（不填充）。
    """
    observations: List[Dict[str, str]] = []
    intraday = registry.get("intraday")
    if intraday:
        m = json.loads(intraday.material)
        parts = []
        sign = _sign_word(m.get("change_pct"))
        if sign:
            parts.append(f"板块指数当日{sign}")
        else:
            parts.append("板块指数当日涨跌方向未取得")
        parts.append("当日行情为单期快照，分位/占比类指标仅表示横截面相对"
                     "位置，不提供方向或历史动量证据")
        # 口径元数据随观察关系进模型 wire（root 反例：材料带来源/asof 但投影
        # 丢失）；纯口径描述，不投影 raw magnitudes（数值仍只在公开载荷）
        if m.get("snapshot_source"):
            parts.append(f"快照口径：{m['snapshot_source']}")
        observations.append({
            "evidence_id": "intraday",
            "observation": "；".join(parts) + "。",
        })
    trend = registry.get("trend")
    if trend:
        m = json.loads(trend.material)
        parts = []
        win = m.get("window") or []
        if win and win[0] and win[1]:
            parts.append(f"已完成窗口（{win[0]}→{win[1]}）内")
        sec = m.get("period_return_pct")
        base = m.get("benchmark_period_return_pct")
        if sec is not None and base is not None:
            # 直接比较已存储的精确值（现有精度纪律；不做 float 减法派生）
            if sec > base:
                parts.append("板块区间收益高于基准（已存相对关系）")
            elif sec < base:
                parts.append("板块区间收益低于基准（已存相对关系）")
            else:
                parts.append("板块区间收益与基准持平（已存相对关系）")
        parts.append("窗口收益为已完成交易日区间关系，不构成趋势延续证据")
        observations.append({
            "evidence_id": "trend",
            "observation": "；".join(parts) + "。",
        })
    peers = registry.get("peers")
    if peers:
        m = json.loads(peers.material)
        rule = m.get("selection_rule")
        parts = []
        if rule:
            parts.append(f"同业样本选取口径：{rule}")
        items = m.get("items") or []
        # P1 证据缺口纪律：任何成员缺有效涨跌观测（None ≠ 0，零是有效
        # 「持平」观测）时不得得出全样本方向结论——公开载荷如实保留该成员
        # 与比较范围缺口，模型投影同样只披露不完整观测，不推断整体方向。
        observed = [_sign_word(i.get("change_pct")) for i in items
                    if i.get("change_pct") is not None]
        missing = len(items) - len(observed)
        if missing:
            parts.append(f"样本内 {missing} 家当日涨跌观测缺失，"
                         "无法得出全样本方向结论")
        elif observed:
            unique = set(observed)
            if len(unique) == 1:
                parts.append(f"样本内当日涨跌方向一致（{observed[0]}）")
            else:
                parts.append("样本内当日涨跌方向存在差异")
        # 口径元数据随观察关系进模型 wire（root final 反例：只写「见公开载荷」
        # 不等于把实际来源/时点传给模型——模型没有该载荷）。保留实际供应商
        # 来源、成交额口径、衍生标记、供应商时点与时间窗、子集范围文本；
        # 纯口径描述，raw magnitudes（价格/涨幅/金额/分位/收益）仍只在公开
        # 载荷与服务端表格。
        if m.get("source"):
            parts.append(f"成员集合来源：{m['source']}")
        if m.get("amount_provenance"):
            parts.append(f"成交额口径：{m['amount_provenance']}")
        if m.get("amount_derived"):
            parts.append("部分或全部成员成交额为供应商分钟累计衍生口径"
                         "（非原样快照字段）")
        if m.get("quotes_as_of"):
            parts.append(f"行情为逐股实际采集（{m['quotes_as_of']}），"
                         "非同一瞬间全表快照")
        observations.append({
            "evidence_id": "peers",
            "observation": "；".join(parts) + "。",
        })
    target = registry.get("target")
    if target:
        m = json.loads(target.material)
        sign = _sign_word(m.get("snapshot_change_pct"))
        name = m.get("stock_name") or "目标股"
        if sign:
            observations.append({
                "evidence_id": "target",
                "observation": f"目标股{name}当日{sign}（单期快照）。",
            })
    return observations


def program_fact_summary(registry: EvidenceRegistry) -> str:
    """服务端程序化事实摘要：仅从已采纳事实与已计算关系生成。

    渲染精确带符号百分比/排名/样本/窗口与单期限制；不消费模型 claims 文本
    （root-decision：模型错误句不可能进入 summary）。缺失块省略。"""
    parts: List[str] = []
    intraday = registry.get("intraday")
    if intraday:
        m = json.loads(intraday.material)
        chg = m.get("change_pct")
        seg = []
        if chg is not None:
            seg.append(f"板块指数当日涨跌幅 {chg}%（{_sign_word(chg)}）")
        rank_of, total = m.get("rank_of"), m.get("total_boards")
        if rank_of is not None and total is not None:
            seg.append(f"全表涨跌幅排名 {rank_of}/{total}")
        breadth = m.get("breadth")
        if breadth is not None:
            seg.append(f"当日上涨家数占比 {breadth}%")
        momentum = m.get("momentum")
        if momentum is not None:
            seg.append(f"涨跌幅分位 {momentum}/100（横截面相对位置，"
                       "不提供方向或历史动量证据）")
        # 快照来源/时点贯穿事实摘要（root 纪律：来源与 as_of 不止 logger；
        # 全表路径 snapshot_source 为 None 不追加）
        if m.get("snapshot_source"):
            seg.append(f"快照口径：{m['snapshot_source']}")
        if seg:
            parts.append("；".join(seg))
    trend = registry.get("trend")
    if trend:
        m = json.loads(trend.material)
        win = m.get("window") or []
        seg = []
        if win and win[0] and win[1]:
            sessions = m.get("qualified_sessions")
            seg.append(f"窗口 {win[0]}→{win[1]}"
                       + (f"（{sessions} 个已完成交易日）" if sessions else ""))
        sec, base = m.get("period_return_pct"), m.get("benchmark_period_return_pct")
        if sec is not None:
            seg.append(f"板块区间收益 {sec}%")
        if base is not None:
            seg.append(f"基准 {base}%")
        rel = m.get("relative_return_pp")
        if rel is not None:
            seg.append(f"相对差 {rel} 个百分点")
        if seg:
            parts.append("；".join(seg))
    peers = registry.get("peers")
    if peers:
        m = json.loads(peers.material)
        rule = m.get("selection_rule")
        items = m.get("items") or []
        seg = []
        if rule:
            seg.append(f"同业样本（{rule}）")
        if items:
            seg.append(f"样本 {len(items)} 家（快照当日口径）")
        # 成交额来源口径与供应商时点贯穿事实摘要（衍生值不冒充原样字段）
        if m.get("quotes_as_of"):
            seg.append(f"行情时点：{m['quotes_as_of']}")
        if seg:
            parts.append("；".join(seg))
    target = registry.get("target")
    if target:
        m = json.loads(target.material)
        chg = m.get("snapshot_change_pct")
        name = m.get("stock_name") or "目标股"
        if chg is not None:
            parts.append(f"目标股{name}当日涨跌幅 {chg}%（{_sign_word(chg)}）")
    if not parts:
        return "服务端事实摘要：本次无已采纳的市场事实块（缺口见下方披露）。"
    return "。".join(parts) + "。"


def program_fact_limitations(registry: EvidenceRegistry) -> Optional[str]:
    """服务端程序化观察限制：实际证据边界与期间/样本限制；不消费模型文本。

    缺失块省略（缺口由编排层如实披露）；全部块缺失时 None。"""
    parts: List[str] = []
    if registry.get("intraday"):
        parts.append("当日行情为单期快照，不外推历史趋势；涨跌幅分位仅为"
                     "横截面相对位置，不提供方向或历史动量证据")
        parts.append("行情与换手表现不构成资金流入流出或投资者偏好证据")
        m = json.loads(registry.get("intraday").material)  # type: ignore[union-attr]
        if m.get("snapshot_derived"):
            parts.append("当日板块快照为供应商分钟累计衍生口径"
                         "（trends2 求和，非原样快照字段；时点见载荷）")
    if registry.get("peers"):
        parts.append("同业样本按成交额活跃代表选取，不代表市值或基本面"
                     "龙头分类")
        m = json.loads(registry.get("peers").material)  # type: ignore[union-attr]
        if m.get("amount_derived"):
            parts.append("部分/全部成员成交额为供应商分钟累计衍生值"
                         "（trends2 求和），非原样快照字段")
        if m.get("quotes_as_of"):
            parts.append("同业行情为逐股实际采集（供应商时点/时间窗见载荷），"
                         "非同一瞬间全表快照")
    if registry.get("trend"):
        parts.append("窗口收益为已完成交易日区间关系，不构成趋势延续证据")
    return "；".join(parts)[:500] if parts else None


# ---------------------------------------------------------------------------
# 模型输出 JSON 解析（strip 代码围栏；失败 None，不启发式拼接）
# ---------------------------------------------------------------------------

def parse_synthesis_json(content: str) -> Optional[Dict[str, Any]]:
    """模型输出 → JSON dict；失败 None。"""
    raw = (content or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# 催化查询词 / 日期兼容辅助
# ---------------------------------------------------------------------------

_SW_LEVEL_SUFFIX_RE = re.compile(r"[ⅠⅡⅢⅣⅤ]+$")


def catalyst_query_term(board_name: str) -> str:
    """行业名 → 查询基词（级位后缀归一：白酒Ⅱ → 白酒）。"""
    base = _SW_LEVEL_SUFFIX_RE.sub("", (board_name or "").strip())
    return base or (board_name or "").strip()


def iso_date_or_none(value: Any) -> Optional[date]:
    """ISO 日期字符串 → date（失败 None；不做宽松解析）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
