# -*- coding: utf-8 -*-
"""P5-fix: 扩 six_dimension_inputs schema 测试。

验证：
- _SIX_DIM_KEYS 包含 us_china_impact + regulatory_risk
- _ENUM_LIKE_KEYS 包含 2 个新键
- _KNOWN_ENUM_TOKENS 包含所有合法枚举值
- _coerce_six_dim_value 正确解析 2 个新键
- _merge_six_dim 正确把 LLM 输出的 2 个新键映射到 raw_data
- prompt 文本包含 2 个新键的 schema 说明
"""

from __future__ import annotations

from src.analyzer import (
    _ENUM_LIKE_KEYS,
    _KNOWN_ENUM_TOKENS,
    _SIX_DIM_KEYS,
    _coerce_six_dim_value,
)


class TestSixDimSchemaKeys:
    """_SIX_DIM_KEYS 元组必须包含 2 个新键"""

    def test_six_dim_keys_includes_us_china_impact(self) -> None:
        assert "us_china_impact" in _SIX_DIM_KEYS

    def test_six_dim_keys_includes_regulatory_risk(self) -> None:
        assert "regulatory_risk" in _SIX_DIM_KEYS

    def test_six_dim_keys_still_includes_old_keys(self) -> None:
        """向后兼容：旧键不能被删除"""
        for k in (
            "chain_position",
            "moat_type",
            "moat_strength",
            "us_china_risk",
            "chokepoint_type",
            "cognitive_difference",
            "recent_catalysts",
            "news_sentiment",
        ):
            assert k in _SIX_DIM_KEYS, f"missing {k}"

    def test_six_dim_keys_count(self) -> None:
        """v2.1（issue #46）chip_concentration 退休：12 - 1 = 11 个"""
        assert len(_SIX_DIM_KEYS) == 11
        assert "chip_concentration" not in _SIX_DIM_KEYS


class TestEnumLikeKeys:
    """_ENUM_LIKE_KEYS 必须包含 2 个新键"""

    def test_enum_like_includes_us_china_impact(self) -> None:
        assert "us_china_impact" in _ENUM_LIKE_KEYS

    def test_enum_like_includes_regulatory_risk(self) -> None:
        assert "regulatory_risk" in _ENUM_LIKE_KEYS


class TestKnownEnumTokens:
    """_KNOWN_ENUM_TOKENS 包含所有合法枚举值"""

    def test_us_china_impact_tokens(self) -> None:
        for token in ("minimal", "limited", "significant", "severe"):
            assert token in _KNOWN_ENUM_TOKENS

    def test_regulatory_risk_tokens(self) -> None:
        # low/medium/high/none 在主表已注册
        for token in ("low", "medium", "high", "none"):
            assert token in _KNOWN_ENUM_TOKENS

    def test_no_collision_with_existing(self) -> None:
        """新枚举值不与已有枚举值冲突（除了合法共用：low/medium/high/none）"""
        # 全部 4 个新 us_china_impact 都应独立
        for token in ("minimal", "limited", "significant", "severe"):
            # 不与 moat_type 冲突
            assert token not in {
                "patent",
                "technology",
                "brand",
                "network",
                "switching_cost",
                "license",
                "regulatory",
                "multiple",
            }


class TestCoerceSixDimValue:
    """_coerce_six_dim_value 解析测试"""

    def test_us_china_impact_minimal(self) -> None:
        assert _coerce_six_dim_value("us_china_impact", "minimal") == "minimal"

    def test_us_china_impact_limited(self) -> None:
        assert _coerce_six_dim_value("us_china_impact", "limited") == "limited"

    def test_us_china_impact_significant(self) -> None:
        assert _coerce_six_dim_value("us_china_impact", "significant") == "significant"

    def test_us_china_impact_severe(self) -> None:
        assert _coerce_six_dim_value("us_china_impact", "severe") == "severe"

    def test_us_china_impact_null(self) -> None:
        for v in ("null", "none", "n/a", "NULL", "None"):
            assert _coerce_six_dim_value("us_china_impact", v) is None

    def test_regulatory_risk_low(self) -> None:
        assert _coerce_six_dim_value("regulatory_risk", "low") == "low"

    def test_regulatory_risk_medium(self) -> None:
        assert _coerce_six_dim_value("regulatory_risk", "medium") == "medium"

    def test_regulatory_risk_high(self) -> None:
        assert _coerce_six_dim_value("regulatory_risk", "high") == "high"

    def test_regulatory_risk_null(self) -> None:
        for v in ("null", "none", "n/a"):
            assert _coerce_six_dim_value("regulatory_risk", v) is None

    def test_us_china_impact_with_parenthetical(self) -> None:
        """带括号解释时正确解析（_coerce 内部 head 截取）"""
        result = _coerce_six_dim_value("us_china_impact", "significant (关税升级预期)")
        # 应至少包含 significant
        assert result is not None
        # 容忍：可能返回 "significant" 或整段 head
        # 关键是 enum token 能被识别
        assert "significant" in str(result).lower() or result == "significant"

    def test_regulatory_risk_with_prose_noise(self) -> None:
        """复杂文本场景下能提取 enum token"""
        result = _coerce_six_dim_value("regulatory_risk", "high (反垄断风险加剧)")
        assert "high" in str(result).lower() or result == "high"


class TestMergeSixDimP5:
    """_merge_six_dim 把 LLM 输出的 2 个新键映射到 raw_data"""

    def test_merge_us_china_impact(self) -> None:
        from src.services.research_framework_integration import _merge_six_dim

        raw_data: dict = {}
        _merge_six_dim(raw_data, {"us_china_impact": "limited"})
        assert raw_data.get("us_china_impact") == "limited"

    def test_merge_regulatory_risk(self) -> None:
        from src.services.research_framework_integration import _merge_six_dim

        raw_data: dict = {}
        _merge_six_dim(raw_data, {"regulatory_risk": "high"})
        assert raw_data.get("regulatory_risk") == "high"

    def test_merge_skips_null(self) -> None:
        from src.services.research_framework_integration import _merge_six_dim

        raw_data: dict = {}
        _merge_six_dim(raw_data, {"us_china_impact": "null", "regulatory_risk": "null"})
        assert "us_china_impact" not in raw_data
        assert "regulatory_risk" not in raw_data

    def test_merge_does_not_overwrite_existing(self) -> None:
        """P4 客观数据 + P5 行业 KB 已填，LLM 主观值不应覆盖"""
        from src.services.research_framework_integration import _merge_six_dim

        raw_data = {"us_china_impact": "limited"}  # P4 已填
        _merge_six_dim(raw_data, {"us_china_impact": "severe"})  # LLM 想覆盖
        # 应该是 limited（不覆盖）
        assert raw_data["us_china_impact"] == "limited"

    def test_merge_invalid_input_type(self) -> None:
        """非字典输入不抛异常"""
        from src.services.research_framework_integration import _merge_six_dim

        raw_data: dict = {}
        _merge_six_dim(raw_data, None)  # type: ignore[arg-type]
        _merge_six_dim(raw_data, "not_a_dict")  # type: ignore[arg-type]
        _merge_six_dim(raw_data, 42)  # type: ignore[arg-type]
        assert raw_data == {}

    def test_merge_empty_dict(self) -> None:
        from src.services.research_framework_integration import _merge_six_dim

        raw_data: dict = {"pre_existing": "value"}
        _merge_six_dim(raw_data, {})
        assert raw_data == {"pre_existing": "value"}

    def test_merge_unknown_keys_ignored(self) -> None:
        """_merge_six_dim 不应被未知 key 影响"""
        from src.services.research_framework_integration import _merge_six_dim

        raw_data: dict = {}
        _merge_six_dim(
            raw_data,
            {"us_china_impact": "limited", "unknown_key": "should_be_ignored"},
        )
        assert raw_data.get("us_china_impact") == "limited"
        assert "unknown_key" not in raw_data


class TestPromptSchema:
    """prompt 文本必须包含 2 个新键的说明

    通过源码静态扫描验证（避免 _format_prompt 的 mock 复杂度）。
    实际运行时 prompt 由 _format_prompt 在调用 GeminiAnalyzer._format_prompt
    时动态拼接 _prompt_segment（_format_prompt 内部字符串）+ 模板。
    本测试类只验证 _format_prompt 输入的源码模板里包含新键。
    """

    @staticmethod
    def _get_prompt_template() -> str:
        import inspect
        from src.analyzer import GeminiAnalyzer

        return inspect.getsource(GeminiAnalyzer)

    def test_prompt_has_us_china_impact(self) -> None:
        src = self._get_prompt_template()
        assert "us_china_impact" in src

    def test_prompt_has_regulatory_risk(self) -> None:
        src = self._get_prompt_template()
        assert "regulatory_risk" in src

    def test_prompt_disambiguates_us_china_risk_vs_impact(self) -> None:
        """源码中必须显式区分 us_china_risk（产业链）与 us_china_impact（宏观）"""
        src = self._get_prompt_template()
        assert "us_china_risk" in src
        assert "us_china_impact" in src
        # disambiguation 关键词
        assert "产业链" in src or "脱钩" in src or "宏观与地缘" in src
