"""Comprehensive tests for Hermes Mods Incremental Test Caching and Test Acceleration."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from engine.models import Mod, ModTarget
from engine.loader import parse_mod_manifest
from engine.version import ENGINE_VERSION
from engine.test_cache import (
    TestCacheManager,
    compute_composite_fingerprint,
    compute_mod_hash,
    get_env_fingerprint,
    get_hermes_git_commit,
)
from engine.tester import ModTestRunner, TestResult, TestSuiteSummary


def test_env_fingerprint_generation():
    """Environment fingerprint captures hermes commit, version, engine version and py version."""
    env1 = get_env_fingerprint(
        repo_root=Path("/usr/local/lib/hermes-agent"),
        engine_ver="1.4.0",
        hermes_ver="0.22.5",
        git_commit="aaaa1111",
    )
    assert env1["hermes_commit"] == "aaaa1111"
    assert env1["hermes_version"] == "0.22.5"
    assert env1["engine_version"] == "1.4.0"
    assert len(env1["digest"]) == 64

    # Identical parameters produce identical digest
    env2 = get_env_fingerprint(
        repo_root=Path("/usr/local/lib/hermes-agent"),
        engine_ver="1.4.0",
        hermes_ver="0.22.5",
        git_commit="aaaa1111",
    )
    assert env1["digest"] == env2["digest"]

    # Changing commit changes digest
    env_diff_commit = get_env_fingerprint(
        repo_root=Path("/usr/local/lib/hermes-agent"),
        engine_ver="1.4.0",
        hermes_ver="0.22.5",
        git_commit="bbbb2222",
    )
    assert env1["digest"] != env_diff_commit["digest"]

    # Changing engine version changes digest
    env_diff_engine = get_env_fingerprint(
        repo_root=Path("/usr/local/lib/hermes-agent"),
        engine_ver="1.5.0",
        hermes_ver="0.22.5",
        git_commit="aaaa1111",
    )
    assert env1["digest"] != env_diff_engine["digest"]

    # Changing hermes version changes digest
    env_diff_hermes = get_env_fingerprint(
        repo_root=Path("/usr/local/lib/hermes-agent"),
        engine_ver="1.4.0",
        hermes_ver="0.23.0",
        git_commit="aaaa1111",
    )
    assert env1["digest"] != env_diff_hermes["digest"]


def test_mod_content_hash_single_file_mod(tmp_path: Path):
    """Single file mod hash changes when manifest content or metadata changes."""
    mod_file = tmp_path / "single_test.yaml"
    mod_file.write_text(yaml.dump({
        "name": "Single Mod",
        "version": "1.0.0",
        "verify_cmd": "echo ok",
    }), encoding="utf-8")

    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="single-test")
    h1 = compute_mod_hash(mod)
    assert len(h1) == 64

    # Unchanged mod returns identical hash
    h2 = compute_mod_hash(mod)
    assert h1 == h2

    # Modify file content
    mod_file.write_text(yaml.dump({
        "name": "Single Mod",
        "version": "1.0.1",
        "verify_cmd": "echo ok2",
    }), encoding="utf-8")
    mod_updated = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="single-test")
    h3 = compute_mod_hash(mod_updated)
    assert h1 != h3


def test_mod_content_hash_folder_mod(tmp_path: Path):
    """Folder mod hash includes all nested scripts and tests, ignoring runtime caches."""
    mod_dir = tmp_path / "folder_mod"
    mod_dir.mkdir()
    (mod_dir / "mod.yaml").write_text(yaml.dump({
        "id": "folder-mod",
        "name": "Folder Mod",
        "version": "1.0.0",
    }), encoding="utf-8")

    tests_dir = mod_dir / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_sample.py"
    test_file.write_text("def test_one(): assert 1 == 1\n", encoding="utf-8")

    mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="folder-mod")
    h1 = compute_mod_hash(mod)

    # Adding a runtime cache directory (__pycache__ / .pytest_cache) should NOT alter hash!
    pycache = tests_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "test_sample.cpython-311.pyc").write_bytes(b"garbage-bytecode")
    pytest_cache = mod_dir / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "nodeids").write_text("['dummy']", encoding="utf-8")

    h_after_cache = compute_mod_hash(mod)
    assert h1 == h_after_cache, "Ignored files like __pycache__ must not alter mod hash"

    # Modifying test code MUST change hash
    test_file.write_text("def test_one(): assert 1 + 1 == 2\n", encoding="utf-8")
    h_modified = compute_mod_hash(mod)
    assert h1 != h_modified, "Editing test file must alter mod hash"


def test_test_cache_manager_crud(tmp_path: Path):
    """TestCacheManager correctly saves, retrieves, and invalidates cache entries."""
    cache_file = tmp_path / "test_cache.json"
    cache_mgr = TestCacheManager(cache_file=cache_file, mods_dir=tmp_path, repo_root=tmp_path)

    mod_file = tmp_path / "sample.yaml"
    mod_file.write_text(yaml.dump({
        "name": "Sample",
        "version": "1.0.0",
    }), encoding="utf-8")
    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="sample-mod")

    # Initial get is None
    assert cache_mgr.get_cached_result(mod) is None
    assert not cache_mgr.has_valid_cache(mod)

    # Cache a successful result
    test_res = TestResult(
        mod_id=mod.id,
        mod_name=mod.name,
        status="passed",
        passed=5,
        total=5,
        duration=1.23,
        method="folder",
        test_files=["tests/test_sample.py"],
    )
    cache_mgr.set_cached_result(mod, test_res)

    assert cache_mgr.has_valid_cache(mod)
    cached = cache_mgr.get_cached_result(mod)
    assert cached is not None
    assert cached["status"] == "passed"
    assert cached["passed"] == 5
    assert cached["verified"] is True
    assert cached["method"] == "folder"

    # Invalidate single mod
    cache_mgr.invalidate("sample-mod")
    assert cache_mgr.get_cached_result(mod) is None


def test_failed_test_is_not_cached(tmp_path: Path):
    """Failed test results must never be stored as verified cache hits."""
    cache_file = tmp_path / "test_cache.json"
    cache_mgr = TestCacheManager(cache_file=cache_file, mods_dir=tmp_path, repo_root=tmp_path)

    mod_file = tmp_path / "fail_mod.yaml"
    mod_file.write_text(yaml.dump({"name": "Fail Mod", "version": "1.0.0"}), encoding="utf-8")
    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="fail-mod")

    failed_res = TestResult(
        mod_id=mod.id,
        mod_name=mod.name,
        status="failed",
        failed=1,
        total=1,
        duration=0.5,
        error_message="Assertion failed",
    )
    cache_mgr.set_cached_result(mod, failed_res)

    # Cache must NOT return failed tests as valid cache!
    assert cache_mgr.get_cached_result(mod) is None
    assert not cache_mgr.has_valid_cache(mod)


def test_incremental_caching_in_test_runner(tmp_path: Path):
    """ModTestRunner skips executing pytest when valid cache exists and honours force=True."""
    cache_file = tmp_path / "test_cache.json"
    cache_mgr = TestCacheManager(cache_file=cache_file, mods_dir=tmp_path, repo_root=tmp_path)

    mod_file = tmp_path / "fast_mod.yaml"
    mod_file.write_text(yaml.dump({
        "name": "Fast Mod",
        "version": "1.0.0",
        "tests": [
            {"name": "check_math", "code": "assert 10 > 5"},
        ],
    }), encoding="utf-8")
    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="fast-mod")

    runner = ModTestRunner(repo_root=Path("/usr/local/lib/hermes-agent"), cache_mgr=cache_mgr)

    # 1. Cold run: executes inline test, writes cache
    res1 = runner.run_test(mod, use_cache=True, force=False)
    assert res1.status == "passed"
    assert res1.passed == 1
    assert res1.cached is False

    # Cache file now exists
    assert cache_file.exists()
    assert cache_mgr.has_valid_cache(mod)

    # 2. Warm run: returns instantly from cache!
    res2 = runner.run_test(mod, use_cache=True, force=False)
    assert res2.status == "passed"
    assert res2.passed == 1
    assert res2.cached is True
    assert res2.duration == 0.0

    # 3. Force run: bypasses cache even though valid cache exists!
    res3 = runner.run_test(mod, use_cache=True, force=True)
    assert res3.status == "passed"
    assert res3.cached is False


def test_cache_invalidation_on_environment_change(tmp_path: Path):
    """Cache is invalidated when Hermes commit or engine version changes."""
    cache_file = tmp_path / "test_cache.json"
    cache_mgr = TestCacheManager(cache_file=cache_file, mods_dir=tmp_path, repo_root=tmp_path)

    mod_file = tmp_path / "env_mod.yaml"
    mod_file.write_text(yaml.dump({"name": "Env Mod", "version": "1.0.0"}), encoding="utf-8")
    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="env-mod")

    # Seed cache under commit 'commit_v1'
    cache_mgr.set_cached_result(
        mod,
        {"verified": True, "passed": 3},
        git_commit="commit_v1",
        engine_ver="1.4.0",
    )

    # Match under exact same environment
    assert cache_mgr.get_cached_result(mod, git_commit="commit_v1", engine_ver="1.4.0") is not None

    # Mismatch under new commit -> cache is invalidated!
    assert cache_mgr.get_cached_result(mod, git_commit="commit_v2", engine_ver="1.4.0") is None

    # Mismatch under new engine version -> cache is invalidated!
    assert cache_mgr.get_cached_result(mod, git_commit="commit_v1", engine_ver="1.5.0") is None


def test_mod_runner_apply_and_verify_cache_integration(tmp_path: Path):
    """runner.py apply_mod returns verified: true, cached: true on cache hit."""
    from runner import ModRunner

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target_file = repo_dir / "sample.py"
    target_file.write_text("LINE_ORIGINAL\n", encoding="utf-8")

    mod_dir = tmp_path / "mods"
    mod_dir.mkdir()
    mod_file = mod_dir / "mod_a.yaml"
    mod_file.write_text(yaml.dump({
        "id": "mod-a",
        "name": "Mod A",
        "version": "1.0.0",
        "targets": [
            {
                "file": "sample.py",
                "old_block": "LINE_ORIGINAL",
                "new_block": "LINE_MODIFIED",
            }
        ],
        "tests": [
            {"name": "check_target", "code": "assert True"},
        ],
    }), encoding="utf-8")

    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="mod-a")
    runner = ModRunner(repo_root=repo_dir, mods_dir=mod_dir)

    # 1. Apply mod (cold execution)
    res_apply = runner.apply_mod(mod, apply_changes=True)
    assert res_apply["status"] == "applied"
    assert res_apply["verified"] is True
    assert res_apply["cached"] is False

    # 2. Re-verify mod (warm execution) -> returns cached: true instantly!
    res_verify = runner.apply_mod(mod, apply_changes=False)
    assert res_verify["status"] == "already_applied"
    assert res_verify["verified"] is True
    assert res_verify["cached"] is True

    # 3. Verify with force=True -> bypasses cache, returns cached: false
    res_force = runner.apply_mod(mod, apply_changes=False, force=True)
    assert res_force["status"] == "already_applied"
    assert res_force["verified"] is True
    assert res_force["cached"] is False


def test_test_suite_parallel_run_all(tmp_path: Path):
    """ModTestRunner.run_all executes multiple mods in parallel and tracks cached counts."""
    cache_file = tmp_path / "test_cache.json"
    cache_mgr = TestCacheManager(cache_file=cache_file, mods_dir=tmp_path, repo_root=tmp_path)

    mods = []
    for i in range(4):
        mf = tmp_path / f"m_{i}.yaml"
        mf.write_text(yaml.dump({
            "name": f"Mod {i}",
            "version": "1.0.0",
            "tests": [{"name": "t", "code": f"assert {i} >= 0"}],
        }), encoding="utf-8")
        m = parse_mod_manifest(mf, mod_dir=None, fallback_id=f"mod-{i}")
        mods.append(m)

    runner = ModTestRunner(repo_root=Path("/usr/local/lib/hermes-agent"), cache_mgr=cache_mgr)

    # Cold run with 4 workers
    sum1 = runner.run_all(mods, use_cache=True, force=False, max_workers=4)
    assert sum1.all_passed is True
    assert sum1.tested_mods == 4
    assert sum1.cached_mods == 0

    # Warm run
    sum2 = runner.run_all(mods, use_cache=True, force=False, max_workers=4)
    assert sum2.all_passed is True
    assert sum2.tested_mods == 4
    assert sum2.cached_mods == 4
    assert sum2.total_duration < 1.0  # Warm run is virtually instantaneous!
