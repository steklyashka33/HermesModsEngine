"""Tests for runner.py CLI commands: info, commands, create, and --lang support."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict
import pytest
import yaml

MODS_DIR = Path(__file__).resolve().parent.parent
ENGINE_DIR = MODS_DIR / ".engine"
if str(MODS_DIR) not in sys.path:
    sys.path.insert(0, str(MODS_DIR))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from runner import resolve_language, is_system_mod, main, ModLoader


class TestLanguageResolution:
    """Tests for resolve_language supporting any ISO code and auto-detection."""

    def test_explicit_language_codes_normalized(self):
        assert resolve_language("en") == "en"
        assert resolve_language("RU") == "ru"
        assert resolve_language("  es  ") == "es"
        assert resolve_language("de") == "de"
        assert resolve_language("zh-cn") == "zh-cn"
        assert resolve_language("pt-br") == "pt-br"
        assert resolve_language("tr") == "tr"

    def test_auto_from_config_yaml(self, tmp_path: Path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("display:\n  language: es\n", encoding="utf-8")
        assert resolve_language("auto", config_path=cfg) == "es"

        cfg.write_text("language: de\n", encoding="utf-8")
        assert resolve_language("auto", config_path=cfg) == "de"

    def test_auto_from_environment(self, tmp_path: Path, monkeypatch):
        non_existent_cfg = tmp_path / "non_existent.yaml"
        monkeypatch.setenv("HERMES_LANGUAGE", "fr")
        assert resolve_language("auto", config_path=non_existent_cfg) == "fr"

    def test_fallback_to_ru(self, tmp_path: Path, monkeypatch):
        non_existent_cfg = tmp_path / "non_existent.yaml"
        monkeypatch.delenv("HERMES_LANGUAGE", raising=False)
        assert resolve_language("auto", config_path=non_existent_cfg) == "ru"
        assert resolve_language(None, config_path=non_existent_cfg) == "ru"
        assert resolve_language("", config_path=non_existent_cfg) == "ru"


@pytest.fixture
def sample_mods_env(tmp_path: Path):
    """Sets up an isolated mods environment with system/ and custom/ mods."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    system_dir = mods_dir / "system"
    system_dir.mkdir(parents=True, exist_ok=True)
    custom_dir = mods_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    # 1. System mod (should be hidden in info catalog)
    sys_mod = system_dir / "mod-sys-core.yaml"
    sys_mod.write_text(yaml.dump({
        "id": "mod-sys-core",
        "name": "System Core Module",
        "author": "Engine",
        "version": "1.0.0",
        "enabled": True,
        "description": "System core functionality",
    }), encoding="utf-8")

    # 2. Custom enabled mod with i18n and slash command
    custom_mod1 = custom_dir / "demo-active.yaml"
    custom_mod1.write_text(yaml.dump({
        "id": "demo-active",
        "name": "Demo Active Mod",
        "author": "Hermes Admin",
        "version": "1.5.0",
        "enabled": True,
        "description": "English description for active mod",
        "i18n": {
            "ru": {
                "name": "Демо Активный Мод",
                "description": "Русское описание активного мода"
            },
            "es": {
                "name": "Mod Demostrativo",
                "description": "Descripción en español"
            }
        },
        "config": {
            "max_retries": {
                "type": "int",
                "default": 3,
                "description": "Maximum retry count",
                "i18n": {
                    "ru": {"description": "Максимальное число повторов"}
                }
            },
            "api_secret": {
                "type": "string",
                "sensitive": True,
                "default": "default_secret",
                "description": "Secret token"
            }
        },
        "commands": [
            {
                "name": "demo-cmd",
                "aliases": ["dcmd", "demo"],
                "args_hint": "[query]",
                "category": "Testing",
                "busy_policy": "reject",
                "description": "Run demo command",
                "i18n": {
                    "ru": {"description": "Запустить демо-команду"}
                }
            }
        ],
        "targets": [
            {
                "file": "agent/some_file.py",
                "description": "Core hook patch",
                "old_block": "# old",
                "new_block": "# new",
                "disabled": True,
                "disabled_reason": "Disabled in test"
            }
        ],
        "tests": {
            "test_sanity": "assert True"
        }
    }), encoding="utf-8")

    # 3. Custom disabled mod
    custom_mod2 = custom_dir / "demo-disabled.yaml"
    custom_mod2.write_text(yaml.dump({
        "id": "demo-disabled",
        "name": "Demo Disabled Mod",
        "author": "User",
        "version": "0.9.0",
        "enabled": False,
        "description": "English description for disabled mod",
        "i18n": {
            "ru": {
                "name": "Демо Отключенный Мод",
                "description": "Русское описание отключенного мода"
            }
        }
    }), encoding="utf-8")

    return mods_dir


class TestCliInfoCommand:
    """Tests for runner.py info without args and with mod_id."""

    def test_info_catalog_excludes_system_mods_and_uses_clean_format(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info",
            "--mods-dir", str(sample_mods_env),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out

        # System mod must NOT be present
        assert "mod-sys-core" not in captured
        assert "System Core Module" not in captured

        # Custom active mod in Russian
        assert "• Демо Активный Мод — Русское описание активного мода" in captured
        # Custom disabled mod in Russian with marker
        assert "• Демо Отключенный Мод [ОТКЛЮЧЕН] — Русское описание отключенного мода" in captured

        # Strict requirement: NO technical (id) in parentheses in catalog output
        assert "(demo-active)" not in captured
        assert "(demo-disabled)" not in captured

    def test_info_catalog_english_marker(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info",
            "--mods-dir", str(sample_mods_env),
            "--lang", "en"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out

        assert "• Demo Active Mod — English description for active mod" in captured
        assert "• Demo Disabled Mod [DISABLED] — English description for disabled mod" in captured
        assert "(demo-active)" not in captured

    def test_info_catalog_json(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info",
            "--mods-dir", str(sample_mods_env),
            "--json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        mod_ids = [m["id"] for m in data]
        assert "mod-sys-core" not in mod_ids
        assert "demo-active" in mod_ids
        assert "demo-disabled" in mod_ids

    def test_info_detailed_card_clean_noise_free(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "demo-active",
            "--mods-dir", str(sample_mods_env),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out

        # Header
        assert "Мод: Демо Активный Мод (demo-active) v1.5.0" in captured
        assert "Автор: Hermes Admin" in captured
        assert "Тип: Однофайловый" in captured

        # Normal mod must NOT print status line
        assert "Статус:" not in captured
        assert "Работает" not in captured

        # Description
        assert "Русское описание активного мода" in captured

        # Targets count only, NO core file paths in stdout
        assert "Цели: 1" in captured
        assert "agent/some_file.py" not in captured

        # Commands section
        assert "Команды:" in captured
        assert "/demo-cmd [query] (псевдонимы: /dcmd, /demo) — Запустить демо-команду" in captured

        # Config section with masked secret
        assert "Конфигурация:" in captured
        assert "max_retries (int, default: 3, текущее: 3) — Максимальное число повторов" in captured
        assert "api_secret (string, default: default_secret, секрет, текущее: ||********||)" in captured

        # Tests section
        assert "Тесты:" in captured
        assert "1 (inline tests)" in captured

    def test_info_detailed_card_disabled_shows_status(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "demo-disabled",
            "--mods-dir", str(sample_mods_env),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out
        assert "Статус: [ОТКЛЮЧЕН]" in captured

    def test_info_detailed_card_json_includes_targets_and_metadata(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "demo-active",
            "--mods-dir", str(sample_mods_env),
            "--json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["id"] == "demo-active"
        assert data["version"] == "1.5.0"
        assert data["targets_count"] == 1
        # Targets are preserved in JSON
        assert len(data["targets"]) == 1
        assert data["targets"][0]["file"] == "agent/some_file.py"
        # Commands
        assert len(data["commands"]) == 1
        assert data["commands"][0]["name"] == "demo-cmd"
        # Config masked
        assert data["config"]["api_secret"]["value"] == "||********||"
        # Tests
        assert data["tests"]["count"] == 1
        assert data["tests"]["method"] == "inline tests"

    def test_info_nonexistent_mod_fails(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "does-not-exist",
            "--mods-dir", str(sample_mods_env)
        ])
        ret = main()
        assert ret == 1
        err = capsys.readouterr().err
        assert "Мод 'does-not-exist' не найден" in err


class TestCliCommandsCommand:
    """Tests for runner.py commands table and JSON."""

    def test_commands_table_columns_and_localization(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "commands",
            "--mods-dir", str(sample_mods_env),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out

        # Header check
        assert "КОМАНДА" in captured
        assert "МОД" in captured
        assert "КАТЕГОРИЯ" in captured
        assert "РЕЖИМ" in captured
        assert "АРГУМЕНТЫ" in captured
        assert "ОПИСАНИЕ" in captured

        # Row check
        assert "/demo-cmd" in captured
        assert "demo-active" in captured
        assert "Testing" in captured
        assert "reject" in captured
        assert "[query]" in captured
        assert "Запустить демо-команду" in captured

    def test_commands_table_english(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "commands",
            "--mods-dir", str(sample_mods_env),
            "--lang", "en"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out
        assert "Run demo command" in captured

    def test_commands_json(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "commands",
            "--mods-dir", str(sample_mods_env),
            "--json"
        ])
        ret = main()
        assert ret == 0
        cmds = json.loads(capsys.readouterr().out)
        assert len(cmds) == 1
        assert cmds[0]["name"] == "demo-cmd"
        assert cmds[0]["category"] == "Testing"
        assert cmds[0]["busy_policy"] == "reject"


class TestCliCreateCommand:
    """Tests for runner.py create scaffolding generator."""

    def test_create_single_mod_scaffolding(self, tmp_path: Path, monkeypatch, capsys):
        mods_dir = tmp_path / "mods"
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "create", "test-scaffold-single",
            "--type", "single",
            "--mods-dir", str(mods_dir)
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out
        assert "✓ Мод 'test-scaffold-single' успешно создан" in captured
        assert "(тип: single)" in captured

        target_file = mods_dir / "custom" / "test-scaffold-single.yaml"
        assert target_file.is_file()

        # Load and verify structure
        mod = ModLoader.load_all(mods_dir)[0]
        assert mod.id == "test-scaffold-single"
        assert mod.name == "Test Scaffold Single"
        assert mod.is_folder is False
        assert len(mod.config) == 2
        assert len(mod.commands) == 1
        assert len(mod.inline_tests) == 1

    def test_create_folder_mod_scaffolding(self, tmp_path: Path, monkeypatch, capsys):
        mods_dir = tmp_path / "mods"
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "create", "test-scaffold-folder",
            "--type", "folder",
            "--mods-dir", str(mods_dir)
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out
        assert "✓ Мод 'test-scaffold-folder' успешно создан" in captured
        assert "(тип: folder)" in captured

        folder = mods_dir / "custom" / "test-scaffold-folder"
        assert (folder / "mod.yaml").is_file()
        assert (folder / "config.yaml").is_file()
        assert (folder / "commands.yaml").is_file()
        assert (folder / "tests" / "test_test_scaffold_folder.py").is_file()

        # Load and verify structure
        mod = ModLoader.load_all(mods_dir)[0]
        assert mod.id == "test-scaffold-folder"
        assert mod.name == "Test Scaffold Folder"
        assert mod.is_folder is True
        assert len(mod.config) == 2
        assert len(mod.commands) == 1
        assert len(mod.test_files) == 1

    def test_create_already_exists_error(self, tmp_path: Path, monkeypatch, capsys):
        mods_dir = tmp_path / "mods"
        # First creation
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "create", "dupe-mod",
            "--type", "single",
            "--mods-dir", str(mods_dir)
        ])
        assert main() == 0

        # Second creation must fail
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "create", "dupe-mod",
            "--type", "single",
            "--mods-dir", str(mods_dir)
        ])
        ret = main()
        assert ret == 1
        err = capsys.readouterr().err
        assert "уже существует" in err

    def test_create_invalid_id_error(self, tmp_path: Path, monkeypatch, capsys):
        mods_dir = tmp_path / "mods"
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "create", "Bad Identifier!",
            "--mods-dir", str(mods_dir)
        ])
        ret = main()
        assert ret == 1
        err = capsys.readouterr().err
        assert "некорректный mod_id" in err


class TestListLanguageSupport:
    """Test runner.py list with --lang parameter."""

    def test_list_uses_localized_mod_title(self, sample_mods_env: Path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(sample_mods_env),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out
        assert "Демо Активный Мод" in captured

        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(sample_mods_env),
            "--lang", "en"
        ])
        ret = main()
        assert ret == 0
        captured = capsys.readouterr().out
        assert "Demo Active Mod" in captured
