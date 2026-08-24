"""Tests for language detection, pair resolution, and prompt building."""

from __future__ import annotations

from linago.lang import (
    build_prompt,
    choice_label,
    detect_lang,
    normalize_text,
    opposite_lang,
    resolve_pair,
)


class TestNormalizeText:
    def test_collapses_blank_line_runs(self):
        assert normalize_text("a\n\n\nb") == "a\nb"

    def test_normalizes_crlf(self):
        assert normalize_text("a\r\nb\rc") == "a\nb\nc"

    def test_strips_outer_whitespace(self):
        assert normalize_text("  hello \n ") == "hello"

    def test_keeps_single_newlines(self):
        assert normalize_text("a\nb") == "a\nb"


class TestDetectLang:
    def test_latin_is_english(self):
        assert detect_lang("hello world") == "en"

    def test_han_is_chinese(self):
        assert detect_lang("你好世界") == "zh"

    def test_mixed_majority_wins(self):
        assert detect_lang("Hello 世界") == "en"
        assert detect_lang("你好世界，这是中文。hello") == "zh"

    def test_empty_defaults_to_english(self):
        assert detect_lang("") == "en"
        assert detect_lang("123 !@#") == "en"


class TestPairResolution:
    def test_auto_flips_by_detection(self):
        pair = resolve_pair("hello", "auto", "auto")
        assert (pair.source, pair.target) == ("en", "zh")

        pair = resolve_pair("你好", "auto", "auto")
        assert (pair.source, pair.target) == ("zh", "en")

    def test_concrete_choices_are_respected(self):
        pair = resolve_pair("hello", "zh", "en")
        assert (pair.source, pair.target) == ("zh", "en")

    def test_identical_sides_are_forced_apart(self):
        pair = resolve_pair("hello", "en", "en")
        assert (pair.source, pair.target) == ("en", "zh")

    def test_choices_are_preserved(self):
        pair = resolve_pair("hi", "auto", "zh")
        assert pair.source_choice == "auto"
        assert pair.target_choice == "zh"
        assert pair.detected == "en"

    def test_opposite_lang(self):
        assert opposite_lang("en") == "zh"
        assert opposite_lang("zh") == "en"


class TestPrompt:
    def _pair(self, source: str, target: str):
        return resolve_pair("x" * 10, source, target)

    def test_default_instruction(self):
        prompt = build_prompt("hi there", self._pair("en", "zh"))
        assert "from English to Chinese (Simplified)" in prompt
        assert prompt.endswith("\n\nhi there")

    def test_template_with_placeholders(self):
        template = "Summarize {text} in {target} for a {source} speaker."
        prompt = build_prompt("hi", self._pair("en", "zh"), template=template)
        assert prompt == ("Summarize hi in Chinese (Simplified) for a English speaker.")

    def test_template_without_text_placeholder_appends(self):
        prompt = build_prompt(
            "hi", self._pair("en", "zh"), template="Explain {source}:"
        )
        assert prompt == "Explain English:\n\nhi"


class TestChoiceLabel:
    def test_plain_code(self):
        assert choice_label("en") == "English"

    def test_auto_without_detection(self):
        assert choice_label("auto") == "自动"

    def test_auto_with_detection(self):
        assert choice_label("auto", detected="zh") == "自动 · 中"
