"""Tests for single-file vs folder mod layout, modular extraction, and strict_folder_layout flag."""
from __future__ import annotations

from pathlib import Path
import yaml
import pytest

from engine.models import Mod, ModTarget
from engine.loader import ModLoader, parse_mod_manifest
from engine.validator import ModValidator


def test_folder_mod_modular_targets_yaml_and_yml(tmp_path: Path):
    """Test folder mod loading targets from targets.yaml and targets.yml."""
    mod_dir = tmp_path / "mod-targets-yaml"
    mod_dir.mkdir()
    (mod_dir / "mod.yaml").write_text("id: mod-targets-yaml\nname: Mod Targets YAML\n", encoding="utf-8")
    
    # 1. targets.yaml
    (mod_dir / "targets.yaml").write_text(yaml.dump([
        {"file": "a.py", "old_block": "a = 1", "new_block": "a = 2", "description": "from targets.yaml"}
    ]), encoding="utf-8")
    
    mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="mod-targets-yaml")
    assert mod is not None
    assert mod.is_folder is True
    assert len(mod.targets) == 1
    assert mod.targets[0].description == "from targets.yaml"
    assert mod.has_inline_targets is False

    # 2. targets.yml in another mod
    mod_dir_yml = tmp_path / "mod-targets-yml"
    mod_dir_yml.mkdir()
    (mod_dir_yml / "mod.yaml").write_text("id: mod-targets-yml\nname: Mod Targets YML\n", encoding="utf-8")
    (mod_dir_yml / "targets.yml").write_text(yaml.dump([
        {"file": "b.py", "old_block": "b = 1", "new_block": "b = 2", "description": "from targets.yml"}
    ]), encoding="utf-8")

    mod_yml = parse_mod_manifest(mod_dir_yml / "mod.yaml", mod_dir=mod_dir_yml, fallback_id="mod-targets-yml")
    assert mod_yml is not None
    assert len(mod_yml.targets) == 1
    assert mod_yml.targets[0].description == "from targets.yml"


def test_strict_folder_layout_toggle(tmp_path: Path):
    """Test strict_folder_layout flag enforcement on folder mods."""
    repo_file = tmp_path / "test_file.py"
    repo_file.write_text("orig = 1\n", encoding="utf-8")

    # Mod with inline targets in mod.yaml
    inline_mod_dir = tmp_path / "inline-folder-mod"
    inline_mod_dir.mkdir()
    inline_manifest = inline_mod_dir / "mod.yaml"
    inline_manifest.write_text(yaml.dump({
        "id": "inline-folder-mod",
        "name": "Inline Folder Mod",
        "targets": [
            {"file": "test_file.py", "old_block": "orig = 1", "new_block": "orig = 2"}
        ],
        "commands": [
            {"name": "testcmd", "python": "print(1)"}
        ]
    }), encoding="utf-8")

    mod_inline = parse_mod_manifest(inline_manifest, mod_dir=inline_mod_dir, fallback_id="inline-folder-mod")
    assert mod_inline is not None
    assert mod_inline.has_inline_targets is True
    assert mod_inline.has_inline_commands is True

    # When strict_folder_layout is False (default): validation PASSES
    val_lenient = ModValidator(repo_root=tmp_path, mods_dir=tmp_path, strict_folder_layout=False)
    res_lenient = val_lenient.validate_mod(mod_inline)
    assert res_lenient.valid is True
    assert len([e for e in res_lenient.errors if e.category == "mod_layout"]) == 0

    # When strict_folder_layout is True: validation FAILS on inline sections
    val_strict = ModValidator(repo_root=tmp_path, mods_dir=tmp_path, strict_folder_layout=True)
    res_strict = val_strict.validate_mod(mod_inline)
    assert res_strict.valid is False
    layout_errors = [e for e in res_strict.errors if e.category == "mod_layout"]
    assert len(layout_errors) == 2  # inline targets and inline commands
    assert any("inline targets" in e.message for e in layout_errors)
    assert any("inline commands" in e.message for e in layout_errors)

    # Now create modular folder mod with targets/ and commands.yaml
    modular_mod_dir = tmp_path / "modular-folder-mod"
    modular_mod_dir.mkdir()
    (modular_mod_dir / "mod.yaml").write_text("id: modular-folder-mod\nname: Modular Folder Mod\n", encoding="utf-8")
    t_dir = modular_mod_dir / "targets"
    t_dir.mkdir()
    (t_dir / "feature.yaml").write_text(yaml.dump([
        {"file": "test_file.py", "old_block": "orig = 1", "new_block": "orig = 2"}
    ]), encoding="utf-8")
    (modular_mod_dir / "commands.yaml").write_text(yaml.dump([
        {"name": "modcmd", "python": "print(2)"}
    ]), encoding="utf-8")

    mod_modular = parse_mod_manifest(modular_mod_dir / "mod.yaml", mod_dir=modular_mod_dir, fallback_id="modular-folder-mod")
    assert mod_modular is not None
    assert mod_modular.has_inline_targets is False
    assert mod_modular.has_inline_commands is False

    # Modular mod passes even under strict_folder_layout
    res_modular_strict = val_strict.validate_mod(mod_modular)
    assert res_modular_strict.valid is True
    assert len([e for e in res_modular_strict.errors if e.category == "mod_layout"]) == 0


def test_single_file_mod_capability_boundaries(tmp_path: Path):
    """Test validator enforces that single-file mods cannot declare local assets (skills/scripts)."""
    # 1. Single-file mod attempting to bundle skills
    skills_single = tmp_path / "bad-skills-single.yaml"
    skills_single.write_text(yaml.dump({
        "id": "bad-skills-single",
        "name": "Bad Skills Single",
        "targets": [],
    }), encoding="utf-8")

    mod_single = parse_mod_manifest(skills_single, mod_dir=None, fallback_id="bad-skills-single")
    # Artificially assign skills_dir to simulate invalid declaration
    mod_single.skills_dir = tmp_path / "skills"

    val = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = val.validate_mod(mod_single)
    assert res.valid is False
    cap_errors = [e for e in res.errors if e.category == "mod_capability"]
    assert len(cap_errors) >= 1
    assert "Single-file mod 'bad-skills-single' cannot provide skills" in cap_errors[0].message

    # 2. Single-file mod with relative handler_script
    script_single = tmp_path / "bad-script-single.yaml"
    script_single.write_text(yaml.dump({
        "id": "bad-script-single",
        "name": "Bad Script Single",
        "targets": [],
        "commands": [
            {"name": "myscript", "handler_script": "scripts/handler.py"}
        ]
    }), encoding="utf-8")

    mod_script = parse_mod_manifest(script_single, mod_dir=None, fallback_id="bad-script-single")
    res_script = val.validate_mod(mod_script)
    assert res_script.valid is False
    script_errors = [e for e in res_script.errors if e.category == "mod_capability"]
    assert len(script_errors) >= 1
    assert "cannot provide local script files" in script_errors[0].message
