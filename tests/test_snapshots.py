"""Tests for Hermes Core Snapshot Manager and Watchdog Rollback subsystem.

Covers:
1. Snapshot creation, metadata, hashing and structure.
2. Snapshot listing, filtering and latest pointer resolution.
3. Snapshot restoration (file rollback, deletion of new files, checksum validation).
4. Snapshot pruning and deletion.
5. Gateway Watchdog health checks, stability monitoring, and automated rollback upon crash.
6. CLI integration in runner.py (snapshot create/list/restore and apply auto-snapshot).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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

from engine.snapshot_manager import (
    SnapshotManager,
    SnapshotManifest,
    SnapshotFileRecord,
    RestoreResult,
    compute_file_sha256,
)
from engine.watchdog import (
    GatewayWatchdog,
    ServiceHealthStatus,
    WatchdogResult,
)
from runner import ModLoader, ModRunner, Mod


@pytest.fixture
def test_env(tmp_path: Path):
    """Creates an isolated Hermes core repo, snapshots directory, and mods dir."""
    core_root = tmp_path / "hermes_core"
    core_root.mkdir(parents=True, exist_ok=True)

    # Core target files
    agent_dir = core_root / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    turn_file = agent_dir / "turn_runner.py"
    turn_file.write_text("def run_turn():\n    return 'original_v1'\n", encoding="utf-8")

    cli_dir = core_root / "hermes_cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cli_dir / "commands.py"
    cmd_file.write_text("# Core commands\nREGISTRY = ['help']\n", encoding="utf-8")

    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)

    # Simple declarative test mod
    mod_yaml = mods_dir / "test_patch.yaml"
    mod_yaml.write_text(
        """id: test-patch
name: Test Patch Mod
version: 1.0.0
enabled: true
targets:
  - file: agent/turn_runner.py
    old_block: "def run_turn():\\n    return 'original_v1'\\n"
    new_block: "def run_turn():\\n    return 'patched_v2'\\n"
""",
        encoding="utf-8",
    )

    mgr = SnapshotManager(snapshots_dir=snapshots_dir, repo_root=core_root)
    return {
        "core_root": core_root,
        "snapshots_dir": snapshots_dir,
        "mods_dir": mods_dir,
        "turn_file": turn_file,
        "cmd_file": cmd_file,
        "mgr": mgr,
    }


# ============================================================================
# 1. Snapshot Creation, Checksums, and Structure
# ============================================================================

def test_create_snapshot_basic(test_env):
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]
    cmd_file: Path = test_env["cmd_file"]

    snap = mgr.create_snapshot(
        files=[turn_file, cmd_file],
        description="Initial test snapshot",
        trigger="unit_test",
        mod_ids=["test-mod-1"],
    )

    assert snap.id.startswith("unit_test_")
    assert snap.description == "Initial test snapshot"
    assert snap.trigger == "unit_test"
    assert snap.applied_mods == ["test-mod-1"]
    assert len(snap.files) == 2
    assert "agent/turn_runner.py" in snap.files
    assert "hermes_cli/commands.py" in snap.files

    # Verify backup copies exist on disk
    snap_dir = test_env["snapshots_dir"] / snap.id
    assert (snap_dir / "manifest.json").is_file()
    assert (snap_dir / "files" / "agent" / "turn_runner.py").is_file()
    assert (snap_dir / "files" / "hermes_cli" / "commands.py").is_file()

    # Verify content and checksum
    saved_content = (snap_dir / "files" / "agent" / "turn_runner.py").read_text(encoding="utf-8")
    assert "original_v1" in saved_content
    expected_sha = compute_file_sha256(turn_file)
    assert snap.files["agent/turn_runner.py"]["sha256"] == expected_sha
    assert snap.files["agent/turn_runner.py"]["exists_before"] is True


def test_create_snapshot_with_nonexistent_file(test_env):
    """If a file does not exist before mod apply, it is marked exists_before=False."""
    mgr: SnapshotManager = test_env["mgr"]
    core_root: Path = test_env["core_root"]
    non_existent = core_root / "gateway" / "new_handler.py"

    snap = mgr.create_snapshot(
        files=[test_env["turn_file"], non_existent],
        description="Snapshot with newly added file target",
        trigger="pre_apply",
    )

    assert "gateway/new_handler.py" in snap.files
    assert snap.files["gateway/new_handler.py"]["exists_before"] is False
    assert snap.files["gateway/new_handler.py"]["sha256"] == ""


# ============================================================================
# 2. Snapshot Listing & Latest Resolution
# ============================================================================

def test_list_and_get_latest_snapshots(test_env):
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]

    snap1 = mgr.create_snapshot(files=[turn_file], description="Snap 1", trigger="manual")
    time.sleep(0.05)
    snap2 = mgr.create_snapshot(files=[turn_file], description="Snap 2", trigger="pre_apply")
    time.sleep(0.05)
    snap3 = mgr.create_snapshot(files=[turn_file], description="Snap 3", trigger="cli")

    all_snaps = mgr.list_snapshots()
    assert len(all_snaps) == 3
    # Sorted newest first
    assert all_snaps[0]["id"] == snap3.id
    assert all_snaps[1]["id"] == snap2.id
    assert all_snaps[2]["id"] == snap1.id

    # Resolve latest
    latest_snap = mgr.get_latest_snapshot()
    assert latest_snap is not None
    assert latest_snap.id == snap3.id

    # Resolve by explicit ID
    loaded_snap2 = mgr.get_snapshot_manifest(snap2.id)
    assert loaded_snap2 is not None
    assert loaded_snap2.id == snap2.id
    assert loaded_snap2.description == "Snap 2"


# ============================================================================
# 3. Snapshot Restoration (Rollback & Cleanup)
# ============================================================================

def test_restore_snapshot_file_mutation(test_env):
    """Mutating a core file and restoring it recovers original pristine content."""
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]

    snap = mgr.create_snapshot(files=[turn_file], description="Before mutation")

    # Mutate the live file (simulate broken patch or crash)
    turn_file.write_text("BROKEN SYNTAX ERROR !!! def corrupted()", encoding="utf-8")
    assert "BROKEN" in turn_file.read_text(encoding="utf-8")

    # Restore snapshot
    res: RestoreResult = mgr.restore_snapshot(snap.id)
    assert res.success is True
    assert "agent/turn_runner.py" in res.restored_files
    assert len(res.errors) == 0

    # Content restored
    restored_text = turn_file.read_text(encoding="utf-8")
    assert "def run_turn():\n    return 'original_v1'\n" == restored_text


def test_restore_snapshot_deletes_newly_created_files(test_env):
    """If a mod created a brand new file, restore removes it returning core to pristine."""
    mgr: SnapshotManager = test_env["mgr"]
    core_root: Path = test_env["core_root"]
    brand_new = core_root / "agent" / "injected_module.py"

    # Snapshot when file does not exist
    snap = mgr.create_snapshot(files=[brand_new], description="Before new file created")

    # Now file is created
    brand_new.write_text("# Injected by rogue mod\ndef rogue(): pass", encoding="utf-8")
    assert brand_new.exists()

    # Restore
    res = mgr.restore_snapshot(snap.id)
    assert res.success is True
    assert "agent/injected_module.py" in res.deleted_files
    assert not brand_new.exists()


def test_restore_latest_snapshot(test_env):
    """Calling restore_snapshot without ID or with 'latest' restores the most recent snapshot."""
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]

    turn_file.write_text("v1", encoding="utf-8")
    snap1 = mgr.create_snapshot(files=[turn_file], description="v1")

    time.sleep(0.05)
    turn_file.write_text("v2", encoding="utf-8")
    snap2 = mgr.create_snapshot(files=[turn_file], description="v2")

    turn_file.write_text("corrupted_v3", encoding="utf-8")

    # Restore latest (should be v2)
    res = mgr.restore_snapshot("latest")
    assert res.success is True
    assert res.snapshot_id == snap2.id
    assert turn_file.read_text(encoding="utf-8") == "v2"


def test_restore_nonexistent_snapshot(test_env):
    mgr: SnapshotManager = test_env["mgr"]
    res = mgr.restore_snapshot("non_existent_id_999")
    assert res.success is False
    assert len(res.errors) > 0


# ============================================================================
# 4. Pruning and Deletion
# ============================================================================

def test_delete_and_prune_snapshots(test_env):
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]

    snaps = []
    for i in range(5):
        s = mgr.create_snapshot(files=[turn_file], description=f"Snap {i}")
        snaps.append(s)
        time.sleep(0.02)

    assert len(mgr.list_snapshots()) == 5

    # Delete one
    deleted = mgr.delete_snapshot(snaps[2].id)
    assert deleted is True
    remaining = [s["id"] for s in mgr.list_snapshots()]
    assert snaps[2].id not in remaining
    assert len(remaining) == 4

    # Prune keep 2
    mgr.prune_snapshots(keep=2)
    after_prune = mgr.list_snapshots()
    assert len(after_prune) == 2
    assert after_prune[0]["id"] == snaps[4].id
    assert after_prune[1]["id"] == snaps[3].id


# ============================================================================
# 5. Gateway Watchdog Health Checks & Automated Rollback
# ============================================================================

def test_watchdog_healthy_window(test_env):
    """When the gateway is healthy for the observation window, watchdog succeeds without rollback."""
    mgr: SnapshotManager = test_env["mgr"]
    watchdog = GatewayWatchdog(service_name="test-service", snapshot_mgr=mgr)

    # Mock health status: always active and stable
    mock_status = ServiceHealthStatus(
        is_active=True,
        is_failed=False,
        pid=12345,
        restarts=0,
        active_state="active",
        sub_state="running",
    )

    with patch.object(watchdog, "check_service_health", return_value=mock_status):
        res: WatchdogResult = watchdog.monitor_and_rollback(
            window_seconds=0.2,
            poll_interval=0.05,
            start_timeout=0.2,
            auto_restore=True,
        )
        assert res.success is True
        assert res.rolled_back is False
        assert res.exit_code == 0
        assert res.error is None


def test_watchdog_crashes_and_rolls_back(test_env):
    """When gateway crashes into failed state, watchdog automatically triggers snapshot rollback."""
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]

    # Initial state saved in snapshot
    snap = mgr.create_snapshot(files=[turn_file], description="Watchdog rollback test baseline")

    # Mod modifies core
    turn_file.write_text("BROKEN CRASH CODE", encoding="utf-8")

    watchdog = GatewayWatchdog(service_name="test-service", snapshot_mgr=mgr)

    # Health returns active at t=0, then failed at t=0.05
    calls = 0

    def mock_health():
        nonlocal calls
        calls += 1
        if calls == 1:
            return ServiceHealthStatus(
                is_active=True, is_failed=False, pid=9999, restarts=0, active_state="active", sub_state="running"
            )
        return ServiceHealthStatus(
            is_active=False, is_failed=True, pid=9999, restarts=0, active_state="failed", sub_state="failed"
        )

    with patch.object(watchdog, "check_service_health", side_effect=mock_health), \
         patch.object(watchdog, "restart_service", return_value=True):
        res: WatchdogResult = watchdog.monitor_and_rollback(
            snapshot_id=snap.id,
            window_seconds=1.0,
            poll_interval=0.02,
            start_timeout=0.2,
            auto_restore=True,
            restart_service_on_rollback=False,
        )

        assert res.success is False
        assert res.rolled_back is True
        assert res.snapshot_id == snap.id
        assert "agent/turn_runner.py" in res.restored_files
        assert res.exit_code == 1

        # Confirm live core file was restored
        assert "def run_turn():\n    return 'original_v1'\n" == turn_file.read_text(encoding="utf-8")


def test_watchdog_restart_loop_detection(test_env):
    """Detects PID changing (restarts) during the observation window and rolls back."""
    mgr: SnapshotManager = test_env["mgr"]
    turn_file: Path = test_env["turn_file"]
    snap = mgr.create_snapshot(files=[turn_file], description="Baseline for restart loop")

    turn_file.write_text("FLAPPING CODE", encoding="utf-8")
    watchdog = GatewayWatchdog(service_name="test-service", snapshot_mgr=mgr)

    step = 0

    def mock_flapping_health():
        nonlocal step
        step += 1
        # First call: active with PID 1000
        if step == 1:
            return ServiceHealthStatus(
                is_active=True, is_failed=False, pid=1000, restarts=0, active_state="active", sub_state="running"
            )
        # Second call: restarted with new PID 1001!
        return ServiceHealthStatus(
            is_active=True, is_failed=False, pid=1001, restarts=1, active_state="active", sub_state="running"
        )

    with patch.object(watchdog, "check_service_health", side_effect=mock_flapping_health):
        res = watchdog.monitor_and_rollback(
            snapshot_id=snap.id,
            window_seconds=0.5,
            poll_interval=0.02,
            auto_restore=True,
            restart_service_on_rollback=False,
        )

        assert res.success is False
        assert res.rolled_back is True
        assert "Обнаружен циклический перезапуск" in res.error or "PID" in res.error
        assert turn_file.read_text(encoding="utf-8") == "def run_turn():\n    return 'original_v1'\n"


# ============================================================================
# 6. Integration with runner.py
# ============================================================================

def test_runner_apply_automatically_creates_snapshot(test_env):
    """runner.run_all(apply_changes=True) automatically creates pre_apply snapshot."""
    core_root = test_env["core_root"]
    mods_dir = test_env["mods_dir"]
    snapshots_dir = test_env["snapshots_dir"]

    runner = ModRunner(repo_root=core_root, mods_dir=mods_dir)
    runner.snapshot_mgr = SnapshotManager(snapshots_dir=snapshots_dir, repo_root=core_root)

    mods = ModLoader.load_all(mods_dir)
    assert len(mods) >= 1

    # Apply mod
    results = runner.run_all(mods, apply_changes=True)
    assert any(r.get("status") in ("applied", "already_applied") for r in results)

    # Check that a pre_apply snapshot was created
    snaps = runner.snapshot_mgr.list_snapshots()
    assert len(snaps) >= 1
    latest = snaps[0]
    assert latest["trigger"] == "pre_apply"
    assert "agent/turn_runner.py" in latest["files"]
    assert "test-patch" in latest["applied_mods"]


def test_cli_snapshot_commands(test_env):
    """Test runner.py snapshot create, list, and restore CLI commands."""
    core_root = test_env["core_root"]
    mods_dir = test_env["mods_dir"]
    snapshots_dir = test_env["snapshots_dir"]

    env = os.environ.copy()
    env["HERMES_ROOT"] = str(core_root)
    env["HERMES_SNAPSHOTS_DIR"] = str(snapshots_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)

    runner_script = Path(__file__).resolve().parent.parent / "runner.py"

    # 1. snapshot create
    res_create = subprocess.run(
        [sys.executable, str(runner_script), "snapshot", "create", "--desc", "CLI test snap", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_create.returncode == 0
    snap_data = json.loads(res_create.stdout)
    snap_id = snap_data["id"]
    assert snap_data["description"] == "CLI test snap"

    # 2. snapshot list
    res_list = subprocess.run(
        [sys.executable, str(runner_script), "snapshot", "list", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_list.returncode == 0
    list_data = json.loads(res_list.stdout)
    assert any(s["id"] == snap_id for s in list_data)

    # 3. Modify core file and run snapshot restore
    turn_file = test_env["turn_file"]
    turn_file.write_text("corrupted_by_cli", encoding="utf-8")

    res_restore = subprocess.run(
        [sys.executable, str(runner_script), "snapshot", "restore", snap_id, "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_restore.returncode == 0
    restore_data = json.loads(res_restore.stdout)
    assert restore_data["success"] is True
    assert "agent/turn_runner.py" in restore_data["restored_files"]
    assert "return 'original_v1'" in turn_file.read_text(encoding="utf-8")


def test_gateway_watchdog_script_standalone(test_env):
    """Test invoking scripts/gateway_watchdog.py directly as a standalone script."""
    core_root = test_env["core_root"]
    snapshots_dir = test_env["snapshots_dir"]

    env = os.environ.copy()
    env["HERMES_ROOT"] = str(core_root)
    env["HERMES_SNAPSHOTS_DIR"] = str(snapshots_dir)

    script_path = Path(__file__).resolve().parent.parent / "scripts" / "gateway_watchdog.py"

    res = subprocess.run(
        [sys.executable, str(script_path), "--window", "0.1", "--poll", "0.05", "--start-timeout", "0.1", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    # The command should run and produce valid JSON output
    data = json.loads(res.stdout)
    assert "service_name" in data
    assert "rolled_back" in data
    assert "stable_seconds" in data


def test_snapshot_restore_checksum_mismatch(test_env):
    """If backup file in snapshot is tampered with, restore detects checksum mismatch."""
    mgr: SnapshotManager = test_env["mgr"]
    turn_file = test_env["turn_file"]

    snap = mgr.create_snapshot(files=[turn_file], description="Integrity check")
    snap_dir = test_env["snapshots_dir"] / snap.id
    backup_file = snap_dir / "files" / "agent" / "turn_runner.py"

    # Tamper with backup file
    backup_file.write_text("TAMPERED DATA", encoding="utf-8")

    res = mgr.restore_snapshot(snap.id)
    assert res.success is False
    assert any("Контрольная сумма" in e for e in res.errors)


def test_snapshot_create_with_explicit_mod_id(test_env):
    """runner.py snapshot create <mod_id> creates snapshot only for that mod's targets."""
    core_root = test_env["core_root"]
    mods_dir = test_env["mods_dir"]
    snapshots_dir = test_env["snapshots_dir"]

    env = os.environ.copy()
    env["HERMES_ROOT"] = str(core_root)
    env["HERMES_SNAPSHOTS_DIR"] = str(snapshots_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)

    runner_script = Path(__file__).resolve().parent.parent / "runner.py"

    res = subprocess.run(
        [sys.executable, str(runner_script), "snapshot", "create", "test-patch", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert "agent/turn_runner.py" in data["files"]
    assert data["applied_mods"] == ["test-patch"]

