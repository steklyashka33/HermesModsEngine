"""Unit tests for Hermes Mods Runtime Circuit Breaker.

Verifies:
1. Incident tracking and sliding window error counting.
2. Threshold tripping: >= 5 errors in 5 minutes trips breaker (OPEN).
3. Mod isolation: marking as `disabled_circuit_breaker` in .state.json.
4. Single protected warning: emitted once to log/state, prevents warning spam.
5. Surgical rollback: target files of the broken mod are restored while healthy mods remain applied.
6. CLI integration: `runner.py disable <id> --circuit-breaker` and `runner.py circuit-breaker`.
7. System engine mod protection: Tier 0 core mods cannot be disabled by circuit breaker.
8. Cooldown and HALF_OPEN state recovery.
9. Protection context manager and ModCommandRegistry dispatch integration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
import pytest

from engine.circuit_breaker import (
    CircuitBreaker,
    get_circuit_breaker,
    CIRCUIT_STATE_CLOSED,
    CIRCUIT_STATE_OPEN,
    CIRCUIT_STATE_HALF_OPEN,
    SYSTEM_MOD_IDS,
)
from engine.models import Mod, ModTarget
from engine.updater import StateManager
from runner import ModLoader, ModRunner, is_system_mod


def _setup_test_env(tmp_path: Path):
    """Setup isolated test environment with repo, mods_dir, and backups_dir."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    mods_dir = tmp_path / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    custom_dir = mods_dir / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)
    backups_dir = tmp_path / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)

    # Copy engine to test mods_dir so runner/loader work
    engine_src = Path(__file__).resolve().parent.parent / ".engine"
    if engine_src.is_dir():
        shutil.copytree(engine_src, mods_dir / ".engine", dirs_exist_ok=True)

    # Copy system mods to test mods_dir
    system_src = Path(__file__).resolve().parent.parent / "system"
    if system_src.is_dir():
        shutil.copytree(system_src, mods_dir / "system", dirs_exist_ok=True)

    # Copy runner.py
    runner_src = Path(__file__).resolve().parent.parent / "runner.py"
    if runner_src.is_file():
        shutil.copy2(runner_src, mods_dir / "runner.py")

    return repo_dir, mods_dir, backups_dir


def _create_mock_mod(
    mods_dir: Path,
    mod_id: str,
    targets: list[tuple[str, str, str]],  # (rel_file, old_block, new_block)
    author: str = "TestAuthor",
) -> Mod:
    """Helper to create a mod manifest and return Mod instance."""
    mod_dir = mods_dir / "custom" / mod_id
    mod_dir.mkdir(parents=True, exist_ok=True)

    mod_yaml = f"""name: {mod_id.replace('-', ' ').title()}
version: 1.0.0
author: {author}
description: Test mod for {mod_id}
enabled: true
targets:
"""
    for rel_file, old_block, new_block in targets:
        mod_yaml += f"""  - file: {rel_file}
    description: Patch for {rel_file}
    old_block: |
      {old_block.strip()}
    new_block: |
      {new_block.strip()}
"""
    (mod_dir / "mod.yaml").write_text(mod_yaml, encoding="utf-8")
    mod_targets = [
        ModTarget(file=rf, old_block=ob.strip(), new_block=nb.strip(), description=f"Patch {rf}")
        for rf, ob, nb in targets
    ]
    return Mod(
        name=mod_id.replace("-", " ").title(),
        id=mod_id,
        path=mod_dir / "mod.yaml",
        mod_dir=mod_dir,
        author=author,
        targets=mod_targets,
    )


# -----------------------------------------------------------------------------
# Test 1: Clean State and Initialization
# -----------------------------------------------------------------------------
def test_circuit_breaker_init_clean_state(tmp_path: Path):
    cb = CircuitBreaker(mods_dir=tmp_path / "mods", failure_threshold=5, window_seconds=300)
    assert cb.get_state("test-mod") == CIRCUIT_STATE_CLOSED
    assert cb.can_execute("test-mod") is True
    assert cb.is_tripped("test-mod") is False
    assert len(cb.get_failures("test-mod")) == 0


# -----------------------------------------------------------------------------
# Test 2: Sliding Window Error Counting and Pruning
# -----------------------------------------------------------------------------
def test_sliding_window_error_counting_and_pruning(tmp_path: Path):
    cb = CircuitBreaker(mods_dir=tmp_path / "mods", failure_threshold=5, window_seconds=10.0, auto_rollback=False)

    now = 1000.0
    # Record 4 errors at t=1000.0 (less than threshold 5)
    for i in range(4):
        tripped = cb.record_error("mod-a", error=f"Error {i}", timestamp=now)
        assert tripped is False

    assert cb.get_state("mod-a", now=now) == CIRCUIT_STATE_CLOSED
    assert len(cb.get_failures("mod-a", now=now)) == 4

    # Advance time past the 10s window: t=1015.0
    # Old errors should be pruned, count should become 1
    tripped = cb.record_error("mod-a", error="New Error", timestamp=now + 15.0)
    assert tripped is False
    assert cb.get_state("mod-a", now=now + 15.0) == CIRCUIT_STATE_CLOSED

    failures = cb.get_failures("mod-a", window_only=True, now=now + 15.0)
    assert len(failures) == 1
    assert failures[0]["error"] == "New Error"


# -----------------------------------------------------------------------------
# Test 3: Threshold Tripping to OPEN
# -----------------------------------------------------------------------------
def test_threshold_tripping_to_open(tmp_path: Path):
    cb = CircuitBreaker(mods_dir=tmp_path / "mods", failure_threshold=5, window_seconds=300.0, auto_rollback=False)

    now = time.time()
    for i in range(4):
        tripped = cb.record_error("mod-crash", error=f"Fail {i}", timestamp=now + i)
        assert tripped is False
        assert cb.get_state("mod-crash") == CIRCUIT_STATE_CLOSED

    # 5th error triggers breaker trip!
    tripped = cb.record_error("mod-crash", error="Fail 5 (critical)", timestamp=now + 4)
    assert tripped is True
    assert cb.get_state("mod-crash") == CIRCUIT_STATE_OPEN
    assert cb.is_tripped("mod-crash") is True
    assert cb.can_execute("mod-crash") is False


# -----------------------------------------------------------------------------
# Test 4: Auto Isolation and State Persistence
# -----------------------------------------------------------------------------
def test_auto_isolation_and_state_persistence(tmp_path: Path):
    repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)
    _create_mock_mod(mods_dir, "flaky-mod", [("app.py", "original", "patched")])

    cb = CircuitBreaker(
        mods_dir=mods_dir,
        repo_root=repo_dir,
        failure_threshold=5,
        window_seconds=300.0,
        auto_rollback=True,
    )

    now = time.time()
    for i in range(5):
        cb.record_error("flaky-mod", error=f"Runtime exception #{i+1}", timestamp=now + i)

    # Verify state in .state.json
    state_mgr = StateManager(mods_dir / ".state.json")
    ovr = state_mgr.get_mod_overrides("flaky-mod")
    assert ovr.get("enabled") is False
    assert ovr.get("disabled_circuit_breaker") is True
    assert "Circuit breaker tripped" in ovr.get("disabled_reason", "")
    assert ovr.get("circuit_breaker_tripped_at") is not None

    # Verify ModLoader loads mod as disabled with circuit breaker marker
    loaded = ModLoader.load_all(mods_dir)
    mod_obj = next(m for m in loaded if m.id == "flaky-mod")
    assert mod_obj.enabled is False
    assert mod_obj.disabled_circuit_breaker is True
    assert mod_obj.disabled_reason is not None


# -----------------------------------------------------------------------------
# Test 5: Single Protected Warning (No Spam)
# -----------------------------------------------------------------------------
def test_single_protected_warning(tmp_path: Path, caplog):
    repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)
    cb = CircuitBreaker(mods_dir=mods_dir, repo_root=repo_dir, failure_threshold=5, window_seconds=300.0)

    with caplog.at_level(logging.WARNING, logger="hermes.circuit_breaker"):
        # Trigger trip
        now = time.time()
        for i in range(5):
            cb.record_error("warn-mod", error=f"Fault #{i}", timestamp=now + i)

        warning_count = sum(1 for r in caplog.records if "warn-mod" in r.message and "CIRCUIT BREAKER" in r.message)
        assert warning_count == 1

        # Record further errors while already OPEN: must NOT spam extra warnings
        for i in range(5):
            cb.record_error("warn-mod", error=f"Subsequent fault #{i}", timestamp=now + 10 + i)

        warning_count_after = sum(1 for r in caplog.records if "warn-mod" in r.message and "CIRCUIT BREAKER" in r.message)
        assert warning_count_after == 1  # Still exactly 1 warning!

    # Check warning was recorded in .state.json
    state_mgr = StateManager(mods_dir / ".state.json")
    state_data = state_mgr.load()
    warnings = state_data.get("circuit_breaker_warnings", {})
    assert "warn-mod" in warnings
    assert "warn-mod" in warnings["warn-mod"]["warning"]


# -----------------------------------------------------------------------------
# Test 6: Surgical Rollback from Core (Healthy Mods Unaffected)
# -----------------------------------------------------------------------------
def test_surgical_rollback_from_core(tmp_path: Path):
    repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)

    # Target file in mock core
    core_file = repo_dir / "service.py"
    core_file.write_text("line_1 = 1\nline_2 = 2\n", encoding="utf-8")

    # Create 2 mods patching core_file
    mod_broken = _create_mock_mod(
        mods_dir,
        "mod-broken",
        [("service.py", "line_1 = 1", "line_1 = 'MOD_BROKEN_PATCH'")],
    )
    mod_healthy = _create_mock_mod(
        mods_dir,
        "mod-healthy",
        [("service.py", "line_2 = 2", "line_2 = 'MOD_HEALTHY_PATCH'")],
    )

    runner = ModRunner(mods_dir=mods_dir, repo_root=repo_dir, backups_dir=backups_dir)

    # Apply both mods
    res_b = runner.apply_mod(mod_broken, apply_changes=True)
    res_h = runner.apply_mod(mod_healthy, apply_changes=True)
    assert res_b["status"] == "applied"
    assert res_h["status"] == "applied"

    content_applied = core_file.read_text(encoding="utf-8")
    assert "MOD_BROKEN_PATCH" in content_applied
    assert "MOD_HEALTHY_PATCH" in content_applied

    # Now trip Circuit Breaker on mod_broken
    cb = CircuitBreaker(mods_dir=mods_dir, repo_root=repo_dir, failure_threshold=5, auto_rollback=True)
    now = time.time()
    for i in range(5):
        cb.record_error("mod-broken", error=f"Crash {i}", timestamp=now + i)

    # Check core file content: mod-broken should be rolled back, mod-healthy MUST STAY!
    content_after = core_file.read_text(encoding="utf-8")
    assert "MOD_BROKEN_PATCH" not in content_after
    assert "line_1 = 1" in content_after
    assert "MOD_HEALTHY_PATCH" in content_after  # Healthy mod remains active!

    # ModRunner verify should confirm mod_healthy is verified, mod_broken is disabled_circuit_breaker
    loaded = ModLoader.load_all(mods_dir)
    m_b = next(m for m in loaded if m.id == "mod-broken")
    m_h = next(m for m in loaded if m.id == "mod-healthy")

    ver_b = runner.apply_mod(m_b, apply_changes=False)
    ver_h = runner.apply_mod(m_h, apply_changes=False)
    assert ver_b["status"] == "disabled_circuit_breaker"
    assert ver_h["status"] == "already_applied"
    assert ver_h["verified"] is True


# -----------------------------------------------------------------------------
# Test 7: CLI runner.py disable <id> --circuit-breaker
# -----------------------------------------------------------------------------
def test_cli_disable_circuit_breaker(tmp_path: Path):
    repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)
    core_file = repo_dir / "feature.py"
    core_file.write_text("feature_flag = False\n", encoding="utf-8")

    mod = _create_mock_mod(
        mods_dir,
        "feature-mod",
        [("feature.py", "feature_flag = False", "feature_flag = True")],
    )

    runner = ModRunner(mods_dir=mods_dir, repo_root=repo_dir, backups_dir=backups_dir)
    runner.apply_mod(mod, apply_changes=True)
    assert core_file.read_text(encoding="utf-8") == "feature_flag = True\n"

    # Invoke runner.py disable feature-mod --circuit-breaker via subprocess
    runner_script = mods_dir / "runner.py"
    env = os.environ.copy()
    env["HERMES_ROOT"] = str(repo_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)

    proc = subprocess.run(
        [
            sys.executable,
            str(runner_script),
            "disable",
            "feature-mod",
            "--circuit-breaker",
            "--reason",
            "Segmentation fault in feature module",
            "--mods-dir",
            str(mods_dir),
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    res_data = json.loads(proc.stdout)
    assert res_data["success"] is True
    assert res_data["mod_id"] == "feature-mod"
    assert res_data["disabled_circuit_breaker"] is True
    assert res_data["status"] == "disabled_circuit_breaker"

    # Core file must have been rolled back
    assert core_file.read_text(encoding="utf-8") == "feature_flag = False\n"

    # .state.json must reflect circuit breaker disable
    state_mgr = StateManager(mods_dir / ".state.json")
    ovr = state_mgr.get_mod_overrides("feature-mod")
    assert ovr["disabled_circuit_breaker"] is True
    assert "Segmentation fault" in ovr["disabled_reason"]


# -----------------------------------------------------------------------------
# Test 8: CLI runner.py circuit-breaker status and reset
# -----------------------------------------------------------------------------
def test_cli_circuit_breaker_status_and_reset(tmp_path: Path):
    repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)
    runner_script = mods_dir / "runner.py"
    env = os.environ.copy()
    env["HERMES_ROOT"] = str(repo_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)

    cb = CircuitBreaker(mods_dir=mods_dir, repo_root=repo_dir, failure_threshold=5, auto_rollback=False)
    now = time.time()
    for i in range(5):
        cb.record_error("api-mod", error=f"Http 500 error #{i}", timestamp=now + i)

    # 1. Check status via CLI
    proc_stat = subprocess.run(
        [sys.executable, str(runner_script), "circuit-breaker", "status", "--mods-dir", str(mods_dir), "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc_stat.returncode == 0
    stat_data = json.loads(proc_stat.stdout)
    assert "api-mod" in stat_data
    assert stat_data["api-mod"]["state"] == CIRCUIT_STATE_OPEN
    assert stat_data["api-mod"]["failures_in_window"] == 5

    # 2. Reset via CLI
    proc_reset = subprocess.run(
        [sys.executable, str(runner_script), "circuit-breaker", "reset", "api-mod", "--mods-dir", str(mods_dir), "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc_reset.returncode == 0
    reset_data = json.loads(proc_reset.stdout)
    assert reset_data["reset"] is True
    assert reset_data["state"] == CIRCUIT_STATE_CLOSED

    # Check status again
    assert cb.get_state("api-mod") == CIRCUIT_STATE_CLOSED
    assert cb.can_execute("api-mod") is True


# -----------------------------------------------------------------------------
# Test 9: System Mod Protection (Tier 0 engine mods cannot be isolated)
# -----------------------------------------------------------------------------
def test_system_mod_protection(tmp_path: Path):
    repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)
    cb = CircuitBreaker(mods_dir=mods_dir, repo_root=repo_dir, failure_threshold=2)

    # Attempt to record repeated errors on system mod
    for sys_id in ("mod-command-registry", "mod-declarative-mcp"):
        for i in range(5):
            tripped = cb.record_error(sys_id, error=f"Internal system error {i}")
            assert tripped is False

        assert cb.get_state(sys_id) == CIRCUIT_STATE_CLOSED
        assert cb.can_execute(sys_id) is True

        res = cb.isolate_mod(sys_id)
        assert res.get("status") == "protected_system_mod"
        assert res.get("tripped") is False

    # Also test via runner.py disable <system_mod> --circuit-breaker
    runner_script = mods_dir / "runner.py"
    env = os.environ.copy()
    env["HERMES_ROOT"] = str(repo_dir)
    env["HERMES_MODS_DIR"] = str(mods_dir)

    proc = subprocess.run(
        [sys.executable, str(runner_script), "disable", "mod-command-registry", "--circuit-breaker", "--mods-dir", str(mods_dir), "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode != 0
    res = json.loads(proc.stdout)
    assert res.get("status") == "protected_system_mod"


# -----------------------------------------------------------------------------
# Test 10: Cooldown and Recovery to HALF_OPEN
# -----------------------------------------------------------------------------
def test_cooldown_and_recovery_to_half_open(tmp_path: Path):
    # Cooldown of 0.2 seconds
    cb = CircuitBreaker(
        mods_dir=tmp_path / "mods",
        failure_threshold=2,
        window_seconds=10.0,
        cooldown_seconds=0.2,
        auto_rollback=False,
    )

    now = time.time()
    cb.record_error("retry-mod", error="err 1", timestamp=now)
    cb.record_error("retry-mod", error="err 2", timestamp=now)
    assert cb.get_state("retry-mod", now=now) == CIRCUIT_STATE_OPEN
    assert cb.can_execute("retry-mod", now=now) is False

    # After cooldown expires (now + 0.3s)
    future_time = now + 0.3
    assert cb.get_state("retry-mod", now=future_time) == CIRCUIT_STATE_HALF_OPEN
    assert cb.can_execute("retry-mod", now=future_time) is True

    # Successful trial closes circuit
    cb.record_success("retry-mod", now=future_time)
    assert cb.get_state("retry-mod", now=future_time) == CIRCUIT_STATE_CLOSED


# -----------------------------------------------------------------------------
# Test 11: Protect Context Manager
# -----------------------------------------------------------------------------
def test_protect_context_manager(tmp_path: Path):
    cb = CircuitBreaker(mods_dir=tmp_path / "mods", failure_threshold=3, auto_rollback=False)

    for i in range(2):
        with pytest.raises(ValueError):
            with cb.protect("calc-mod"):
                raise ValueError("math error")

    assert cb.can_execute("calc-mod") is True

    # 3rd error trips breaker
    with pytest.raises(ValueError):
        with cb.protect("calc-mod"):
            raise ValueError("math error")

    assert cb.can_execute("calc-mod") is False

    # Blocked execution raises RuntimeError
    with pytest.raises(RuntimeError, match="Circuit breaker is OPEN"):
        with cb.protect("calc-mod"):
            pass


# -----------------------------------------------------------------------------
# Test 12: ModCommandRegistry Dispatch Runtime Integration
# -----------------------------------------------------------------------------
def test_mod_command_registry_dispatch_integration(tmp_path: Path):
    async def _run():
        repo_dir, mods_dir, backups_dir = _setup_test_env(tmp_path)

        # Create a failing command in commands cache
        cache_path = mods_dir / ".commands_cache.json"
        cache_path.write_text(
            json.dumps({
                "commands": {
                    "crashcmd": {
                        "name": "crashcmd",
                        "mod_id": "buggy-mod",
                        "description": "Failing command",
                        "python": "raise RuntimeError('Boom in command handler')",
                    },
                    "healthycmd": {
                        "name": "healthycmd",
                        "mod_id": "good-mod",
                        "description": "Healthy command",
                        "python": "result = 'ALL_GOOD'",
                    }
                }
            }),
            encoding="utf-8",
        )

        from gateway.mod_commands import ModCommandRegistry
        registry = ModCommandRegistry(mods_dir=mods_dir)

        class DummyEvent:
            text = "/crashcmd"
            def get_command_args(self):
                return ""

        class DummySource:
            chat_id = "test_chat"

        event = DummyEvent()
        source = DummySource()

        cb = get_circuit_breaker(mods_dir, failure_threshold=5, window_seconds=300.0)
        cb.reset("buggy-mod")

        # Call 4 times: errors are returned, not yet isolated
        for i in range(4):
            res = await registry.dispatch(event, source, "crashcmd")
            assert "Boom in command handler" in res
            assert "изолирован" not in res

        # 5th call: threshold reached, circuit breaker trips and isolates buggy-mod!
        res5 = await registry.dispatch(event, source, "crashcmd")
        assert "изолирован Circuit Breaker" in res5 or "превысил порог" in res5

        # 6th call: immediately blocked by Circuit Breaker before executing!
        res6 = await registry.dispatch(event, source, "crashcmd")
        assert "временно отключен предохранителем (Circuit Breaker)" in res6

        # Meanwhile, healthycmd continues to work without failing!
        event.text = "/healthycmd"
        res_h = await registry.dispatch(event, source, "healthycmd")
        assert res_h == "ALL_GOOD"

    asyncio.run(_run())
