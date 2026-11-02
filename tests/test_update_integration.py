"""Tests for mod-update-integration system mod."""
from __future__ import annotations

from pathlib import Path
import pytest

from engine.loader import ModLoader, parse_mod_manifest
from engine.validator import ModValidator


def test_update_integration_manifest_structure(repo_root: Path):
    """Verify mod-update-integration declaration in system/."""
    mod_file = repo_root / "system" / "mod-update-integration.yaml"
    assert mod_file.exists(), f"Missing {mod_file}"

    mod = parse_mod_manifest(mod_file, fallback_id="mod-update-integration")
    assert mod is not None
    assert mod.id == "mod-update-integration"
    assert mod.author.strip().lower() == "engine"
    assert mod.enabled is True
    assert len(mod.targets) == 2

    target_files = [t.file for t in mod.targets]
    assert "gateway/slash_commands.py" in target_files
    assert "hermes_cli/update_cmd_maint.py" in target_files

    # Check that runner.py apply is called in both new_blocks
    for t in mod.targets:
        assert "runner.py" in t.new_block and "apply" in t.new_block


def test_update_integration_ast_and_syntax(repo_root: Path):
    """Verify that mod-update-integration passes static AST checks."""
    mod_file = repo_root / "system" / "mod-update-integration.yaml"
    mod = parse_mod_manifest(mod_file, fallback_id="mod-update-integration")
    val = ModValidator(mods_dir=repo_root)
    res = val.validate_mod(mod)
    assert res.valid is True
    assert len(res.errors) == 0


def test_update_integration_simulation_on_mock_files(tmp_path: Path, repo_root: Path):
    """Simulate applying mod-update-integration onto mock Hermes files."""
    mod_file = repo_root / "system" / "mod-update-integration.yaml"
    mod = parse_mod_manifest(mod_file, fallback_id="mod-update-integration")

    # Create mock target files matching old_block
    slash_file = tmp_path / "gateway" / "slash_commands.py"
    slash_file.parent.mkdir(parents=True, exist_ok=True)
    t0 = next(t for t in mod.targets if t.file == "gateway/slash_commands.py")
    slash_file.write_text(f"def _spawn_detached_update():\n{t0.old_block}\n", encoding="utf-8")

    maint_file = tmp_path / "hermes_cli" / "update_cmd_maint.py"
    maint_file.parent.mkdir(parents=True, exist_ok=True)
    t1 = next(t for t in mod.targets if t.file == "hermes_cli/update_cmd_maint.py")
    maint_file.write_text(f"def _run_post_update_maintenance():\n{t1.old_block}\n", encoding="utf-8")

    val = ModValidator(repo_root=tmp_path, mods_dir=repo_root)
    res = val.validate_mod(mod)
    assert res.valid is True
    assert len(res.errors) == 0
