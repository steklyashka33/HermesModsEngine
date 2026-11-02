"""Hermes Mods Native Test Subsystem.

Provides test discovery, execution, and reporting for:
1. Folder mods (native mod_dir/tests/test_*.py test suites)
2. Single-file mods (strictly inline tests in manifest YAML)
3. Backward-compatible verify_cmd execution fallback
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Mod
from .config_manager import ModConfigManager
from .test_cache import TestCacheManager
from .loader import detect_hermes_root

HERMES_ROOT = detect_hermes_root()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(HERMES_HOME / "mods")))
CONFIG_FILE = Path(os.environ.get("HERMES_CONFIG_FILE", str(HERMES_HOME / "config.yaml")))
SECRETS_FILE = Path(os.environ.get("HERMES_SECRETS_FILE", str(HERMES_HOME / "secrets.yaml")))


def _find_test_python(root: Path) -> Tuple[Path, Path]:
    candidates = [
        (root / "venv" / "bin" / "python3", root / "venv" / "bin" / "pytest"),
        (root / ".venv" / "bin" / "python3", root / ".venv" / "bin" / "pytest"),
        (Path(sys.prefix) / "bin" / "python3", Path(sys.prefix) / "bin" / "pytest"),
        (Path(sys.executable), Path(sys.executable).parent / "pytest"),
    ]
    for py, pyt in candidates:
        if py.exists() and os.access(py, os.X_OK):
            return py, pyt
    return Path(sys.executable), Path(sys.executable).parent / "pytest"


VENV_PYTHON, VENV_PYTEST = _find_test_python(HERMES_ROOT)
SCRATCH_BASE = Path(os.environ.get("TMPDIR", str(HERMES_HOME / "cache" / "scratch")))

# Regex helpers for parsing pytest output
RE_PASSED = re.compile(r"(\d+)\s+passed")
RE_FAILED = re.compile(r"(\d+)\s+failed")
RE_SKIPPED = re.compile(r"(\d+)\s+skipped")
RE_ERRORS = re.compile(r"(\d+)\s+error")
RE_DURATION = re.compile(r"in\s+([\d\.]+)s")


@dataclass
class TestResult:
    __test__ = False
    mod_id: str
    mod_name: str
    status: str  # "passed", "failed", "skipped", "no_tests", "error"
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error_message: Optional[str] = None
    test_files: List[str] = field(default_factory=list)
    is_inline: bool = False
    method: str = "none"  # "folder", "inline", "verify_cmd", "none"
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "mod_name": self.mod_name,
            "status": self.status,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "errors": self.errors,
            "total": self.total,
            "duration": round(self.duration, 3),
            "is_inline": self.is_inline,
            "method": self.method,
            "test_files": self.test_files,
            "error_message": self.error_message,
            "cached": self.cached,
        }


@dataclass
class TestSuiteSummary:
    __test__ = False
    total_mods: int = 0
    tested_mods: int = 0
    passed_mods: int = 0
    failed_mods: int = 0
    skipped_mods: int = 0
    no_tests_mods: int = 0
    cached_mods: int = 0
    total_tests_passed: int = 0
    total_tests_failed: int = 0
    total_duration: float = 0.0
    results: List[TestResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.failed_mods == 0 and all(r.status in ("passed", "no_tests", "skipped") for r in self.results)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "all_passed": self.all_passed,
            "total_mods": self.total_mods,
            "tested_mods": self.tested_mods,
            "passed_mods": self.passed_mods,
            "failed_mods": self.failed_mods,
            "skipped_mods": self.skipped_mods,
            "no_tests_mods": self.no_tests_mods,
            "cached_mods": self.cached_mods,
            "total_tests_passed": self.total_tests_passed,
            "total_tests_failed": self.total_tests_failed,
            "total_duration": round(self.duration, 3) if hasattr(self, "duration") else round(self.total_duration, 3),
            "results": [r.to_dict() for r in self.results],
        }


class ModTestRunner:
    """Discovers and runs tests for Hermes mods."""

    def __init__(
        self,
        repo_root: Path = HERMES_ROOT,
        python_bin: Optional[Path] = None,
        mods_dir: Optional[Path] = None,
        cache_mgr: Optional[TestCacheManager] = None,
    ):
        self.repo = Path(repo_root)
        self.mods_dir = Path(mods_dir) if mods_dir else MODS_DIR
        self.cache_mgr = cache_mgr if cache_mgr is not None else TestCacheManager(mods_dir=self.mods_dir, repo_root=self.repo)
        if python_bin:
            self.python_bin = Path(python_bin)
        elif VENV_PYTHON.exists():
            self.python_bin = VENV_PYTHON
        else:
            self.python_bin = Path(sys.executable)

        # Locate pytest binary
        pytest_candidate = self.python_bin.parent / "pytest"
        if pytest_candidate.exists():
            self.pytest_bin = pytest_candidate
        elif VENV_PYTEST.exists():
            self.pytest_bin = VENV_PYTEST
        elif shutil.which("pytest"):
            self.pytest_bin = Path(shutil.which("pytest"))
        else:
            self.pytest_bin = None

    def _build_env(self, mod: Mod) -> Dict[str, str]:
        """Construct isolated environment variables for running mod tests."""
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")

        path_elements = []
        if mod.mod_dir and mod.mod_dir.is_dir():
            path_elements.append(str(mod.mod_dir))
        path_elements.append(str(self.repo))
        path_elements.append(str(self.mods_dir))
        if existing_pp and "/.hermes/installs/" not in existing_pp:
            path_elements.append(existing_pp)

        env["PATH"] = f"{self.python_bin.parent}:{env.get('PATH', '')}"
        env["VIRTUAL_ENV"] = str(self.python_bin.parent.parent)
        env["PYTHONPATH"] = ":".join(path_elements)
        env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        env["HERMES_MOD_ID"] = mod.id
        env["HERMES_MOD_VERSION"] = mod.version
        env["HERMES_MOD_TEST"] = "1"
        if mod.mod_dir:
            env["HERMES_MOD_DIR"] = str(mod.mod_dir)
        else:
            env["HERMES_MOD_DIR"] = ""

        # Inject config values if defined
        if mod.config:
            try:
                cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
                resolved = cfg_mgr.get_resolved_config(mod)
                for k, v in resolved.items():
                    if v is not None:
                        env[f"HERMES_MOD_CONFIG_{str(k).upper()}"] = str(v)
                        env[f"HERMES_MOD_OPTION_{str(k).upper()}"] = str(v)
            except Exception:
                pass

        return env

    def _parse_pytest_output(self, output: str, returncode: int, duration: float) -> Tuple[str, int, int, int, int]:
        """Parse pytest stdout/stderr to extract counts: status, passed, failed, skipped, errors."""
        passed = 0
        failed = 0
        skipped = 0
        errors = 0

        m_p = RE_PASSED.search(output)
        if m_p:
            passed = int(m_p.group(1))

        m_f = RE_FAILED.search(output)
        if m_f:
            failed = int(m_f.group(1))

        m_s = RE_SKIPPED.search(output)
        if m_s:
            skipped = int(m_s.group(1))

        m_e = RE_ERRORS.search(output)
        if m_e:
            errors = int(m_e.group(1))

        if returncode == 0:
            status = "passed"
            if passed == 0 and skipped > 0:
                status = "skipped"
        else:
            status = "failed" if (failed > 0 or errors > 0) else "error"

        return status, passed, failed, skipped, errors

    def run_folder_tests(
        self,
        mod: Mod,
        verbose: bool = False,
        fail_fast: bool = False,
        test_filter: Optional[str] = None,
        timeout: int = 120,
    ) -> TestResult:
        """Run native folder tests inside mod_dir/tests/."""
        if not mod.test_files:
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="no_tests",
                method="folder",
            )

        cmd: List[str] = []
        if self.pytest_bin:
            cmd.append(str(self.pytest_bin))
        else:
            cmd.extend([str(self.python_bin), "-m", "pytest"])

        cmd.extend(["-o", "asyncio_mode=auto", "-o", f"pythonpath={self.repo}"])
        if verbose:
            cmd.append("-vv")
        else:
            cmd.append("-q")

        if fail_fast:
            cmd.append("-x")

        if test_filter:
            cmd.extend(["-k", test_filter])

        # Add target test files
        test_file_paths = [str(p) for p in mod.test_files]
        cmd.extend(test_file_paths)

        env = self._build_env(mod)
        cwd = str(mod.mod_dir) if mod.mod_dir else str(self.repo)

        start_time = time.time()
        try:
            res = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.time() - start_time
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            status, passed, failed, skipped, errors = self._parse_pytest_output(combined, res.returncode, duration)

            err_msg = None
            if status in ("failed", "error"):
                err_msg = combined.strip()

            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status=status,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
                total=passed + failed + skipped + errors,
                duration=duration,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                error_message=err_msg,
                test_files=test_file_paths,
                is_inline=False,
                method="folder",
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="failed",
                duration=duration,
                error_message=f"Tests timed out after {timeout} seconds",
                test_files=test_file_paths,
                is_inline=False,
                method="folder",
            )
        except Exception as exc:
            duration = time.time() - start_time
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="error",
                duration=duration,
                error_message=f"Failed to execute tests: {exc}",
                test_files=test_file_paths,
                is_inline=False,
                method="folder",
            )

    def run_inline_tests(
        self,
        mod: Mod,
        verbose: bool = False,
        fail_fast: bool = False,
        test_filter: Optional[str] = None,
        timeout: int = 60,
    ) -> TestResult:
        """Run inline tests defined strictly inside single-file mod manifest YAML."""
        if not mod.inline_tests:
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="no_tests",
                method="inline",
            )

        # Generate a temporary test module in isolated scratch directory
        scratch_dir = SCRATCH_BASE / f"inline_tests_{mod.id}_{os.getpid()}_{time.time_ns()}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        test_file = scratch_dir / f"test_{mod.id.replace('-', '_')}_inline.py"

        # Build clean test script
        lines: List[str] = [
            '"""Auto-generated test module for inline single-file mod tests."""',
            "from __future__ import annotations",
            "import os, sys, pytest",
            "",
        ]

        has_any_def = any("def test_" in str(item.get("code", "")) for item in mod.inline_tests)

        if has_any_def:
            # Inline snippets already define test functions
            for item in mod.inline_tests:
                lines.append(f"# --- Inline block: {item.get('name', 'test')} ---")
                lines.append(item.get("code", ""))
                lines.append("")
        else:
            # Wrap snippets into pytest test functions
            for idx, item in enumerate(mod.inline_tests, 1):
                raw_name = str(item.get("name") or f"case_{idx}")
                func_name = "test_" + re.sub(r"[^a-zA-Z0-9_]", "_", raw_name)
                lines.append(f"def {func_name}():")
                code_body = item.get("code", "").strip()
                if not code_body:
                    lines.append("    pass")
                else:
                    for body_line in code_body.splitlines():
                        lines.append(f"    {body_line}")
                lines.append("")

        test_file.write_text("\n".join(lines), encoding="utf-8")

        cmd: List[str] = []
        if self.pytest_bin:
            cmd.append(str(self.pytest_bin))
        else:
            cmd.extend([str(self.python_bin), "-m", "pytest"])

        cmd.extend(["-o", "asyncio_mode=auto", "-o", f"pythonpath={self.repo}"])
        if verbose:
            cmd.append("-vv")
        else:
            cmd.append("-q")

        if fail_fast:
            cmd.append("-x")

        if test_filter:
            cmd.extend(["-k", test_filter])

        cmd.append(str(test_file))

        env = self._build_env(mod)
        start_time = time.time()
        inline_cwd = str(self.repo) if self.repo.exists() else str(self.mods_dir)
        try:
            res = subprocess.run(
                cmd,
                cwd=inline_cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.time() - start_time
            combined = (res.stdout or "") + "\n" + (res.stderr or "")
            status, passed, failed, skipped, errors = self._parse_pytest_output(combined, res.returncode, duration)

            err_msg = None
            if status in ("failed", "error"):
                err_msg = combined.strip()

            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status=status,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
                total=passed + failed + skipped + errors,
                duration=duration,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                error_message=err_msg,
                test_files=[f"<inline:{item.get('name', 'test')}>" for item in mod.inline_tests],
                is_inline=True,
                method="inline",
            )
        except subprocess.TimeoutExpired:
            duration = time.time() - start_time
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="failed",
                duration=duration,
                error_message=f"Inline tests timed out after {timeout} seconds",
                test_files=["<inline>"],
                is_inline=True,
                method="inline",
            )
        except Exception as exc:
            duration = time.time() - start_time
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="error",
                duration=duration,
                error_message=f"Failed to execute inline tests: {exc}",
                test_files=["<inline>"],
                is_inline=True,
                method="inline",
            )
        finally:
            try:
                shutil.rmtree(scratch_dir, ignore_errors=True)
            except OSError:
                pass

    def run_verify_cmd_fallback(self, mod: Mod, timeout: int = 60) -> TestResult:
        """Fallback runner for legacy verify_cmd string."""
        if not mod.verify_cmd:
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="no_tests",
                method="verify_cmd",
            )

        cmd_str = " ".join(line.strip() for line in mod.verify_cmd.strip().splitlines())
        if cmd_str.startswith("python3 ") or cmd_str.startswith("python "):
            space_idx = cmd_str.index(" ")
            cmd = str(self.python_bin) + cmd_str[space_idx:]
        elif cmd_str.startswith("pytest ") or cmd_str == "pytest":
            pytest_exec = str(self.pytest_bin) if self.pytest_bin else f"{self.python_bin} -m pytest"
            space_idx = cmd_str.find(" ")
            cmd = pytest_exec + (cmd_str[space_idx:] if space_idx != -1 else "")
        else:
            cmd = cmd_str

        env = self._build_env(mod)
        cwd = str(mod.mod_dir) if mod.mod_dir else str(self.repo)

        start_time = time.time()
        try:
            res = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            duration = time.time() - start_time
            combined = (res.stdout or "") + "\n" + (res.stderr or "")

            if "passed" in combined or "failed" in combined:
                status, passed, failed, skipped, errors = self._parse_pytest_output(combined, res.returncode, duration)
            else:
                status = "passed" if res.returncode == 0 else "failed"
                passed = 1 if res.returncode == 0 else 0
                failed = 0 if res.returncode == 0 else 1
                skipped = 0
                errors = 0

            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status=status,
                passed=passed,
                failed=failed,
                skipped=skipped,
                errors=errors,
                total=passed + failed + skipped + errors,
                duration=duration,
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                error_message=combined.strip() if status != "passed" else None,
                test_files=[f"<verify_cmd:{cmd_str[:40]}>"],
                is_inline=False,
                method="verify_cmd",
            )
        except Exception as exc:
            duration = time.time() - start_time
            return TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="error",
                duration=duration,
                error_message=str(exc),
                test_files=["<verify_cmd>"],
                is_inline=False,
                method="verify_cmd",
            )

    def run_test(
        self,
        mod: Mod,
        verbose: bool = False,
        fail_fast: bool = False,
        test_filter: Optional[str] = None,
        timeout: int = 120,
        use_cache: bool = True,
        force: bool = False,
    ) -> TestResult:
        """Run tests for a single mod using the best matching strategy with caching."""
        if use_cache and not force and not test_filter and (mod.has_tests or mod.verify_cmd):
            cached = self.cache_mgr.get_cached_result(mod)
            if cached:
                return TestResult(
                    mod_id=mod.id,
                    mod_name=mod.name,
                    status="passed",
                    passed=cached.get("passed", 1),
                    failed=0,
                    skipped=0,
                    errors=0,
                    total=cached.get("total", 1),
                    duration=0.0,
                    test_files=cached.get("test_files", []),
                    is_inline=cached.get("method") == "inline",
                    method=cached.get("method", "folder" if mod.test_files else ("inline" if mod.inline_tests else "verify_cmd")),
                    cached=True,
                )

        # 1. Native folder tests (mod_dir/tests/test_*.py)
        if mod.test_files:
            res = self.run_folder_tests(
                mod,
                verbose=verbose,
                fail_fast=fail_fast,
                test_filter=test_filter,
                timeout=timeout,
            )
        # 2. Native inline tests (manifest 'tests:' section in single-file mod)
        elif mod.inline_tests:
            res = self.run_inline_tests(
                mod,
                verbose=verbose,
                fail_fast=fail_fast,
                test_filter=test_filter,
                timeout=timeout,
            )
        # 3. Legacy verify_cmd fallback
        elif mod.verify_cmd:
            res = self.run_verify_cmd_fallback(mod, timeout=timeout)
        else:
            # 4. No tests defined
            res = TestResult(
                mod_id=mod.id,
                mod_name=mod.name,
                status="no_tests",
                method="none",
            )

        if res.status == "passed" and not test_filter:
            self.cache_mgr.set_cached_result(mod, res)

        return res

    def run_all(
        self,
        mods: List[Mod],
        verbose: bool = False,
        fail_fast: bool = False,
        test_filter: Optional[str] = None,
        include_disabled: bool = False,
        max_workers: Optional[int] = None,
        use_cache: bool = True,
        force: bool = False,
    ) -> TestSuiteSummary:
        """Run tests across multiple mods with caching and parallel execution."""
        summary = TestSuiteSummary(total_mods=len(mods))
        start_all = time.time()

        results_by_id: Dict[str, TestResult] = {}
        mods_to_run: List[Mod] = []

        for m in mods:
            if not m.enabled and not include_disabled:
                summary.skipped_mods += 1
                results_by_id[m.id] = TestResult(
                    mod_id=m.id,
                    mod_name=m.name,
                    status="skipped",
                    error_message="Mod is disabled (use --all to run)",
                )
                continue

            if not m.has_tests and not m.verify_cmd:
                summary.no_tests_mods += 1
                results_by_id[m.id] = TestResult(
                    mod_id=m.id,
                    mod_name=m.name,
                    status="no_tests",
                    method="none",
                )
                continue

            # Check cache
            if use_cache and not force and not test_filter:
                cached = self.cache_mgr.get_cached_result(m)
                if cached:
                    summary.tested_mods += 1
                    summary.passed_mods += 1
                    summary.cached_mods += 1
                    summary.total_tests_passed += cached.get("passed", 1)
                    results_by_id[m.id] = TestResult(
                        mod_id=m.id,
                        mod_name=m.name,
                        status="passed",
                        passed=cached.get("passed", 1),
                        failed=0,
                        skipped=0,
                        errors=0,
                        total=cached.get("total", 1),
                        duration=0.0,
                        test_files=cached.get("test_files", []),
                        is_inline=cached.get("method") == "inline",
                        method=cached.get("method", "folder" if m.test_files else ("inline" if m.inline_tests else "verify_cmd")),
                        cached=True,
                    )
                    continue

            mods_to_run.append(m)

        # Execute mods that were not cached
        if mods_to_run:
            if fail_fast or len(mods_to_run) <= 1 or max_workers == 1:
                for m in mods_to_run:
                    res = self.run_test(
                        m,
                        verbose=verbose,
                        fail_fast=fail_fast,
                        test_filter=test_filter,
                        use_cache=False,
                        force=True,
                    )
                    results_by_id[m.id] = res
                    if res.status == "passed":
                        summary.tested_mods += 1
                        summary.passed_mods += 1
                        summary.total_tests_passed += res.passed
                    elif res.status in ("failed", "error"):
                        summary.tested_mods += 1
                        summary.failed_mods += 1
                        summary.total_tests_passed += res.passed
                        summary.total_tests_failed += (res.failed + res.errors)
                        if fail_fast:
                            break
                    elif res.status == "skipped":
                        summary.skipped_mods += 1
                    elif res.status == "no_tests":
                        summary.no_tests_mods += 1
            else:
                workers = max_workers or min(12, max(4, (os.cpu_count() or 1) * 4))

                def _execute_mod(mod: Mod) -> TestResult:
                    return self.run_test(
                        mod,
                        verbose=verbose,
                        fail_fast=False,
                        test_filter=test_filter,
                        use_cache=False,
                        force=True,
                    )

                with ThreadPoolExecutor(max_workers=workers) as executor:
                    future_to_mod = {executor.submit(_execute_mod, m): m for m in mods_to_run}
                    for future in as_completed(future_to_mod):
                        m = future_to_mod[future]
                        try:
                            res = future.result()
                        except Exception as exc:
                            res = TestResult(
                                mod_id=m.id,
                                mod_name=m.name,
                                status="error",
                                duration=0.0,
                                error_message=f"Failed to execute mod tests: {exc}",
                            )
                        results_by_id[m.id] = res

                # Aggregate stats for parallel executed mods
                for m in mods_to_run:
                    res = results_by_id.get(m.id)
                    if not res:
                        continue
                    if res.status == "passed":
                        summary.tested_mods += 1
                        summary.passed_mods += 1
                        summary.total_tests_passed += res.passed
                    elif res.status in ("failed", "error"):
                        summary.tested_mods += 1
                        summary.failed_mods += 1
                        summary.total_tests_passed += res.passed
                        summary.total_tests_failed += (res.failed + res.errors)
                    elif res.status == "skipped":
                        summary.skipped_mods += 1
                    elif res.status == "no_tests":
                        summary.no_tests_mods += 1

        # Reconstruct results in original order
        summary.results = [results_by_id[m.id] for m in mods if m.id in results_by_id]
        summary.total_duration = time.time() - start_all
        return summary
