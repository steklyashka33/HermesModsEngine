"""Comprehensive test suite for Hermes Mod Engine v1.4.0 improvements:
- Crash-Consistent Atomic Write (atomic_write_text)
- Per-Mod Atomic Rollback (All-or-Nothing for a single mod; other mods intact)
- Cross-Mod Collision Simulation & Whole-AST Pre-Flight (simulate_composite_apply)
- Non-destructive enable/disable via .state.json (preserving comments and Git status)
- Concurrent File Locking (fcntl.flock on secrets.json, .state.json, .commands_cache.json)
- Developer Tools: runner.py diff & runner.py doctor
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch
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

from engine.models import Mod, ModTarget
from engine.version import ENGINE_VERSION
from engine.file_ops import FileLock, atomic_write_text, SignalTrap
from engine.config_manager import SecretsStorage
from engine.updater import StateManager
from engine.loader import update_commands_cache, ModDependencyGraph
from engine.validator import ModValidator, CompositeSimulationReport
from runner import ModLoader, ModRunner, HERMES_ROOT


# ============================================================================
# 1. Crash-Consistent Atomic Write & File Locking
# ============================================================================

def test_atomic_write_text_success_and_permissions(tmp_path: Path):
    target = tmp_path / "subdir" / "sample.py"
    content = "x = 42\nprint(x)\n"
    atomic_write_text(target, content)

    assert target.exists()
    assert target.read_text(encoding="utf-8") == content

    # Test permission preservation
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    atomic_write_text(target, "x = 43\n")
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600


def test_atomic_write_cleanup_on_failure(tmp_path: Path):
    target = tmp_path / "fail.py"
    target.write_text("initial", encoding="utf-8")

    # Simulate error by pointing to non-writable target or patching os.replace
    with patch("os.replace", side_effect=OSError("Disk write failed")):
        with pytest.raises(OSError):
            atomic_write_text(target, "corrupted")

    # Target remains untouched
    assert target.read_text(encoding="utf-8") == "initial"
    # No temporary files left behind
    tmp_files = list(tmp_path.glob(".tmp_*"))
    assert len(tmp_files) == 0


def test_file_lock_mutual_exclusion(tmp_path: Path):
    lock_file = tmp_path / "test.lock"

    # Acquire lock in first context
    with FileLock(lock_file, timeout=1.0) as lock1:
        assert lock1.fd is not None

        # Trying to acquire same lock with short timeout should raise TimeoutError
        with pytest.raises(TimeoutError) as exc_info:
            with FileLock(lock_file, timeout=0.1):
                pass
        assert "Could not acquire lock" in str(exc_info.value)

    # After exit, lock can be acquired again immediately
    with FileLock(lock_file, timeout=0.5) as lock2:
        assert lock2.fd is not None


def test_secrets_storage_flock_concurrency(tmp_path: Path):
    sec_file = tmp_path / "secrets.json"
    storage = SecretsStorage(sec_file)

    storage.set_mod_secret("mod-a", "token", "val1")
    assert storage.get_mod_secrets("mod-a") == {"token": "val1"}
    assert stat.S_IMODE(sec_file.stat().st_mode) == 0o600

    # Modify under lock
    storage.set_mod_secret("mod-a", "api_key", "val2")
    secrets = storage.load_all()
    assert secrets["mods"]["mod-a"]["token"] == "val1"
    assert secrets["mods"]["mod-a"]["api_key"] == "val2"


def test_state_manager_flock_protection(tmp_path: Path):
    state_file = tmp_path / ".state.json"
    mgr = StateManager(state_file)

    mgr.set_mod_override("mod-x", {"enabled": False})
    mgr.set_mod_override("mod-y", {"enabled": True})

    loaded = mgr.load()
    assert loaded["overrides"]["mod-x"]["enabled"] is False
    assert loaded["overrides"]["mod-y"]["enabled"] is True


def test_update_commands_cache_flock(tmp_path: Path):
    mod = Mod(
        id="cmd-mod",
        name="Cmd Mod",
        path=tmp_path / "mod.yaml",
        targets=[],
        enabled=True,
    )
    cache_path = update_commands_cache([mod], tmp_path)
    assert cache_path.exists()
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "_meta" in data


# ============================================================================
# 2. Per-Mod Atomic Rollback (Single Mod All-or-Nothing)
# ============================================================================

def test_per_mod_atomic_rollback_on_verify_cmd_fail(tmp_path: Path):
    """When a mod's verify_cmd fails, all targets of THAT mod are rolled back."""
    repo_file1 = tmp_path / "core1.py"
    repo_file2 = tmp_path / "core2.py"
    repo_file1.write_text("val1 = 10\n", encoding="utf-8")
    repo_file2.write_text("val2 = 20\n", encoding="utf-8")

    mod_path = tmp_path / "bad.yaml"
    mod_path.write_text("id: bad-mod\ntargets: []\n", encoding="utf-8")

    mod = Mod(
        id="bad-mod",
        name="Bad Mod",
        path=mod_path,
        enabled=True,
        targets=[
            ModTarget(file="core1.py", old_block="val1 = 10", new_block="val1 = 99"),
            ModTarget(file="core2.py", old_block="val2 = 20", new_block="val2 = 99"),
        ],
        verify_cmd="python3 -c 'import sys; sys.exit(1)'",
    )

    runner = ModRunner(repo_root=tmp_path)
    res = runner.apply_mod(mod, apply_changes=True)

    assert res["status"] == "failed_verification_rolled_back"
    assert res["verified"] is False
    # Ensure BOTH target files were rolled back to their original state
    assert repo_file1.read_text(encoding="utf-8") == "val1 = 10\n"
    assert repo_file2.read_text(encoding="utf-8") == "val2 = 20\n"


def test_per_mod_atomic_rollback_does_not_affect_other_mods(tmp_path: Path):
    """Failure in one mod does NOT roll back previous successful mods."""
    f_good = tmp_path / "good.py"
    f_bad = tmp_path / "bad.py"
    f_good.write_text("good_val = 1\n", encoding="utf-8")
    f_bad.write_text("bad_val = 1\n", encoding="utf-8")

    path_good = tmp_path / "good.yaml"
    path_good.write_text("id: mod-good\ntargets: []\n", encoding="utf-8")
    path_bad = tmp_path / "bad.yaml"
    path_bad.write_text("id: mod-bad\ntargets: []\n", encoding="utf-8")

    mod_good = Mod(
        id="mod-good",
        name="Good Mod",
        path=path_good,
        enabled=True,
        targets=[ModTarget(file="good.py", old_block="good_val = 1", new_block="good_val = 2")],
    )
    mod_bad = Mod(
        id="mod-bad",
        name="Bad Mod",
        path=path_bad,
        enabled=True,
        targets=[ModTarget(file="bad.py", old_block="bad_val = 1", new_block="bad_val = 999")],
        verify_cmd="python3 -c 'import sys; sys.exit(1)'",
    )

    runner = ModRunner(repo_root=tmp_path)
    results = runner.run_all([mod_good, mod_bad], apply_changes=True)

    res_good = next(r for r in results if r["id"] == "mod-good")
    res_bad = next(r for r in results if r["id"] == "mod-bad")

    # Mod Good must remain applied and verified!
    assert res_good["status"] == "applied"
    assert res_good["verified"] is True
    assert f_good.read_text(encoding="utf-8") == "good_val = 2\n"

    # Mod Bad must be rolled back
    assert res_bad["status"] == "failed_verification_rolled_back"
    assert res_bad["verified"] is False
    assert f_bad.read_text(encoding="utf-8") == "bad_val = 1\n"


def test_signal_trap_rollback_execution(tmp_path: Path):
    """Verify SignalTrap invokes rollback handler when triggered."""
    rolled_back = False

    def do_rollback():
        nonlocal rolled_back
        rolled_back = True

    trap = SignalTrap(do_rollback)
    # Simulate signal handler invocation
    with trap:
        with pytest.raises(SystemExit) as exc:
            trap._handler(signal.SIGTERM, None)
        assert exc.value.code == 128 + signal.SIGTERM

    assert rolled_back is True


# ============================================================================
# 3. Cross-Mod Collision Simulation & Composite AST Pre-Flight
# ============================================================================

def test_simulate_composite_apply_clean_flow(tmp_path: Path):
    f = tmp_path / "app.py"
    f.write_text("step0 = True\nstep1 = False\n", encoding="utf-8")

    mod_a = Mod(
        id="mod-a",
        name="Mod A",
        path=tmp_path / "a.yaml",
        enabled=True,
        targets=[ModTarget(file="app.py", old_block="step0 = True", new_block="step0 = 100")],
    )
    mod_b = Mod(
        id="mod-b",
        name="Mod B",
        path=tmp_path / "b.yaml",
        enabled=True,
        targets=[ModTarget(file="app.py", old_block="step1 = False", new_block="step1 = 200")],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    report = validator.simulate_composite_apply([mod_a, mod_b])

    assert report.valid is True
    assert len(report.errors) == 0
    # Disk file must remain untouched during simulation
    assert f.read_text(encoding="utf-8") == "step0 = True\nstep1 = False\n"


def test_simulate_composite_apply_detects_collision(tmp_path: Path):
    f = tmp_path / "core.py"
    f.write_text("def handle():\n    return 'base'\n", encoding="utf-8")

    # Mod A replaces the return statement
    mod_a = Mod(
        id="mod-a",
        name="Mod A",
        path=tmp_path / "a.yaml",
        enabled=True,
        targets=[ModTarget(file="core.py", old_block="    return 'base'", new_block="    return 'mod_a'")],
    )
    # Mod B expects the original 'base' return statement which Mod A overwrote
    mod_b = Mod(
        id="mod-b",
        name="Mod B",
        path=tmp_path / "b.yaml",
        enabled=True,
        dependencies=["mod-a"],
        targets=[ModTarget(file="core.py", old_block="    return 'base'", new_block="    return 'mod_b'")],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    report = validator.simulate_composite_apply([mod_a, mod_b])

    assert report.valid is False
    assert len(report.errors) >= 1
    err = report.errors[0]
    assert err.category == "target_conflict"
    assert "mod-a" in err.message  # Mentions earlier mod that modified the file


def test_simulate_composite_apply_detects_composite_syntax_error(tmp_path: Path):
    f = tmp_path / "syntax.py"
    f.write_text("def run():\n    pass\n", encoding="utf-8")

    # Mod A replaces pass with an unclosed statement start
    mod_a = Mod(
        id="mod-a",
        name="Mod A",
        path=tmp_path / "a.yaml",
        enabled=True,
        targets=[ModTarget(file="syntax.py", old_block="    pass", new_block="    if True:")],
    )
    # Mod B appends an unindented statement, making invalid Python AST
    mod_b = Mod(
        id="mod-b",
        name="Mod B",
        path=tmp_path / "b.yaml",
        enabled=True,
        dependencies=["mod-a"],
        targets=[ModTarget(file="syntax.py", old_block="    if True:", new_block="    if True:\nreturn 42")],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    report = validator.simulate_composite_apply([mod_a, mod_b])

    assert report.valid is False
    assert any(e.category == "ast" for e in report.errors)


def test_run_all_blocks_apply_on_simulation_conflict(tmp_path: Path):
    """When simulate_composite_apply fails, run_all blocks execution without touching disk."""
    f = tmp_path / "target.py"
    original = "x = 10\n"
    f.write_text(original, encoding="utf-8")

    mod_a = Mod(
        id="mod-a",
        name="Mod A",
        path=tmp_path / "a.yaml",
        enabled=True,
        targets=[ModTarget(file="target.py", old_block="x = 10", new_block="x = 20")],
    )
    mod_b = Mod(
        id="mod-b",
        name="Mod B",
        path=tmp_path / "b.yaml",
        enabled=True,
        dependencies=["mod-a"],
        targets=[ModTarget(file="target.py", old_block="x = 10", new_block="x = 30")],
    )

    runner = ModRunner(repo_root=tmp_path)
    results = runner.run_all([mod_a, mod_b], apply_changes=True)

    assert any(r["status"] == "failed_preflight_validation" for r in results)
    # Disk file was NEVER touched!
    assert f.read_text(encoding="utf-8") == original


# ============================================================================
# 4. Non-Destructive Enable/Disable via .state.json
# ============================================================================

def test_non_destructive_enable_disable_via_cli(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    yaml_content = (
        "# Important header comment\n"
        "id: my-mod\n"
        "name: My Mod\n"
        "version: 1.0.0\n"
        "enabled: true # inline comment\n"
        "targets: []\n"
    )
    mod_file = mods_dir / "my-mod.yaml"
    mod_file.write_text(yaml_content, encoding="utf-8")

    # 1. Disable via runner.py CLI
    cmd = [sys.executable, str(MODS_DIR / "runner.py"), "disable", "my-mod", "--mods-dir", str(mods_dir)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0

    # YAML file must be 100% UNTOUCHED (comments intact!)
    assert mod_file.read_text(encoding="utf-8") == yaml_content

    # State is recorded in .state.json
    state_file = mods_dir / ".state.json"
    assert state_file.exists()
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_data["overrides"]["my-mod"]["enabled"] is False

    # ModLoader respects .state.json override
    mods = ModLoader.load_all(mods_dir)
    assert next(m for m in mods if m.id == "my-mod").enabled is False

    # 2. Re-enable via CLI
    cmd_en = [sys.executable, str(MODS_DIR / "runner.py"), "enable", "my-mod", "--mods-dir", str(mods_dir)]
    res_en = subprocess.run(cmd_en, capture_output=True, text=True)
    assert res_en.returncode == 0

    # YAML file still 100% untouched
    assert mod_file.read_text(encoding="utf-8") == yaml_content
    mods_en = ModLoader.load_all(mods_dir)
    assert next(m for m in mods_en if m.id == "my-mod").enabled is True


def test_write_manifest_flag_updates_yaml_safely(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()

    yaml_content = (
        "# Top comment\n"
        "id: flag-mod\n"
        "enabled: true\n"
        "targets: []\n"
    )
    mod_file = mods_dir / "flag-mod.yaml"
    mod_file.write_text(yaml_content, encoding="utf-8")

    cmd = [
        sys.executable, str(MODS_DIR / "runner.py"), "disable", "flag-mod",
        "--mods-dir", str(mods_dir), "--write-manifest"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0

    new_content = mod_file.read_text(encoding="utf-8")
    assert "# Top comment" in new_content
    assert "enabled: false" in new_content


# ============================================================================
# 5. Developer Tools: runner.py diff & runner.py doctor
# ============================================================================

def test_runner_diff_cli_output(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    target_file = repo_dir / "core.py"
    target_file.write_text("status = 'idle'\n", encoding="utf-8")

    mod_data = {
        "id": "diff-mod",
        "name": "Diff Mod",
        "version": "1.0.0",
        "enabled": True,
        "targets": [
            {
                "file": "core.py",
                "old_block": "status = 'idle'",
                "new_block": "status = 'active'",
            }
        ],
    }
    (mods_dir / "diff-mod.yaml").write_text(yaml.dump(mod_data), encoding="utf-8")

    with patch("runner.HERMES_ROOT", repo_dir):
        cmd = [
            sys.executable, str(MODS_DIR / "runner.py"), "diff", "diff-mod",
            "--mods-dir", str(mods_dir), "--no-color"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "HERMES_ROOT": str(repo_dir)})
        assert res.returncode == 0
        assert "=== Mod: diff-mod" in res.stdout
        assert "-status = 'idle'" in res.stdout
        assert "+status = 'active'" in res.stdout


def test_runner_diff_json_mode(tmp_path: Path):
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    target_file = repo_dir / "core.py"
    target_file.write_text("x = 1\n", encoding="utf-8")

    mod_data = {
        "id": "json-diff-mod",
        "name": "JSON Diff Mod",
        "version": "1.0.0",
        "enabled": True,
        "targets": [{"file": "core.py", "old_block": "x = 1", "new_block": "x = 2"}],
    }
    (mods_dir / "json-diff-mod.yaml").write_text(yaml.dump(mod_data), encoding="utf-8")

    cmd = [
        sys.executable, str(MODS_DIR / "runner.py"), "diff", "json-diff-mod",
        "--mods-dir", str(mods_dir), "--json"
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "HERMES_ROOT": str(repo_dir)})
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert len(data) == 1
    assert data[0]["mod_id"] == "json-diff-mod"
    assert data[0]["status"] == "pending"
    assert "-x = 1" in data[0]["diff"]


def test_runner_doctor_cli_output():
    cmd = [sys.executable, str(MODS_DIR / "runner.py"), "doctor"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    assert "Doctor Environment Diagnostics" in res.stdout
    assert "Python Environment" in res.stdout
    assert "Git CLI" in res.stdout
    assert "Hermes Core" in res.stdout
    assert "Secrets Storage" in res.stdout
    assert "Engine Integrity" in res.stdout


def test_runner_doctor_json_mode():
    cmd = [sys.executable, str(MODS_DIR / "runner.py"), "doctor", "--json"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0
    checks = json.loads(res.stdout)
    assert isinstance(checks, list)
    names = [c["name"] for c in checks]
    assert "Python Environment" in names
    assert "Git CLI" in names
    assert "Hermes Core" in names
    assert "Secrets Storage" in names
    assert "Engine Integrity" in names
    assert all(c["status"] in ("ok", "warn") for c in checks)
