"""Tests for Hermes Mod Engine Loader, Manifest Parser, and Structured Discovery."""
from __future__ import annotations

from pathlib import Path
import yaml

from engine.models import ConfigField
from engine.loader import (
    ModLoader,
    parse_mod_manifest,
    interpolate_mcp_config,
)


def test_parse_single_file_mod(tmp_path: Path):
    """Verify single-file mod manifest parsing with all components."""
    manifest_file = tmp_path / "test-mod.yaml"
    manifest_data = {
        "id": "test-mod",
        "name": "Test Single Mod",
        "version": "1.2.3",
        "author": "Tester",
        "description": "Test description",
        "enabled": True,
        "dependencies": ["dep-a", "dep-b"],
        "config": {
            "retries": {
                "type": "int",
                "default": 3,
                "min": 1,
                "max": 10,
                "description": "Retry limit",
            },
            "token": {
                "type": "string",
                "sensitive": True,
                "required": True,
            },
        },
        "targets": [
            {
                "file": "sample.py",
                "old_block": "old = 1",
                "new_block": "new = 2",
                "description": "Sample target",
            }
        ],
        "commands": [
            {
                "name": "ping",
                "description": "Ping command",
                "python": "print('pong')",
                "aliases": ["p"],
            }
        ],
        "monitors": [
            {
                "id": "heartbeat",
                "command": "python3 ping.py",
                "interval": 15,
            }
        ],
        "mcp_servers": {
            "demo-mcp": {
                "command": "python3",
                "args": ["server.py"],
            }
        },
    }
    manifest_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

    mod = parse_mod_manifest(manifest_file, mod_dir=None, fallback_id="test-mod")
    assert mod is not None
    assert mod.id == "test-mod"
    assert mod.name == "Test Single Mod"
    assert mod.version == "1.2.3"
    assert mod.author == "Tester"
    assert mod.enabled is True
    assert mod.dependencies == ["dep-a", "dep-b"]
    assert len(mod.targets) == 1
    assert mod.targets[0].file == "sample.py"
    assert len(mod.config) == 2
    assert mod.config["retries"].default == 3
    assert mod.config["token"].sensitive is True
    # Backward compatibility checks
    assert len(mod.user_config) == 2
    assert mod.user_config["retries"].default == 3
    assert mod.user_config["token"].sensitive is True
    assert len(mod.commands) == 1
    assert mod.commands[0].name == "ping"
    assert mod.commands[0].aliases == ["p"]
    assert len(mod.monitors) == 1
    assert mod.monitors[0].id == "heartbeat"
    assert "demo-mcp" in mod.mcp_servers


def test_parse_folder_mod(tmp_path: Path):
    """Verify folder mod parsing with modular targets/, skills/, and commands/."""
    mod_dir = tmp_path / "sample-folder-mod"
    mod_dir.mkdir(parents=True, exist_ok=True)

    manifest_file = mod_dir / "mod.yaml"
    manifest_file.write_text(
        yaml.dump({
            "id": "sample-folder-mod",
            "name": "Sample Folder Mod",
            "version": "2.0.0",
            "enabled": True,
        }),
        encoding="utf-8",
    )

    # Modular targets
    targets_dir = mod_dir / "targets"
    targets_dir.mkdir(parents=True, exist_ok=True)
    (targets_dir / "patch1.yaml").write_text(
        yaml.dump([
            {"file": "a.py", "old_block": "a = 1", "new_block": "a = 2"}
        ]),
        encoding="utf-8",
    )
    (targets_dir / "patch2.yaml").write_text(
        yaml.dump([
            {"file": "b.py", "old_block": "b = 1", "new_block": "b = 2"}
        ]),
        encoding="utf-8",
    )

    # Skills directory
    skills_dir = mod_dir / "skills" / "my-skill"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Test\n---\n# Skill\n",
        encoding="utf-8",
    )

    mod = parse_mod_manifest(manifest_file, mod_dir=mod_dir, fallback_id="sample-folder-mod")
    assert mod is not None
    assert mod.id == "sample-folder-mod"
    assert len(mod.targets) == 2
    assert {t.file for t in mod.targets} == {"a.py", "b.py"}
    assert mod.skills_dir is not None
    assert mod.has_skills is True
    skills = mod.list_skills()
    assert len(skills) == 1
    assert skills[0].name == "SKILL.md"


def test_loader_structured_scan(tmp_path: Path):
    """Verify ModLoader scans system/ and custom/ while ignoring templates, docs, tests."""
    repo = tmp_path / "hermes-mods"
    repo.mkdir(parents=True, exist_ok=True)

    # system/ mod
    sys_dir = repo / "system"
    sys_dir.mkdir()
    (sys_dir / "sys-mod.yaml").write_text(
        yaml.dump({"id": "sys-mod", "name": "System Mod", "author": "Engine"}),
        encoding="utf-8",
    )

    # custom/ mod
    cust_dir = repo / "custom"
    cust_dir.mkdir()
    (cust_dir / "cust-mod.yaml").write_text(
        yaml.dump({"id": "cust-mod", "name": "Custom Mod", "author": "User"}),
        encoding="utf-8",
    )

    # Excluded directories
    for ignored in ("templates", "docs", "tests", "skills", ".engine"):
        ign_dir = repo / ignored
        ign_dir.mkdir()
        (ign_dir / "ignored-mod.yaml").write_text(
            yaml.dump({"id": f"ign-{ignored}", "name": "Should Be Ignored"}),
            encoding="utf-8",
        )

    mods = ModLoader.load_all(repo)
    mod_ids = [m.id for m in mods]
    assert "sys-mod" in mod_ids
    assert "cust-mod" in mod_ids
    assert not any(m.id.startswith("ign-") for m in mods)
    assert len(mods) == 2


def test_user_config_validation_and_coercion(tmp_path: Path):
    """Verify ConfigField validates and coerces different data types."""
    # Integer field with min/max
    int_field = ConfigField(name="count", type="int", min=1, max=10, default=5)
    ok, val, err = int_field.validate_value("7")
    assert ok is True and val == 7

    ok, val, err = int_field.validate_value(15)
    assert ok is False and "above maximum" in err

    # Boolean field
    bool_field = ConfigField(name="flag", type="bool", default=False)
    assert bool_field.validate_value("yes")[1] is True
    assert bool_field.validate_value("false")[1] is False
    assert bool_field.validate_value(1)[1] is True

    # Float field
    float_field = ConfigField(name="rate", type="float", min=0.1, max=1.0)
    ok, val, _ = float_field.validate_value("0.75")
    assert ok is True and val == 0.75

    # String with options
    opt_field = ConfigField(name="mode", type="string", options=["fast", "slow"])
    assert opt_field.validate_value("fast")[0] is True
    assert opt_field.validate_value("medium")[0] is False

    # Path fields
    file_path = tmp_path / "exist.txt"
    file_path.write_text("ok")
    file_field = ConfigField(name="fpath", type="file")
    assert file_field.validate_value(str(file_path))[0] is True
    assert file_field.validate_value(str(tmp_path / "missing.txt"))[0] is False


def test_interpolate_mcp_config():
    """Verify ${mod_dir}, ${config.key}, and legacy ${user_config.key} interpolation."""
    cfg = {
        "command": "python3",
        "args": ["${mod_dir}/server.py", "--token", "${config.api_key}"],
        "env": {"DIR": "${MOD_DIR}", "LEVEL": "${user_config.log_level}"},
    }
    interpolated = interpolate_mcp_config(
        cfg,
        mod_dir="/opt/mods/sample",
        config_values={"api_key": "secret123", "log_level": "DEBUG"},
    )
    assert interpolated["args"][0] == "/opt/mods/sample/server.py"
    assert interpolated["args"][2] == "secret123"
    assert interpolated["env"]["DIR"] == "/opt/mods/sample"
    assert interpolated["env"]["LEVEL"] == "DEBUG"


def test_legacy_user_config_manifest_compat(tmp_path: Path):
    """Ensure legacy 'user_config:' manifests load into mod.config and mod.user_config."""
    legacy_file = tmp_path / "legacy-mod.yaml"
    legacy_file.write_text(yaml.dump({
        "id": "legacy-mod",
        "name": "Legacy Mod",
        "user_config": {
            "timeout": {"type": "int", "default": 30}
        }
    }), encoding="utf-8")
    mod = parse_mod_manifest(legacy_file, mod_dir=None, fallback_id="legacy-mod")
    assert mod is not None
    assert "timeout" in mod.config
    assert "timeout" in mod.user_config
    assert mod.config["timeout"].default == 30


def test_malformed_yaml_tolerance(tmp_path: Path):
    """Ensure malformed or unreadable YAML does not crash loader."""
    bad_file = tmp_path / "broken.yaml"
    bad_file.write_text("key: [unclosed list", encoding="utf-8")
    assert parse_mod_manifest(bad_file, mod_dir=None, fallback_id="broken") is None

    empty_file = tmp_path / "empty.yaml"
    empty_file.write_text("", encoding="utf-8")
    assert parse_mod_manifest(empty_file, mod_dir=None, fallback_id="empty") is None
