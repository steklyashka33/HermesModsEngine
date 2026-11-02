"""Hermes Mod Updater: синхронизация с upstream, проверка совместимости,
транзакционный apply с бэкапом и откатом при сбоях.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .version import ENGINE_VERSION
from .models import Mod, CycleDetectionError
try:
    from .file_ops import FileLock, atomic_write_text, SignalTrap
except ImportError:
    class FileLock:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def atomic_write_text(p, content, encoding="utf-8"):
        Path(p).write_text(content, encoding=encoding)
    class SignalTrap:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

try:
    from .snapshot_manager import SnapshotManager
except ImportError:
    SnapshotManager = None

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
from .loader import detect_hermes_root, is_valid_hermes_root
HERMES_ROOT = detect_hermes_root()
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(Path(__file__).resolve().parent.parent)))
MODS_DIR = DEFAULT_MODS_DIR
BACKUPS_DIR = HERMES_HOME / "backups"
STATE_FILE = MODS_DIR / ".state.json"
DEFAULT_REMOTE_URL = "https://github.com/steklyashka33/HermesModsEngine.git"


def parse_version(v: Optional[str]) -> Tuple[int, ...]:
    """Преобразование строки версии в кортеж чисел для корректного сравнения."""
    if not v:
        return (0, 0, 0)
    cleaned = str(v).strip().lstrip("v")
    parts = re.findall(r"\d+", cleaned)
    if not parts:
        return (0, 0, 0)
    return tuple(int(p) for p in parts)


def compare_versions(v1: str, v2: str) -> int:
    """Сравнение двух версий: -1 если v1 < v2, 0 если v1 == v2, 1 если v1 > v2."""
    p1 = parse_version(v1)
    p2 = parse_version(v2)
    maxlen = max(len(p1), len(p2))
    p1_padded = p1 + (0,) * (maxlen - len(p1))
    p2_padded = p2 + (0,) * (maxlen - len(p2))
    if p1_padded < p2_padded:
        return -1
    elif p1_padded > p2_padded:
        return 1
    return 0


def get_hermes_version(repo_root: Path = HERMES_ROOT) -> str:
    """Определение текущей версии Hermes Agent."""
    # 0. Переменная окружения
    env_ver = os.environ.get("HERMES_VERSION", "").strip()
    if env_ver:
        return env_ver.lstrip("v")

    # 1. Попытка вызова hermes --version
    try:
        res = subprocess.run(
            ["hermes", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout:
            m = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", res.stdout)
            if m:
                return m.group(1)
    except Exception:
        pass

    # 2. Попытка чтения pyproject.toml
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text(encoding="utf-8")
            m = re.search(r'version\s*=\s*["\']([^"\']+)["\']', content)
            if m and m.group(1) != "0.0.0":
                return m.group(1)
        except Exception:
            pass

    # 3. Попытка importlib.metadata
    try:
        import importlib.metadata
        v = importlib.metadata.version("hermes-agent")
        if v and v != "0.0.0":
            return v
    except Exception:
        pass

    # 4. Попытка git describe
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "describe", "--tags", "--always"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            raw_tag = res.stdout.strip().lstrip("v")
            m = re.search(r"(\d+\.\d+(?:\.\d+)?)", raw_tag)
            if m:
                return m.group(1)
            return raw_tag
    except Exception:
        pass

    return "unknown"


@dataclass
class CompatibilityIssue:
    mod_id: str
    issue_type: str  # "engine_outdated" | "hermes_outdated"
    current_version: str
    required_version: str
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "issue_type": self.issue_type,
            "current_version": self.current_version,
            "required_version": self.required_version,
            "message": self.message,
        }


def check_compatibility(
    mods: List[Mod],
    engine_version: str = ENGINE_VERSION,
    hermes_version: Optional[str] = None,
    repo_root: Path = HERMES_ROOT,
) -> List[CompatibilityIssue]:
    """Проверка совместимости списка модов с версиями движка и Hermes."""
    if hermes_version is None:
        hermes_version = get_hermes_version(repo_root)

    issues: List[CompatibilityIssue] = []
    for mod in mods:
        if mod.min_engine_version:
            if compare_versions(engine_version, mod.min_engine_version) < 0:
                issues.append(
                    CompatibilityIssue(
                        mod_id=mod.id,
                        issue_type="engine_outdated",
                        current_version=engine_version,
                        required_version=mod.min_engine_version,
                        message=f"Моду '{mod.id}' требуется движок версии >= {mod.min_engine_version} (текущая: {engine_version})",
                    )
                )

        if mod.min_hermes_version:
            if compare_versions(hermes_version, mod.min_hermes_version) < 0:
                issues.append(
                    CompatibilityIssue(
                        mod_id=mod.id,
                        issue_type="hermes_outdated",
                        current_version=hermes_version,
                        required_version=mod.min_hermes_version,
                        message=f"Моду '{mod.id}' требуется Hermes версии >= {mod.min_hermes_version} (текущая: {hermes_version})",
                    )
                )

    return issues


class StateManager:
    """Управление локальным оверлеем состояния (.state.json) с flock-блокировками."""

    def __init__(self, state_file: Path = STATE_FILE):
        self.state_file = Path(state_file)

    def _load_unlocked(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {
                "overrides": {},
                "last_update_check": None,
                "installed_engine_version": ENGINE_VERSION,
            }
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {
                "overrides": {},
                "last_update_check": None,
                "installed_engine_version": ENGINE_VERSION,
            }

    def load(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {
                "overrides": {},
                "last_update_check": None,
                "installed_engine_version": ENGINE_VERSION,
            }
        try:
            with FileLock(self.state_file):
                return self._load_unlocked()
        except Exception:
            return self._load_unlocked()

    def save(self, data: Dict[str, Any]) -> None:
        try:
            with FileLock(self.state_file):
                atomic_write_text(self.state_file, json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as exc:
            sys.stderr.write(f"Warning: failed to save state to {self.state_file}: {exc}\n")

    def record_update(self, engine_version: str = ENGINE_VERSION) -> None:
        with FileLock(self.state_file):
            data = self._load_unlocked()
            data["last_update_check"] = int(time.time())
            data["installed_engine_version"] = engine_version
            atomic_write_text(self.state_file, json.dumps(data, indent=2, ensure_ascii=False))

    def set_mod_override(self, mod_id: str, overrides: Dict[str, Any]) -> None:
        with FileLock(self.state_file):
            data = self._load_unlocked()
            mod_ovr = data.setdefault("overrides", {}).setdefault(mod_id, {})
            mod_ovr.update(overrides)
            atomic_write_text(self.state_file, json.dumps(data, indent=2, ensure_ascii=False))

    def get_mod_overrides(self, mod_id: str) -> Dict[str, Any]:
        return self.load().get("overrides", {}).get(mod_id, {})


class BackupManager:
    """Управление атомарными снимками/бэкапами перед наложением патчей."""

    def __init__(self, backups_dir: Path = BACKUPS_DIR):
        self.backups_dir = backups_dir

    def create_backup(self, files: List[Path], repo_root: Path = HERMES_ROOT) -> Optional[Path]:
        if not files:
            return None
        timestamp = int(time.time())
        backup_id = f"update_backup_{timestamp}"
        target_dir = self.backups_dir / backup_id
        target_dir.mkdir(parents=True, exist_ok=True)

        manifest = {}
        for f in files:
            if f.exists():
                try:
                    rel = f.relative_to(repo_root)
                except ValueError:
                    rel = f.name
                dest = target_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                manifest[str(rel)] = str(f)

        (target_dir / "backup_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return target_dir

    def get_latest_backup(self) -> Optional[Path]:
        """Возвращает путь к последней папке бэкапа с backup_manifest.json."""
        if not self.backups_dir.exists():
            return None
        candidates = []
        try:
            for entry in self.backups_dir.iterdir():
                if entry.is_dir() and (entry / "backup_manifest.json").is_file():
                    candidates.append(entry)
        except OSError:
            return None
        if not candidates:
            return None
        candidates.sort(key=lambda p: (p.stat().st_mtime, p.name))
        return candidates[-1]

    def restore_backup(self, backup_dir: Path) -> bool:
        manifest_file = backup_dir / "backup_manifest.json"
        if not manifest_file.exists():
            return False
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            for rel, original_path_str in manifest.items():
                src = backup_dir / rel
                dest = Path(original_path_str)
                if src.exists():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
            return True
        except Exception as exc:
            sys.stderr.write(f"Error restoring backup from {backup_dir}: {exc}\n")
            return False


class ModUpdater:
    """Координатор синхронизации, проверки обновлений и безопасного наложения модов."""

    def __init__(
        self,
        mods_dir: Path = MODS_DIR,
        repo_root: Optional[Path] = None,
        git_bin: str = "git",
        backups_dir: Optional[Path] = None,
        default_remote_url: str = DEFAULT_REMOTE_URL,
    ):
        self.mods_dir = Path(mods_dir)
        self.repo_root = Path(repo_root) if repo_root else detect_hermes_root()
        self.git_bin = git_bin
        self.default_remote_url = default_remote_url
        self.state_mgr = StateManager(self.mods_dir / ".state.json")
        self.backup_mgr = BackupManager(backups_dir or (Path.home() / ".hermes" / "backups"))
        self.snapshot_mgr = SnapshotManager(repo_root=self.repo_root) if SnapshotManager else None

    def run_git(self, args: List[str]) -> Tuple[int, str, str]:
        cmd = [self.git_bin, "-C", str(self.mods_dir)] + args
        try:
            env = os.environ.copy()
            askpass_script = self.mods_dir / "scripts" / "git-askpass.sh"
            if askpass_script.exists():
                env["GIT_ASKPASS"] = str(askpass_script)
                env.setdefault("GIT_TERMINAL_PROMPT", "0")
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=env,
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except Exception as exc:
            return 1, "", str(exc)

    def ensure_remote(self, remote: str = "origin", default_url: Optional[str] = None) -> bool:
        """Проверяет наличие remote и при необходимости безопасно добавляет default_url."""
        if not self.is_git_repo():
            return False
        if remote.startswith(("http://", "https://", "git@", "ssh://")) or "/" in remote or "\\" in remote:
            return True
        code, remotes_out, _ = self.run_git(["remote"])
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip()]
        if remote not in remotes:
            target_url = default_url or self.default_remote_url
            code_add, _, _ = self.run_git(["remote", "add", remote, target_url])
            return code_add == 0
        return True

    def is_git_repo(self) -> bool:
        code, out, _ = self.run_git(["rev-parse", "--is-inside-work-tree"])
        if code != 0 or out.strip() != "true":
            return False
        # Prevent parent repository leakage (e.g. ~/.git leaking into mods_dir)
        code_top, out_top, _ = self.run_git(["rev-parse", "--show-toplevel"])
        if code_top == 0 and out_top.strip():
            try:
                top_path = Path(out_top.strip()).resolve()
                mods_res = self.mods_dir.resolve()
                if top_path != mods_res and mods_res.is_relative_to(top_path) and not (self.mods_dir / ".git").exists():
                    return False
            except Exception:
                pass
        return True

    def get_current_branch(self) -> str:
        code, out, _ = self.run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        return out if code == 0 else "main"

    def protect_local_mods(self) -> None:
        """Гарантирует защиту директории custom/ и .state.json."""
        custom_dir = self.mods_dir / "custom"
        if not custom_dir.exists():
            try:
                custom_dir.mkdir(parents=True, exist_ok=True)
                gitkeep = custom_dir / ".gitkeep"
                if not gitkeep.exists():
                    gitkeep.touch()
            except OSError:
                pass

        gitignore = self.mods_dir / ".gitignore"
        if gitignore.exists():
            try:
                content = gitignore.read_text(encoding="utf-8")
                needed = ["custom/*", "!custom/.gitkeep", ".state.json"]
                lines_to_add = [n for n in needed if n not in content]
                if lines_to_add:
                    with open(gitignore, "a", encoding="utf-8") as f:
                        f.write("\n" + "\n".join(lines_to_add) + "\n")
            except OSError:
                pass

    def check_updates(self, remote: str = "origin", branch: str = "main") -> Dict[str, Any]:
        """Проверка наличия обновлений в upstream без их применения.
        Возвращает:
          status: 'up_to_date' | 'updates_available' | 'not_a_git_repo' | 'fetch_failed'
          exit_code: 0 (актуально) | 2 (есть обновления) | 1 (ошибка)
        """
        self.protect_local_mods()

        if not self.is_git_repo():
            return {
                "status": "not_a_git_repo",
                "message": f"{self.mods_dir} не является Git-репозиторием",
                "exit_code": 1,
                "behind": 0,
                "ahead": 0,
                "compatible": True,
                "issues": [],
            }

        # git fetch
        if remote.startswith(("http://", "https://", "git@", "ssh://")):
            remote_ref = "FETCH_HEAD"
            code, _, err = self.run_git(["fetch", remote, branch, "--tags"])
        else:
            self.ensure_remote(remote)
            remote_ref = f"{remote}/{branch}"
            code, _, err = self.run_git(["fetch", remote, branch, "--tags"])
        if code != 0:
            return {
                "status": "fetch_failed",
                "message": f"Ошибка выполнения git fetch {remote} {branch}: {err}",
                "exit_code": 1,
                "behind": 0,
                "ahead": 0,
                "compatible": True,
                "issues": [],
            }

        # commit comparison
        c_loc, local_commit, _ = self.run_git(["rev-parse", "HEAD"])
        c_rem, remote_commit, _ = self.run_git(["rev-parse", remote_ref])
        if c_loc != 0 or c_rem != 0:
            return {
                "status": "error",
                "message": f"Не удалось определить хэши коммитов для HEAD или {remote_ref}",
                "exit_code": 1,
                "behind": 0,
                "ahead": 0,
                "compatible": True,
                "issues": [],
            }

        _, behind_str, _ = self.run_git(["rev-list", "--count", f"HEAD..{remote_ref}"])
        _, ahead_str, _ = self.run_git(["rev-list", "--count", f"{remote_ref}..HEAD"])
        behind = int(behind_str) if behind_str.isdigit() else 0
        ahead = int(ahead_str) if ahead_str.isdigit() else 0

        from .loader import ModLoader
        current_mods = ModLoader.load_all(self.mods_dir)
        hermes_ver = get_hermes_version(self.repo_root)
        issues = check_compatibility(current_mods, ENGINE_VERSION, hermes_ver, self.repo_root)

        self.state_mgr.record_update(ENGINE_VERSION)

        if behind == 0:
            return {
                "status": "up_to_date",
                "message": f"Экосистема модов актуальна (v{ENGINE_VERSION})",
                "exit_code": 0,
                "behind": 0,
                "ahead": ahead,
                "local_commit": local_commit[:8],
                "remote_commit": remote_commit[:8],
                "compatible": len(issues) == 0,
                "issues": [i.to_dict() for i in issues],
            }
        else:
            return {
                "status": "updates_available",
                "message": f"Доступно {behind} обновлений из {remote}/{branch}",
                "exit_code": 2,
                "behind": behind,
                "ahead": ahead,
                "local_commit": local_commit[:8],
                "remote_commit": remote_commit[:8],
                "compatible": len(issues) == 0,
                "issues": [i.to_dict() for i in issues],
            }

    def pull_updates(self, remote: str = "origin", branch: str = "main", force: bool = False) -> Tuple[bool, str]:
        """Синхронизация с веткой remote/branch с защитой custom/ и .state.json."""
        if not self.is_git_repo():
            return False, f"{self.mods_dir} не является Git-репозиторием"

        self.protect_local_mods()

        if remote.startswith(("http://", "https://", "git@", "ssh://")):
            remote_ref = "FETCH_HEAD"
        else:
            self.ensure_remote(remote)
            remote_ref = f"{remote}/{branch}"

        custom_path = self.mods_dir / "custom"
        state_path = self.mods_dir / ".state.json"
        cached_custom: Dict[Path, bytes] = {}
        cached_state: Optional[str] = None

        if custom_path.exists():
            for p in custom_path.rglob("*"):
                if p.is_file():
                    try:
                        cached_custom[p.relative_to(custom_path)] = p.read_bytes()
                    except OSError:
                        pass

        if state_path.exists():
            try:
                cached_state = state_path.read_text(encoding="utf-8")
            except OSError:
                pass

        stashed = False
        try:
            if force:
                code, _, err = self.run_git(["reset", "--hard", remote_ref])
                if code != 0:
                    return False, f"git reset --hard failed: {err}"
            else:
                code, status_out, _ = self.run_git(["status", "--porcelain"])
                if status_out:
                    t_stamp = int(time.time())
                    c_stash, _, _ = self.run_git(["stash", "push", "-m", f"hermes-mods-autostash-{t_stamp}"])
                    if c_stash == 0:
                        stashed = True

                code, out, err = self.run_git(["merge", "--ff-only", remote_ref])
                if code != 0:
                    if stashed:
                        self.run_git(["stash", "pop"])
                    return False, f"Fast-forward merge невозможен: {err or out}. Ветки разошлись."

                if stashed:
                    self.run_git(["stash", "pop"])

            return True, "Успешно получены обновления"

        finally:
            # Восстанавливаем custom/ и .state.json при любых обстоятельствах
            if cached_custom:
                for rel, data in cached_custom.items():
                    target = custom_path / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists() or target.read_bytes() != data:
                        target.write_bytes(data)

            if cached_state:
                if not state_path.exists() or state_path.read_text(encoding="utf-8") != cached_state:
                    state_path.write_text(cached_state, encoding="utf-8")

    def apply_updated_mods(
        self,
        dry_run: bool = False,
        runner_instance: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Топологический apply модов с бэкапом и verify."""
        from runner import ModLoader, ModRunner
        from .loader import ModDependencyGraph

        runner = runner_instance or ModRunner(self.repo_root)
        mods = ModLoader.load_all(self.mods_dir)

        hermes_ver = get_hermes_version(self.repo_root)
        issues = check_compatibility(mods, ENGINE_VERSION, hermes_ver, self.repo_root)
        incompatible_map = {i.mod_id: i for i in issues}

        graph = ModDependencyGraph(mods)
        try:
            graph.detect_cycles()
        except CycleDetectionError as exc:
            return {
                "success": False,
                "status": "cycle_detected",
                "error": str(exc),
                "mods_results": [],
            }

        ordered_mods = graph.topological_sort(mods)

        if dry_run:
            simulated = []
            for mod in ordered_mods:
                if mod.id in incompatible_map:
                    issue = incompatible_map[mod.id]
                    simulated.append({
                        "id": mod.id,
                        "name": mod.name,
                        "status": f"skipped_{issue.issue_type}",
                        "reason": issue.message,
                        "verified": False,
                    })
                else:
                    res = runner.apply_mod(mod, apply_changes=False, graph=graph)
                    simulated.append(res)

            return {
                "success": True,
                "status": "dry_run_completed",
                "dry_run": True,
                "mods_results": simulated,
                "compatibility_issues": [i.to_dict() for i in issues],
            }

        files_to_backup: Set[Path] = set()
        active_mod_ids = []
        for mod in ordered_mods:
            if not mod.enabled or mod.id in incompatible_map:
                continue
            active_mod_ids.append(mod.id)
            for t in mod.targets:
                if not t.disabled:
                    p = self.repo_root / t.file
                    if p.exists():
                        files_to_backup.add(p)

        if files_to_backup and self.snapshot_mgr:
            try:
                self.snapshot_mgr.create_snapshot(
                    files=list(files_to_backup),
                    description="Auto snapshot before update apply",
                    trigger="pre_apply",
                    mod_ids=active_mod_ids,
                    mods_dir=self.mods_dir,
                )
            except Exception as exc:
                sys.stderr.write(f"Warning: failed to create core snapshot in updater: {exc}\n")

        backup_dir = self.backup_mgr.create_backup(list(files_to_backup), self.repo_root)

        applied = []
        any_failed = False
        failed_mod_id = None

        for mod in ordered_mods:
            if mod.id in incompatible_map:
                issue = incompatible_map[mod.id]
                applied.append({
                    "id": mod.id,
                    "name": mod.name,
                    "status": f"skipped_{issue.issue_type}",
                    "reason": issue.message,
                    "verified": False,
                })
                continue

            res = runner.apply_mod(mod, apply_changes=True, graph=graph)
            applied.append(res)

            if res.get("status") in (
                "failed",
                "failed_verification",
                "failed_verification_rolled_back",
                "failed_exception_rolled_back",
                "failed_preflight_validation",
            ):
                any_failed = True
                failed_mod_id = mod.id
                break

        if any_failed:
            if backup_dir:
                self.backup_mgr.restore_backup(backup_dir)

            return {
                "success": False,
                "status": "failed_verification_rolled_back",
                "failed_mod": failed_mod_id,
                "error": f"Верификация провалена для мода '{failed_mod_id}'. Выполнен автоматический откат файлов ядра.",
                "backup_dir": str(backup_dir) if backup_dir else None,
                "mods_results": applied,
            }

        self.state_mgr.record_update(ENGINE_VERSION)

        # Атомарная регенерация всех JSON-кэшей ядра (Zero Downtime)
        try:
            from .loader import update_commands_cache, update_locales_cache, update_mcp_cache
            all_mods = ModLoader.load_all(self.mods_dir)
            update_commands_cache(self.mods_dir, all_mods)
            update_locales_cache(self.mods_dir, all_mods, repo_root=self.repo_root)
            update_mcp_cache(self.mods_dir, all_mods, repo_root=self.repo_root)
        except Exception as exc:
            sys.stderr.write(f"Warning: error recompiling mod caches after update: {exc}\n")

        return {
            "success": True,
            "status": "applied_successfully",
            "mods_results": applied,
            "backup_dir": str(backup_dir) if backup_dir else None,
            "compatibility_issues": [i.to_dict() for i in issues],
        }

    def update(
        self,
        check: bool = False,
        dry_run: bool = False,
        no_apply: bool = False,
        force: bool = False,
        remote: str = "origin",
        branch: str = "main",
        runner_instance: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Полный цикл обновления."""
        if check:
            return self.check_updates(remote=remote, branch=branch)

        if dry_run:
            check_res = {}
            if self.is_git_repo():
                try:
                    check_res = self.check_updates(remote=remote, branch=branch)
                except Exception:
                    pass
            apply_res = self.apply_updated_mods(dry_run=True, runner_instance=runner_instance)
            return {
                "status": "dry_run",
                "git_check": check_res,
                "apply_simulation": apply_res,
                "exit_code": 0 if apply_res.get("success") else 1,
            }

        git_updated = False
        if self.is_git_repo():
            self.ensure_remote(remote)
            fetch_code, _, fetch_err = self.run_git(["fetch", remote, branch, "--tags"])
            if fetch_code != 0 and not force:
                return {
                    "status": "error",
                    "error": f"git fetch failed: {fetch_err}",
                    "exit_code": 1,
                }
            pull_ok, pull_msg = self.pull_updates(remote=remote, branch=branch, force=force)
            if not pull_ok:
                return {
                    "status": "error",
                    "error": pull_msg,
                    "exit_code": 1,
                }
            git_updated = True

        if no_apply:
            self.state_mgr.record_update(ENGINE_VERSION)
            return {
                "status": "repo_updated_only",
                "git_updated": git_updated,
                "exit_code": 0,
                "message": "Репозиторий модов успешно обновлен без модификации файлов Hermes Agent.",
            }

        apply_res = self.apply_updated_mods(dry_run=False, runner_instance=runner_instance)
        exit_code = 0 if apply_res.get("success") else 1
        return {
            "status": "completed" if apply_res.get("success") else "failed",
            "git_updated": git_updated,
            "apply": apply_res,
            "exit_code": exit_code,
        }
