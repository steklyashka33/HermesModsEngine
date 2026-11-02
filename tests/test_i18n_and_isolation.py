"""Unit tests for catalog isolation (custom/), forbidden commands/ dir, i18n co-location, timeouts, and secrets.yaml."""
import os
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from engine.models import ConfigField, Mod, ModCommand, ModTarget, MonitorSpec
from engine.loader import ModLoader, parse_mod_manifest, parse_mod_command, parse_config_field, _load_folder_mod_sections
from engine.validator import ModValidator
from engine.config_manager import SecretsStorage, ModConfigManager
from engine.monitor_manager import ModMonitorManager


class TestCatalogIsolation:
    """Ensure load_all scans strictly system/ then custom/, completely ignoring root."""

    def test_load_all_scans_only_system_and_custom(self, tmp_path: Path):
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()
        system_dir = mods_dir / "system"
        system_dir.mkdir()
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir()

        # 1. System mod
        sys_mod = system_dir / "sys-mod.yaml"
        sys_mod.write_text("id: sys-mod\nname: Sys Mod\nenabled: true\n", encoding="utf-8")

        # 2. Custom mod
        cust_mod = custom_dir / "cust-mod.yaml"
        cust_mod.write_text("id: cust-mod\nname: Cust Mod\nenabled: true\n", encoding="utf-8")

        # 3. Stray mod in root (must NOT be loaded)
        stray_mod = mods_dir / "stray-root.yaml"
        stray_mod.write_text("id: stray-root\nname: Stray Mod\nenabled: true\n", encoding="utf-8")

        # 4. Stray folder mod in root (must NOT be loaded)
        stray_folder = mods_dir / "stray-folder"
        stray_folder.mkdir()
        (stray_folder / "mod.yaml").write_text("id: stray-folder\nname: Stray Folder\nenabled: true\n", encoding="utf-8")

        loaded = ModLoader.load_all(mods_dir)
        loaded_ids = [m.id for m in loaded]

        assert "sys-mod" in loaded_ids
        assert "cust-mod" in loaded_ids
        assert "stray-root" not in loaded_ids
        assert "stray-folder" not in loaded_ids
        assert len(loaded) == 2


class TestForbiddenCommandsDirectory:
    """Ensure commands/ directory is forbidden and ignored, while commands.yaml works."""

    def test_commands_dir_ignored_by_loader(self, tmp_path: Path):
        mod_dir = tmp_path / "folder-mod"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text("id: folder-mod\nname: Folder Mod\nenabled: true\n", encoding="utf-8")

        # Create forbidden commands/ directory
        cmd_dir = mod_dir / "commands"
        cmd_dir.mkdir()
        (cmd_dir / "forbidden.yaml").write_text("name: forbidden-cmd\nshell: echo no\n", encoding="utf-8")

        compiled = _load_folder_mod_sections(mod_dir)
        # commands section must NOT be populated from commands/ dir
        assert "forbidden-cmd" not in [c.get("name") for c in compiled.get("commands", [])]

    def test_commands_yaml_single_file_loaded(self, tmp_path: Path):
        mod_dir = tmp_path / "folder-mod-yaml"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text("id: folder-mod-yaml\nname: Folder Mod Yaml\nenabled: true\n", encoding="utf-8")

        # Single commands.yaml file
        (mod_dir / "commands.yaml").write_text(yaml.dump([
            {"name": "valid-cmd", "description": "Valid from commands.yaml", "shell": "echo yes"}
        ]), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="folder-mod-yaml")
        assert mod is not None
        assert len(mod.commands) == 1
        assert mod.commands[0].name == "valid-cmd"
        assert mod.commands[0].description == "Valid from commands.yaml"

    def test_validator_rejects_commands_dir(self, tmp_path: Path):
        mod_dir = tmp_path / "bad-cmd-mod"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text("id: bad-cmd-mod\nname: Bad Mod\nenabled: true\n", encoding="utf-8")
        (mod_dir / "commands").mkdir()
        (mod_dir / "commands" / "foo.yaml").write_text("name: foo\n", encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="bad-cmd-mod")
        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        issues = validator.check_layout_and_capabilities(mod)
        errs = [i for i in issues if "forbidden 'commands/'" in i.message]
        assert len(errs) == 1
        assert errs[0].severity == "error"


class TestCoLocatedI18n:
    """Test co-located i18n support in Mod, ModCommand, and ConfigField."""

    def test_config_field_i18n_and_fallback(self):
        raw = {
            "type": "string",
            "title": "API Token",
            "description": "Service API authentication token",
            "i18n": {
                "ru": {
                    "title": "API Токен",
                    "description": "Токен аутентификации сервиса"
                },
                "es": {
                    "title": "Token API",
                    "description": "Token de autenticación"
                }
            }
        }
        field = parse_config_field("api_token", raw)
        assert field.get_title("en") == "API Token"
        assert field.get_title("ru") == "API Токен"
        assert field.get_title("es") == "Token API"
        assert field.get_title("de") == "API Token"  # fallback

        assert field.get_description("en") == "Service API authentication token"
        assert field.get_description("ru") == "Токен аутентификации сервиса"
        assert field.get_description("es") == "Token de autenticación"
        assert field.get_description("fr") == "Service API authentication token"  # fallback

    def test_command_i18n_and_timeout(self):
        raw = {
            "name": "status",
            "description": "Display system status",
            "timeout": 15.5,
            "i18n": {
                "ru": {
                    "description": "Отобразить статус системы"
                }
            }
        }
        cmd = parse_mod_command(raw, mod_id="test-mod", mod_dir=None)
        assert cmd is not None
        assert cmd.name == "status"
        assert cmd.timeout == 15.5
        assert cmd.get_description("en") == "Display system status"
        assert cmd.get_description("ru") == "Отобразить статус системы"
        assert cmd.get_description("zh") == "Display system status"

        d = cmd.to_dict()
        assert d["timeout"] == 15.5
        assert "ru" in d["i18n"]

    def test_mod_manifest_i18n_and_verify_timeout(self, tmp_path: Path):
        manifest_file = tmp_path / "i18n-mod.yaml"
        manifest_file.write_text(yaml.dump({
            "id": "i18n-mod",
            "name": "Network Guard",
            "description": "Protects connections",
            "verify_timeout": 30,
            "i18n": {
                "ru": {
                    "name": "Сетевой страж",
                    "description": "Защищает сетевые соединения"
                }
            }
        }), encoding="utf-8")

        mod = parse_mod_manifest(manifest_file, mod_dir=None, fallback_id="i18n-mod")
        assert mod is not None
        assert mod.verify_timeout == 30
        assert mod.get_title("en") == "Network Guard"
        assert mod.get_title("ru") == "Сетевой страж"
        assert mod.get_description("en") == "Protects connections"
        assert mod.get_description("ru") == "Защищает сетевые соединения"

    def test_legacy_description_ru_backwards_compatibility(self):
        cmd = parse_mod_command({"name": "legacy", "description": "English", "description_ru": "Русский"}, "mod", None)
        assert cmd.get_description("ru") == "Русский"
        assert cmd.i18n["ru"]["description"] == "Русский"


class TestSecretsYamlStorageAndMigration:
    """Test YAML storage, 0600 permissions, and automatic migration from secrets.json."""

    def test_yaml_storage_and_0600(self, tmp_path: Path):
        sec_yaml = tmp_path / "secrets.yaml"
        storage = SecretsStorage(sec_yaml)
        storage.set_mod_secret("my-mod", "api_key", "secret123")

        assert sec_yaml.exists()
        # Verify 0600
        mode = stat.S_IMODE(sec_yaml.stat().st_mode)
        assert mode == 0o600

        # Verify YAML content
        content = sec_yaml.read_text(encoding="utf-8")
        parsed = yaml.safe_load(content)
        assert parsed["mods"]["my-mod"]["api_key"] == "secret123"

        # Verify read
        assert storage.get_mod_secrets("my-mod")["api_key"] == "secret123"

    def test_auto_migration_from_json(self, tmp_path: Path):
        sec_json = tmp_path / "secrets.json"
        import json
        sec_json.write_text(json.dumps({"mods": {"migrated-mod": {"token": "secret_token"}}}), encoding="utf-8")
        assert sec_json.exists()

        sec_yaml = tmp_path / "secrets.yaml"
        assert not sec_yaml.exists()

        storage = SecretsStorage(sec_yaml)
        data = storage.get_mod_secrets("migrated-mod")

        assert data.get("token") == "secret_token"
        assert sec_yaml.exists()
        assert not sec_json.exists()  # Old file deleted
        assert stat.S_IMODE(sec_yaml.stat().st_mode) == 0o600


class TestPersistentStrictFolderLayout:
    """Test strict_folder_layout persistence in config.yaml and CLI precedence."""

    def test_reads_from_config_yaml(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("mods:\n  strict_folder_layout: true\n", encoding="utf-8")

        val = ModValidator(repo_root=tmp_path, mods_dir=tmp_path, config_file=cfg, strict_folder_layout=None)
        assert val.strict_folder_layout is True

    def test_cli_flag_overrides_config(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("mods:\n  strict_folder_layout: true\n", encoding="utf-8")

        # Explicit False overrides config True
        val = ModValidator(repo_root=tmp_path, mods_dir=tmp_path, config_file=cfg, strict_folder_layout=False)
        assert val.strict_folder_layout is False

    def test_secrets_yaml_never_loaded_as_mod(self, tmp_path: Path):
        mods_dir = tmp_path / "mods"
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir(parents=True)

        sec_file = custom_dir / "secrets.yaml"
        sec_file.write_text("mods:\n  test:\n    key: secret\n", encoding="utf-8")

        cfg_file = custom_dir / "config.yaml"
        cfg_file.write_text("mods:\n  test: true\n", encoding="utf-8")

        normal_mod = custom_dir / "normal.yaml"
        normal_mod.write_text("id: normal\nname: Normal Mod\nenabled: true\n", encoding="utf-8")

        loaded = ModLoader.load_all(mods_dir)
        loaded_ids = [m.id for m in loaded]
        assert "normal" in loaded_ids
        assert "secrets" not in loaded_ids
        assert "config" not in loaded_ids

    def test_validator_rejects_mod_bundling_secrets(self, tmp_path: Path):
        mod_dir = tmp_path / "custom" / "leak-mod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "mod.yaml").write_text("id: leak-mod\nname: Leak\nenabled: true\n", encoding="utf-8")
        (mod_dir / "secrets.yaml").write_text("token: leaked\n", encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="leak-mod")
        val = ModValidator(repo_root=tmp_path, mods_dir=tmp_path / "custom")
        issues = val.check_layout_and_capabilities(mod)
        errs = [i for i in issues if "forbidden 'secrets.yaml'" in i.message]
        assert len(errs) > 0


class TestUniversalDeclarativeI18n:
    """Test universal collocated i18n in folder and single-file mods, cache updates and core resolution."""

    def test_folder_mod_locales_directory_discovery(self, tmp_path: Path):
        mod_dir = tmp_path / "custom" / "my-folder-mod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "mod.yaml").write_text("id: my-folder-mod\nname: My Mod\nversion: 1.0.0\nenabled: true\n", encoding="utf-8")

        locales_dir = mod_dir / "locales"
        locales_dir.mkdir()
        (locales_dir / "en.yaml").write_text("name: English Mod\ndescription: English description\n", encoding="utf-8")
        (locales_dir / "ru.yaml").write_text("name: Русский Мод\ndescription: Русское описание\n", encoding="utf-8")
        (locales_dir / "ru.tui.yaml").write_text("status: Готово в TUI\n", encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="my-folder-mod")
        assert mod is not None
        assert mod.get_title("en") == "English Mod"
        assert mod.get_description("en") == "English description"
        assert mod.get_title("ru") == "Русский Мод"
        assert mod.get_description("ru") == "Русское описание"
        assert "ru" in mod.i18n
        assert "en" in mod.i18n
        assert "tui" in mod.i18n_surfaces
        assert "ru" in mod.i18n_surfaces["tui"]
        assert mod.i18n_surfaces["tui"]["ru"]["status"] == "Готово в TUI"

    def test_folder_mod_i18n_directory_discovery(self, tmp_path: Path):
        mod_dir = tmp_path / "custom" / "my-i18n-folder-mod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "mod.yaml").write_text("id: my-i18n-folder-mod\nname: My i18n Mod\nversion: 1.0.0\nenabled: true\n", encoding="utf-8")

        i18n_dir = mod_dir / "i18n"
        i18n_dir.mkdir()
        (i18n_dir / "en.yaml").write_text("name: English i18n Mod\ndescription: English description\n", encoding="utf-8")
        (i18n_dir / "es.yaml").write_text("name: Mod Español\ndescription: Descripción en español\n", encoding="utf-8")
        (i18n_dir / "zh.yaml").write_text("name: 中文Mod\ndescription: 中文描述\n", encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="my-i18n-folder-mod")
        assert mod is not None
        assert mod.get_title("en") == "English i18n Mod"
        assert mod.get_title("es") == "Mod Español"
        assert mod.get_description("es") == "Descripción en español"
        assert mod.get_title("zh") == "中文Mod"
        assert "es" in mod.i18n
        assert "zh" in mod.i18n

    def test_folder_mod_locales_propagates_to_commands_and_config(self, tmp_path: Path):
        mod_dir = tmp_path / "custom" / "cmd-cfg-mod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "mod.yaml").write_text("id: cmd-cfg-mod\nname: Cmd Cfg Mod\nenabled: true\n", encoding="utf-8")
        (mod_dir / "commands.yaml").write_text(yaml.dump([
            {"name": "inspect", "description": "Inspect system", "shell": "echo ok"}
        ]), encoding="utf-8")
        (mod_dir / "config.yaml").write_text(yaml.dump({
            "config": {
                "api_endpoint": {
                    "type": "string",
                    "title": "API Endpoint",
                    "description": "Remote endpoint URL"
                }
            }
        }), encoding="utf-8")

        locales_dir = mod_dir / "locales"
        locales_dir.mkdir()
        (locales_dir / "ru.yaml").write_text(yaml.dump({
            "commands": {
                "inspect": {
                    "description": "Инспекция системы мода"
                }
            },
            "config": {
                "api_endpoint": {
                    "title": "Эндпоинт API",
                    "description": "URL удаленного эндпоинта"
                }
            }
        }), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="cmd-cfg-mod")
        assert mod is not None
        assert len(mod.commands) == 1
        cmd = mod.commands[0]
        assert cmd.name == "inspect"
        assert cmd.get_description("en") == "Inspect system"
        assert cmd.get_description("ru") == "Инспекция системы мода"

        assert "api_endpoint" in mod.config
        cf = mod.config["api_endpoint"]
        assert cf.get_title("en") == "API Endpoint"
        assert cf.get_title("ru") == "Эндпоинт API"
        assert cf.get_description("en") == "Remote endpoint URL"
        assert cf.get_description("ru") == "URL удаленного эндпоинта"

    def test_compile_mod_locales_and_cache_generation(self, tmp_path: Path):
        from engine.loader import compile_mod_locales, update_locales_cache
        mod_dir = tmp_path / "custom" / "cached-mod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "mod.yaml").write_text("id: cached-mod\nname: Cached Mod\nenabled: true\n", encoding="utf-8")
        (mod_dir / "commands.yaml").write_text(yaml.dump([
            {"name": "ping", "description": "Send ping", "shell": "echo pong"}
        ]), encoding="utf-8")
        (mod_dir / "locales").mkdir()
        (mod_dir / "locales" / "ru.yaml").write_text(yaml.dump({
            "description": "Мод с кэшированием",
            "commands": {"ping": {"description": "Отправить пинг"}},
            "custom": {"notice": "Важное оповещение"}
        }), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="cached-mod")
        assert mod is not None

        compiled = compile_mod_locales([mod])
        assert "ru" in compiled
        ru_cat = compiled["ru"]["core"]
        assert ru_cat["mods.cached-mod.name"] == "Cached Mod"
        assert ru_cat["mods.cached-mod.description"] == "Мод с кэшированием"
        assert ru_cat["slash.ping.description"] == "Отправить пинг"
        assert ru_cat["custom.notice"] == "Важное оповещение"

        cache_path = update_locales_cache([mod], tmp_path)
        assert cache_path.is_file()
        import json
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        assert "ru" in data["catalogs"]
        assert data["_meta"]["active_mods_count"] == 1
        assert "ru" in data["_meta"]["languages"]

    def test_core_override_and_custom_keys_resolution_with_highest_priority(self, tmp_path: Path, monkeypatch):
        from agent import i18n
        from engine.loader import update_locales_cache

        mods_dir = tmp_path / "mods"
        custom_dir = mods_dir / "custom" / "override-mod"
        custom_dir.mkdir(parents=True)
        (custom_dir / "mod.yaml").write_text("id: override-mod\nname: Override Mod\nenabled: true\n", encoding="utf-8")
        loc_dir = custom_dir / "locales"
        loc_dir.mkdir()
        (loc_dir / "ru.yaml").write_text(yaml.dump({
            "gateway": {
                "status": {
                    "threshold": "Тестовый порог автосжатия"
                }
            },
            "subscription": {
                "limit_notice": "Внимание: подписочный лимит исчерпан!"
            }
        }), encoding="utf-8")

        mod = parse_mod_manifest(custom_dir / "mod.yaml", mod_dir=custom_dir, fallback_id="override-mod")
        assert mod is not None

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_MODS_DIR", str(mods_dir))
        update_locales_cache([mod], mods_dir)
        i18n.reset_language_cache()

        # 1. Custom mod key is accessible globally
        assert i18n.t("subscription.limit_notice", lang="ru") == "Внимание: подписочный лимит исчерпан!"

        # 2. Existing Hermes core key is smoothly overridden with highest priority without touching core files
        assert i18n.t("gateway.status.threshold", lang="ru") == "Тестовый порог автосжатия"

    def test_validator_accepts_valid_locales_and_warns_invalid(self, tmp_path: Path):
        mod_dir = tmp_path / "custom" / "valid-loc-mod"
        mod_dir.mkdir(parents=True)
        (mod_dir / "mod.yaml").write_text("id: valid-loc-mod\nname: Valid Mod\nenabled: true\n", encoding="utf-8")
        loc_dir = mod_dir / "locales"
        loc_dir.mkdir()
        (loc_dir / "ru.yaml").write_text("name: Тест\n", encoding="utf-8")
        (loc_dir / "en.yaml").write_text("name: Test\n", encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="valid-loc-mod")
        val = ModValidator(repo_root=tmp_path, mods_dir=tmp_path / "custom")
        issues = val.check_layout_and_capabilities(mod)
        loc_issues = [i for i in issues if i.category == "i18n"]
        assert len(loc_issues) == 0

        # Add stray non-locale file
        (loc_dir / "notes.txt").write_text("some notes\n", encoding="utf-8")
        mod_with_notes = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="valid-loc-mod")
        issues_warn = val.check_layout_and_capabilities(mod_with_notes)
        warns = [i for i in issues_warn if i.category == "i18n" and "unrecognized locale file" in i.message]
        assert len(warns) == 1

