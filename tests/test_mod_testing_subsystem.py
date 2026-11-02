"""Unit tests for Hermes Mods native test subsystem (tester.py, models, loader, and CLI)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import pytest
import yaml

from engine.models import Mod, ModTarget
from engine.loader import ModLoader, parse_mod_manifest, parse_inline_tests
from engine.tester import ModTestRunner, TestResult, TestSuiteSummary


def test_parse_inline_tests_formats():
    """Verify various shapes of inline tests in manifest YAML."""
    # 1. String block
    raw_str = "assert 1 + 1 == 2\nassert True"
    parsed_str = parse_inline_tests(raw_str)
    assert len(parsed_str) == 1
    assert parsed_str[0]["name"] == "test_inline"
    assert "assert 1 + 1 == 2" in parsed_str[0]["code"]

    # 2. List of dicts
    raw_list = [
        {"name": "check_a", "code": "assert True"},
        {"name": "check_b", "code": "assert 2 > 1"},
    ]
    parsed_list = parse_inline_tests(raw_list)
    assert len(parsed_list) == 2
    assert parsed_list[0]["name"] == "check_a"
    assert parsed_list[1]["name"] == "check_b"

    # 3. Dict of cases
    raw_dict = {
        "case_1": "assert 10 == 10",
        "case_2": {"code": "assert 'x' in 'xyz'"},
    }
    parsed_dict = parse_inline_tests(raw_dict)
    assert len(parsed_dict) == 2
    names = {p["name"] for p in parsed_dict}
    assert names == {"case_1", "case_2"}


def test_folder_mod_tests_discovery(tmp_path: Path):
    """Folder mods automatically discover test_*.py and *_test.py in tests/."""
    mod_dir = tmp_path / "sample-folder-mod"
    mod_dir.mkdir()
    (mod_dir / "mod.yaml").write_text(yaml.dump({
        "id": "sample-folder-mod",
        "name": "Sample Folder Mod",
        "version": "1.0.0",
    }), encoding="utf-8")

    tests_dir = mod_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_alpha.py").write_text("def test_alpha(): assert True\n", encoding="utf-8")
    (tests_dir / "beta_test.py").write_text("def test_beta(): assert True\n", encoding="utf-8")
    (tests_dir / "ignore_helper.py").write_text("# not a test\n", encoding="utf-8")

    mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="sample-folder-mod")
    assert mod is not None
    assert mod.is_folder is True
    assert mod.has_tests is True
    assert mod.tests_count == 2
    assert len(mod.test_files) == 2
    test_names = {p.name for p in mod.test_files}
    assert test_names == {"test_alpha.py", "beta_test.py"}


def test_single_file_mod_strictly_single_file_no_companion_scan(tmp_path: Path):
    """Single file mod must NOT scan companion test files; only inline tests are allowed."""
    mod_file = tmp_path / "single-strict.yaml"
    mod_file.write_text(yaml.dump({
        "name": "Single Strict",
        "version": "1.0.0",
        "targets": [{"file": "f.py", "old_block": "a", "new_block": "b"}],
    }), encoding="utf-8")

    # Creating a companion file nearby should be completely ignored
    (tmp_path / "test_single_strict.py").write_text("def test_bad(): pass\n", encoding="utf-8")
    (tmp_path / "single-strict.test.py").write_text("def test_bad2(): pass\n", encoding="utf-8")

    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="single-strict")
    assert mod is not None
    assert mod.is_folder is False
    assert mod.has_tests is False
    assert len(mod.test_files) == 0
    assert len(mod.inline_tests) == 0


def test_single_file_mod_inline_tests_execution(tmp_path: Path):
    """Single-file mod with inline tests executes successfully via runner."""
    mod_file = tmp_path / "single-inline.yaml"
    mod_file.write_text(yaml.dump({
        "name": "Single Inline",
        "version": "1.0.0",
        "tests": [
            {"name": "math_pass", "code": "assert 2 + 2 == 4"},
            {"name": "logic_pass", "code": "val = 'abc'\nassert val.upper() == 'ABC'"},
        ],
    }), encoding="utf-8")

    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="single-inline")
    assert mod is not None
    assert mod.is_folder is False
    assert mod.has_tests is True
    assert len(mod.inline_tests) == 2

    runner = ModTestRunner(repo_root=Path("/usr/local/lib/hermes-agent"))
    res = runner.run_test(mod)
    assert res.status == "passed"
    assert res.passed == 2
    assert res.failed == 0
    assert res.is_inline is True
    assert res.method == "inline"


def test_single_file_mod_inline_tests_failure_reporting(tmp_path: Path):
    """Single-file mod inline test failure is caught and reported accurately."""
    mod_file = tmp_path / "single-fail.yaml"
    mod_file.write_text(yaml.dump({
        "name": "Single Fail",
        "version": "1.0.0",
        "tests": [
            {"name": "should_fail", "code": "assert 1 == 999, 'Mismatch expected'"},
        ],
    }), encoding="utf-8")

    mod = parse_mod_manifest(mod_file, mod_dir=None, fallback_id="single-fail")
    assert mod is not None
    assert mod.has_tests is True

    runner = ModTestRunner(repo_root=Path("/usr/local/lib/hermes-agent"))
    res = runner.run_test(mod)
    assert res.status == "failed"
    assert res.failed >= 1
    assert "Mismatch expected" in (res.error_message or "")


def test_folder_mod_tests_execution(tmp_path: Path):
    """Folder mod test execution passes when assertions succeed."""
    mod_dir = tmp_path / "exec-folder-mod"
    mod_dir.mkdir()
    (mod_dir / "mod.yaml").write_text(yaml.dump({
        "id": "exec-folder-mod",
        "name": "Exec Folder Mod",
        "version": "1.0.0",
    }), encoding="utf-8")

    tests_dir = mod_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_success.py").write_text(
        "def test_one(): assert 10 == 10\n"
        "def test_two(): assert 'hello'.title() == 'Hello'\n",
        encoding="utf-8",
    )

    mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="exec-folder-mod")
    assert mod is not None
    assert mod.is_folder is True

    runner = ModTestRunner(repo_root=Path("/usr/local/lib/hermes-agent"))
    res = runner.run_test(mod)
    assert res.status == "passed"
    assert res.passed == 2
    assert res.failed == 0
    assert res.method == "folder"


def test_test_suite_summary_aggregation(tmp_path: Path):
    """ModTestRunner.run_all correctly aggregates results across diverse mods."""
    # Mod 1: Folder mod with 2 passed tests
    m1_dir = tmp_path / "m1"
    m1_dir.mkdir()
    (m1_dir / "mod.yaml").write_text("id: m1\nname: M1\n", encoding="utf-8")
    t1 = m1_dir / "tests"
    t1.mkdir()
    (t1 / "test_m1.py").write_text("def test_a(): assert True\ndef test_b(): assert True\n", encoding="utf-8")
    mod1 = parse_mod_manifest(m1_dir / "mod.yaml", mod_dir=m1_dir, fallback_id="m1")

    # Mod 2: Single-file mod with 1 inline test (dictionary syntax)
    m2_file = tmp_path / "m2.yaml"
    m2_file.write_text("name: M2\ntests:\n  t1: |\n    assert True\n", encoding="utf-8")
    mod2 = parse_mod_manifest(m2_file, mod_dir=None, fallback_id="m2")

    # Mod 3: Mod without tests
    m3_file = tmp_path / "m3.yaml"
    m3_file.write_text("name: M3\n", encoding="utf-8")
    mod3 = parse_mod_manifest(m3_file, mod_dir=None, fallback_id="m3")

    runner = ModTestRunner(repo_root=Path("/usr/local/lib/hermes-agent"))
    summary = runner.run_all([mod1, mod2, mod3])

    assert summary.total_mods == 3
    assert summary.tested_mods == 2
    assert summary.passed_mods == 2
    assert summary.failed_mods == 0
    assert summary.no_tests_mods == 1
    assert summary.total_tests_passed == 3
    assert summary.all_passed is True
