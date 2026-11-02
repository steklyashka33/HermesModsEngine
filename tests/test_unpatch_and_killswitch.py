"""Tests for Hermes Mods killswitch (mods.enabled) and unpatch (ModRunner.unpatch_all / CLI unpatch).

Verifies:
1. Master killswitch `mods.enabled` in config.yaml:
   - Default true.
   - When false: blocks `apply` and initiates `unpatch_all()` if mods were applied.
2. ModRunner.unpatch_all:
   - Atomic rollback of target files to original state.
   - Clears `.commands_cache.json` (resets to `{}`).
   - Resets skills and MCP caches.
   - Returns report with `status: unpatched` and restored files.
3. CLI commands:
   - `runner.py unpatch`
   - `runner.py rollback --all`
   - Prints: «Все моды успешно сняты. Ядро Hermes возвращено к исходному состоянию».
4. Full cycle:
   - `apply` patches -> `unpatch` cleanly removes -> `apply` re-applies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_default_mods = HERMES_HOME / "mods"
if not _default_mods.exists() and (Path(__file__).resolve().parent.parent / ".engine").is_dir():
    _default_mods = Path(__file__).resolve().parent.parent
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(_default_mods)))
ENGINE_DIR = MODS_DIR / ".engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
if str(MODS_DIR) not in sys.path:
    sys.path.insert(0, str(MODS_DIR))

import runner
from engine.models import Mod, ModTarget
from engine.config_manager import ModConfigManager
from engine.loader import update_commands_cache
from runner import ModLoader, ModRunner, handle_unpatch_cli, handle_purge_cli, is_system_mod


def _create_mock_environment(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    config_file = tmp_path / "config.yaml"

    return repo_dir, mods_dir, backups_dir, config_file


def _create_test_mod(mods_dir: Path, mod_id: str, targets: list, name: str = "Test Mod") -> Mod:
    custom_dir = mods_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    manifest_data = {
        "id": mod_id,
        "name": name,
        "author": "User",
        "enabled": True,
        "targets": [
            {
                "file": t.file,
                "old_block": t.old_block,
                "new_block": t.new_block,
            }
            for t in targets
        ],
    }
    manifest_path = custom_dir / f"{mod_id}.yaml"
    manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
    return Mod(
        id=mod_id,
        name=name,
        author="User",
        path=manifest_path,
        enabled=True,
        targets=targets,
    )


def _create_test_system_mod(mods_dir: Path, mod_id: str, targets: list, name: str = "System Mod") -> Mod:
    system_dir = mods_dir / "system"
    system_dir.mkdir(parents=True, exist_ok=True)
    manifest_data = {
        "id": mod_id,
        "name": name,
        "author": "Engine",
        "enabled": True,
        "targets": [
            {
                "file": t.file,
                "old_block": t.old_block,
                "new_block": t.new_block,
            }
            for t in targets
        ],
    }
    manifest_path = system_dir / f"{mod_id}.yaml"
    manifest_path.write_text(yaml.dump(manifest_data), encoding="utf-8")
    return Mod(
        id=mod_id,
        name=name,
        author="Engine",
        path=manifest_path,
        enabled=True,
        targets=targets,
    )


# ============================================================================
# 1. Killswitch mods.enabled tests
# ============================================================================

def test_killswitch_defaults_to_true_when_missing(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("model: default\n", encoding="utf-8")

    mgr = ModConfigManager(config_path=config_file)
    assert mgr.is_mods_enabled() is True

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    assert runner_inst.is_mods_enabled() is True


def test_killswitch_values(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)

    # 1. Explicit true
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")
    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    assert runner_inst.is_mods_enabled() is True

    # 2. Explicit false
    config_file.write_text("mods:\n  enabled: false\n", encoding="utf-8")
    assert runner_inst.is_mods_enabled() is False

    # 3. Direct boolean false
    config_file.write_text("mods: false\n", encoding="utf-8")
    assert runner_inst.is_mods_enabled() is False

    # 4. Non-existent config defaults to true
    non_existent = tmp_path / "no_config.yaml"
    runner_no_cfg = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=non_existent)
    assert runner_no_cfg.is_mods_enabled() is True


def test_apply_blocked_when_killswitch_false(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: false\n", encoding="utf-8")

    # Create target file in mock repo
    target_file = repo_dir / "agent.py"
    target_file.write_text("def run():\n    return 'original'\n", encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "sample-mod",
        targets=[
            ModTarget(
                file="agent.py",
                old_block="return 'original'",
                new_block="return 'patched'",
            )
        ],
        name="Sample Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # Attempt to apply via apply()
    results = runner_inst.apply([mod])

    assert len(results) == 1
    assert results[0]["status"] == "blocked_killswitch"
    assert "mods.enabled is set to false" in results[0]["reason"]
    assert results[0]["verified"] is False

    # Verify file was NOT modified
    assert target_file.read_text(encoding="utf-8") == "def run():\n    return 'original'\n"


def test_apply_mod_single_blocked_when_killswitch_false(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: false\n", encoding="utf-8")

    target_file = repo_dir / "core.py"
    target_file.write_text("x = 10\n", encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "single-mod",
        targets=[ModTarget(file="core.py", old_block="x = 10", new_block="x = 20")],
        name="Single Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    res = runner_inst.apply_mod(mod, apply_changes=True)

    assert res["status"] == "blocked_killswitch"
    assert target_file.read_text(encoding="utf-8") == "x = 10\n"


# ============================================================================
# 2. ModRunner.unpatch_all tests
# ============================================================================

def test_unpatch_all_restores_files_atomically(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    target_file = repo_dir / "pipeline.py"
    target_file.write_text("def process():\n    step_one()\n    step_two()\n", encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "pipeline-mod",
        targets=[
            ModTarget(
                file="pipeline.py",
                old_block="    step_two()",
                new_block="    step_two()\n    step_mod()",
            )
        ],
        name="Pipeline Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # 1. Apply mod
    apply_res = runner_inst.apply([mod])
    assert apply_res[0]["status"] == "applied"
    assert "step_mod()" in target_file.read_text(encoding="utf-8")
    assert runner_inst.is_any_applied([mod]) is True

    # Populate commands cache
    cache_path = mods_dir / ".commands_cache.json"
    cache_path.write_text('{"commands": {"test": {}}}', encoding="utf-8")

    # 2. Unpatch all
    report = runner_inst.unpatch_all([mod])
    assert report["status"] == "unpatched"
    assert report["restored_count"] == 1
    assert "pipeline.py" in report["restored_files"]
    assert report["commands_cache_cleared"] is True
    assert report["caches_reset"] is True

    # Verify file is restored to original
    expected = "def process():\n    step_one()\n    step_two()\n"
    assert target_file.read_text(encoding="utf-8") == expected
    assert runner_inst.is_any_applied([mod]) is False

    # Verify .commands_cache.json is cleared to empty dict {}
    assert json.loads(cache_path.read_text(encoding="utf-8")) == {}


def test_auto_unpatch_when_killswitch_tripped_on_already_applied_mods(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    target_file = repo_dir / "service.py"
    target_file.write_text("STATUS = 'idle'\n", encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "status-mod",
        targets=[
            ModTarget(
                file="service.py",
                old_block="STATUS = 'idle'",
                new_block="STATUS = 'mod_active'",
            )
        ],
        name="Status Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # 1. Apply mod while enabled: true
    runner_inst.apply([mod])
    assert "STATUS = 'mod_active'" in target_file.read_text(encoding="utf-8")
    assert runner_inst.is_any_applied([mod]) is True

    # 2. Toggle killswitch: mods.enabled: false
    config_file.write_text("mods:\n  enabled: false\n", encoding="utf-8")
    assert runner_inst.is_mods_enabled() is False

    # 3. Call runner.apply([mod]) -> must automatically unpatch!
    results = runner_inst.apply([mod])
    assert len(results) == 1
    assert results[0]["status"] == "blocked_killswitch"
    assert results[0]["unpatch_report"] is not None
    assert results[0]["unpatch_report"]["status"] == "unpatched"
    assert results[0]["unpatch_report"]["restored_count"] == 1

    # Verify file is back to pristine
    assert target_file.read_text(encoding="utf-8") == "STATUS = 'idle'\n"
    assert runner_inst.is_any_applied([mod]) is False


def test_unpatch_multiple_mods_and_reverse_ordering(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    f1 = repo_dir / "module_a.py"
    f2 = repo_dir / "module_b.py"
    f1.write_text("def a(): return 1\n", encoding="utf-8")
    f2.write_text("def b(): return 2\n", encoding="utf-8")

    mod1 = _create_test_mod(
        mods_dir,
        "mod-1",
        targets=[ModTarget(file="module_a.py", old_block="return 1", new_block="return 100")],
        name="Mod 1",
    )
    mod2 = _create_test_mod(
        mods_dir,
        "mod-2",
        targets=[ModTarget(file="module_b.py", old_block="return 2", new_block="return 200")],
        name="Mod 2",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    runner_inst.apply([mod1, mod2])

    assert "return 100" in f1.read_text(encoding="utf-8")
    assert "return 200" in f2.read_text(encoding="utf-8")

    report = runner_inst.unpatch_all([mod1, mod2])
    assert report["status"] == "unpatched"
    assert report["restored_count"] == 2
    assert "module_a.py" in report["restored_files"]
    assert "module_b.py" in report["restored_files"]

    assert f1.read_text(encoding="utf-8") == "def a(): return 1\n"
    assert f2.read_text(encoding="utf-8") == "def b(): return 2\n"


def test_unpatch_when_nothing_applied(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    f = repo_dir / "clean.py"
    f.write_text("original = True\n", encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "clean-mod",
        targets=[ModTarget(file="clean.py", old_block="original = True", new_block="modified = True")],
        name="Clean Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    report = runner_inst.unpatch_all([mod])

    assert report["status"] == "unpatched"
    assert report["restored_count"] == 0
    assert report["restored_files"] == []
    assert f.read_text(encoding="utf-8") == "original = True\n"


# ============================================================================
# 3. CLI Tests (unpatch and rollback --all)
# ============================================================================

def test_cli_unpatch_and_rollback_alias(tmp_path: Path, capsys):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    target = repo_dir / "app.py"
    target.write_text("VERSION = '1.0'\n", encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "app-mod",
        targets=[ModTarget(file="app.py", old_block="VERSION = '1.0'", new_block="VERSION = '1.0-mod'")],
        name="App Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    runner_inst.apply([mod])
    assert "VERSION = '1.0-mod'" in target.read_text(encoding="utf-8")

    class FakeArgs:
        action = "unpatch"
        target = None
        all_mods = False
        json = False

    ret = handle_unpatch_cli(FakeArgs(), [mod], runner_inst)
    captured = capsys.readouterr()

    assert ret == 0
    assert "Все пользовательские моды (custom/) успешно сняты. Системные компоненты движка активны. (Для полного удаления системных модов используйте --all)." in captured.out
    assert "Восстановлено файлов ядра: 1" in captured.out
    assert "app.py" in captured.out
    assert target.read_text(encoding="utf-8") == "VERSION = '1.0'\n"

    # Now re-apply and test rollback --all alias
    runner_inst.apply([mod])
    assert "VERSION = '1.0-mod'" in target.read_text(encoding="utf-8")

    class FakeRollbackArgs:
        action = "rollback"
        target = None
        all_mods = True
        json = False

    ret2 = handle_unpatch_cli(FakeRollbackArgs(), [mod], runner_inst)
    captured2 = capsys.readouterr()

    assert ret2 == 0
    assert "Все моды успешно сняты. Ядро Hermes возвращено к исходному состоянию." in captured2.out
    assert target.read_text(encoding="utf-8") == "VERSION = '1.0'\n"


def test_full_lifecycle_apply_unpatch_reapply(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    core_file = repo_dir / "core.py"
    initial_text = "def calculate():\n    return 42\n"
    core_file.write_text(initial_text, encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "calc-mod",
        targets=[
            ModTarget(
                file="core.py",
                old_block="    return 42",
                new_block="    return 42 + 100",
            )
        ],
        name="Calc Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # 1. Apply
    res1 = runner_inst.apply([mod])
    assert res1[0]["status"] == "applied"
    assert "return 42 + 100" in core_file.read_text(encoding="utf-8")

    # 2. Unpatch
    unpatch_rep = runner_inst.unpatch_all([mod])
    assert unpatch_rep["status"] == "unpatched"
    assert unpatch_rep["restored_count"] == 1
    assert core_file.read_text(encoding="utf-8") == initial_text

    # 3. Re-apply
    res2 = runner_inst.apply([mod])
    assert res2[0]["status"] == "applied"
    assert "return 42 + 100" in core_file.read_text(encoding="utf-8")

    # 4. Final unpatch
    unpatch_rep2 = runner_inst.unpatch_all([mod])
    assert unpatch_rep2["status"] == "unpatched"
    assert core_file.read_text(encoding="utf-8") == initial_text


def test_cli_subprocess_unpatch(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    core_file = repo_dir / "target.py"
    core_file.write_text("value = 1\n", encoding="utf-8")

    _create_test_mod(
        mods_dir,
        "cli-mod",
        targets=[ModTarget(file="target.py", old_block="value = 1", new_block="value = 999")],
        name="CLI Mod",
    )

    env = os.environ.copy()
    env["HERMES_ROOT"] = str(repo_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)
    env["HERMES_CONFIG_FILE"] = str(config_file)
    env["PYTHONPATH"] = f"{MODS_DIR}:{MODS_DIR / '.engine'}"

    # 1. Apply via CLI
    p_apply = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "apply", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p_apply.returncode == 0
    assert "value = 999" in core_file.read_text(encoding="utf-8")

    # 2. Unpatch via CLI
    p_unpatch = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "unpatch", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p_unpatch.returncode == 0
    assert "Все пользовательские моды (custom/) успешно сняты. Системные компоненты движка активны. (Для полного удаления системных модов используйте --all)." in p_unpatch.stdout
    assert "target.py" in p_unpatch.stdout
    assert core_file.read_text(encoding="utf-8") == "value = 1\n"

    # 3. Rollback --all alias via CLI
    # Re-apply first
    subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "apply", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "value = 999" in core_file.read_text(encoding="utf-8")

    p_rollback = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "rollback", "--all", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p_rollback.returncode == 0
    assert "Все моды успешно сняты. Ядро Hermes возвращено к исходному состоянию." in p_rollback.stdout
    assert core_file.read_text(encoding="utf-8") == "value = 1\n"


def test_unpatch_with_snapshot_backup_restoration(tmp_path: Path):
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    core_file = repo_dir / "backup_test.py"
    initial_text = "# Original content with complex indentation\nclass Worker:\n    pass\n"
    core_file.write_text(initial_text, encoding="utf-8")

    mod = _create_test_mod(
        mods_dir,
        "backup-mod",
        targets=[
            ModTarget(
                file="backup_test.py",
                old_block="    pass",
                new_block="    # patched line\n    pass",
            )
        ],
        name="Backup Test Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # 1. Apply mod (this creates snapshot backup in backups_dir)
    res = runner_inst.apply([mod])
    assert res[0]["status"] == "applied"
    assert "# patched line" in core_file.read_text(encoding="utf-8")

    # Verify backup was created
    latest = runner_inst.backup_mgr.get_latest_backup()
    assert latest is not None
    assert (latest / "backup_manifest.json").is_file()

    # 2. Corrupt or further modify file slightly so simple string replace might fail
    # or rely on snapshot restoration
    report = runner_inst.unpatch_all([mod])
    assert report["status"] == "unpatched"
    assert report["restored_count"] == 1
    assert core_file.read_text(encoding="utf-8") == initial_text


# ============================================================================
# 4. System vs Custom Mods Unpatch & Purge Tests
# ============================================================================

def test_unpatch_preserves_system_mods_by_default(tmp_path: Path):
    """Verify ModRunner.unpatch_all() by default unpatches ONLY custom mods and leaves system mods applied."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    sys_file = repo_dir / "core_sys.py"
    user_file = repo_dir / "core_user.py"
    sys_file.write_text("SYS_VAL = 'sys_orig'\n", encoding="utf-8")
    user_file.write_text("USER_VAL = 'user_orig'\n", encoding="utf-8")

    sys_mod = _create_test_system_mod(
        mods_dir,
        "mod-command-registry",
        targets=[ModTarget(file="core_sys.py", old_block="SYS_VAL = 'sys_orig'", new_block="SYS_VAL = 'sys_patched'")],
        name="Command Registry",
    )
    user_mod = _create_test_mod(
        mods_dir,
        "my-user-mod",
        targets=[ModTarget(file="core_user.py", old_block="USER_VAL = 'user_orig'", new_block="USER_VAL = 'user_patched'")],
        name="User Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # 1. Apply both
    res = runner_inst.apply([sys_mod, user_mod])
    assert all(r["status"] == "applied" for r in res)
    assert "SYS_VAL = 'sys_patched'" in sys_file.read_text(encoding="utf-8")
    assert "USER_VAL = 'user_patched'" in user_file.read_text(encoding="utf-8")

    # 2. Unpatch without include_system (default behavior)
    report = runner_inst.unpatch_all([sys_mod, user_mod], include_system=False)
    assert report["status"] == "unpatched"
    assert report["include_system"] is False
    assert "core_user.py" in report["restored_files"]
    assert "core_sys.py" not in report["restored_files"]

    # Verify custom mod was rolled back, but system mod stays applied!
    assert user_file.read_text(encoding="utf-8") == "USER_VAL = 'user_orig'\n"
    assert "SYS_VAL = 'sys_patched'" in sys_file.read_text(encoding="utf-8")
    assert runner_inst.is_any_applied([sys_mod]) is True
    assert runner_inst.is_any_applied([user_mod]) is False


def test_unpatch_with_all_flag_removes_both_system_and_custom_mods(tmp_path: Path):
    """Verify ModRunner.unpatch_all(include_system=True) rolls back both custom and system mods."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    sys_file = repo_dir / "core_sys.py"
    user_file = repo_dir / "core_user.py"
    sys_file.write_text("SYS_VAL = 'sys_orig'\n", encoding="utf-8")
    user_file.write_text("USER_VAL = 'user_orig'\n", encoding="utf-8")

    sys_mod = _create_test_system_mod(
        mods_dir,
        "mod-declarative-mcp",
        targets=[ModTarget(file="core_sys.py", old_block="SYS_VAL = 'sys_orig'", new_block="SYS_VAL = 'sys_patched'")],
        name="Declarative MCP",
    )
    user_mod = _create_test_mod(
        mods_dir,
        "my-user-mod",
        targets=[ModTarget(file="core_user.py", old_block="USER_VAL = 'user_orig'", new_block="USER_VAL = 'user_patched'")],
        name="User Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    runner_inst.apply([sys_mod, user_mod])

    # Unpatch with include_system=True
    report = runner_inst.unpatch_all([sys_mod, user_mod], include_system=True)
    assert report["status"] == "unpatched"
    assert report["include_system"] is True
    assert "core_user.py" in report["restored_files"]
    assert "core_sys.py" in report["restored_files"]

    # Both files restored to pristine upstream
    assert user_file.read_text(encoding="utf-8") == "USER_VAL = 'user_orig'\n"
    assert sys_file.read_text(encoding="utf-8") == "SYS_VAL = 'sys_orig'\n"
    assert runner_inst.is_any_applied([sys_mod]) is False
    assert runner_inst.is_any_applied([user_mod]) is False


def test_killswitch_unpatches_custom_mods_but_keeps_system_mods(tmp_path: Path):
    """Verify that mods.enabled: false rolls back custom mods while keeping system mods applied."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    sys_file = repo_dir / "core_sys.py"
    user_file = repo_dir / "core_user.py"
    sys_file.write_text("SYS_VAL = 'sys_orig'\n", encoding="utf-8")
    user_file.write_text("USER_VAL = 'user_orig'\n", encoding="utf-8")

    sys_mod = _create_test_system_mod(
        mods_dir,
        "mod-folder-skills",
        targets=[ModTarget(file="core_sys.py", old_block="SYS_VAL = 'sys_orig'", new_block="SYS_VAL = 'sys_patched'")],
        name="Folder Skills",
    )
    user_mod = _create_test_mod(
        mods_dir,
        "user-mod",
        targets=[ModTarget(file="core_user.py", old_block="USER_VAL = 'user_orig'", new_block="USER_VAL = 'user_patched'")],
        name="User Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)

    # 1. Apply both when enabled
    runner_inst.apply([sys_mod, user_mod])
    assert "SYS_VAL = 'sys_patched'" in sys_file.read_text(encoding="utf-8")
    assert "USER_VAL = 'user_patched'" in user_file.read_text(encoding="utf-8")

    # 2. Trip killswitch: mods.enabled: false
    config_file.write_text("mods:\n  enabled: false\n", encoding="utf-8")
    assert runner_inst.is_mods_enabled() is False

    # 3. Apply triggered
    results = runner_inst.apply([sys_mod, user_mod])
    user_res = next(r for r in results if r["id"] == "user-mod")
    sys_res = next(r for r in results if r["id"] == "mod-folder-skills")

    assert user_res["status"] == "blocked_killswitch"
    assert user_res["unpatch_report"] is not None
    assert "core_user.py" in user_res["unpatch_report"]["restored_files"]
    assert "core_sys.py" not in user_res["unpatch_report"]["restored_files"]

    assert sys_res["status"] in ("applied", "already_applied")

    # Verify filesystem: custom mod rolled back, system mod intact!
    assert user_file.read_text(encoding="utf-8") == "USER_VAL = 'user_orig'\n"
    assert "SYS_VAL = 'sys_patched'" in sys_file.read_text(encoding="utf-8")


def test_cli_unpatch_system_and_all_flags(tmp_path: Path, capsys):
    """Verify handle_unpatch_cli with default (custom only) vs --all / --system flags."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    sys_file = repo_dir / "sys.py"
    user_file = repo_dir / "user.py"
    sys_file.write_text("SYS = 1\n", encoding="utf-8")
    user_file.write_text("USER = 1\n", encoding="utf-8")

    sys_mod = _create_test_system_mod(
        mods_dir,
        "mod-update-integration",
        targets=[ModTarget(file="sys.py", old_block="SYS = 1", new_block="SYS = 2")],
        name="Update Integration",
    )
    user_mod = _create_test_mod(
        mods_dir,
        "custom-feature",
        targets=[ModTarget(file="user.py", old_block="USER = 1", new_block="USER = 2")],
        name="Custom Feature",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    runner_inst.apply([sys_mod, user_mod])

    # 1. Default unpatch: only custom mod
    class DefaultArgs:
        action = "unpatch"
        target = None
        all_mods = False
        include_system = False
        json = False

    ret = handle_unpatch_cli(DefaultArgs(), [sys_mod, user_mod], runner_inst)
    cap = capsys.readouterr()
    assert ret == 0
    assert "Все пользовательские моды (custom/) успешно сняты. Системные компоненты движка активны. (Для полного удаления системных модов используйте --all)." in cap.out
    assert user_file.read_text(encoding="utf-8") == "USER = 1\n"
    assert "SYS = 2" in sys_file.read_text(encoding="utf-8")

    # Re-apply user mod
    runner_inst.apply([user_mod])
    assert "USER = 2" in user_file.read_text(encoding="utf-8")

    # 2. Unpatch with --all flag
    class AllArgs:
        action = "unpatch"
        target = None
        all_mods = True
        include_system = False
        json = False

    ret2 = handle_unpatch_cli(AllArgs(), [sys_mod, user_mod], runner_inst)
    cap2 = capsys.readouterr()
    assert ret2 == 0
    assert "Все моды успешно сняты. Ядро Hermes возвращено к исходному состоянию." in cap2.out
    assert user_file.read_text(encoding="utf-8") == "USER = 1\n"
    assert sys_file.read_text(encoding="utf-8") == "SYS = 1\n"


def test_purge_cli_requires_confirmation(tmp_path: Path, capsys):
    """Verify handle_purge_cli refuses without --confirm / --yes."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    sys_file = repo_dir / "sys.py"
    sys_file.write_text("SYS = 100\n", encoding="utf-8")

    sys_mod = _create_test_system_mod(
        mods_dir,
        "mod-command-registry",
        targets=[ModTarget(file="sys.py", old_block="SYS = 100", new_block="SYS = 200")],
        name="Command Registry",
    )
    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    runner_inst.apply([sys_mod])
    assert "SYS = 200" in sys_file.read_text(encoding="utf-8")

    class UnconfirmedArgs:
        action = "purge"
        target = None
        confirm = False
        yes = False
        json = False

    ret = handle_purge_cli(UnconfirmedArgs(), [sys_mod], runner_inst)
    cap = capsys.readouterr()
    combined_out = cap.out + cap.err

    assert ret == 1
    assert "Это полностью снимет все моды, включая системные. Для подтверждения укажите --confirm" in combined_out
    # Mod was NOT unpatched
    assert "SYS = 200" in sys_file.read_text(encoding="utf-8")


def test_purge_cli_with_confirmation_wipes_all_and_caches(tmp_path: Path, capsys):
    """Verify handle_purge_cli with --confirm cleanly unpatches all mods and removes test/commands caches."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    sys_file = repo_dir / "sys.py"
    user_file = repo_dir / "user.py"
    sys_file.write_text("ORIG_SYS = True\n", encoding="utf-8")
    user_file.write_text("ORIG_USER = True\n", encoding="utf-8")

    sys_mod = _create_test_system_mod(
        mods_dir,
        "mod-command-registry",
        targets=[ModTarget(file="sys.py", old_block="ORIG_SYS = True", new_block="ORIG_SYS = False")],
        name="Command Registry",
    )
    user_mod = _create_test_mod(
        mods_dir,
        "user-mod",
        targets=[ModTarget(file="user.py", old_block="ORIG_USER = True", new_block="ORIG_USER = False")],
        name="User Mod",
    )

    runner_inst = ModRunner(repo_root=repo_dir, mods_dir=mods_dir, backups_dir=backups_dir, config_file=config_file)
    runner_inst.apply([sys_mod, user_mod])

    # Create dummy cache files
    test_cache = mods_dir / ".test_cache.json"
    test_cache.write_text('{"dummy": true}', encoding="utf-8")
    cmd_cache = mods_dir / ".commands_cache.json"
    cmd_cache.write_text('{"dummy_cmd": true}', encoding="utf-8")

    class ConfirmedArgs:
        action = "purge"
        target = None
        confirm = True
        yes = False
        json = False

    ret = handle_purge_cli(ConfirmedArgs(), [sys_mod, user_mod], runner_inst)
    cap = capsys.readouterr()

    assert ret == 0
    assert "Все моды (включая системные компоненты движка) полностью удалены. Ядро Hermes возвращено к чистому upstream." in cap.out
    assert sys_file.read_text(encoding="utf-8") == "ORIG_SYS = True\n"
    assert user_file.read_text(encoding="utf-8") == "ORIG_USER = True\n"
    # test cache file deleted
    assert not test_cache.exists()
    # commands cache cleared to {}
    assert json.loads(cmd_cache.read_text(encoding="utf-8")) == {}


def test_cli_subprocess_purge(tmp_path: Path):
    """Verify subprocess CLI runner.py purge requires --confirm and works with --confirm."""
    repo_dir, mods_dir, backups_dir, config_file = _create_mock_environment(tmp_path)
    config_file.write_text("mods:\n  enabled: true\n", encoding="utf-8")

    core_file = repo_dir / "target.py"
    core_file.write_text("value = 1\n", encoding="utf-8")

    _create_test_system_mod(
        mods_dir,
        "mod-command-registry",
        targets=[ModTarget(file="target.py", old_block="value = 1", new_block="value = 999")],
        name="Command Registry",
    )

    env = os.environ.copy()
    env["HERMES_ROOT"] = str(repo_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)
    env["HERMES_CONFIG_FILE"] = str(config_file)
    env["PYTHONPATH"] = f"{MODS_DIR}:{MODS_DIR / '.engine'}"

    # 1. Apply
    p_apply = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "apply", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p_apply.returncode == 0
    assert "value = 999" in core_file.read_text(encoding="utf-8")

    # 2. Purge without --confirm
    p_purge_no_confirm = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "purge", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p_purge_no_confirm.returncode == 1
    combined = p_purge_no_confirm.stdout + p_purge_no_confirm.stderr
    assert "Это полностью снимет все моды, включая системные. Для подтверждения укажите --confirm" in combined
    assert "value = 999" in core_file.read_text(encoding="utf-8")

    # 3. Purge with --confirm
    p_purge_confirmed = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "purge", "--confirm", "--mods-dir", str(mods_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert p_purge_confirmed.returncode == 0
    assert "Все моды (включая системные компоненты движка) полностью удалены. Ядро Hermes возвращено к чистому upstream." in p_purge_confirmed.stdout
    assert core_file.read_text(encoding="utf-8") == "value = 1\n"
