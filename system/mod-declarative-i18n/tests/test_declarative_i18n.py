"""Unit tests for Mod Declarative i18n (Tier 0 engine localization subsystem)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pytest
import yaml

from agent import i18n, i18n_layers


def test_declarative_i18n_symbols_exported():
    """Verify all declarative i18n symbols are exported in __all__."""
    assert "get_active_language" in i18n.__all__
    assert i18n.get_active_language is i18n.get_language

    for symbol in (
        "is_mod_cache_stale",
        "mod_languages",
        "mod_locales_cache_path",
        "mod_overlay_layer",
    ):
        assert symbol in i18n_layers.__all__
        assert hasattr(i18n_layers, symbol)


def test_mod_locales_cache_path(tmp_path: Path, monkeypatch):
    """Verify mod_locales_cache_path respects HERMES_MODS_DIR and home directory."""
    monkeypatch.delenv("HERMES_MODS_DIR", raising=False)
    assert i18n_layers.mod_locales_cache_path(tmp_path) == tmp_path / "mods" / ".locales_cache.json"

    custom_mods = tmp_path / "custom_mods"
    monkeypatch.setenv("HERMES_MODS_DIR", str(custom_mods))
    assert i18n_layers.mod_locales_cache_path(tmp_path) == custom_mods / ".locales_cache.json"


def test_is_mod_cache_stale(tmp_path: Path, monkeypatch):
    """Verify cache staleness check with timestamp invalidation."""
    cache_file = tmp_path / ".locales_cache.json"
    monkeypatch.setenv("HERMES_MODS_DIR", str(tmp_path))

    # Missing file is not stale
    assert not i18n_layers.is_mod_cache_stale(tmp_path)

    # Created file is detected as stale initially
    cache_file.write_text("{}", encoding="utf-8")
    assert i18n_layers.is_mod_cache_stale(tmp_path)

    # Calling mod_overlay_layer updates mtime tracking
    i18n_layers.mod_overlay_layer(tmp_path, "ru")
    assert not i18n_layers.is_mod_cache_stale(tmp_path)


def test_mod_overlay_layer_and_priority_override(tmp_path: Path, monkeypatch):
    """Verify mod translations load with highest priority and override core strings."""
    cache_file = tmp_path / ".locales_cache.json"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MODS_DIR", str(tmp_path))

    cache_data = {
        "_meta": {
            "version": "1.4.0",
            "active_mods_count": 1,
            "languages": ["ru", "en"],
        },
        "catalogs": {
            "ru": {
                "core": {
                    "gateway.turn_error": "Контролируемая ошибка мода",
                    "custom_mod.sample_key": "Кастомная строка локализации мода",
                }
            }
        }
    }
    cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

    # Invalidate cache
    i18n.reset_language_cache()
    i18n_layers.clear_cache()

    # 1. Custom mod key is accessible globally
    assert i18n.t("custom_mod.sample_key", lang="ru") == "Кастомная строка локализации мода"

    # 2. Existing Hermes core key is overridden by mod overlay layer
    assert i18n.t("gateway.turn_error", lang="ru") == "Контролируемая ошибка мода"

    # 3. Layered languages includes mod languages
    langs = i18n_layers.layered_languages(tmp_path)
    assert "ru" in langs

    # 4. Clear cache resets internal dicts
    i18n_layers.clear_cache()
    assert len(i18n_layers._mod_cache) == 0
    assert len(i18n_layers._mod_languages_cache) == 0


def test_mod_i18n_folder_fallback_discovery(tmp_path: Path, monkeypatch):
    """Verify fallback discovery scans both locales/*.yaml and canonical i18n/*.yaml."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MODS_DIR", str(tmp_path))

    # Create dummy folder mod with i18n/*.yaml (no .locales_cache.json)
    custom_mod = tmp_path / "custom" / "test-i18n-fallback" / "i18n"
    custom_mod.mkdir(parents=True, exist_ok=True)
    (custom_mod / "ru.yaml").write_text("test_fallback_key: 'Успешный фоллбэк'\n", encoding="utf-8")

    i18n.reset_language_cache()
    i18n_layers.clear_cache()

    flat = i18n_layers.mod_overlay_layer(tmp_path, "ru")
    assert flat.get("test_fallback_key") == "Успешный фоллбэк"
    assert "ru" in i18n_layers.mod_languages(tmp_path)
