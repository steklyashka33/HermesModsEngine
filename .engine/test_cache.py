"""Hermes Mod Engine Incremental Test & Verification Cache.

Computes composite fingerprints for mods:
- Mod content hash (manifest, targets, tests, scripts, skills)
- Hermes Agent core git commit hash / version
- Mod Engine version (ENGINE_VERSION)
- Python runtime major.minor version

Provides incremental caching for runner.py verify, apply, and test.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import Mod
from .file_ops import FileLock, atomic_write_text
from .version import ENGINE_VERSION
from .loader import detect_hermes_root

HERMES_ROOT = detect_hermes_root()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(HERMES_HOME / "mods")))
DEFAULT_CACHE_REL_PATH = Path(".engine") / "test_cache.json"

# Files and directory patterns to ignore when computing mod directory hash
IGNORED_DIRS: Set[str] = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".git",
    ".idea",
    ".vscode",
    ".coverage",
    ".mypy_cache",
}
IGNORED_EXTS: Set[str] = {".pyc", ".pyo", ".pyd", ".swp", ".swo"}
IGNORED_NAMES: Set[str] = {
    ".DS_Store",
    "Thumbs.db",
    ".state.json",
    ".commands_cache.json",
    ".commands_cache.json.lock",
    "test_cache.json",
    ".test_cache.json",
    "test_cache.json.lock",
    ".test_cache.json.lock",
}


@functools.lru_cache(maxsize=32)
def get_hermes_git_commit(repo_root: Path = HERMES_ROOT) -> str:
    """Return the git commit hash of the Hermes Agent repository, or empty string."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass
    return ""


@functools.lru_cache(maxsize=64)
def _get_env_fingerprint_cached(
    repo_root_str: str,
    engine_ver: Optional[str],
    hermes_ver: Optional[str],
    git_commit: Optional[str],
) -> Dict[str, str]:
    repo_root = Path(repo_root_str)
    engine_version = str(engine_ver or ENGINE_VERSION)
    hermes_c = str(git_commit if git_commit is not None else get_hermes_git_commit(repo_root))

    if hermes_ver is not None:
        hermes_v = str(hermes_ver)
    elif hermes_c:
        hermes_v = hermes_c[:12]
    else:
        from .updater import get_hermes_version
        hermes_v = str(get_hermes_version(repo_root))

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"

    hasher = hashlib.sha256()
    hasher.update(hermes_c.encode("utf-8"))
    hasher.update(b":")
    hasher.update(hermes_v.encode("utf-8"))
    hasher.update(b":")
    hasher.update(engine_version.encode("utf-8"))
    hasher.update(b":")
    hasher.update(py_ver.encode("utf-8"))

    return {
        "digest": hasher.hexdigest(),
        "hermes_commit": hermes_c,
        "hermes_version": hermes_v,
        "engine_version": engine_version,
        "python_version": py_ver,
    }


def get_env_fingerprint(
    repo_root: Path = HERMES_ROOT,
    engine_ver: Optional[str] = None,
    hermes_ver: Optional[str] = None,
    git_commit: Optional[str] = None,
) -> Dict[str, str]:
    """Calculate environment fingerprint combining Hermes Agent commit, version and Engine version."""
    return _get_env_fingerprint_cached(
        str(repo_root),
        engine_ver,
        hermes_ver,
        git_commit,
    )


def compute_mod_hash(mod: Mod) -> str:
    """Calculate deterministic SHA256 content hash of a mod (files and metadata)."""
    hasher = hashlib.sha256()

    # 1. Base metadata
    mod_id = getattr(mod, "id", "") or ""
    mod_ver = getattr(mod, "version", "") or ""
    verify_cmd = getattr(mod, "verify_cmd", "") or ""
    hasher.update(f"id:{mod_id};version:{mod_ver};verify_cmd:{verify_cmd};".encode("utf-8"))

    # 2. Files
    if mod.is_folder and mod.mod_dir and mod.mod_dir.is_dir():
        mod_dir = mod.mod_dir.resolve()
        files_to_hash: List[Path] = []
        for root, dirs, files in os.walk(mod_dir):
            dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
            for f in files:
                if f in IGNORED_NAMES or any(f.endswith(ext) for ext in IGNORED_EXTS):
                    continue
                files_to_hash.append(Path(root) / f)

        # Sort files deterministically by relative path
        files_to_hash.sort(key=lambda p: str(p.relative_to(mod_dir)))
        for p in files_to_hash:
            try:
                rel = str(p.relative_to(mod_dir))
                content = p.read_bytes()
                hasher.update(rel.encode("utf-8"))
                hasher.update(b"\0")
                hasher.update(content)
                hasher.update(b"\0")
            except OSError:
                continue
    elif mod.path and mod.path.is_file():
        try:
            hasher.update(mod.path.read_bytes())
        except OSError:
            pass

    # 3. Inline tests (if single-file mod has parsed inline tests)
    if getattr(mod, "inline_tests", None):
        try:
            hasher.update(json.dumps(mod.inline_tests, sort_keys=True).encode("utf-8"))
        except Exception:
            pass

    return hasher.hexdigest()


def compute_composite_fingerprint(
    mod: Mod,
    repo_root: Path = HERMES_ROOT,
    engine_ver: Optional[str] = None,
    hermes_ver: Optional[str] = None,
    git_commit: Optional[str] = None,
) -> Tuple[str, str, Dict[str, str]]:
    """Compute composite fingerprint: (composite_hash, mod_hash, env_info)."""
    mod_hash = compute_mod_hash(mod)
    env_info = get_env_fingerprint(
        repo_root=repo_root,
        engine_ver=engine_ver,
        hermes_ver=hermes_ver,
        git_commit=git_commit,
    )
    composite = hashlib.sha256(f"{mod_hash}:{env_info['digest']}".encode("utf-8")).hexdigest()
    return composite, mod_hash, env_info


class TestCacheManager:
    """Manages reading, writing, and invalidation of mod test and verification cache."""
    __test__ = False

    def __init__(
        self,
        cache_file: Optional[Path] = None,
        mods_dir: Optional[Path] = None,
        repo_root: Optional[Path] = None,
    ):
        import threading
        self._mem_lock = threading.Lock()
        self.mods_dir = Path(mods_dir) if mods_dir else DEFAULT_MODS_DIR
        self.repo_root = Path(repo_root) if repo_root else HERMES_ROOT

        if cache_file:
            self.cache_file = Path(cache_file)
        else:
            # Check default .engine/test_cache.json or .test_cache.json
            p_engine = self.mods_dir / ".engine" / "test_cache.json"
            p_dot = self.mods_dir / ".test_cache.json"
            if p_dot.exists() and not p_engine.exists():
                self.cache_file = p_dot
            else:
                self.cache_file = p_engine

        self.lock_file = self.cache_file.parent / f".{self.cache_file.name}.lock"

    def load(self) -> Dict[str, Any]:
        """Load cache from disk."""
        if not self.cache_file.exists():
            return {"version": 1, "entries": {}}
        try:
            raw = self.cache_file.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                return {"version": 1, "entries": {}}
            if "entries" not in data or not isinstance(data["entries"], dict):
                data["entries"] = {}
            return data
        except Exception:
            return {"version": 1, "entries": {}}

    def _save_unlocked(self, data: Dict[str, Any]) -> None:
        """Internal write without acquiring locks (locks must be already held)."""
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        try:
            atomic_write_text(self.cache_file, content)
        except Exception:
            try:
                self.cache_file.write_text(content, encoding="utf-8")
            except Exception:
                pass

    def save(self, data: Dict[str, Any]) -> None:
        """Atomically persist cache data to disk with in-process and inter-process locking."""
        with self._mem_lock:
            try:
                with FileLock(self.lock_file):
                    self._save_unlocked(data)
            except Exception:
                self._save_unlocked(data)

    def get_cached_result(
        self,
        mod: Mod,
        repo_root: Optional[Path] = None,
        engine_ver: Optional[str] = None,
        hermes_ver: Optional[str] = None,
        git_commit: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return cached test result if fingerprint matches and result was verified."""
        root = repo_root or self.repo_root
        composite, mod_hash, env_info = compute_composite_fingerprint(
            mod,
            repo_root=root,
            engine_ver=engine_ver,
            hermes_ver=hermes_ver,
            git_commit=git_commit,
        )

        with self._mem_lock:
            data = self.load()
        entries = data.get("entries", {})
        entry = entries.get(mod.id)
        if not entry or not isinstance(entry, dict):
            return None

        # Verify composite fingerprint match
        if entry.get("fingerprint") != composite:
            return None

        # Must have verified == True and status == 'passed'
        if not entry.get("verified") or entry.get("status") != "passed":
            return None

        return entry

    def set_cached_result(
        self,
        mod: Mod,
        result: Any,
        repo_root: Optional[Path] = None,
        method: str = "",
        engine_ver: Optional[str] = None,
        hermes_ver: Optional[str] = None,
        git_commit: Optional[str] = None,
    ) -> None:
        """Cache a successful test or verification result."""
        root = repo_root or self.repo_root

        # Determine counts and success
        status = "passed"
        passed = 1
        failed = 0
        errors = 0
        skipped = 0
        total = 1
        duration = 0.0
        test_files: List[str] = []
        res_method = method

        def _rel_tf(p_str: str) -> str:
            try:
                p = Path(p_str)
                if p.is_relative_to(self.mods_dir):
                    return str(p.relative_to(self.mods_dir))
                try:
                    home = Path.home()
                    if p.is_relative_to(home):
                        return f"~/{p.relative_to(home)}"
                except Exception:
                    pass
            except Exception:
                pass
            return p_str

        if hasattr(result, "status"):
            # TestResult instance
            if result.status != "passed":
                # Only cache successful tests!
                return
            status = result.status
            passed = result.passed
            failed = result.failed
            errors = result.errors
            skipped = result.skipped
            total = result.total
            duration = result.duration
            res_method = result.method or method
            test_files = [_rel_tf(str(tf)) for tf in (result.test_files or [])]
        elif isinstance(result, dict):
            if not result.get("verified", False):
                return
            passed = result.get("passed", 1)
            total = passed
            duration = result.get("duration", 0.0)
            res_method = result.get("method", method or "verify_cmd")
            test_files = [_rel_tf(str(tf)) for tf in result.get("test_files", [])]
        elif result is True:
            passed = 1
            total = 1
            res_method = method or "verify_cmd"
        else:
            return

        composite, mod_hash, env_info = compute_composite_fingerprint(
            mod,
            repo_root=root,
            engine_ver=engine_ver,
            hermes_ver=hermes_ver,
            git_commit=git_commit,
        )

        entry = {
            "fingerprint": composite,
            "mod_hash": mod_hash,
            "env_fingerprint": env_info["digest"],
            "hermes_commit": env_info["hermes_commit"],
            "hermes_version": env_info["hermes_version"],
            "engine_version": env_info["engine_version"],
            "status": status,
            "verified": True,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "errors": errors,
            "total": total,
            "duration": round(duration, 3),
            "method": res_method,
            "test_files": test_files,
            "timestamp": time.time(),
        }

        with self._mem_lock:
            try:
                with FileLock(self.lock_file):
                    data = self.load()
                    entries = data.setdefault("entries", {})
                    entries[mod.id] = entry
                    data["last_updated"] = time.time()
                    data["hermes_commit"] = env_info["hermes_commit"]
                    data["engine_version"] = env_info["engine_version"]
                    self._save_unlocked(data)
            except Exception:
                data = self.load()
                entries = data.setdefault("entries", {})
                entries[mod.id] = entry
                self._save_unlocked(data)

    def has_valid_cache(
        self,
        mod: Mod,
        repo_root: Optional[Path] = None,
        engine_ver: Optional[str] = None,
        hermes_ver: Optional[str] = None,
        git_commit: Optional[str] = None,
    ) -> bool:
        """Check whether valid cache entry exists for mod."""
        return (
            self.get_cached_result(
                mod,
                repo_root=repo_root,
                engine_ver=engine_ver,
                hermes_ver=hermes_ver,
                git_commit=git_commit,
            )
            is not None
        )

    def invalidate(self, mod_id: Optional[str] = None) -> None:
        """Invalidate cache for a specific mod or all mods."""
        with self._mem_lock:
            try:
                with FileLock(self.lock_file):
                    data = self.load()
                    if mod_id:
                        if "entries" in data and mod_id in data["entries"]:
                            del data["entries"][mod_id]
                    else:
                        data["entries"] = {}
                    self._save_unlocked(data)
            except Exception:
                data = self.load()
                if mod_id:
                    if "entries" in data and mod_id in data["entries"]:
                        del data["entries"][mod_id]
                else:
                    data["entries"] = {}
                self._save_unlocked(data)
