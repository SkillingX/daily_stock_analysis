# -*- coding: utf-8 -*-
"""Regression tests for analyzer JSON extraction (600176/601208/688002).

The previous ``find('{')`` / ``rfind('}')`` heuristic captured the wrong
substring whenever the LLM (notably the MiniMax-M3 thinking model) emitted
prose containing stray braces or followed the JSON with a second
incomplete object after ``}``. The captured range then mixed prose and JSON,
broke ``json.loads`` and silently dropped the ``six_dimension_inputs``
block that the framework scoring pipeline reads.

These tests pin the new behaviour to ensure we never regress to the
naive heuristic.
"""

import unittest
from typing import cast

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    from tests.litellm_stub import ensure_litellm_stub

    ensure_litellm_stub()

from src.analyzer import GeminiAnalyzer


class ExtractFirstJsonObjectTestCase(unittest.TestCase):
    """Pin the bracket-aware JSON extractor to handle 3 known failure modes."""

    def test_pure_json_object(self) -> None:
        text = '{"chain_position": "upstream", "score": 60}'
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(
            result,
            '{"chain_position": "upstream", "score": 60}',
        )

    def test_prose_with_stray_braces_before_json(self) -> None:
        # 600176 repro: prompt text says "Start with { and end with }"
        text = (
            "Follow these rules:\n"
            "1. Output must be JSON.\n"
            "2. Start with { and end with }.\n"
            "3. All text in Chinese.\n"
            '{"chain_position": "upstream", "score": 60}'
        )
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(
            result,
            '{"chain_position": "upstream", "score": 60}',
        )

    def test_json_fenced_in_markdown_code_block(self) -> None:
        # 688002 repro: LLM wraps JSON in ```json ... ``` despite prompt
        # forbidding fences, then appends prose after the closing fence.
        text = (
            "Let me note both.\n\n"
            "Final JSON:\n\n"
            "```json\n"
            '{"chain_position": "midstream", "score": 60}\n'
            "```\n\n"
            "Wait, the format should be... more commentary."
        )
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(
            result,
            '{"chain_position": "midstream", "score": 60}',
        )

    def test_json_followed_by_incomplete_second_object(self) -> None:
        # 688002 repro (variant): LLM emits first JSON, then prose, then a
        # second JSON object that is itself incomplete. The naive
        # rfind('}') picks the last '}' of the second object, mixing
        # the two responses.
        text = (
            "First JSON:\n"
            '{"chain_position": "midstream", "score": 60}\n'
            "\n\nThen the LLM continues and starts a second JSON object\n"
            "but forgets to close it:\n"
            '{"chain_position": "downstream", "score": 30\n'
        )
        result = GeminiAnalyzer._extract_first_json_object(text)
        # We must capture the *first* balanced object, not the broken
        # second one.
        self.assertEqual(
            result,
            '{"chain_position": "midstream", "score": 60}',
        )

    def test_prose_with_nested_braces(self) -> None:
        # Ensure nested object values don't throw off the depth counter.
        text = (
            "Note: the dashboard includes nested structures.\n"
            '{"dashboard": {"core": {"score": 50}}, "score": 60}\n'
            "Trailing prose."
        )
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(
            result,
            '{"dashboard": {"core": {"score": 50}}, "score": 60}',
        )

    def test_strings_containing_braces(self) -> None:
        # Strings inside JSON may legitimately contain '{' and '}'.
        # The depth counter must skip them.
        text = (
            "Some intro prose.\n"
            '{"text": "use { or } literally", "score": 60}\n'
            "Trailing prose."
        )
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(
            result,
            '{"text": "use { or } literally", "score": 60}',
        )

    def test_escaped_quotes_inside_strings(self) -> None:
        # An escaped \" should not toggle out of the string state.
        text = '{"text": "escaped \\" quote and { brace", "score": 60}'
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(
            result,
            '{"text": "escaped \\" quote and { brace", "score": 60}',
        )

    def test_empty_text_returns_empty_string(self) -> None:
        result = GeminiAnalyzer._extract_first_json_object("")
        self.assertEqual(result, "")

    def test_text_without_braces_falls_back_to_empty(self) -> None:
        result = GeminiAnalyzer._extract_first_json_object("no json here")
        self.assertEqual(result, "")

    def test_unterminated_object_returns_empty(self) -> None:
        # No balanced object found; safety net returns empty rather than
        # the legacy find/rfind range.
        text = '{"unterminated": 1, "no closing brace"'
        result = GeminiAnalyzer._extract_first_json_object(text)
        self.assertEqual(result, "")

    def test_real_response_for_600176_extracts_six_dimension_inputs(self) -> None:
        # Mirror the actual 600176 raw_response structure (truncated for
        # brevity) and assert the result parses cleanly with
        # six_dimension_inputs intact.
        text = (
            "You must follow these rules strictly:\n"
            "1. Output must be valid JSON.\n"
            "2. Start with { and end with }.\n"
            "3. All text in Chinese.\n"
            "4. decision_type must be buy/hold/sell.\n"
            "5. Include six_dimension_inputs.\n"
            '{"code": "600176", "score": 60, '
            '"six_dimension_inputs": {"chain_position": "upstream", '
            '"moat_type": "technology", "moat_strength": "strong", '
            '"us_china_risk": "medium"}}'
        )
        candidate = GeminiAnalyzer._extract_first_json_object(text)
        import json

        parsed = json.loads(candidate)
        six = parsed["six_dimension_inputs"]
        self.assertEqual(six["chain_position"], "upstream")
        self.assertEqual(six["moat_type"], "technology")
        self.assertEqual(six["moat_strength"], "strong")

    def test_real_response_for_688002_with_fenced_block(self) -> None:
        # Mirror the actual 688002 raw_response structure (the LLM emits
        # ```json ... ``` despite prompt forbidding it).
        text = (
            "Let me note both.\n\n"
            "Final JSON:\n\n"
            "```json\n"
            '{"code": "688002", "score": 50, '
            '"six_dimension_inputs": {"chain_position": "midstream", '
            '"moat_type": "technology", "moat_strength": "moderate", '
            '"us_china_risk": "low"}}\n'
            "```\n\n"
            "Wait, the format should be ...more commentary."
        )
        candidate = GeminiAnalyzer._extract_first_json_object(text)
        import json

        parsed = json.loads(candidate)
        six = parsed["six_dimension_inputs"]
        self.assertEqual(six["chain_position"], "midstream")
        self.assertEqual(six["us_china_risk"], "low")

    def test_prose_recovery_for_truncated_response(self) -> None:
        # When the LLM truncates its final JSON (token limit hit) but
        # already enumerated the six_dimension_inputs values in its
        # planning section, the extractor must recover those values so
        # downstream scoring does not collapse to "数据缺失".

        text = (
            "Let me work this out.\n\n"
            "Eight: Six Dimension Inputs:\n"
            "- chain_position: midstream (电子材料产业链中游，薄膜/树脂材料)\n"
            "- moat_type: technology (technology-based materials)\n"
            "- moat_strength: moderate (有技术壁垒但竞争激烈)\n"
            "- customer_concentration: null (unable to estimate)\n"
            "- us_china_risk: medium (电子材料可能受出口管制影响)\n"
            "- chokepoint_type: tech (technology chokepoint)\n"
            "- cognitive_difference: market_fair\n"
            "- recent_catalysts: []\n"
            "- news_sentiment: neutral\n"
            "- chip_concentration: null\n"
            "\nNow let me build the JSON."
        )
        import json

        candidate = cast(str, GeminiAnalyzer._extract_json_candidate(text))
        self.assertTrue(candidate)
        parsed = json.loads(candidate)
        self.assertTrue(parsed.get("_recovered_from_prose"))
        six = parsed["six_dimension_inputs"]
        self.assertEqual(six["chain_position"], "midstream")
        self.assertEqual(six["moat_type"], "technology")
        self.assertEqual(six["moat_strength"], "moderate")
        self.assertEqual(six["customer_concentration"], None)
        self.assertEqual(six["us_china_risk"], "medium")
        self.assertEqual(six["chokepoint_type"], "tech")
        self.assertEqual(six["news_sentiment"], "neutral")
        # ``[]`` literal in markdown should round-trip to an empty list,
        # not a single-element list containing the string "[]".
        self.assertEqual(six["recent_catalysts"], [])

    def test_prose_recovery_with_thinking_for_truncated_response(self) -> None:
        # 688002 repro: thinking trace lists the values mid-paragraph
        # before the truncated JSON, including some inline commentary.
        text = (
            "Let me work this out.\n\n"
            "Six Dimension Inputs:\n"
            "- chain_position: midstream or upstream? They make infrared detectors.\n"
            '  I\'d say "midstream" (component maker).\n'
            "- moat_type: technology (红外探测器技术)\n"
            "- moat_strength: moderate (technology leader in China)\n"
            "- us_china_risk: medium (红外探测器有出口管制相关)\n"
            "- chokepoint_type: tech (核心技术)\n"
        )
        import json

        candidate = cast(str, GeminiAnalyzer._extract_json_candidate(text))
        self.assertTrue(candidate)
        parsed = json.loads(candidate)
        six = parsed["six_dimension_inputs"]
        # The heuristic must snap to the canonical "midstream" token even
        # when the LLM emits reasoning noise around it.
        self.assertEqual(six["chain_position"], "midstream")
        self.assertEqual(six["moat_type"], "technology")
        self.assertEqual(six["moat_strength"], "moderate")
        self.assertEqual(six["us_china_risk"], "medium")
        self.assertEqual(six["chokepoint_type"], "tech")

    def test_prose_recovery_picks_canonical_enum_token(self) -> None:
        # LLM reasoning noise around an enum value should not bleed into
        # the recovered scalar. We must pick the canonical token.
        from src.analyzer import _coerce_six_dim_value

        self.assertEqual(
            _coerce_six_dim_value(
                "chain_position",
                "upstream (raw materials supplier)",
            ),
            "upstream",
        )
        self.assertEqual(
            _coerce_six_dim_value(
                "chain_position",
                'midstream or upstream? They make detectors. I\'d say "midstream"',
            ),
            "midstream",
        )
        self.assertEqual(
            _coerce_six_dim_value("us_china_risk", "medium (出口管制)"),
            "medium",
        )
        self.assertEqual(
            _coerce_six_dim_value("moat_type", "patent/technology"),
            "patent",
        )
        self.assertEqual(
            _coerce_six_dim_value("chokepoint_type", "tech"),
            "tech",
        )
        # Null / empty handled.
        self.assertIsNone(_coerce_six_dim_value("chain_position", "null"))
        self.assertIsNone(_coerce_six_dim_value("chain_position", ""))
        self.assertIsNone(_coerce_six_dim_value("chain_position", "N/A"))

    def test_prose_recovery_requires_minimum_keys(self) -> None:
        # If the prose only mentions one known key, we do not inject a
        # partial envelope — the heuristic is conservative so we never
        # produce a misleading six_dimension_inputs block.
        text = (
            "Just one bullet here:\n"
            "- chain_position: midstream\n"
            "Nothing else about the schema.\n"
        )
        candidate = GeminiAnalyzer._extract_json_candidate(text)
        self.assertIsNone(candidate)

    def test_600176_bare_wrapper_recovery(self) -> None:
        # 600176 repro: the LLM emits the main dashboard JSON and then
        # appends a *second* JSON object that is a bare six-dimension
        # dict (no ``six_dimension_inputs`` wrapper). The extractor must
        # merge the bare dict into the dashboard so framework scoring
        # can see ``chain_position`` / ``moat_type`` etc.
        dashboard_json = (
            '{"stock_name": "中国巨石", "sentiment_score": 18, '
            '"operation_advice": "减仓", "decision_type": "sell", '
            '"dashboard": {"core_conclusion": {"one_sentence": "弱势"}}'
            "}"
        )
        bare_six_dim = (
            '{"chain_position": "upstream", "moat_type": "technology", '
            '"moat_strength": "strong", "customer_concentration": null, '
            '"us_china_risk": "medium", "chokepoint_type": "capacity", '
            '"cognitive_difference": "market_fair", '
            '"recent_catalysts": [], "news_sentiment": "neutral"}'
        )
        text = (
            "Some intro prose.\n"
            + dashboard_json
            + "\nMore prose.\n"
            + bare_six_dim
            + "\nTrailing prose."
        )

        import json

        candidate = cast(str, GeminiAnalyzer._extract_json_candidate(text))
        self.assertTrue(candidate)
        parsed = json.loads(candidate)
        self.assertIn("dashboard", parsed)
        six = parsed["six_dimension_inputs"]
        self.assertEqual(six["chain_position"], "upstream")
        self.assertEqual(six["moat_type"], "technology")
        self.assertEqual(six["moat_strength"], "strong")
        self.assertEqual(six["us_china_risk"], "medium")
        self.assertEqual(six["chokepoint_type"], "capacity")
        self.assertTrue(parsed.get("_recovered_from_bare_wrapper"))

    def test_wrap_bare_six_dim_dict_unit(self) -> None:
        from src.analyzer import _wrap_bare_six_dim_dict

        # 4+ six-dim keys with no other keys → wrap.
        wrapped = _wrap_bare_six_dim_dict(
            {
                "chain_position": "upstream",
                "moat_type": "technology",
                "moat_strength": "strong",
                "us_china_risk": "medium",
            }
        )
        self.assertIsNotNone(wrapped)
        assert wrapped is not None  # narrow for pyright
        self.assertIn("six_dimension_inputs", wrapped)
        self.assertEqual(wrapped["six_dimension_inputs"]["chain_position"], "upstream")
        self.assertTrue(wrapped["_recovered_from_bare_wrapper"])

        # Mixed keys (3 known + 1 unknown) → not wrapped.
        self.assertIsNone(
            _wrap_bare_six_dim_dict(
                {
                    "chain_position": "upstream",
                    "moat_type": "technology",
                    "moat_strength": "strong",
                    "operation_advice": "buy",
                }
            )
        )

        # Already wrapped → not wrapped again.
        self.assertIsNone(
            _wrap_bare_six_dim_dict(
                {
                    "six_dimension_inputs": {"chain_position": "upstream"},
                    "moat_type": "technology",
                }
            )
        )

        # Too few six-dim keys (3 < 4 threshold) → not wrapped.
        self.assertIsNone(
            _wrap_bare_six_dim_dict(
                {
                    "chain_position": "upstream",
                    "moat_type": "technology",
                    "moat_strength": "strong",
                    "stock_name": "X",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
