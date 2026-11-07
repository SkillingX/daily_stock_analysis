# -*- coding: utf-8 -*-
"""证据登记表与综合输出校验（issue #83 单元契约，纯函数离线）。

有限明确量值契约（issue #85；root 数值反例 + 三轮机制反例的收敛形态）：
- target 1.0%、板块 1.9%：claim 引 target 说「1.9%」→ 拒绝（明确量值陈述）
- claim「1.0%」引 target（数值与所引材料完全一致）→ 仍拒绝：重述数值本身
  就是明确量值陈述，数字事实由服务端渲染（不存在跨字段/跨主体借用通道）
- 字面数字层（Unicode 类别 Nd/Nl/No + 百分号）：「0%」/「-3.2%」/全角「１.５」
  /「١٢倍」/「Ⅱ级」/「x²」/「½」/证据 ID 进文本 → 整条拒绝，无名称豁免
- 结构层（封闭量值结构）：「百分之五」「约三成」「第三个月」「二〇二六年」
  「三季度」「翻了一番」「倆元」「市盈率为十/两百」→ 整条拒绝
- 双视图（原码点 + NFKC）：兼容/康熙码点（⼆/㋀）折叠出字面数字或量值结构
  同样拒绝；输出原文不改写
- 普通中文含数量形汉字不因字形拒绝：一致/统一/一方面/三一重工/二手设备/
  半导体/半年报/参考/陆续/一般/一度/二级市场 照常通过（v1 逐字禁令已废弃）
- 自然定性 complete 控制（无数字的证据关联解读 + 反证/风险）照常通过
- 引用资格：未知 ID / 缺失事实引用（无 trend 时引用 trend）/ bg-* 唯一支撑
- 行动词 / 超长文本 / 畸形结构 / claims 原始条数超限 → 整体拒绝
"""

from __future__ import annotations

from datetime import date

import pytest

from src.services.sector_research import (
    EvidenceEntry,
    EvidenceRegistry,
    SYNTH_CLAIMS_RAW_CAP,
    is_retrospective_title,
    market_observation_relations,
    normalize_catalyst_url,
    program_fact_limitations,
    program_fact_summary,
    qualifies_event,
    urlsplit_host,
    validate_public_http_url,
    validate_synthesis_claim,
    validate_synthesis_output,
)


@pytest.fixture()
def registry() -> EvidenceRegistry:
    """固定登记表：target 1.0、trend 板块 1.9/基准 0.6、cat-1 政策、bg-1 降级背景。"""
    reg = EvidenceRegistry()
    reg.register(EvidenceEntry.build(
        "identity", "identity", "主行业身份",
        {"industry_em": "白酒Ⅱ", "board_code": "BK1277", "member_count": 20},
    ))
    reg.register(EvidenceEntry.build(
        "target", "fact", "目标股快照",
        {"stock_name": "贵州茅台", "snapshot_change_pct": 1.0},
    ))
    reg.register(EvidenceEntry.build(
        "trend", "fact", "板块趋势",
        {"period_return_pct": 1.9, "benchmark_period_return_pct": 0.6,
         "window": ["2026-10-09", "2026-11-04"]},
    ))
    reg.register(EvidenceEntry.build(
        "cat-1", "catalyst", "白酒行业新政策发布",
        {"title": "白酒行业新政策发布", "published_date": "2026-11-05",
         "source_domain": "example.com", "snippet": "新规落地"},
    ))
    reg.register(EvidenceEntry.build(
        "peers", "fact", "同业成交活跃代表",
        [{"name": "贵州茅台", "is_target": True, "change_pct": 1.5}],
    ))
    reg.register(EvidenceEntry.build(
        "bg-1", "background", "2020年白酒行情回顾",
        {"title": "2020年白酒行情回顾", "limitation": "标题声明主题时点早于窗口起始"},
    ))
    return reg


class TestQualitativeOnlyClaims:
    """模型陈述不含明确业务量值：明确量值/倍数陈述整条拒绝
    （issue #85 有限合同；root 数值反例 + 机制反例收敛形态）。"""

    def test_wrong_fact_borrowed_from_other_evidence_rejected(self, registry):
        # root 反例：target 1.0、板块 1.9；claim 引 target 说 1.9% → 拒绝
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "目标股区间收益1.9%表现良好",
             "evidence_refs": ["target"]}, registry,
        )
        assert verdict is None
        assert reason == "quantitative_statement:1"  # 稳定类别 + 首个命中字符

    def test_exact_reference_value_still_rejected(self, registry):
        # 收敛关键：数值与所引材料**完全一致**也拒绝——重述数值即明确量值陈述，
        # 数字事实由服务端渲染（不存在「正确锚定」通道，也无借用通道）
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "目标股当日涨跌幅1.0%",
             "evidence_refs": ["target"]}, registry,
        )
        assert verdict is None
        assert reason == "quantitative_statement:1"

    def test_substring_zero_rejected(self, registry):
        # root 反例形态：claim「0%」（"0" 是 "1.0" 的子串与否不再相关）
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "目标股当日涨跌幅0%",
             "evidence_refs": ["target"]}, registry,
        )
        assert verdict is None
        assert reason == "quantitative_statement:0"

    def test_sign_and_unit_formulations_rejected(self, registry):
        # 符号/单位/子串统一为同一字符边界（不依赖后随单位）
        for text in ("板块当日-3.2%", "板块当日上涨 3.2 个百分点",
                     "板块市盈率20倍", "成分数量为20"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["identity"]}, registry,
            )
            assert verdict is None, text
            assert reason.startswith("quantitative_statement:"), (text, reason)

    def test_identity_member_count_no_metric_channel(self, registry):
        # root 反例：member_count=20 曾被借为「市盈率20倍」；定性契约下
        # 数值重述一律拒绝（身份事实更不能佐证 fabricated metrics）
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "板块市盈率处于低位",
             "evidence_refs": ["identity"]}, registry,
        )
        assert verdict is not None, reason  # 纯定性表述（无数字）通过

    def test_fullwidth_digits_rejected(self, registry):
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "目标股涨幅１.５个百分点居前",
             "evidence_refs": ["target"]}, registry,
        )
        assert verdict is None
        assert reason.startswith("quantitative_statement:")

    def test_chinese_numeric_formulations_rejected(self, registry):
        # 结构层边界：占比/百分比/序数时长/日期/季度/倍数/货币结构拒绝
        # （不依赖后随单位；简繁数量字在结构中同样命中）
        for text in ("政策利好约三成", "目标涨幅百分之五",
                     "板块已连续第三个月走强", "新规于二〇二六年落地",
                     "三季度景气上行", "翻了一番",
                     "目标股股价为倆元"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry,
            )
            assert verdict is None, text
            assert reason.startswith("quantitative_statement:"), (text, reason)

    def test_explicit_doubling_words_rejected(self, registry):
        # spec FAIL 反例（P1）：「企业利润将翻倍」是显式利润 ×2 声明——
        # 不含数量字符的词级倍数表述同样整条拒绝（有限已知倍数词合同，
        # 非增长词表/NLP；材料没有利润数字时不得编造倍数比率）
        for text in ("企业利润将翻倍", "行业利润翻番",
                     "政策落地后板块利润将翻倍", "需求翻番式增长"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry,
            )
            assert verdict is None, text
            assert reason.startswith("quantitative_statement:"), (text, reason)

    def test_natural_qualitative_wording_without_multiple_words_passes(self, registry):
        # 自然定性控制：改用同义定性说法（相同、趋同、部分）的措辞不含
        # 数量字符也不含倍数词，照常通过——模型保留有用分析
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation",
             "text": "政策催化落地，目标股与同业代表的相对位置靠前，"
                     "板块与基准的区间表现差距明显",
             "evidence_refs": ["cat-1"]}, registry,
        )
        assert verdict is not None, reason

    def test_unit_free_chinese_values_rejected(self, registry):
        # root 边界反例（unit-free）：『目标股股价为一百元』『市盈率为十/两百』
        # 无单位终点同样整条拒绝（量纲词+系词桥+完结数字段）；时长量纲保持
        for text in ("目标股股价为一百元", "目标股市盈率为十", "目标股市盈率为两百",
                     "政策持续了几个月"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["target"]}, registry,
            )
            assert verdict is None, text
            assert reason.startswith("quantitative_statement:"), (text, reason)

    def test_non_ascii_numeric_scripts_rejected(self, registry):
        # root 边界反例：阿拉伯-印度数字『١٢倍』曾放行；Unicode 数字码点
        # （含全角/上标/罗马数字）逐字符拒绝
        for text in ("目标涨幅١٢倍", "板块涨了１０个百分点", "评级Ⅱ级表现",
                     "板块估值x²抬升", "市占率½以上"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry,
            )
            assert verdict is None, text
            assert reason.startswith("quantitative_statement:"), (text, reason)

    def test_ordinary_phrases_with_numeral_chars_pass(self, registry):
        # issue #85 获准语义变化：普通词不因数量形汉字字形拒绝（原逐字禁令
        # 的已知保守代价集全部翻转放行；结构层不命中即放行，无逐词豁免）
        for text in ("行业观点高度一致", "产业链上下游统一涨价预期",
                     "一方面供给收缩，另一方面需求平稳", "三一重工带动板块",
                     "板块已连续走强一段时间", "二手设备出口景气提升",
                     "半导体材料国产化加快", "另一家同业公告扩产",
                     "一体化布局推进协同显现", "政策在两方面形成支撑",
                     "资金在产业链两端均有布局", "半年报披露后反应积极",
                     "行业格局并非一成不变", "板块前景十分明朗",
                     "需求呈现多元化特征", "二级市场流动性改善",
                     "市场参考政策动向", "企業陸續披露訂單",
                     "目标股与同业俩龙头共振", "兩大龙头共振带动板块",
                     "板块裡僅幾家受惠", "业务数一数二的同业公告扩产"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry,
            )
            assert verdict is not None, (text, reason)

    def test_natural_qualitative_control_with_plain_synonyms_passes(self, registry):
        # 自然定性控制（无任何数量字符，含普通同义替换）：interpretation +
        # counterevidence + risk 全部通过——字符边界不误伤自然定性表达
        accepted, rejected = validate_synthesis_output({"claims": [
            {"kind": "interpretation",
             "text": "政策催化落地，目标股与同业代表的相对位置靠前，"
                     "板块与基准的区间表现差距明显",
             "evidence_refs": ["cat-1", "peers", "trend"]},
            {"kind": "counterevidence",
             "text": "政策落地节奏与执行力度仍待观察，板块相对优势未必延续",
             "evidence_refs": ["cat-1", "trend"]},
            {"kind": "risk",
             "text": "行情活跃度集中在少数成交代表股，板块内部分化风险需要跟踪",
             "evidence_refs": ["peers"]},
        ]}, registry)
        assert len(accepted) == 3, rejected
        assert rejected == []

    def test_evidence_id_not_repeatable_in_prose(self, registry):
        # 证据 ID 是独立 refs 字段：文本重述 ID（含编号数字）同样拒绝
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "如cat-1所示政策落地",
             "evidence_refs": ["cat-1"]}, registry,
        )
        assert verdict is None
        assert reason == "quantitative_statement:1"

    def test_natural_qualitative_complete_control_passes(self, registry):
        # 自然定性 complete 控制：无数字的证据关联解读 + 反证 + 风险全部通过
        accepted, rejected = validate_synthesis_output({"claims": [
            {"kind": "interpretation",
             "text": "新政策落地构成行业催化，目标股在本轮同业成交活跃样本中"
                     "涨幅居前，板块区间表现领先基准",
             "evidence_refs": ["cat-1", "peers", "trend"]},
            {"kind": "counterevidence",
             "text": "政策落地节奏与执行力度仍待观察，板块相对优势未必延续",
             "evidence_refs": ["cat-1", "trend"]},
            {"kind": "risk",
             "text": "行情活跃度集中在少数成交代表股，板块内部分化风险需要跟踪",
             "evidence_refs": ["peers"]},
        ]}, registry)
        assert len(accepted) == 3, rejected
        assert rejected == []
        for claim in accepted:
            assert claim["text"]
            assert claim["evidence_refs"]


class TestExplicitQuantityMechanism:
    """issue #85 有限明确量值机制（原型三轮收敛：root 探针 v1/v2 + 决策记录）。

    - 类别制字面层：Lo 数量形汉字归结构层（参/參/陸/陆带 Numeric 属性）
    - 完结边界：量纲词后数字段必须完结（一般/一度/十大 放行）
    - 有限分隔：空格/冒号分隔与紧邻同判；不跨句
    - 双视图：兼容/康熙码点 NFKC 折叠命中（输出原文不改写）
    - Roman 无例外：名称含字面数字（白酒Ⅱ）仍拒，身份走服务端字段+中性指称
    """

    def test_lo_numeric_property_chars_not_literal(self, registry):
        # 参/參/陸/陆 category=Lo 且 isnumeric=True：旧 isnumeric 视图逐字
        # 拒绝是机制错误；类别制下归结构层，普通词放行
        for text in ("市场参考政策动向", "市場參考政策動向", "企業陸續披露訂單",
                     "陆续披露订单的公司增加", "板块参与度提升"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry)
            assert verdict is not None, (text, reason)

    def test_metric_completion_boundary(self, registry):
        # 普通词内部数字形（数字段未完结）放行；完结数字段拒绝
        for text in ("利润一般，但订单回暖", "股价一度承压，另一催化仍待落地",
                     "估值一般，相关政策提供支撑", "利润一般化趋势明显",
                     "利润十大重仓股居前"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry)
            assert verdict is not None, (text, reason)
        for text in ("利润为一", "利润为一亿五", "利润一亿以上",
                     "利润高于五成", "营收过十亿"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry)
            assert verdict is None, (text, reason)
            assert reason and reason.startswith("quantitative_statement:"), (text, reason)

    def test_metric_separator_variants_same_verdict(self, registry):
        # 有限分隔语法：空格/冒号/繁体系词与紧邻形态同判（不跨句）
        for text in ("市盈率为十", "市盈率为 十", "市盈率为：十",
                     "市盈率：十", "市盈率為十", "市盈率达到 十",
                     "市盈率達兩百", "估值為 十亿元"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["target"]}, registry)
            assert verdict is None, (text, reason)
            assert reason and reason.startswith("quantitative_statement:"), (text, reason)
        # 句读不跨句：量纲词后接句号，句号后的数字形汉字（三方面）不因
        # 跨句桥接命中 metric_value（「三方面」本身亦无量值结构）
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "估值回落。三方面支撑明显",
             "evidence_refs": ["cat-1"]}, registry)
        assert verdict is not None, reason

    def test_nfkc_folded_literal_and_structure(self, registry):
        # 兼容/康熙部首/电报月名码点：折叠视图命中，原因记 nfkc:，原文不改写
        for text, marker in (("市盈率为⼆", "nfkc:"), ("利润为㋀", "nfkc:"),
                             ("市盈率为㍘", "nfkc:"), ("估值为㋁", "nfkc:")):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry)
            assert verdict is None, (text, reason)
            assert reason and reason.startswith(
                "quantitative_statement:" + marker), (text, reason)

    def test_roman_literals_no_name_exception(self, registry):
        # Roman=Nl 字面数字无任何名称/上下文豁免（root v2 反例：与金融数值
        # 同构的 Ⅱ/Ⅹ/Ⅻ 必须拒）；身份由服务端 identity 字段+中性指称承担
        for text in ("市盈率Ⅱ", "市盈率为Ⅹ", "股价Ⅱ元", "目标股市盈率Ⅻ倍",
                     "市盈率Ⅴ倍", "第Ⅱ名", "评级Ⅱ级表现",
                     "白酒Ⅱ", "白酒Ⅱ板块的另一催化仍待落地",
                     "白酒Ⅲ子行业的另一催化", "行业Ⅴ类后缀显性化"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry)
            assert verdict is None, (text, reason)
            assert reason and reason.startswith("quantitative_statement:"), (text, reason)

    def test_names_without_literal_digits_pass(self, registry):
        # 名称无字面数字时照常放行（对照）；汉字数量形名称不误拒
        for text in ("白酒板块情绪一致", "工程机械出口景气", "三一重工带动板块"):
            verdict, reason = validate_synthesis_claim(
                {"kind": "interpretation", "text": text,
                 "evidence_refs": ["cat-1"]}, registry)
            assert verdict is not None, (text, reason)


class TestReferenceQualification:
    """引用资格：未知 ID / 缺失事实 / 背景唯一支撑 / 数量上限。"""

    def test_unknown_evidence_ref_rejected(self, registry):
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "趋势向好", "evidence_refs": ["cat-9"]},
            registry,
        )
        assert verdict is None
        assert reason == "unknown_evidence_ref:cat-9"

    def test_missing_fact_not_referenceable(self):
        # 无 trend 取得时 registry 无 "trend" ID → 引用即未知
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build(
            "identity", "identity", "身份", {"board_name": "白酒Ⅱ"},
        ))
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "板块趋势走强",
             "evidence_refs": ["trend"]}, reg,
        )
        assert verdict is None
        assert reason == "unknown_evidence_ref:trend"

    def test_background_only_support_rejected(self, registry):
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "历史行情重演",
             "evidence_refs": ["bg-1"]}, registry,
        )
        assert verdict is None
        assert reason == "background_only_support"

    def test_background_as_additional_ref_allowed(self, registry):
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "政策催化落地，与历史政策周期背景相呼应",
             "evidence_refs": ["cat-1", "bg-1"]}, registry,
        )
        assert verdict is not None, reason


class TestClaimStructure:
    """结构/行动词/超限全量前置校验。"""

    def test_action_word_rejected(self, registry):
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "建议买入白酒板块",
             "evidence_refs": ["cat-1"]}, registry,
        )
        assert verdict is None
        assert reason == "action_word"

    def test_target_price_rejected(self, registry):
        verdict, reason = validate_synthesis_claim(
            {"kind": "risk", "text": "若跌破支撑可止损离场",
             "evidence_refs": ["cat-1"]}, registry,
        )
        assert verdict is None
        assert reason == "action_word"

    def test_overlong_text_rejected_whole(self, registry):
        verdict, _ = validate_synthesis_claim(
            {"kind": "interpretation", "text": "x" * 501,
             "evidence_refs": ["cat-1"]}, registry,
        )
        assert verdict is None

    def test_ref_count_cap(self, registry):
        refs = ["cat-1", "bg-1", "identity", "target", "trend"]
        # 9 个引用（含重复合法 ID）超过 ≤8 上限 → 结构拒绝（第 9 个即拒）
        verdict, reason = validate_synthesis_claim(
            {"kind": "interpretation", "text": "多证据",
             "evidence_refs": refs + refs[:4]}, registry,
        )
        assert verdict is None
        assert reason == "refs_shape"

    def test_malformed_shapes_rejected(self, registry):
        assert validate_synthesis_claim("not dict", registry)[0] is None
        assert validate_synthesis_claim({"kind": "other", "text": "x",
                                         "evidence_refs": ["cat-1"]}, registry)[0] is None
        assert validate_synthesis_claim({"kind": "interpretation", "text": 123,
                                         "evidence_refs": ["cat-1"]}, registry)[0] is None
        assert validate_synthesis_claim({"kind": "interpretation", "text": "x",
                                         "evidence_refs": "cat-1"}, registry)[0] is None
        assert validate_synthesis_claim({"kind": "interpretation", "text": "x",
                                         "evidence_refs": []}, registry)[0] is None


class TestOutputValidation:
    """整体输出：claims 原始条数超限整体拒绝；合格条数超展示上限拒绝保留前 6。"""

    def test_raw_count_exceeded_rejects_whole_output(self, registry):
        claims = [{"kind": "interpretation", "text": "定性结论",
                   "evidence_refs": ["cat-1"]}
                  for _ in range(SYNTH_CLAIMS_RAW_CAP + 1)]
        accepted, rejected = validate_synthesis_output({"claims": claims}, registry)
        assert accepted == []
        assert rejected and rejected[0].startswith("raw_count_exceeded:")

    def test_display_cap_keeps_first_six(self, registry):
        ordinals = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
        claims = [{"kind": "interpretation", "text": f"政策催化结论{ordinal}",
                   "evidence_refs": ["cat-1"]}
                  for ordinal in ordinals]
        accepted, rejected = validate_synthesis_output({"claims": claims}, registry)
        assert len(accepted) == 6
        assert rejected.count("display_cap_exceeded") == 2

    def test_missing_claims_rejected(self, registry):
        accepted, rejected = validate_synthesis_output({"other": 1}, registry)
        assert accepted == []
        assert rejected == ["claims_missing"]

    def test_quantitative_claim_rejected_valid_neighbor_kept(self, registry):
        claims = [
            {"kind": "interpretation", "text": "政策催化落地，板块动能居前",
             "evidence_refs": ["cat-1", "trend"]},
            {"kind": "interpretation", "text": "板块区间收益5.0%远超基准",
             "evidence_refs": ["trend"]},  # 明确量值陈述 → 整条拒绝
        ]
        accepted, rejected = validate_synthesis_output({"claims": claims}, registry)
        assert len(accepted) == 1
        assert rejected and rejected[0].startswith("quantitative_statement:")

    def test_program_summary_and_limitations_server_owned(self):
        """root-decision：summary/limitations 程序化生成，不消费模型 claims。

        1429/1449 真实反例形状：分位/排名/占比/窗口收益以精确值呈现，
        无形容词序数；模型 claims 文本不可能进入两个核心字段。"""
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build("intraday", "fact",
                    "当日板块行情（当前快照，单期观测）",
                    {"change_pct": -2.15, "rank_of": 364, "total_boards": 496,
                     "momentum": 26.3, "breadth": 10.0}))
        reg.register(EvidenceEntry.build("trend", "fact", "板块趋势",
                    {"window": ["2026-10-09", "2026-11-05"],
                     "qualified_sessions": 20, "period_return_pct": 11.2933,
                     "benchmark_period_return_pct": -4.7215,
                     "relative_return_pp": 16.0148}))
        reg.register(EvidenceEntry.build("peers", "fact", "同业成交活跃代表",
                    {"selection_rule": "同一成分快照按成交额前五 + 目标股去重",
                     "items": [{"name": "甲", "is_target": True,
                                "change_pct": 1.0}]}))
        reg.register(EvidenceEntry.build("target", "fact", "目标股快照",
                    {"stock_name": "贵州茅台", "snapshot_change_pct": 1.0}))
        s = program_fact_summary(reg)
        # 精确带符号百分比/排名/样本/窗口（原始值逐字呈现）
        assert "-2.15%" in s and "364/496" in s and "10.0%" in s
        assert "26.3/100" in s and "11.2933%" in s and "-4.7215%" in s
        assert "16.0148" in s and "2026-10-09→2026-11-05" in s
        assert "贵州茅台当日涨跌幅 1.0%（上涨）" in s
        # 无形容词序数（server 不发明偏高/偏低/龙头）
        for banned in ("偏高", "偏低", "龙头", "仍", "资金偏好"):
            assert banned not in s
        lim = program_fact_limitations(reg)
        assert lim and "单期快照" in lim and "不构成资金流入流出" in lim
        assert "不代表市值或基本面龙头" in lim
        # claims 文本不影响程序化摘要（所有权：不消费模型文本）
        claims = [{"kind": "risk", "text": "分位仍处于相对偏高区间",
                   "evidence_refs": ["intraday"]}]
        accepted, _rej = validate_synthesis_output({"claims": claims}, reg)
        assert program_fact_summary(reg) == s  # 摘要与 claims 无关

    def test_program_summary_missing_blocks_omitted(self):
        """缺失块省略不填充；全空时如实披露而非编造。"""
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build("cat-1", "catalyst", "政策新闻",
                    {"title": "政策新闻", "published_date": "2026-11-06"}))
        assert "无已采纳的市场事实块" in program_fact_summary(reg)
        assert program_fact_limitations(reg) is None
        assert market_observation_relations(reg) == []

    def test_market_observation_relations_sign_and_equal(self):
        """观察关系：符号（含零=持平）、板块对基准关系、选取口径；无 raw 值。"""
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build("intraday", "fact", "当日行情",
                    {"change_pct": 0.0, "rank_of": 1, "total_boards": 3,
                     "momentum": 100.0, "breadth": 90.0}))
        reg.register(EvidenceEntry.build("trend", "fact", "板块趋势",
                    {"window": ["2026-10-09", "2026-11-05"], "qualified_sessions": 20,
                     "period_return_pct": 1.0, "benchmark_period_return_pct": 1.0,
                     "relative_return_pp": 0.0}))
        reg.register(EvidenceEntry.build("peers", "fact", "同业代表",
                    {"selection_rule": "按成交额前五",
                     "items": [{"name": "甲", "is_target": True, "change_pct": -1.0}]}))
        obs = {o["evidence_id"]: o["observation"]
               for o in market_observation_relations(reg)}
        assert "持平" in obs["intraday"]  # equal relation
        assert "与基准持平" in obs["trend"]  # equal relation
        assert "按成交额前五" in obs["peers"] and "方向一致" in obs["peers"]
        # raw magnitudes 不进投影
        joined = "".join(obs.values())
        for raw in ("100.0", "1/3", "90.0", "1.0", "-1.0"):
            assert raw not in joined, raw

    def test_peers_observation_missing_change_never_whole_sample_direction(self):
        """P1：任何成员涨跌观测缺失时不得声明全样本方向（root 反例
        600519 +1.0 / 000568 None——公开载荷保留成员与缺口，投影不得
        断言「方向一致」）。"""
        def peers_obs(items):
            reg = EvidenceRegistry()
            reg.register(EvidenceEntry.build("peers", "fact", "同业代表",
                        {"selection_rule": "按成交额前五", "items": items}))
            obs = market_observation_relations(reg)
            assert len(obs) == 1
            return obs[0]["observation"]

        # one known + one missing：不推断方向、显式披露缺失
        text = peers_obs([{"name": "甲", "is_target": True, "change_pct": 1.0},
                          {"name": "乙", "is_target": False, "change_pct": None}])
        assert "无法得出全样本方向结论" in text
        assert "1 家当日涨跌观测缺失" in text
        assert "方向一致" not in text and "方向存在差异" not in text
        # all missing：同样只披露
        text = peers_obs([{"name": "甲", "is_target": True, "change_pct": None},
                          {"name": "乙", "is_target": False, "change_pct": None}])
        assert "2 家当日涨跌观测缺失" in text
        assert "方向一致" not in text and "方向存在差异" not in text
        # real zero vs None：零是有效「持平」观测，None 是缺失——不混淆
        text = peers_obs([{"name": "甲", "is_target": True, "change_pct": 0.0},
                          {"name": "乙", "is_target": False, "change_pct": None}])
        assert "1 家当日涨跌观测缺失" in text and "方向一致" not in text
        text = peers_obs([{"name": "甲", "is_target": True, "change_pct": 0.0},
                          {"name": "乙", "is_target": False, "change_pct": 0.0}])
        assert "方向一致（持平）" in text and "观测缺失" not in text
        # all-known mixed：真实差异关系保留
        text = peers_obs([{"name": "甲", "is_target": True, "change_pct": 1.0},
                          {"name": "乙", "is_target": False, "change_pct": -2.0}])
        assert "方向存在差异" in text and "观测缺失" not in text
        # all-known equal：真实一致关系保留
        text = peers_obs([{"name": "甲", "is_target": True, "change_pct": 1.0},
                          {"name": "乙", "is_target": False, "change_pct": 1.0}])
        assert "方向一致（上涨）" in text and "观测缺失" not in text


class TestEventQualification:
    """统一事件资格与历史回顾判定（标题声明主题时点口径；无豁免词表）。"""

    def test_window_bounds(self):
        start, end = date(2026, 11, 4), date(2026, 11, 6)
        assert qualifies_event(date(2026, 11, 5), start, end) is None
        assert qualifies_event(date(2026, 11, 7), start, end) == "future"
        assert qualifies_event(date(2026, 11, 3), start, end) == "past"

    def test_retrospective_old_year_title(self):
        assert is_retrospective_title("回顾2020年白酒板块行情", date(2026, 11, 4))

    def test_retrospective_old_month_in_same_year(self):
        # root 反例：2026 年 1 月回顾在 11 月窗口 → 主题时点早于窗口起始月
        assert is_retrospective_title("2026年1月白酒政策回顾", date(2026, 11, 4))

    def test_historical_comparison_published_today_is_retrospective(self):
        # root 反例（converged 轮）：今天发布的销量对比是历史背景，不是催化；
        # 「对比」等豁免词表已删除——标题声明的最晚主题时点决定属性
        assert is_retrospective_title(
            "白酒行业2020年与2019年销量对比", date(2026, 11, 6))

    def test_old_comparison_words_no_longer_exempt(self):
        # 已删除的豁免词不再绕过日期主体判定（修订/新版/废止/取代/vs 同理）
        for title in ("白酒新规出台 与2018年版规定对比",
                      "行业规范修订：2020年版办法废止",
                      "新标准取代2019年旧规", "白酒板块 vs 2021年周期复盘"):
            assert is_retrospective_title(title, date(2026, 11, 4)), title

    def test_current_policy_quoting_old_year_not_retrospective(self):
        # 新政策主题在当前（标题声明最晚年份=窗口年，无月份）→ 不降级；
        # 正文/参照对象的旧年份不参与判定（标题声明主题时点口径）
        assert not is_retrospective_title("2026年白酒新规出台 与2018年版对比", date(2026, 11, 4))

    def test_title_without_declared_year_not_retrospective(self):
        assert not is_retrospective_title("白酒行业新政策发布", date(2026, 11, 4))

    def test_latest_declared_year_decides_theme(self):
        # 「2018-2020 牛市回顾」最晚 2020 < 2026 → 回顾
        assert is_retrospective_title("2018-2020年白酒牛市回顾", date(2026, 11, 4))

    def test_declared_day_earlier_than_window_start_is_retrospective(self):
        # root 边界反例：标题声明完整日（11-01）早于窗口起始（11-04），
        # 发布日期在窗口内（今天）→ 历史回顾降级；不把精度降为「月」
        assert is_retrospective_title(
            "白酒行业2026年11月1日政策回顾", date(2026, 11, 4))

    def test_declared_day_within_window_not_retrospective(self):
        # 窗口内/同日完整日期 → 不降级（正控：同日与次日事件保持催化）
        assert not is_retrospective_title(
            "白酒行业2026年11月4日新政落地", date(2026, 11, 4))
        assert not is_retrospective_title(
            "白酒行业2026年11月5日行业研讨会召开", date(2026, 11, 4))
        assert not is_retrospective_title(
            "白酒行业2026年11月6日渠道大会举行", date(2026, 11, 4))

    def test_declared_day_after_window_start_not_retrospective(self):
        # 完整日期晚于窗口起始（未来主题）不是「回顾」——本判定只降级过去
        assert not is_retrospective_title(
            "白酒行业2026年12月1日经销商大会预告", date(2026, 11, 4))

    def test_declared_month_day_still_uses_earliest_precision_fallback(self):
        # 混合声明取最晚上界：旧月完整日期 + 当前年仅年 → 不降级
        assert not is_retrospective_title(
            "回顾2026年1月政策调整，白酒行业2026年迎新规", date(2026, 11, 4))

    def test_invalid_title_date_components_degrade_precision_locally(self):
        # 供应商标题无效日期分量本地保守处理（非 500、不发明精度）：
        # 13 月无效 → 按年仅年上界（窗口年内）不降级；2 月 30 日日无效 →
        # 按月整月上界（2 月全在窗口前）降级
        assert not is_retrospective_title(
            "白酒行业2026年13月45日回顾", date(2026, 11, 4))
        assert is_retrospective_title(
            "白酒行业2026年2月30日研讨会回顾", date(2026, 11, 4))

    def test_declared_month_same_as_window_month_not_retrospective(self):
        # 年月精度（无日）：与实际窗口月比较——同月声明不因缺日降级
        assert not is_retrospective_title(
            "白酒行业2026年11月政策盘点", date(2026, 11, 4))
        # 上月声明降级（既有行为保持）
        assert is_retrospective_title(
            "白酒行业2026年10月政策盘点", date(2026, 11, 4))


class TestPublicHttpSourceContract:
    """公共 HTTP(S) 来源边界（准入与文章出站同一入口）。"""

    def test_valid_public_https_accepted(self):
        ok, reason = validate_public_http_url(
            "https://news.example.com/baijiu/12345.html")
        assert ok, reason

    def test_valid_http_with_explicit_port_accepted(self):
        ok, reason = validate_public_http_url("http://news.example.com:8080/a")
        assert ok, reason

    def test_non_http_scheme_rejected(self):
        for url in ("ftp://news.example.com/a", "file:///etc/hosts",
                    "javascript:alert(1)", "gopher://example.com/x"):
            ok, reason = validate_public_http_url(url)
            assert not ok, url
            assert reason == "source_scheme_not_http", url

    def test_private_loopback_literal_ip_rejected(self):
        for url in ("http://127.0.0.1/private.html", "https://10.0.0.5/a",
                    "http://192.168.1.10/a", "https://172.16.0.9/a",
                    "http://169.254.1.1/a", "http://0.0.0.0/a",
                    "http://[::1]/a", "http://[fe80::1]/a"):
            ok, reason = validate_public_http_url(url)
            assert not ok, url
            assert reason == "source_non_public_host", url

    def test_localhost_names_rejected(self):
        for url in ("http://localhost/a", "https://meta.localhost/a",
                    "http://printer.local/a"):
            ok, reason = validate_public_http_url(url)
            assert not ok, url
            assert reason == "source_non_public_host", url

    def test_userinfo_rejected(self):
        ok, reason = validate_public_http_url("https://user@example.com/a")
        assert not ok
        assert reason == "source_userinfo"
        ok, reason = validate_public_http_url("https://user:pw@news.example.com/a")
        assert not ok
        assert reason == "source_userinfo"

    def test_invalid_port_rejected(self):
        ok, reason = validate_public_http_url("https://news.example.com:99999/a")
        assert not ok
        assert reason == "source_invalid_port"

    def test_invalid_field_types_rejected_not_stringified(self):
        # 供应商字段类型错误：本地拒绝（不 str(...) 伪合法）
        for bad in (None, 123, ["https://example.com/a"], {"u": 1}, b"https://x"):
            ok, reason = validate_public_http_url(bad)
            assert not ok, bad
            assert reason == "source_not_string", bad

    def test_empty_and_oversize_rejected(self):
        ok, reason = validate_public_http_url("   ")
        assert not ok and reason == "source_empty"
        ok, reason = validate_public_http_url("https://example.com/" + "a" * 600)
        assert not ok and reason == "source_too_long"

    def test_malformed_hostname_rejected(self):
        for url in ("https://news_site.example.com/a", "https://.example.com/a",
                    "https://example.com./a", "https://-bad.example.com/a"):
            ok, reason = validate_public_http_url(url)
            assert not ok, url
            assert reason == "source_invalid_hostname", url

    def test_unparseable_url_rejected(self):
        ok, reason = validate_public_http_url("http://[::1")
        assert not ok


class TestUrlNormalization:
    """去重键归一化：跟踪参数剥离 / host 提取 / 非法 URL。"""

    def test_tracking_params_stripped(self):
        a = normalize_catalyst_url("https://Example.com/a/1?utm_source=x&id=2")
        b = normalize_catalyst_url("https://example.com/a/1/?id=2")
        assert a == b

    def test_fragment_stripped_and_trailing_slash(self):
        assert normalize_catalyst_url(
            "https://example.com/a/#top") == normalize_catalyst_url("https://example.com/a")

    def test_host_extraction(self):
        assert urlsplit_host("https://news.example.com:8080/a") == "news.example.com"
        assert urlsplit_host("not-a-url") == ""
        assert urlsplit_host("") == ""

    def test_ipv6_host_extraction_full_address(self):
        # 根因反例：IPv6 literal 按冒号切分 netloc 会得到 '[2606' 这样的
        # 损坏 host，两个不同公网 IPv6 源被误判同域合并（false dedup）；
        # stdlib 解析的 hostname 取 brackets 内完整地址、去端口、小写
        assert urlsplit_host(
            "https://[2606:4700:4700::1111]:8443/a") == "2606:4700:4700::1111"
        assert urlsplit_host(
            "http://[2606:4700:4700::1001]/b") == "2606:4700:4700::1001"
        assert urlsplit_host(
            "http://[2606:4700:4700::1111]") == "2606:4700:4700::1111"
        # 两个不同 IPv6 host 提取结果不同（不互相坍缩）
        assert (urlsplit_host("https://[2606:4700:4700::1111]/x")
                != urlsplit_host("https://[2606:4700:4700::1001]/x"))

    def test_ipv6_public_literal_admitted_and_local_rejected(self):
        # 结构契约与 host 提取一致：公网 IPv6 通过（global），本地/私网拒绝
        ok, reason = validate_public_http_url("https://[2606:4700:4700::1111]/a")
        assert ok, reason
        ok, reason = validate_public_http_url("https://[::1]/a")
        assert not ok and reason == "source_non_public_host"
        ok, reason = validate_public_http_url("https://[fe80::1]/a")
        assert not ok and reason == "source_non_public_host"
        ok, reason = validate_public_http_url("http://[::1")
        assert not ok  # 未闭合 brackets 不可解析

    def test_empty_url_key(self):
        assert normalize_catalyst_url("") == ""
        assert normalize_catalyst_url(None) == ""


class TestRegistryBuilding:
    """登记表行为：重复注册拒绝 / facts-backgrounds 分组 / 无数字锚定字段。"""

    def test_duplicate_registration_rejected(self):
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build("cat-1", "catalyst", "t", {"a": 1}))
        with pytest.raises(ValueError):
            reg.register(EvidenceEntry.build("cat-1", "catalyst", "t", {"a": 2}))

    def test_no_numeric_anchor_channel_exists(self):
        # 定性契约：登记表不提供任何 numeric token / anchored 查询通道
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build("a", "fact", "a", {"v": 1.9}))
        entry = reg.get("a")
        assert entry is not None
        assert not hasattr(entry, "numeric_tokens")
        assert not hasattr(reg, "anchored_tokens")

    def test_facts_and_backgrounds_split(self):
        reg = EvidenceRegistry()
        reg.register(EvidenceEntry.build("cat-1", "catalyst", "t", {}))
        reg.register(EvidenceEntry.build("bg-1", "background", "t", {}))
        assert [e.evidence_id for e in reg.facts()] == ["cat-1"]
        assert [e.evidence_id for e in reg.backgrounds()] == ["bg-1"]
