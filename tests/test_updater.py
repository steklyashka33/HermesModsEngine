"""Tests for Hermes Mod Engine Updater (git sync, version gates, backup & rollback)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_default_mods = HERMES_HOME / "mods"
if not _default_mods.exists() and (Path(__file__).resolve().parent.parent / ".engine").is_dir():
    _default_mods = Path(__file__).resolve().parent.parent
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(_default_mods)))
ENGINE_DIR = MODS_DIR / ".engine"
if str(MODS_DIR) not in sys.path:
    sys.path.insert(0, str(MODS_DIR))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from engine.models import Mod, ModTarget
from engine.version import ENGINE_VERSION, MIN_SUPPORTED_HERMES_VERSION
from engine.loader import detect_hermes_root, is_valid_hermes_root
from engine.updater import (
    ModUpdater,
    StateManager,
    BackupManager,
    parse_version,
    compare_versions,
    get_hermes_version,
    check_compatibility,
    CompatibilityIssue,
    DEFAULT_REMOTE_URL,
)
from runner import ModLoader, ModRunner, HERMES_ROOT


# ---------------------------------------------------------------------------
# 1. Version Parsing & Comparison
# ---------------------------------------------------------------------------


def test_parse_version():
    assert parse_version("1.2.0") == (1, 2, 0)
    assert parse_version("v2.1.3") == (2, 1, 3)
    assert parse_version("0.22.5") == (0, 22, 5)
    assert parse_version("1.0.0-beta2") == (1, 0, 0, 2)
    assert parse_version("") == (0, 0, 0)
    assert parse_version(None) == (0, 0, 0)


def test_compare_versions():
    assert compare_versions("1.2.0", "1.2.0") == 0
    assert compare_versions("1.2.1", "1.2.0") == 1
    assert compare_versions("1.1.9", "1.2.0") == -1
    assert compare_versions("2.0", "1.9.9") == 1
    assert compare_versions("0.22", "0.22.5") == -1
    assert compare_versions("0.22.5", "0.22") == 1


# ---------------------------------------------------------------------------
# 2. Compatibility Checks (Engine & Hermes Version Gates)
# ---------------------------------------------------------------------------


def test_check_compatibility_clean(tmp_path):
    mod = Mod(
        id="test-clean",
        name="Clean Mod",
        path=tmp_path / "mod.yaml",
        targets=[],
        min_engine_version="1.0.0",
        min_hermes_version="0.20.0",
    )
    issues = check_compatibility([mod], engine_version="1.2.0", hermes_version="0.22.5")
    assert len(issues) == 0


def test_check_compatibility_outdated_engine(tmp_path):
    mod = Mod(
        id="test-engine-outdated",
        name="Requires Engine 2.0.0",
        path=tmp_path / "mod.yaml",
        targets=[],
        min_engine_version="2.0.0",
    )
    issues = check_compatibility([mod], engine_version="1.2.0", hermes_version="0.22.5")
    assert len(issues) == 1
    assert issues[0].issue_type == "engine_outdated"
    assert issues[0].mod_id == "test-engine-outdated"
    assert "требуется движок версии >= 2.0.0" in issues[0].message


def test_check_compatibility_outdated_hermes(tmp_path):
    mod = Mod(
        id="test-hermes-outdated",
        name="Requires Hermes 0.30.0",
        path=tmp_path / "mod.yaml",
        targets=[],
        min_hermes_version="0.30.0",
    )
    issues = check_compatibility([mod], engine_version="1.2.0", hermes_version="0.22.5")
    assert len(issues) == 1
    assert issues[0].issue_type == "hermes_outdated"
    assert issues[0].mod_id == "test-hermes-outdated"
    assert "требуется Hermes версии >= 0.30.0" in issues[0].message


def test_apply_mod_skips_outdated_engine(tmp_path):
    repo_file = tmp_path / "core.py"
    repo_file.write_text("x = 1\n", encoding="utf-8")

    mod = Mod(
        id="test-mod",
        name="Test Mod",
        path=tmp_path / "mod.yaml",
        min_engine_version="99.0.0",
        targets=[ModTarget(file="core.py", old_block="x = 1", new_block="x = 2")],
    )

    runner = ModRunner(repo_root=tmp_path)
    res = runner.apply_mod(mod, apply_changes=True)
    assert res["status"] == "skipped_engine_outdated"
    assert res["verified"] is False
    assert repo_file.read_text(encoding="utf-8") == "x = 1\n"


def test_apply_mod_skips_outdated_hermes(tmp_path):
    repo_file = tmp_path / "core.py"
    repo_file.write_text("x = 1\n", encoding="utf-8")

    mod = Mod(
        id="test-mod",
        name="Test Mod",
        path=tmp_path / "mod.yaml",
        min_hermes_version="99.0.0",
        targets=[ModTarget(file="core.py", old_block="x = 1", new_block="x = 2")],
    )

    runner = ModRunner(repo_root=tmp_path)
    res = runner.apply_mod(mod, apply_changes=True)
    assert res["status"] == "skipped_hermes_outdated"
    assert res["verified"] is False
    assert repo_file.read_text(encoding="utf-8") == "x = 1\n"


# ---------------------------------------------------------------------------
# 3. State Management & User Custom Mods Protection
# ---------------------------------------------------------------------------


def test_state_manager_lifecycle(tmp_path):
    state_file = tmp_path / ".state.json"
    mgr = StateManager(state_file)

    data = mgr.load()
    assert data["overrides"] == {}
    assert data["last_update_check"] is None

    mgr.record_update("1.2.0")
    loaded = mgr.load()
    assert loaded["installed_engine_version"] == "1.2.0"
    assert loaded["last_update_check"] is not None


def test_protect_local_mods_creates_custom_and_gitkeep(tmp_path):
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    updater.protect_local_mods()

    custom_dir = tmp_path / "custom"
    assert custom_dir.is_dir()
    assert (custom_dir / ".gitkeep").exists()


def test_protect_local_mods_updates_gitignore(tmp_path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n", encoding="utf-8")

    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    updater.protect_local_mods()

    content = gitignore.read_text(encoding="utf-8")
    assert "custom/*" in content
    assert "!custom/.gitkeep" in content
    assert ".state.json" in content


def test_custom_mods_loaded_by_mod_loader(tmp_path):
    custom_dir = tmp_path / "custom"
    custom_dir.mkdir(parents=True)

    # 1. Single file mod inside custom/
    (custom_dir / "user-single.yaml").write_text(yaml.dump({
        "id": "user-single",
        "name": "User Single Mod",
        "targets": []
    }), encoding="utf-8")

    # 2. Folder mod inside custom/
    subfolder = custom_dir / "user-folder"
    subfolder.mkdir()
    (subfolder / "mod.yaml").write_text(yaml.dump({
        "id": "user-folder",
        "name": "User Folder Mod",
        "targets": []
    }), encoding="utf-8")

    mods = ModLoader.load_all(tmp_path)
    mod_ids = [m.id for m in mods]
    assert "user-single" in mod_ids
    assert "user-folder" in mod_ids


def test_state_json_overrides_applied_by_mod_loader(tmp_path):
    # Mod enabled by default in YAML
    (tmp_path / "sample.yaml").write_text(yaml.dump({
        "id": "sample-mod",
        "name": "Sample Mod",
        "enabled": True,
        "targets": []
    }), encoding="utf-8")

    # Override disabled in .state.json
    (tmp_path / ".state.json").write_text(json.dumps({
        "overrides": {
            "sample-mod": {"enabled": False}
        }
    }), encoding="utf-8")

    mods = ModLoader.load_all(tmp_path)
    sample = next(m for m in mods if m.id == "sample-mod")
    assert sample.enabled is False


# ---------------------------------------------------------------------------
# 4. Atomic Backup and Rollback
# ---------------------------------------------------------------------------


def test_backup_and_restore_cycle(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    f1 = repo_dir / "file1.txt"
    f2 = repo_dir / "file2.txt"
    f1.write_text("initial f1", encoding="utf-8")
    f2.write_text("initial f2", encoding="utf-8")

    backups_dir = tmp_path / "backups"
    bm = BackupManager(backups_dir)

    # 1. Create backup
    backup_path = bm.create_backup([f1, f2], repo_root=repo_dir)
    assert backup_path is not None
    assert (backup_path / "backup_manifest.json").exists()

    # 2. Modify files (simulate bad patch)
    f1.write_text("corrupted f1", encoding="utf-8")
    f2.write_text("corrupted f2", encoding="utf-8")

    # 3. Restore backup
    ok = bm.restore_backup(backup_path)
    assert ok is True
    assert f1.read_text(encoding="utf-8") == "initial f1"
    assert f2.read_text(encoding="utf-8") == "initial f2"


def test_apply_updated_mods_rollback_on_verify_fail(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target_file = repo_dir / "core.py"
    target_file.write_text("initial_code = True\n", encoding="utf-8")

    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    # Mod with a verify command that fails
    mod_yaml = mods_dir / "bad-mod.yaml"
    mod_yaml.write_text(yaml.dump({
        "id": "bad-mod",
        "name": "Bad Mod",
        "enabled": True,
        "targets": [{
            "file": "core.py",
            "old_block": "initial_code = True",
            "new_block": "initial_code = False",
        }],
        "verify_cmd": "python3 -c 'import sys; sys.exit(1)'"
    }), encoding="utf-8")

    updater = ModUpdater(mods_dir=mods_dir, repo_root=repo_dir, backups_dir=tmp_path / "backups")
    res = updater.apply_updated_mods(dry_run=False)

    assert res["success"] is False
    assert res["status"] == "failed_verification_rolled_back"
    assert res["failed_mod"] == "bad-mod"
    # Ensure atomic rollback restored original content
    assert target_file.read_text(encoding="utf-8") == "initial_code = True\n"


# ---------------------------------------------------------------------------
# 5. Git Mocking: --check and --dry-run
# ---------------------------------------------------------------------------


def test_check_updates_up_to_date(tmp_path):
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)

    def mock_run_git(args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true", ""
        if args == ["fetch", "origin", "main", "--tags"]:
            return 0, "", ""
        if args == ["rev-parse", "HEAD"]:
            return 0, "a1b2c3d4e5f67890", ""
        if args == ["rev-parse", "origin/main"]:
            return 0, "a1b2c3d4e5f67890", ""
        if args == ["rev-list", "--count", "HEAD..origin/main"]:
            return 0, "0", ""
        if args == ["rev-list", "--count", "origin/main..HEAD"]:
            return 0, "0", ""
        return 0, "", ""

    with patch.object(updater, "run_git", side_effect=mock_run_git):
        res = updater.check_updates(remote="origin", branch="main")
        assert res["status"] == "up_to_date"
        assert res["exit_code"] == 0
        assert res["behind"] == 0
        assert res["local_commit"] == "a1b2c3d4"


def test_check_updates_available(tmp_path):
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)

    def mock_run_git(args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true", ""
        if args == ["fetch", "origin", "main", "--tags"]:
            return 0, "", ""
        if args == ["rev-parse", "HEAD"]:
            return 0, "1111222233334444", ""
        if args == ["rev-parse", "origin/main"]:
            return 0, "5555666677778888", ""
        if args == ["rev-list", "--count", "HEAD..origin/main"]:
            return 0, "3", ""
        if args == ["rev-list", "--count", "origin/main..HEAD"]:
            return 0, "0", ""
        return 0, "", ""

    with patch.object(updater, "run_git", side_effect=mock_run_git):
        res = updater.check_updates(remote="origin", branch="main")
        assert res["status"] == "updates_available"
        assert res["exit_code"] == 2
        assert res["behind"] == 3
        assert res["local_commit"] == "11112222"
        assert res["remote_commit"] == "55556666"


def test_check_updates_not_git_repo(tmp_path):
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)

    def mock_run_git(args):
        return 1, "", "fatal: not a git repository"

    with patch.object(updater, "run_git", side_effect=mock_run_git):
        res = updater.check_updates(remote="origin", branch="main")
        assert res["status"] == "not_a_git_repo"
        assert res["exit_code"] == 1


def test_updater_dry_run_does_not_modify_disk(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target_file = repo_dir / "target.py"
    target_file.write_text("v = 1\n", encoding="utf-8")

    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    (mods_dir / "mod.yaml").write_text(yaml.dump({
        "id": "dry-mod",
        "name": "Dry Mod",
        "enabled": True,
        "targets": [{
            "file": "target.py",
            "old_block": "v = 1",
            "new_block": "v = 2",
        }]
    }), encoding="utf-8")

    updater = ModUpdater(mods_dir=mods_dir, repo_root=repo_dir)

    with patch.object(updater, "is_git_repo", return_value=False):
        res = updater.update(dry_run=True)

    assert res["status"] == "dry_run"
    assert res["exit_code"] == 0
    sim = res["apply_simulation"]
    assert sim["dry_run"] is True
    # The file on disk must NOT be modified
    assert target_file.read_text(encoding="utf-8") == "v = 1\n"


def test_updater_no_apply_pulls_git_only(tmp_path):
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)

    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "run_git", return_value=(0, "", "")), \
         patch.object(updater, "pull_updates", return_value=(True, "Pulled")):

        res = updater.update(no_apply=True)
        assert res["status"] == "repo_updated_only"
        assert res["exit_code"] == 0
        assert res["git_updated"] is True


def test_updater_pull_with_force_resets_hard(tmp_path):
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    calls = []

    def mock_run_git(args):
        calls.append(args)
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true", ""
        return 0, "", ""

    with patch.object(updater, "run_git", side_effect=mock_run_git):
        ok, msg = updater.pull_updates(remote="origin", branch="main", force=True)
        assert ok is True
        assert ["reset", "--hard", "origin/main"] in calls


# ---------------------------------------------------------------------------
# 6. CLI Execution Tests
# ---------------------------------------------------------------------------


def test_cli_update_dry_run_command():
    cmd = [
        sys.executable,
        str(MODS_DIR / "runner.py"),
        "update",
        "--dry-run",
        "--json",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"Error: {res.stderr}"
    data = json.loads(res.stdout)
    assert data["status"] == "dry_run"
    assert data["exit_code"] == 0
    assert "apply_simulation" in data


# ---------------------------------------------------------------------------
# 7. Path Independence & Zero Path Assumptions Tests
# ---------------------------------------------------------------------------


def test_detect_hermes_root_with_env_vars(tmp_path, monkeypatch):
    """Verifies HERMES_ROOT_DIR, HERMES_AGENT_PATH, and HERMES_ROOT resolution."""
    dir1 = tmp_path / "root_dir"
    dir1.mkdir()
    dir2 = tmp_path / "agent_path"
    dir2.mkdir()
    dir3 = tmp_path / "hermes_root"
    dir3.mkdir()

    # HERMES_ROOT priority
    monkeypatch.setenv("HERMES_ROOT", str(dir3))
    assert detect_hermes_root() == dir3.resolve()

    # HERMES_ROOT_DIR
    monkeypatch.delenv("HERMES_ROOT", raising=False)
    monkeypatch.setenv("HERMES_ROOT_DIR", str(dir1))
    assert detect_hermes_root() == dir1.resolve()

    # HERMES_AGENT_PATH
    monkeypatch.delenv("HERMES_ROOT_DIR", raising=False)
    monkeypatch.setenv("HERMES_AGENT_PATH", str(dir2))
    assert detect_hermes_root() == dir2.resolve()


def test_is_valid_hermes_root_markers(tmp_path):
    """Tests validation of candidate directories containing Hermes markers."""
    assert not is_valid_hermes_root(tmp_path)

    # 1. hermes_cli marker
    (tmp_path / "hermes_cli").mkdir()
    assert is_valid_hermes_root(tmp_path)

    # Clean up
    (tmp_path / "hermes_cli").rmdir()

    # 2. agent + gateway markers
    (tmp_path / "agent").mkdir()
    (tmp_path / "gateway").mkdir()
    assert is_valid_hermes_root(tmp_path)


def test_updater_ensure_remote_default_url(tmp_path):
    """Tests that ensure_remote sets the default remote URL if missing."""
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)

    commands_run = []

    def mock_run_git(args):
        commands_run.append(args)
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return 0, "true", ""
        if args == ["remote"]:
            return 0, "", ""
        if args[:2] == ["remote", "add"]:
            return 0, "", ""
        return 0, "", ""

    with patch.object(updater, "run_git", side_effect=mock_run_git):
        ok = updater.ensure_remote("origin")
        assert ok is True
        assert ["remote", "add", "origin", DEFAULT_REMOTE_URL] in commands_run


def test_engine_version_is_150():
    """Engine version must be v1.5.0."""
    from engine.version import ENGINE_VERSION as ver
    from engine import __version__
    assert ver == "1.5.0"
    assert __version__ == "1.5.0"


# ---------------------------------------------------------------------------
# 8. Three-Phase Pipeline & --with-hermes / --all
# ---------------------------------------------------------------------------


def test_update_with_hermes_check(tmp_path):
    """Test updater.update(with_hermes=True, check=True)."""
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    with patch.object(updater, "check_updates", return_value={"status": "up_to_date"}), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0, stdout="Up to date")
        res = updater.update(check=True, with_hermes=True)
        assert res["with_hermes"] is True
        assert res["status"] in ("up_to_date", "updates_available")
        assert "engine" in res
        assert "hermes" in res


def test_update_with_hermes_dry_run(tmp_path):
    """Test updater.update(with_hermes=True, dry_run=True)."""
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    with patch.object(updater, "apply_updated_mods", return_value={"success": True, "mods_results": []}):
        res = updater.update(dry_run=True, with_hermes=True)
        assert res["status"] == "dry_run"
        assert res["with_hermes"] is True
        assert res["hermes_update_simulated"] is True
        assert res["exit_code"] == 0


def test_update_with_hermes_full_success(tmp_path):
    """Test full 3-phase update pipeline when engine and hermes succeed."""
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "ensure_remote", return_value=True), \
         patch.object(updater, "run_git", return_value=(0, "", "")), \
         patch.object(updater, "pull_updates", return_value=(True, "Pulled")), \
         patch("subprocess.run") as mock_sub, \
         patch.object(updater, "apply_updated_mods", return_value={"success": True, "mods_results": []}):
        mock_sub.return_value = MagicMock(returncode=0, stdout="Hermes updated", stderr="")
        res = updater.update(with_hermes=True)
        assert res["status"] == "completed"
        assert res["exit_code"] == 0
        assert res["phase1_engine"]["success"] is True
        assert res["phase2_hermes"]["success"] is True
        assert res["phase3_apply"]["success"] is True


def test_update_with_hermes_failure_in_hermes(tmp_path):
    """Test 3-phase update pipeline when Hermes update fails in phase 2."""
    updater = ModUpdater(mods_dir=tmp_path, repo_root=tmp_path)
    with patch.object(updater, "is_git_repo", return_value=True), \
         patch.object(updater, "ensure_remote", return_value=True), \
         patch.object(updater, "run_git", return_value=(0, "", "")), \
         patch.object(updater, "pull_updates", return_value=(True, "Pulled")), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="Git pull conflict in Hermes core")
        res = updater.update(with_hermes=True)
        assert res["status"] == "failed"
        assert res["phase"] == "phase2_hermes"
        assert res["exit_code"] == 1
        assert res["phase2_hermes"]["success"] is False
