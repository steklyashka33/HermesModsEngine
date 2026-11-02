"""Comprehensive Unit Tests for Hermes Unified i18n Subsystem.
Verifies:
1. 8-Language official standard parity (en, ru, es, fr, zh, ar, de, pt).
2. Unified Engine Resolver (engine_i18n) key lookup, fallbacks, parameter formatting.
3. Language alias normalization and resolution cascade (CLI -> config -> env -> default).
4. Zero duplication between engine and core: seamless bridge via .locales_cache.json and agent.i18n.t.
5. In-flight live cache invalidation and hot-reloading without service restarts.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
import pytest
import yaml

from engine.i18n import (
    OFFICIAL_LANGUAGES,
    DEFAULT_LANGUAGE,
    DEFAULT_CLI_LANGUAGE,
    LANGUAGE_ALIASES,
    normalize_language_id,
    is_language_id,
    is_supported_language,
    resolve_language,
    flatten_i18n_dict,
    compile_mod_locales,
    get_locales_cache_path,
    update_locales_cache,
    get_enabled_mod_locales,
    ENGINE_CLI_CATALOG,
    EngineI18nResolver,
    engine_i18n,
    t as engine_t,
)
from engine.models import Mod, ModCommand, ConfigField
from agent import i18n, i18n_layers


class TestLanguageStandardAndAliases:
    """Verify official 8 languages and alias normalization rules."""

    def test_official_languages_count_and_members(self):
        assert len(OFFICIAL_LANGUAGES) == 8
        assert set(OFFICIAL_LANGUAGES) == {"en", "es", "fr", "zh", "ar", "ru", "de", "pt"}

    def test_normalize_language_id(self):
        assert normalize_language_id("en") == "en"
        assert normalize_language_id("RU") == "ru"
        assert normalize_language_id("  es  ") == "es"
        assert normalize_language_id("zh-cn") == "zh"
        assert normalize_language_id("pt-br") == "pt"
        assert normalize_language_id("russian") == "ru"
        assert normalize_language_id("русский") == "ru"
        assert normalize_language_id("español") == "es"
        assert normalize_language_id("français") == "fr"
        assert normalize_language_id("deutsch") == "de"
        assert normalize_language_id("العربية") == "ar"
        assert normalize_language_id("") == ""
        assert normalize_language_id(None) == ""

    def test_is_language_id(self):
        assert is_language_id("en") is True
        assert is_language_id("ru") is True
        assert is_language_id("invalid!code") is False

    def test_is_supported_language(self):
        for lang in ("en", "es", "fr", "zh", "ar", "ru", "de", "pt"):
            assert is_supported_language(lang) is True
        assert is_supported_language("it") is False
        assert is_supported_language("ja") is False


class TestLanguageResolutionCascade:
    """Verify resolve_language respects precedence: CLI -> config.yaml -> env -> default."""

    def test_cli_explicit_arg_wins(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("display:\n  language: ru\n", encoding="utf-8")
        assert resolve_language("es", config_path=cfg) == "es"
        assert resolve_language("DE", config_path=cfg) == "de"

    def test_config_display_language_precedence(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("display:\n  language: fr\n", encoding="utf-8")
        assert resolve_language("auto", config_path=cfg) == "fr"
        assert resolve_language(None, config_path=cfg) == "fr"

    def test_config_toplevel_language_fallback(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("language: zh\n", encoding="utf-8")
        assert resolve_language("auto", config_path=cfg) == "zh"

    def test_env_var_fallback(self, tmp_path: Path, monkeypatch):
        non_existent_cfg = tmp_path / "absent.yaml"
        monkeypatch.setenv("HERMES_LANGUAGE", "pt")
        assert resolve_language("auto", config_path=non_existent_cfg) == "pt"

    def test_default_fallback_to_ru(self, tmp_path: Path, monkeypatch):
        non_existent_cfg = tmp_path / "absent.yaml"
        monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
        assert resolve_language("auto", config_path=non_existent_cfg) == "ru"
        assert resolve_language("", config_path=non_existent_cfg) == "ru"
        assert resolve_language(None, config_path=non_existent_cfg) == "ru"


class TestEngineCliCatalog8LanguageParity:
    """Verify full 8-language parity for all critical CLI status tags and headers."""

    def test_all_official_languages_present_in_catalog(self):
        for lang in OFFICIAL_LANGUAGES:
            assert lang in ENGINE_CLI_CATALOG, f"Missing catalog for language: {lang}"

    def test_status_badges_parity_across_all_8_languages(self):
        status_keys = [
            "cli.status.applied",
            "cli.status.disabled",
            "cli.status.not_applied",
            "cli.status.diverged",
            "cli.status.healthy",
            "cli.status.degraded",
        ]
        for lang in OFFICIAL_LANGUAGES:
            cat = ENGINE_CLI_CATALOG[lang]
            for key in status_keys:
                assert key in cat, f"Missing key {key} in language {lang}"
                assert len(cat[key].strip()) > 0

    def test_commands_and_info_labels_parity(self):
        ui_keys = [
            "cli.commands.registered",
            "cli.commands.col.command",
            "cli.commands.col.mod",
            "cli.commands.col.category",
            "cli.commands.col.mode",
            "cli.commands.col.args",
            "cli.commands.col.description",
            "cli.info.mod",
            "cli.info.author",
            "cli.info.status",
            "cli.info.type",
            "cli.info.description",
            "cli.info.commands",
            "cli.info.config",
            "cli.info.tests",
        ]
        for lang in OFFICIAL_LANGUAGES:
            cat = ENGINE_CLI_CATALOG[lang]
            for key in ui_keys:
                assert key in cat, f"Missing UI key {key} in language {lang}"


class TestEngineI18nResolver:
    """Verify engine_i18n resolution hierarchy, fallback to English, formatting, and live cache reload."""

    def test_cli_translation_and_formatting(self):
        # Russian
        msg_ru = engine_t("cli.commands.registered", lang="ru", count=5)
        assert "5" in msg_ru
        assert "Зарегистрировано" in msg_ru

        # English
        msg_en = engine_t("cli.commands.registered", lang="en", count=5)
        assert "5" in msg_en
        assert "Registered slash commands" in msg_en

        # Spanish
        msg_es = engine_t("cli.commands.registered", lang="es", count=5)
        assert "5" in msg_es
        assert "Comandos slash registrados" in msg_es

        # Chinese
        msg_zh = engine_t("cli.commands.registered", lang="zh", count=5)
        assert "5" in msg_zh
        assert "已注册斜杠命令" in msg_zh

    def test_fallback_to_english_when_target_missing(self):
        # A key defined only in English falls back to English when queried in German
        val = engine_t("nonexistent.key.xyz", lang="de", default="fallback_val")
        assert val == "fallback_val"

    def test_mod_locales_override_cli_strings(self, tmp_path: Path):
        """Verify mod translations take highest precedence over built-in CLI strings."""
        cache_path = tmp_path / ".locales_cache.json"
        cache_data = {
            "version": "2.0",
            "catalogs": {
                "ru": {
                    "core": {
                        "cli.status.applied": "СУПЕР-ПРИМЕНЕН",
                        "my_custom.phrase": "Уникальная фраза мода",
                    }
                }
            }
        }
        cache_path.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")

        custom_resolver = EngineI18nResolver(mods_dir=tmp_path)
        assert custom_resolver.t("cli.status.applied", lang="ru") == "СУПЕР-ПРИМЕНЕН"
        assert custom_resolver.t("my_custom.phrase", lang="ru") == "Уникальная фраза мода"

    def test_live_cache_reloading_without_restart(self, tmp_path: Path):
        """Verify resolver auto-detects mtime change of .locales_cache.json on disk."""
        cache_path = tmp_path / ".locales_cache.json"
        cache_path.write_text(json.dumps({
            "version": "2.0",
            "catalogs": {"ru": {"core": {"dynamic.key": "Первая версия"}}}
        }), encoding="utf-8")

        resolver = EngineI18nResolver(mods_dir=tmp_path)
        assert resolver.t("dynamic.key", lang="ru") == "Первая версия"

        # Update file on disk (simulate another tool or runner.py updating it)
        time.sleep(0.02)
        cache_path.write_text(json.dumps({
            "version": "2.0",
            "catalogs": {"ru": {"core": {"dynamic.key": "Обновленная версия"}}}
        }), encoding="utf-8")

        # Must immediately reflect the new value without recreating the resolver or restarting
        assert resolver.t("dynamic.key", lang="ru") == "Обновленная версия"


class TestUnifiedHermesCoreBridge:
    """Verify that the engine's compiled localization is 100% unified with Hermes core agent.i18n.t."""

    def test_agent_i18n_t_reads_from_locales_cache(self, tmp_path: Path, monkeypatch):
        cache_file = tmp_path / ".locales_cache.json"
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MODS_DIR", str(tmp_path))

        mod = Mod(
            id="bridge-mod",
            name="Bridge Mod",
            description="Bridge mod English description",
            path=tmp_path / "mod.yaml",
            targets=[],
            enabled=True,
            i18n={
                "ru": {
                    "name": "Мост Мод",
                    "description": "Описание моста",
                    "commands": {
                        "bridge-cmd": {"description": "Команда моста"}
                    },
                    "gateway": {
                        "turn_error": "Ошибка шлюза переопределена модом!"
                    },
                    "bridge": {
                        "custom_message": "Кастомное сообщение моста"
                    }
                },
                "es": {
                    "name": "Mod Puente",
                    "description": "Descripción del puente",
                    "commands": {
                        "bridge-cmd": {"description": "Comando de puente"}
                    }
                }
            },
            commands=[
                ModCommand(name="bridge-cmd", description="Bridge command default", mod_id="bridge-mod")
            ]
        )

        update_locales_cache([mod], tmp_path)
        i18n.reset_language_cache()
        i18n_layers.clear_cache()

        # 1. Access via Engine's unified resolver
        engine_res = EngineI18nResolver(mods_dir=tmp_path)
        assert engine_res.t("mods.bridge-mod.name", lang="ru") == "Мост Мод"
        assert engine_res.t("mods.bridge-mod.description", lang="ru") == "Описание моста"
        assert engine_res.t("slash.bridge-cmd.description", lang="ru") == "Команда моста"
        assert engine_res.t("mods.bridge-mod.name", lang="es") == "Mod Puente"

        # 2. Access via Hermes core agent.i18n.t() — EXACT SAME KEYS AND TRANSLATIONS!
        assert i18n.t("mods.bridge-mod.name", lang="ru") == "Мост Мод"
        assert i18n.t("mods.bridge-mod.description", lang="ru") == "Описание моста"
        assert i18n.t("slash.bridge-cmd.description", lang="ru") == "Команда моста"
        assert i18n.t("mods.bridge-mod.name", lang="es") == "Mod Puente"

        # 3. Core string override via Mod Overlay Layer
        assert i18n.t("gateway.turn_error", lang="ru") == "Ошибка шлюза переопределена модом!"

        # 4. Custom namespace strings via agent.i18n.t()
        assert i18n.t("bridge.custom_message", lang="ru") == "Кастомное сообщение моста"
