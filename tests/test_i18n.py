"""Tests for catalog installation and lookup."""

from __future__ import annotations

import gettext as gt_mod

import pytest

from linago import i18n


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("LINAGO_LANG", raising=False)
    yield
    i18n.install(None)
    i18n._current = gt_mod.NullTranslations()


def test_english_fallback_without_catalog():
    i18n.install(None)
    assert i18n._("Copy") == "Copy"


def test_zh_catalog_lookup():
    i18n.install("zh_CN")
    assert i18n._("Translate") == "翻译"
    assert i18n._("Copy") == "复制"
    assert i18n._("Auto · {}").format("中") == "自动 · 中"


def test_missing_msgid_passes_through():
    i18n.install("zh_CN")
    assert i18n._("no such msgid") == "no such msgid"


def test_env_variable_selects_language(monkeypatch):
    monkeypatch.setenv("LINAGO_LANG", "zh_CN")
    i18n.install(None)
    assert i18n._("Swap languages") == "交换语言"


def test_unknown_lang_falls_back_to_msgids():
    i18n.install("xx_XX")
    assert i18n._("Translate") == "Translate"


def test_lookup_function_identity_is_stable():
    before = i18n._
    i18n.install("zh_CN")
    after = i18n._
    assert before is after
