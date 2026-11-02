"""Tests for runner.py CLI commands: list, info, status, logs with text and JSON formats."""
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

from runner import main, ModLoader, ModRunner, ModInspector


@pytest.fixture
def mock_inspection_env(tmp_path: Path, monkeypatch):
    """Sets up an isolated environment with sample mods, config, and log files."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir(parents=True, exist_ok=True)
    mods_dir = hermes_home / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    custom_dir = mods_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    system_dir = mods_dir / "system"
    system_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = hermes_home / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    config_file = hermes_home / "config.yaml"
    secrets_file = hermes_home / "secrets.yaml"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_ROOT", str(repo_dir))
    monkeypatch.setenv("HERMES_CONFIG", str(config_file))
    monkeypatch.setenv("HERMES_SECRETS", str(secrets_file))
    monkeypatch.setenv("HERMES_LOG_DIR", str(logs_dir))
    monkeypatch.setenv("HERMES_NO_JOURNAL", "1")

    # Create dummy core file for targets
    target_file = repo_dir / "agent" / "sample.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("def sample():\n    # NEW_BLOCK_INSTALLED\n    pass\n", encoding="utf-8")

    # Mod 1: Applied custom mod with targets, dependencies, config
    mod1 = custom_dir / "mod-alpha.yaml"
    mod1.write_text(yaml.dump({
        "id": "mod-alpha",
        "name": "Mod Alpha",
        "author": "Alice",
        "version": "1.2.0",
        "enabled": True,
        "description": "Alpha mod description",
        "i18n": {
            "ru": {
                "name": "Мод Альфа",
                "description": "Русское описание Альфа"
            }
        },
        "dependencies": ["mod-core"],
        "targets": [
            {
                "file": "agent/sample.py",
                "old_block": "# OLD",
                "new_block": "# NEW_BLOCK_INSTALLED",
                "description": "Patches sample.py"
            }
        ],
        "config": {
            "rate_limit": {
                "type": "int",
                "default": 10,
                "description": "Rate limit setting"
            }
        },
        "commands": [
            {
                "name": "alpha",
                "aliases": ["al"],
                "args_hint": "[arg]",
                "description": "Run alpha command"
            }
        ]
    }), encoding="utf-8")

    # Mod 2: Disabled custom mod
    mod2 = custom_dir / "mod-beta.yaml"
    mod2.write_text(yaml.dump({
        "id": "mod-beta",
        "name": "Mod Beta",
        "author": "Bob",
        "version": "2.0.0",
        "enabled": False,
        "description": "Beta mod description (disabled)",
        "i18n": {
            "ru": {
                "name": "Мод Бета",
                "description": "Русское описание Бета"
            }
        }
    }), encoding="utf-8")

    # Mod 3: System core mod
    mod3 = system_dir / "mod-core.yaml"
    mod3.write_text(yaml.dump({
        "id": "mod-core",
        "name": "Core System Mod",
        "author": "Engine",
        "version": "1.0.0",
        "enabled": True,
        "description": "Core engine component"
    }), encoding="utf-8")

    # Create config.yaml and secrets.yaml (0600)
    config_file.write_text(yaml.dump({"mods": {"enabled": True}}), encoding="utf-8")
    secrets_file.write_text("{}", encoding="utf-8")
    os.chmod(secrets_file, 0o600)

    # Create synthetic log files with entries for mod-alpha and mod-beta
    gateway_log = logs_dir / "gateway.log"
    gateway_log.write_text(
        "2026-10-30 10:00:00 INFO gateway: Starting gateway\n"
        "2026-10-30 10:01:00 INFO gateway: Loaded mod-alpha successfully\n"
        "2026-10-30 10:02:00 WARNING gateway: Executing /alpha command for user 123\n"
        "2026-10-30 10:03:00 ERROR gateway: Mod-alpha minor warning\n",
        encoding="utf-8"
    )

    return {
        "mods_dir": mods_dir,
        "repo_dir": repo_dir,
        "logs_dir": logs_dir,
        "config_file": config_file,
        "secrets_file": secrets_file,
    }


class TestCliListCommand:
    """Tests for 'runner.py list [--format text|json] [--status applied|disabled|all]'."""

    def test_list_text_default_all(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--lang", "en",
        ])
        ret = main()
        assert ret == 0
        out = capsys.readouterr().out
        assert "Hermes Mods" in out
        assert "Mod Alpha v1.2.0 (mod-alpha)" in out
        assert "Mod Beta v2.0.0 (mod-beta)" in out
        assert "Core System Mod v1.0.0 (mod-core)" in out
        assert "Alpha mod description" in out

    def test_list_format_json(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--format", "json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        assert len(data) == 3
        ids = [m["id"] for m in data]
        assert "mod-alpha" in ids
        assert "mod-beta" in ids
        assert "mod-core" in ids

        alpha = next(m for m in data if m["id"] == "mod-alpha")
        assert alpha["version"] == "1.2.0"
        assert alpha["author"] == "Alice"
        assert alpha["description_en"] == "Alpha mod description"
        assert alpha["status"] == "applied"

    def test_list_json_flag(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert isinstance(data, list)
        assert len(data) == 3

    def test_list_status_filter_applied(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--status", "applied",
            "--format", "json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        ids = [m["id"] for m in data]
        assert "mod-alpha" in ids
        assert "mod-core" in ids
        assert "mod-beta" not in ids

    def test_list_status_filter_disabled(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--status", "disabled",
            "--format", "json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert len(data) == 1
        assert data[0]["id"] == "mod-beta"
        assert data[0]["status"] == "disabled"

    def test_list_localization(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "list",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        out = capsys.readouterr().out
        assert "Мод Альфа" in out
        assert "Русское описание Альфа" in out


class TestCliInfoCommand:
    """Tests for 'runner.py info <mod_id>'."""

    def test_info_text_format(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "mod-alpha",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--lang", "ru"
        ])
        ret = main()
        assert ret == 0
        out = capsys.readouterr().out
        assert "Мод: Мод Альфа (mod-alpha) v1.2.0" in out
        assert "Автор: Alice" in out
        assert "Русское описание Альфа" in out
        assert "Зависимости:" in out
        assert "mod-core" in out
        assert "Цели: 1" in out
        assert "Команды:" in out
        assert "/alpha" in out
        assert "Конфигурация:" in out
        assert "rate_limit" in out

    def test_info_json_format(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "mod-alpha",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--format", "json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["id"] == "mod-alpha"
        assert data["version"] == "1.2.0"
        assert data["author"] == "Alice"
        assert data["dependencies"] == ["mod-core"]
        assert len(data["targets"]) == 1
        assert data["targets"][0]["file"] == "agent/sample.py"
        assert "rate_limit" in data["config"]

    def test_info_nonexistent_mod(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "info", "unknown-mod",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
        ])
        ret = main()
        assert ret == 1
        err = capsys.readouterr().err
        assert "не найден" in err


class TestCliStatusCommand:
    """Tests for 'runner.py status'."""

    def test_status_text(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "status",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
        ])
        ret = main()
        assert ret == 0
        out = capsys.readouterr().out
        assert "Hermes Mods Ecosystem Status" in out
        assert "Overall Health:" in out
        assert "Master Killswitch:" in out
        assert "Total registered: 3" in out
        assert "Applied & Active: 2" in out
        assert "Disabled:         1" in out
        assert "Runtime Subsystems:" in out
        assert "System Integrity:" in out

    def test_status_format_json(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "status",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--format", "json"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["overall_status"] == "HEALTHY"
        assert data["healthy"] is True
        assert data["counts"]["total"] == 3
        assert data["counts"]["applied"] == 2
        assert data["counts"]["disabled"] == 1
        assert "mod-alpha" in data["mod_ids"]["applied"]
        assert "mod-beta" in data["mod_ids"]["disabled"]
        assert data["integrity"]["dag_clean"] is True


class TestCliLogsCommand:
    """Tests for 'runner.py logs <mod_id> [--lines N]'."""

    def test_logs_missing_target(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "logs",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
        ])
        ret = main()
        assert ret == 1
        err = capsys.readouterr().err
        assert "Укажите mod_id для просмотра логов" in err

    def test_logs_nonexistent_mod(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "logs", "unknown-mod",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
        ])
        ret = main()
        assert ret == 1
        err = capsys.readouterr().err
        assert "не найден" in err

    def test_logs_text_and_custom_lines(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "logs", "mod-alpha",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--lines", "2",
            "--lang", "en"
        ])
        ret = main()
        assert ret == 0
        out = capsys.readouterr().out
        assert "mod-alpha" in out
        assert "Mod-alpha minor warning" in out

    def test_logs_format_json(self, mock_inspection_env: Dict[str, Path], monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "runner.py", "logs", "mod-alpha",
            "--mods-dir", str(mock_inspection_env["mods_dir"]),
            "--format", "json",
            "--lines", "5"
        ])
        ret = main()
        assert ret == 0
        data = json.loads(capsys.readouterr().out)
        assert data["mod_id"] == "mod-alpha"
        assert data["lines_count"] >= 1
        assert any("mod-alpha" in l.lower() or "/alpha" in l for l in data["lines"])
