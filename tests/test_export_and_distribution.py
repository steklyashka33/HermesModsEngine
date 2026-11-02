"""Comprehensive test suite for Hermes mod export, pack, distribution, and import subsystem.

Validates:
- runner.py export / pack CLI commands
- runner.py import CLI command
- Automatic exclusion of caches, logs, databases, git artifacts, and secrets
- Manifest validation before packaging
- Security against TarBomb / path traversal
- Output formatting and --json machine-readable responses
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / ".engine"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from engine.exporter import (
    ModExporter,
    ModImporter,
    should_exclude_path,
    check_file_for_secrets,
    is_system_mod,
)
from engine.loader import ModLoader
from engine.validator import ModValidator


@pytest.fixture
def mock_mods_workspace(tmp_path: Path) -> Dict[str, Path]:
    """Sets up an isolated mods directory with system and custom mods."""
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    system_dir = mods_dir / "system"
    system_dir.mkdir(parents=True, exist_ok=True)
    custom_dir = mods_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    # 1. System mod: mod-core-sys
    sys_mod_dir = system_dir / "mod-core-sys"
    sys_mod_dir.mkdir(parents=True, exist_ok=True)
    (sys_mod_dir / "mod.yaml").write_text(yaml.dump({
        "id": "mod-core-sys",
        "name": "System Core Mod",
        "author": "Engine",
        "version": "1.0.0",
        "enabled": True,
        "description": "Core system mod description",
    }), encoding="utf-8")
    (sys_mod_dir / "targets.yaml").write_text(yaml.dump([
        {
            "file": "hermes_cli/commands.py",
            "old_block": "# old",
            "new_block": "# new",
            "disabled": True,
            "disabled_reason": "test disabled",
        }
    ]), encoding="utf-8")

    # 2. Custom folder mod: custom-folder-mod
    custom_folder = custom_dir / "custom-folder-mod"
    custom_folder.mkdir(parents=True, exist_ok=True)
    (custom_folder / "mod.yaml").write_text(yaml.dump({
        "id": "custom-folder-mod",
        "name": "Custom Folder Mod",
        "author": "Tester",
        "version": "1.2.0",
        "enabled": True,
        "description": "User created folder mod",
    }), encoding="utf-8")
    (custom_folder / "targets.yaml").write_text(yaml.dump([
        {
            "file": "hermes_cli/commands.py",
            "old_block": "# tool old",
            "new_block": "# tool new",
            "disabled": True,
            "disabled_reason": "test disabled",
        }
    ]), encoding="utf-8")

    # 3. Custom single file mod: custom-single-mod.yaml
    custom_single = custom_dir / "custom-single-mod.yaml"
    custom_single.write_text(yaml.dump({
        "id": "custom-single-mod",
        "name": "Custom Single Mod",
        "author": "Tester",
        "version": "1.0.0",
        "enabled": True,
        "description": "User single file mod",
        "targets": [
            {
                "file": "hermes_cli/commands.py",
                "old_block": "# cfg old",
                "new_block": "# cfg new",
                "disabled": True,
                "disabled_reason": "test disabled",
            }
        ]
    }), encoding="utf-8")

    return {
        "root": tmp_path,
        "mods_dir": mods_dir,
        "system_dir": system_dir,
        "custom_dir": custom_dir,
        "custom_folder": custom_folder,
        "custom_single": custom_single,
    }


class TestExclusionFilters:
    """Tests for should_exclude_path ensuring no secrets, caches, or logs leak."""

    def test_cache_files_excluded(self):
        caches = [
            "__pycache__/foo.cpython-311.pyc",
            "custom/mod/__pycache__/bar.pyc",
            ".test_cache.json",
            "custom/mod/.test_cache.json",
            ".commands_cache.json",
            "custom/mod/..commands_cache.json.lock",
            "custom/mod/.pytest_cache/v/cache",
            ".ruff_cache/content",
        ]
        for c in caches:
            excluded, reason = should_exclude_path(c)
            assert excluded is True, f"Failed for {c}"

    def test_secret_files_excluded(self):
        secrets = [
            "secrets.yaml",
            "custom/mod/secrets.yaml",
            "custom/mod/secrets.yml",
            "custom/mod/secrets.json",
            "custom/mod/.secrets.yaml",
            "custom/mod/.env",
            "custom/mod/.env.production",
            "custom/mod/server.key",
            "custom/mod/cert.pem",
            "custom/mod/id_rsa",
            "custom/mod/id_ed25519",
        ]
        for s in secrets:
            excluded, reason = should_exclude_path(s)
            assert excluded is True, f"Failed for {s}: {reason}"

    def test_database_and_log_files_excluded(self):
        db_logs = [
            "custom/mod/local.db",
            "custom/mod/data.sqlite",
            "custom/mod/data.sqlite3",
            "custom/mod/app.log",
            "custom/mod/server.log.1",
            "custom/mod/file.tmp",
            "custom/mod/file.bak",
            "custom/mod/.DS_Store",
        ]
        for item in db_logs:
            excluded, reason = should_exclude_path(item)
            assert excluded is True, f"Failed for {item}: {reason}"

    def test_valid_mod_files_not_excluded(self):
        valid = [
            "custom/mod/mod.yaml",
            "custom/mod/targets.yaml",
            "custom/mod/config.yaml",
            "custom/mod/commands.yaml",
            "custom/mod/tests/test_sample.py",
            "custom/mod/skills/my_skill/SKILL.md",
            "custom/single-mod.yaml",
            "system/mod-registry/mod.yaml",
        ]
        for item in valid:
            excluded, reason = should_exclude_path(item)
            assert excluded is False, f"Unexpectedly excluded {item}: {reason}"


class TestModExporter:
    """Tests for ModExporter and export functionality."""

    def test_export_single_folder_mod(self, mock_mods_workspace: Dict[str, Path]):
        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "export_test.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            target_mod_id="custom-folder-mod",
        )

        assert result.success is True
        assert out_tar.is_file()
        assert result.mods_count == 1
        assert result.export_type == "single"

        # Check tarball contents
        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
            assert "mod_export.json" in names
            assert "custom/custom-folder-mod/mod.yaml" in names
            assert "custom/custom-folder-mod/targets.yaml" in names

            # Check normalized ownership
            for ti in tar.getmembers():
                assert ti.uname == "hermes"
                assert ti.gname == "hermes"
                assert ti.uid == 0
                assert ti.gid == 0

    def test_export_single_file_mod(self, mock_mods_workspace: Dict[str, Path]):
        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "single_test.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            target_mod_id="custom-single-mod",
        )

        assert result.success is True
        assert out_tar.is_file()
        assert result.mods_count == 1

        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
            assert "mod_export.json" in names
            assert "custom/custom-single-mod.yaml" in names

    def test_export_bundle_system_default(self, mock_mods_workspace: Dict[str, Path]):
        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "system_bundle.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            include_custom=False,
        )

        assert result.success is True
        assert result.mods_count == 1  # Only mod-core-sys
        assert result.mods[0]["id"] == "mod-core-sys"

        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
            assert any(n.startswith("system/mod-core-sys") for n in names)
            assert not any(n.startswith("custom/") for n in names)

    def test_export_bundle_include_custom(self, mock_mods_workspace: Dict[str, Path]):
        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "all_bundle.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            include_custom=True,
        )

        assert result.success is True
        assert result.mods_count == 3  # 1 system + 2 custom

        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
            assert any(n.startswith("system/mod-core-sys") for n in names)
            assert any(n.startswith("custom/custom-folder-mod") for n in names)
            assert "custom/custom-single-mod.yaml" in names

    def test_secrets_and_caches_completely_excluded(self, mock_mods_workspace: Dict[str, Path]):
        """Inject cache, logs, databases, and secrets into a mod folder and verify they are omitted."""
        custom_folder = mock_mods_workspace["custom_folder"]

        # Inject forbidden files
        (custom_folder / "secrets.yaml").write_text("token: 123456\n", encoding="utf-8")
        (custom_folder / ".env").write_text("DATABASE_URL=postgres://\n", encoding="utf-8")
        (custom_folder / "test.db").write_bytes(b"SQLITE FORMAT 3")
        (custom_folder / "debug.log").write_text("error occurred\n", encoding="utf-8")
        (custom_folder / "dump.tmp").write_text("temp", encoding="utf-8")
        (custom_folder / ".test_cache.json").write_text("{}", encoding="utf-8")
        pycache_dir = custom_folder / "__pycache__"
        pycache_dir.mkdir(parents=True, exist_ok=True)
        (pycache_dir / "mod.cpython-311.pyc").write_bytes(b"\x00\x00")

        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "clean_export.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            target_mod_id="custom-folder-mod",
            validate_manifests=False,
        )

        assert result.success is True
        assert result.files_excluded_count >= 7

        excluded_paths = [e["path"] for e in result.files_excluded]
        assert any("secrets.yaml" in p for p in excluded_paths)
        assert any(".env" in p for p in excluded_paths)
        assert any("test.db" in p for p in excluded_paths)
        assert any("debug.log" in p for p in excluded_paths)
        assert any("__pycache__" in p for p in excluded_paths)

        # Confirm tarball does not contain any of them
        with tarfile.open(out_tar, "r:gz") as tar:
            names = tar.getnames()
            for forbidden in ("secrets.yaml", ".env", "test.db", "debug.log", "dump.tmp", ".test_cache.json", ".pyc"):
                assert not any(forbidden in n for n in names), f"Found {forbidden} in archive {names}"

    def test_strict_no_secrets_flag_detects_leaked_key(self, mock_mods_workspace: Dict[str, Path]):
        custom_folder = mock_mods_workspace["custom_folder"]
        # Add a text file containing an RSA private key
        (custom_folder / "key.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEogIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----\n", encoding="utf-8")

        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "no_secrets_test.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            target_mod_id="custom-folder-mod",
            no_secrets=True,
        )

        assert result.success is False
        assert "подозрительные секреты" in (result.error or "")
        assert not out_tar.exists()

    def test_manifest_validation_rejection(self, mock_mods_workspace: Dict[str, Path]):
        custom_folder = mock_mods_workspace["custom_folder"]
        # Add a target pointing to a non-existent file without disabled: true
        (custom_folder / "targets.yaml").write_text(yaml.dump([
            {
                "file": "non_existent_target_file_12345.py",
                "old_block": "# old",
                "new_block": "# new",
                "disabled": False,
            }
        ]), encoding="utf-8")

        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = mock_mods_workspace["root"] / "should_fail.tar.gz"
        result = exporter.export(
            mods=mods,
            out_path=out_tar,
            target_mod_id="custom-folder-mod",
            validate_manifests=True,
        )

        assert result.success is False
        assert result.validation_passed is False
        assert len(result.validation_errors) > 0
        assert not out_tar.exists()


class TestModImporter:
    """Tests for ModImporter and round-trip packaging/importing."""

    def test_import_roundtrip(self, mock_mods_workspace: Dict[str, Path], tmp_path: Path):
        # 1. Export from workspace
        mods_dir = mock_mods_workspace["mods_dir"]
        mods = ModLoader.load_all(mods_dir)
        exporter = ModExporter(mods_dir=mods_dir)

        out_tar = tmp_path / "roundtrip.tar.gz"
        exp_res = exporter.export(
            mods=mods,
            out_path=out_tar,
            target_mod_id="custom-folder-mod",
        )
        assert exp_res.success is True

        # 2. Import into a clean new mods directory
        new_mods_dir = tmp_path / "new_mods_env"
        new_mods_dir.mkdir(parents=True, exist_ok=True)

        importer = ModImporter(mods_dir=new_mods_dir)
        imp_res = importer.import_archive(out_tar)

        assert imp_res.success is True
        assert "custom-folder-mod" in imp_res.imported_mods
        assert (new_mods_dir / "custom" / "custom-folder-mod" / "mod.yaml").is_file()
        assert (new_mods_dir / "custom" / "custom-folder-mod" / "targets.yaml").is_file()

        # 3. Reload from new directory and verify loaded mod matches
        new_mods = ModLoader.load_all(new_mods_dir)
        assert len(new_mods) == 1
        assert new_mods[0].id == "custom-folder-mod"
        assert new_mods[0].version == "1.2.0"

    def test_import_tarbomb_protection(self, tmp_path: Path):
        # Create malicious tarball with path traversal
        bad_tar = tmp_path / "malicious.tar.gz"
        with tarfile.open(bad_tar, "w:gz") as tar:
            data = b"malicious payload"
            ti = tarfile.TarInfo(name="../escape.txt")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))

        dest_dir = tmp_path / "protected_mods"
        dest_dir.mkdir(parents=True, exist_ok=True)

        importer = ModImporter(mods_dir=dest_dir)
        res = importer.import_archive(bad_tar)

        assert res.success is False
        assert any("TarBomb" in err for err in res.errors)
        assert not (tmp_path / "escape.txt").exists()


class TestCliIntegration:
    """CLI execution tests via runner.py."""

    def test_cli_export_and_pack_alias(self, mock_mods_workspace: Dict[str, Path], tmp_path: Path):
        mods_dir = mock_mods_workspace["mods_dir"]
        runner_py = REPO_ROOT / "runner.py"

        # 1. Test runner.py export with --json
        out_tar1 = tmp_path / "cli_exp.tar.gz"
        cmd1 = [
            sys.executable,
            str(runner_py),
            "export",
            "custom-folder-mod",
            "--mods-dir", str(mods_dir),
            "--out", str(out_tar1),
            "--json",
        ]
        res1 = subprocess.run(cmd1, capture_output=True, text=True)
        assert res1.returncode == 0
        data1 = json.loads(res1.stdout)
        assert data1["success"] is True
        assert data1["mods_count"] == 1
        assert out_tar1.is_file()

        # 2. Test runner.py pack alias
        out_tar2 = tmp_path / "cli_pack.tar.gz"
        cmd2 = [
            sys.executable,
            str(runner_py),
            "pack",
            "custom-single-mod",
            "--mods-dir", str(mods_dir),
            "--out", str(out_tar2),
            "--json",
        ]
        res2 = subprocess.run(cmd2, capture_output=True, text=True)
        assert res2.returncode == 0
        data2 = json.loads(res2.stdout)
        assert data2["success"] is True
        assert out_tar2.is_file()

    def test_cli_import(self, mock_mods_workspace: Dict[str, Path], tmp_path: Path):
        mods_dir = mock_mods_workspace["mods_dir"]
        runner_py = REPO_ROOT / "runner.py"

        # Export archive first
        out_tar = tmp_path / "export_for_cli_import.tar.gz"
        subprocess.run([
            sys.executable, str(runner_py), "export", "custom-folder-mod",
            "--mods-dir", str(mods_dir), "--out", str(out_tar)
        ], check=True)

        # Import into fresh directory
        fresh_mods_dir = tmp_path / "cli_imported_mods"
        cmd = [
            sys.executable,
            str(runner_py),
            "import",
            str(out_tar),
            "--mods-dir", str(fresh_mods_dir),
            "--json",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["success"] is True
        assert "custom-folder-mod" in data["imported_mods"]
        assert (fresh_mods_dir / "custom" / "custom-folder-mod" / "mod.yaml").is_file()
