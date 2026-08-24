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

    def test_kana_implies_japanese_even_with_kanji(self):
        assert detect_lang("こんにちは世界") == "ja"

    def test_hangul_implies_korean(self):
        assert detect_lang("안녕하세요") == "ko"

    def test_cyrillic_implies_russian(self):
        assert detect_lang("Привет мир") == "ru"

    def test_latin_romance_languages_fall_back_to_english(self):
        assert detect_lang("bonjour le monde") == "en"
        assert detect_lang("Guten Morgen") == "en"

    def test_empty_defaults_to_english(self):
        assert detect_lang("") == "en"
        assert detect_lang("123 !@#") == "en"


class TestPairResolution:
    def test_auto_flips_by_detection(self):
        pair = resolve_pair("hello", "auto", "auto")
        assert (pair.source, pair.target) == ("en", "zh")

        pair = resolve_pair("你好", "auto", "auto")
        assert (pair.source, pair.target) == ("zh", "en")

        pair = resolve_pair("こんにちは", "auto", "auto")
        assert (pair.source, pair.target) == ("ja", "en")

        pair = resolve_pair("Привет", "auto", "auto")
        assert (pair.source, pair.target) == ("ru", "en")

    def test_concrete_choices_are_respected(self):
        pair = resolve_pair("hello", "zh", "en")
        assert (pair.source, pair.target) == ("zh", "en")

        pair = resolve_pair("hello", "de", "ja")
        assert (pair.source, pair.target) == ("de", "ja")

    def test_identical_sides_are_forced_apart(self):
        pair = resolve_pair("hello", "en", "en")
        assert (pair.source, pair.target) == ("en", "zh")

        pair = resolve_pair("bonjour", "fr", "fr")
        assert (pair.source, pair.target) == ("fr", "en")

    def test_choices_are_preserved(self):
        pair = resolve_pair("hi", "auto", "zh")
        assert pair.source_choice == "auto"
        assert pair.target_choice == "zh"
        assert pair.detected == "en"

    def test_opposite_lang_peers_with_english(self):
        assert opposite_lang("en") == "zh"
        assert opposite_lang("zh") == "en"
        assert opposite_lang("ja") == "en"
        assert opposite_lang("ko") == "en"
        assert opposite_lang("ru") == "en"
        assert opposite_lang("fr") == "en"
        assert opposite_lang("de") == "en"
        assert opposite_lang("es") == "en"


class TestPrompt:
    def _pair(self, source: str, target: str):
        return resolve_pair("x" * 10, source, target)

    def test_default_instruction(self):
        prompt = build_prompt("hi there", self._pair("en", "zh"))
        assert "from English to Chinese (Simplified)" in prompt
        assert prompt.endswith("\n\nhi there")

    def test_non_default_pair(self):
        prompt = build_prompt("猫", self._pair("ja", "en"))
        assert "from Japanese to English" in prompt
        assert prompt.endswith("\n\n猫")

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
        assert choice_label("ja") == "日本語"

    def test_auto_without_detection(self):
        assert choice_label("auto") == "自动"

    def test_auto_with_detection(self):
        assert choice_label("auto", detected="zh") == "自动 · 中"
        assert choice_label("auto", detected="ja") == "自动 · 日"
