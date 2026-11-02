#!/usr/bin/env python3
"""
Hermes Mod Runner: гибридный движок загрузки, применения и оркестрации модов.
Поддерживает:
- Однофайловые моды (*.yaml) и моды-папки (<id>/mod.yaml)
- Атомарное наложение патчей (targets) с откатом
- Декларативный config с защищенным хранением секретов (0600)
- Фоновые демоны-мониторы (monitors) с rate-limiting и стримингом вывода
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml

# Resolve hidden engine package (.engine) relative to this runner script
import importlib.util
_script_dir = Path(__file__).resolve().parent
_engine_dir = _script_dir / ".engine"
if str(_engine_dir) not in sys.path:
    sys.path.insert(0, str(_engine_dir))

if "engine" not in sys.modules and (_engine_dir / "__init__.py").exists():
    _spec = importlib.util.spec_from_file_location("engine", str(_engine_dir / "__init__.py"))
    if _spec and _spec.loader:
        _pkg = importlib.util.module_from_spec(_spec)
        sys.modules["engine"] = _pkg
        _spec.loader.exec_module(_pkg)

from engine import (
    Mod, ModCommand, CycleDetectionError,
    ModDependencyGraph, parse_mod_manifest, ModConfigManager, ModMonitorManager, get_enabled_mod_skills_dirs,
    resolve_mod_mcp_servers, _mod_mcp_cache_clear,
    ModValidator, update_commands_cache, update_locales_cache, update_mcp_cache, register_mod_locales,
    ENGINE_VERSION, ModUpdater,
    FileLock, atomic_write_text, SignalTrap, StateManager, BackupManager, CompositeSimulationReport,
    ModTestRunner, TestResult, TestSuiteSummary, TestCacheManager,
    SnapshotManager, SnapshotManifest, RestoreResult,
    GatewayWatchdog, WatchdogResult,
    CircuitBreaker, get_circuit_breaker, CIRCUIT_STATE_OPEN,
    ModInspector,
    engine_i18n, t as engine_t, OFFICIAL_LANGUAGES,
    is_valid_hermes_root, detect_hermes_root,
)
from engine.config_manager import SECRETS_FILE, CONFIG_FILE

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES_ROOT = detect_hermes_root()
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(_script_dir)))
MODS_DIR = DEFAULT_MODS_DIR


def find_venv_python(hermes_root: Path) -> Path:
    candidates = [
        hermes_root / "venv" / "bin" / "python3",
        hermes_root / ".venv" / "bin" / "python3",
        Path(sys.prefix) / "bin" / "python3",
        Path(sys.executable),
    ]
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            return c
    return Path(sys.executable)


VENV_PYTHON = find_venv_python(HERMES_ROOT)


class ModLoader:
    """Обнаруживает и парсит однофайловые моды и моды-папки."""

    @staticmethod
    def load_all(mods_dir: Path = MODS_DIR) -> List[Mod]:
        if not mods_dir.exists():
            return []

        mods: Dict[str, Mod] = {}

        EXCLUDED_DIRS = {
            "templates", "docs", "tests", "skills", ".engine", ".git", ".github",
            "__pycache__", ".pytest_cache", ".venv", "venv", "node_modules", "build", "dist"
        }

        # 1. Сначала сканируем директории (моды-папки)
        for entry in sorted(mods_dir.iterdir()):
            if entry.name.startswith((".", "_")) or entry.name.endswith(".disabled") or entry.name in EXCLUDED_DIRS:
                continue

            if entry.is_dir():
                if entry.name in ("custom", "system"):
                    # Сканируем моды внутри custom/ и system/
                    for sub in sorted(entry.iterdir()):
                        if sub.name.startswith((".", "_")) or sub.name.endswith(".disabled"):
                            continue
                        if sub.is_dir():
                            sub_manifest = sub / "mod.yaml"
                            if not sub_manifest.exists():
                                sub_manifest = sub / f"{sub.name}.yaml"
                            if sub_manifest.exists():
                                mod = parse_mod_manifest(sub_manifest, mod_dir=sub, fallback_id=sub.name)
                                if mod and mod.id not in mods:
                                    mods[mod.id] = mod
                        elif sub.is_file() and sub.suffix in (".yaml", ".yml"):
                            mod_id = sub.stem
                            if mod_id not in mods:
                                mod = parse_mod_manifest(sub, mod_dir=None, fallback_id=mod_id)
                                if mod and mod.id not in mods:
                                    mods[mod.id] = mod
                    continue

                manifest = entry / "mod.yaml"
                if not manifest.exists():
                    manifest = entry / f"{entry.name}.yaml"
                if manifest.exists():
                    mod = parse_mod_manifest(manifest, mod_dir=entry, fallback_id=entry.name)
                    if mod:
                        mods[mod.id] = mod

        # 2. Сканируем однофайловые моды (*.yaml, *.yml)
        for entry in sorted(mods_dir.iterdir()):
            if entry.name.startswith((".", "_")) or entry.name.endswith(".disabled"):
                continue

            if entry.is_file() and entry.suffix in (".yaml", ".yml"):
                mod_id = entry.stem
                if mod_id in mods:
                    continue  # Папка имеет приоритет
                mod = parse_mod_manifest(entry, mod_dir=None, fallback_id=mod_id)
                if mod:
                    mods[mod.id] = mod

        # 3. Применяем оверлей состояния из .state.json при наличии
        state_file = mods_dir / ".state.json"
        if state_file.is_file():
            try:
                state_data = json.loads(state_file.read_text(encoding="utf-8"))
                overrides = state_data.get("overrides", {})
                for mid, m_overrides in overrides.items():
                    if mid in mods and isinstance(m_overrides, dict):
                        if "enabled" in m_overrides:
                            mods[mid].enabled = bool(m_overrides["enabled"])
                        if "disabled_circuit_breaker" in m_overrides:
                            mods[mid].disabled_circuit_breaker = bool(m_overrides["disabled_circuit_breaker"])
                        if "disabled_reason" in m_overrides:
                            mods[mid].disabled_reason = str(m_overrides["disabled_reason"])
            except Exception:
                pass

        return sorted(mods.values(), key=lambda m: m.id)

    @staticmethod
    def build_graph(mods: Optional[List[Mod]] = None, mods_dir: Path = MODS_DIR) -> ModDependencyGraph:
        if mods is None:
            mods = ModLoader.load_all(mods_dir)
        return ModDependencyGraph(mods)

    @staticmethod
    def get_active_skills_dirs(mods_dir: Path = MODS_DIR) -> List[Path]:
        return get_enabled_mod_skills_dirs(mods_dir)


SYSTEM_MOD_IDS = {
    "mod-command-registry",
    "mod-declarative-mcp",
    "mod-declarative-i18n",
    "mod-folder-skills",
    "mod-update-integration",
}


def is_system_mod(mod: Mod, mods_dir: Optional[Path] = None) -> bool:
    """Check if mod belongs to the system/ directory or is authored by Engine (Tier 0)."""
    if getattr(mod, "id", None) in SYSTEM_MOD_IDS:
        return True
    if (getattr(mod, "author", "") or "").strip().lower() == "engine":
        return True
    if mods_dir is not None and getattr(mod, "path", None):
        try:
            system_dir = (Path(mods_dir) / "system").resolve()
            if Path(mod.path).resolve().is_relative_to(system_dir):
                return True
        except (OSError, ValueError):
            pass

    if getattr(mod, "path", None):
        norm_path = str(mod.path).replace("\\", "/")
        if "/system/" in norm_path or norm_path.startswith("system/"):
            return True

    return False


class ModRunner:
    """Применяет моды, проверяет их состояние и выполняет верификацию."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        mods_dir: Optional[Path] = None,
        backups_dir: Optional[Path] = None,
        config_file: Optional[Path] = None,
        cache_mgr: Optional[TestCacheManager] = None,
    ):
        self.repo = Path(repo_root) if repo_root else Path(os.environ.get("HERMES_ROOT", HERMES_ROOT))
        self.mods_dir = Path(mods_dir) if mods_dir else Path(os.environ.get("HERMES_MODS_DIR", MODS_DIR))
        self.python_bin = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
        self.backups_dir = Path(backups_dir) if backups_dir else (HERMES_HOME / "backups")
        self.backup_mgr = BackupManager(self.backups_dir)
        self.snapshot_mgr = SnapshotManager(repo_root=self.repo) if SnapshotManager else None
        self.config_file = Path(config_file) if config_file else CONFIG_FILE
        self.config_mgr = ModConfigManager(self.config_file)
        self.cache_mgr = cache_mgr if cache_mgr is not None else TestCacheManager(mods_dir=self.mods_dir, repo_root=self.repo)

    def get_inspector(self) -> ModInspector:
        """Returns initialized ModInspector instance."""
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        return ModInspector(
            mods_dir=self.mods_dir,
            repo_root=self.repo,
            config_file=self.config_file,
            secrets_file=Path(os.environ.get("HERMES_SECRETS", SECRETS_FILE)),
            log_dir=Path(os.environ.get("HERMES_LOG_DIR", hermes_home / "logs")),
        )

    def list_mods(self, status_filter: str = "all", lang: str = "auto") -> List[Dict[str, Any]]:
        """List mods with filtering by application status."""
        return self.get_inspector().list_mods(status_filter=status_filter, lang=lang)

    def get_status(self, lang: str = "auto") -> Dict[str, Any]:
        """Get comprehensive ecosystem status."""
        return self.get_inspector().get_ecosystem_status(lang=lang)

    def get_logs(self, mod: Mod, lines: int = 50, lang: str = "auto") -> Dict[str, Any]:
        """Get recent logs for a specific mod."""
        return self.get_inspector().get_mod_logs(mod, lines=lines, lang=lang)

    def is_mods_enabled(self) -> bool:
        """Проверяет значение mods.enabled в config.yaml (по умолчанию True)."""
        return self.config_mgr.is_mods_enabled()

    def is_any_applied(self, mods: Optional[List[Mod]] = None) -> bool:
        """Проверяет, применен ли хотя бы один таргет активных модов к коду ядра."""
        if mods is None:
            mods = ModLoader.load_all(self.mods_dir)
        for mod in mods:
            for t in mod.targets:
                if t.disabled:
                    continue
                target_path = self.repo / t.file
                if target_path.is_file():
                    try:
                        content = target_path.read_text(encoding="utf-8")
                        if t.new_block in content:
                            return True
                    except Exception:
                        pass
        return False

    def reset_caches(self) -> None:
        """Сбрасывает кэши скиллов и MCP."""
        try:
            from agent.skill_utils import _mod_skills_dirs_cache_clear
            _mod_skills_dirs_cache_clear()
        except Exception:
            pass

        try:
            _mod_mcp_cache_clear()
        except Exception:
            pass

        try:
            update_mcp_cache(None, self.mods_dir)
        except Exception:
            pass

        try:
            self.mods_dir.touch()
        except OSError:
            pass

    def unpatch_all(
        self,
        mods: Optional[List[Mod]] = None,
        graph: Optional[ModDependencyGraph] = None,
        include_system: bool = False,
    ) -> Dict[str, Any]:
        """Чистый ООП-метод:
        - Обходит все файлы ядра /usr/local/lib/hermes-agent, затронутые активными модами.
        - По умолчанию откатывает ТОЛЬКО пользовательские моды из custom/ (Tier 1 User mods), сохраняя системные моды ядра.
        - При include_system=True откатывает все моды, включая системные (Tier 0 Engine mods).
        - Атомарно восстанавливает файлы ядра в оригинальное состояние.
        - Обновляет или очищает .commands_cache.json.
        - Сбрасывает кэши скиллов и MCP.
        - Возвращает отчет: сколько файлов восстановлено, статус unpatched.
        """
        if mods is None:
            mods = ModLoader.load_all(self.mods_dir)

        # По умолчанию откатываем ТОЛЬКО пользовательские моды (Tier 1), сохраняя системные (Tier 0)
        if not include_system:
            target_mods = [m for m in mods if not is_system_mod(m, self.mods_dir)]
        else:
            target_mods = list(mods)

        if graph is not None:
            ordered_mods = graph.topological_sort(target_mods)
        else:
            ordered_mods = sorted(target_mods, key=lambda m: (0 if (m.author or "").strip().lower() == "engine" else 1, m.id))

        restored_files: Set[str] = set()

        # 1. Попытка загрузить последний snapshot бэкап
        latest_backup = None
        if hasattr(self.backup_mgr, "get_latest_backup"):
            latest_backup = self.backup_mgr.get_latest_backup()
        backup_manifest: Dict[str, str] = {}
        if latest_backup and (latest_backup / "backup_manifest.json").is_file():
            try:
                backup_manifest = json.loads((latest_backup / "backup_manifest.json").read_text(encoding="utf-8"))
            except Exception:
                backup_manifest = {}

        # 2. Обход файлов и атомарный откат по таргетам в обратном порядке наложения
        files_content: Dict[Path, str] = {}
        files_original: Dict[Path, str] = {}

        for mod in reversed(ordered_mods):
            for t in reversed(mod.targets):
                if t.disabled:
                    continue
                target_path = self.repo / t.file
                if not target_path.exists() or not target_path.is_file():
                    continue

                if target_path not in files_content:
                    try:
                        text = target_path.read_text(encoding="utf-8")
                        files_content[target_path] = text
                        files_original[target_path] = text
                    except Exception:
                        continue

                sim = files_content[target_path]
                if t.new_block in sim:
                    files_content[target_path] = sim.replace(t.new_block, t.old_block, 1)

        # 3. Если snapshot бэкап доступен, используем его для файлов, где таргеты не снялись
        if latest_backup and backup_manifest:
            all_known_mods = ModLoader.load_all(self.mods_dir) if self.mods_dir.exists() else []
            system_target_files = {
                t.file for m in all_known_mods if is_system_mod(m, self.mods_dir) for t in m.targets
            }
            for rel, orig_path_str in backup_manifest.items():
                dest = Path(orig_path_str)
                src = latest_backup / rel
                if src.is_file() and dest.exists():
                    try:
                        backup_text = src.read_text(encoding="utf-8")
                        current_text = files_content.get(dest, dest.read_text(encoding="utf-8"))
                        if current_text != backup_text:
                            rel_repo = None
                            try:
                                rel_repo = str(dest.relative_to(self.repo))
                            except ValueError:
                                pass
                            if rel_repo and any(t.file == rel_repo for m in ordered_mods for t in m.targets):
                                # При обычном unpatch (без --all/--system) не восстанавливаем из бэкапа файлы системных модов
                                if not include_system and rel_repo in system_target_files:
                                    continue
                                # Если откатывается отдельный мод, не перезаписываем файл бэкапом, если другие активные моды патчат его
                                other_active_target_files = {
                                    t.file for m in all_known_mods if m.id not in {mod.id for mod in ordered_mods} and m.enabled for t in m.targets
                                }
                                if rel_repo in other_active_target_files:
                                    continue
                                files_content[dest] = backup_text
                                if dest not in files_original:
                                    files_original[dest] = current_text
                    except Exception:
                        pass

        # 4. Атомарно записываем восстановленные файлы ядра
        for target_path, new_text in files_content.items():
            orig_text = files_original.get(target_path)
            if orig_text is not None and new_text != orig_text:
                atomic_write_text(target_path, new_text)
                try:
                    rel_name = str(target_path.relative_to(self.repo))
                except ValueError:
                    rel_name = str(target_path)
                restored_files.add(rel_name)

        # 5. Очищаем или обновляем .commands_cache.json
        cache_path = self.mods_dir / ".commands_cache.json"
        all_mods_for_cache = ModLoader.load_all(self.mods_dir) if self.mods_dir.exists() else []
        unpatched_ids = {m.id for m in ordered_mods}
        if not include_system:
            remaining_mods = [m for m in all_mods_for_cache if m.id not in unpatched_ids and m.enabled]
        else:
            remaining_mods = []
        if remaining_mods:
            try:
                update_commands_cache(remaining_mods, self.mods_dir)
                update_locales_cache(remaining_mods, self.mods_dir)
                update_mcp_cache(remaining_mods, self.mods_dir)
            except Exception:
                pass
        else:
            try:
                with FileLock(cache_path):
                    atomic_write_text(cache_path, "{}\n")
            except Exception:
                try:
                    cache_path.write_text("{}\n", encoding="utf-8")
                except Exception:
                    pass
            locales_cache = self.mods_dir / ".locales_cache.json"
            try:
                with FileLock(locales_cache):
                    atomic_write_text(locales_cache, "{}\n")
            except Exception:
                try:
                    locales_cache.write_text("{}\n", encoding="utf-8")
                except Exception:
                    pass
            mcp_cache = self.mods_dir / ".mcp_cache.json"
            try:
                with FileLock(mcp_cache):
                    atomic_write_text(mcp_cache, "{}\n")
            except Exception:
                try:
                    mcp_cache.write_text("{}\n", encoding="utf-8")
                except Exception:
                    pass

        # 6. Сбрасываем кэши скиллов и MCP
        self.reset_caches()

        return {
            "status": "unpatched",
            "restored_count": len(restored_files),
            "restored_files": sorted(list(restored_files)),
            "commands_cache_cleared": True,
            "caches_reset": True,
            "include_system": include_system,
        }

    def run_verify(self, cmd_str: str, cwd: Optional[Path] = None, mod: Optional[Mod] = None) -> Tuple[bool, str]:
        parts = cmd_str.split()
        if parts and parts[0] in ("python", "python3"):
            parts[0] = str(self.python_bin)
            cmd = " ".join(parts)
        elif parts and parts[0] == "pytest":
            parts[0] = str(self.python_bin.parent / "pytest")
            cmd = " ".join(parts)
        else:
            cmd = cmd_str

        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")

        if mod:
            env["HERMES_MOD_ID"] = mod.id
            env["HERMES_MOD_VERSION"] = mod.version
            if mod.mod_dir:
                env["HERMES_MOD_DIR"] = str(mod.mod_dir)
                env["PYTHONPATH"] = f"{mod.mod_dir}:{self.repo}:{existing_pp}" if existing_pp else f"{mod.mod_dir}:{self.repo}"
            else:
                env["HERMES_MOD_DIR"] = ""
                env["PYTHONPATH"] = f"{self.repo}:{existing_pp}" if existing_pp else str(self.repo)

            if mod.config:
                try:
                    cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
                    resolved_cfg = cfg_mgr.get_resolved_config(mod)
                    for k, v in resolved_cfg.items():
                        if v is not None:
                            env[f"HERMES_MOD_CONFIG_{str(k).upper()}"] = str(v)
                            env[f"HERMES_MOD_OPTION_{str(k).upper()}"] = str(v)
                except Exception:
                    pass
        else:
            env["PYTHONPATH"] = f"{self.repo}:{existing_pp}" if existing_pp else str(self.repo)

        run_cwd = cwd or self.repo
        if mod and mod.mod_dir and not cwd:
            for token in parts[1:]:
                if (mod.mod_dir / token).exists():
                    run_cwd = mod.mod_dir
                    break

        try:
            verify_timeout = int(getattr(mod, "verify_timeout", 45) if mod else 45)
            res = subprocess.run(
                cmd,
                shell=True,
                cwd=str(run_cwd),
                env=env,
                capture_output=True,
                text=True,
                timeout=verify_timeout,
            )
            return res.returncode == 0, (res.stderr or res.stdout).strip()
        except Exception as e:
            return False, str(e)

    def apply_mod(
        self,
        mod: Mod,
        apply_changes: bool = True,
        graph: Optional[ModDependencyGraph] = None,
        transaction_managed: bool = False,
        force: bool = False,
    ) -> Dict[str, Any]:
        result_base = {
            "id": mod.id,
            "name": mod.name,
            "version": mod.version,
            "targets_count": len(mod.targets),
            "config_count": len(mod.config),
            "user_config_count": len(mod.config),
            "monitors_count": len(mod.monitors),
            "mcp_servers_count": len(mod.mcp_servers),
            "dependencies": mod.dependencies,
        }

        if apply_changes and not self.is_mods_enabled():
            if not is_system_mod(mod, self.mods_dir):
                unpatch_report = None
                if self.is_any_applied([mod]):
                    unpatch_report = self.unpatch_all([mod], graph=graph, include_system=False)
                return {
                    **result_base,
                    "status": "blocked_killswitch",
                    "reason": "mods.enabled is set to false in config.yaml",
                    "verified": False,
                    "unpatch_report": unpatch_report,
                }

        if getattr(mod, "disabled_circuit_breaker", False):
            return {
                **result_base,
                "status": "disabled_circuit_breaker",
                "reason": getattr(mod, "disabled_reason", None) or "disabled by runtime circuit breaker",
                "verified": True,
            }

        if not mod.enabled:
            return {**result_base, "status": "disabled", "verified": True}

        # Check dependencies if graph is available
        if graph is not None:
            missing_deps = graph.get_missing_dependencies(mod)
            if missing_deps:
                return {
                    **result_base,
                    "status": "skipped_missing_dependency",
                    "reason": f"missing dependencies: {', '.join(missing_deps)}",
                    "verified": False,
                }
            disabled_deps = graph.get_disabled_dependencies(mod)
            if disabled_deps:
                disabled_names = [d.id for d in disabled_deps]
                return {
                    **result_base,
                    "status": "skipped_disabled_dependency",
                    "reason": f"disabled dependencies: {', '.join(disabled_names)}",
                    "verified": False,
                }

        # Check compatibility with engine and hermes
        from engine.updater import parse_version, get_hermes_version
        if mod.min_engine_version:
            if parse_version(ENGINE_VERSION) < parse_version(mod.min_engine_version):
                return {
                    **result_base,
                    "status": "skipped_engine_outdated",
                    "reason": f"engine version {ENGINE_VERSION} < required {mod.min_engine_version}",
                    "verified": False,
                }
        if mod.min_hermes_version:
            curr_hermes = get_hermes_version(self.repo)
            if parse_version(curr_hermes) < parse_version(mod.min_hermes_version):
                return {
                    **result_base,
                    "status": "skipped_hermes_outdated",
                    "reason": f"hermes version {curr_hermes} < required {mod.min_hermes_version}",
                    "verified": False,
                }

        if not mod.targets:
            return {**result_base, "status": "already_applied", "verified": True}

        # Фильтруем активные таргеты
        active_targets = [t for t in mod.targets if not t.disabled]
        if not active_targets:
            return {**result_base, "status": "disabled_upstream_absorbed", "reason": "all targets disabled", "verified": True}

        # 1. Проверяем, не наложен ли уже мод целиком
        all_new_blocks_present = True
        for t in active_targets:
            target_path = self.repo / t.file
            if not target_path.exists():
                return {**result_base, "status": "skipped", "reason": f"file not found: {t.file}", "verified": False}
            content = target_path.read_text(encoding="utf-8")
            if t.new_block not in content:
                all_new_blocks_present = False
                break

        if all_new_blocks_present:
            cached_entry = None
            if not force and (mod.has_tests or mod.verify_cmd):
                cached_entry = self.cache_mgr.get_cached_result(mod)

            if cached_entry:
                return {**result_base, "status": "already_applied", "verified": True, "cached": True}

            if mod.has_tests:
                t_runner = ModTestRunner(repo_root=self.repo, python_bin=self.python_bin, mods_dir=self.mods_dir, cache_mgr=self.cache_mgr)
                t_res = t_runner.run_test(mod, force=True, use_cache=False)
                if t_res.status not in ("passed", "skipped", "no_tests"):
                    return {**result_base, "status": "failed_verification", "verified": False, "cached": False, "error": t_res.error_message or t_res.stderr or t_res.stdout}
                self.cache_mgr.set_cached_result(mod, t_res)
            elif mod.verify_cmd:
                ok, err = self.run_verify(mod.verify_cmd, mod.mod_dir, mod=mod)
                if not ok:
                    return {**result_base, "status": "failed_verification", "verified": False, "cached": False, "error": err}
                self.cache_mgr.set_cached_result(mod, {"verified": True, "passed": 1, "method": "verify_cmd"})

            return {**result_base, "status": "already_applied", "verified": True, "cached": False}

        # 2. Проверяем наличие old_block для каждого таргета
        files_touched: Dict[Path, str] = {}
        for t in active_targets:
            target_path = self.repo / t.file
            if target_path not in files_touched:
                files_touched[target_path] = target_path.read_text(encoding="utf-8")

            simulated = files_touched[target_path]
            if t.new_block in simulated:
                continue

            cnt = simulated.count(t.old_block)
            if cnt == 0:
                return {
                    **result_base,
                    "status": "skipped_upstream_changed",
                    "reason": f"target block not found in {t.file} (upstream changed)",
                    "verified": False,
                }
            if cnt > 1:
                return {
                    **result_base,
                    "status": "skipped_ambiguous",
                    "reason": f"target block matched {cnt} times in {t.file}",
                    "verified": False,
                }
            files_touched[target_path] = simulated.replace(t.old_block, t.new_block, 1)

        if not apply_changes:
            return {**result_base, "status": "not_applied", "verified": False}

        # Pre-flight validation before modifying files on disk
        validator = ModValidator(repo_root=self.repo, mods_dir=self.mods_dir)
        val_res = validator.validate_mod(mod)
        if not val_res.valid:
            err_msg = val_res.errors[0].message if val_res.errors else "Pre-flight validation failed"
            return {
                **result_base,
                "status": "failed_preflight_validation",
                "verified": False,
                "error": f"Pre-flight validation failed: {err_msg}",
                "validation_errors": [e.to_dict() for e in val_res.errors],
            }

        # 3. Per-Mod Atomic Application with Crash-Consistent writes and Signal Trap
        backups: Dict[Path, str] = {}

        def rollback_this_mod():
            for p, original_text in backups.items():
                try:
                    atomic_write_text(p, original_text)
                except Exception:
                    pass

        try:
            with SignalTrap(rollback_this_mod):
                for target_path, new_content in files_touched.items():
                    backups[target_path] = target_path.read_text(encoding="utf-8")
                    atomic_write_text(target_path, new_content)

                # 4. Верификация после применения (только для этого мода)
                if mod.has_tests:
                    t_runner = ModTestRunner(repo_root=self.repo, python_bin=self.python_bin, mods_dir=self.mods_dir, cache_mgr=self.cache_mgr)
                    t_res = t_runner.run_test(mod, force=True, use_cache=False)
                    if t_res.status not in ("passed", "skipped", "no_tests"):
                        rollback_this_mod()
                        return {
                            **result_base,
                            "status": "failed_verification_rolled_back",
                            "verified": False,
                            "cached": False,
                            "error": t_res.error_message or t_res.stderr or t_res.stdout,
                        }
                    self.cache_mgr.set_cached_result(mod, t_res)
                elif mod.verify_cmd:
                    ok, err = self.run_verify(mod.verify_cmd, mod.mod_dir, mod=mod)
                    if not ok:
                        rollback_this_mod()
                        return {
                            **result_base,
                            "status": "failed_verification_rolled_back",
                            "verified": False,
                            "cached": False,
                            "error": err,
                        }
                    self.cache_mgr.set_cached_result(mod, {"verified": True, "passed": 1, "method": "verify_cmd"})

                return {**result_base, "status": "applied", "verified": True, "cached": False}

        except Exception as exc:
            rollback_this_mod()
            return {
                **result_base,
                "status": "failed_exception_rolled_back",
                "verified": False,
                "error": str(exc),
            }

    run_mod = apply_mod

    def apply(self, mods: Optional[List[Mod]] = None, graph: Optional[ModDependencyGraph] = None, force: bool = False) -> List[Dict[str, Any]]:
        """Применяет моды к коду ядра Hermes с проверкой mods.enabled."""
        if mods is None:
            mods = ModLoader.load_all(self.mods_dir)
        return self.run_all(mods, apply_changes=True, graph=graph, force=force)

    def run_all(self, mods: List[Mod], apply_changes: bool = True, graph: Optional[ModDependencyGraph] = None, force: bool = False) -> List[Dict[str, Any]]:
        results = []
        if graph is not None:
            ordered_mods = graph.topological_sort(mods)
        else:
            ordered_mods = sorted(mods, key=lambda m: (0 if (m.author or "").strip().lower() == "engine" else 1, m.id))

        if not apply_changes:
            for mod in ordered_mods:
                res = self.apply_mod(mod, apply_changes=False, graph=graph, force=force)
                results.append(res)
            return results

        # 0. Check master killswitch mods.enabled in config.yaml
        if not self.is_mods_enabled():
            unpatch_report = None
            custom_mods = [m for m in ordered_mods if not is_system_mod(m, self.mods_dir)]
            system_mods = [m for m in ordered_mods if is_system_mod(m, self.mods_dir)]
            if self.is_any_applied(custom_mods):
                unpatch_report = self.unpatch_all(custom_mods, graph=graph, include_system=False)
            for mod in custom_mods:
                results.append({
                    "id": mod.id,
                    "name": mod.name,
                    "version": mod.version,
                    "targets_count": len(mod.targets),
                    "config_count": len(mod.config),
                    "user_config_count": len(mod.config),
                    "monitors_count": len(mod.monitors),
                    "mcp_servers_count": len(mod.mcp_servers),
                    "dependencies": mod.dependencies,
                    "status": "blocked_killswitch",
                    "reason": "mods.enabled is set to false in config.yaml",
                    "verified": False,
                    "unpatch_report": unpatch_report,
                })
            for mod in system_mods:
                res = self.apply_mod(mod, apply_changes=apply_changes, graph=graph)
                results.append(res)
            return results

        # 1. Pre-flight collision & composite AST simulation in virtual RAM
        # Only run composite simulation if there are unapplied mods that need changes applied
        unapplied_mods = [
            m for m in ordered_mods
            if m.enabled and m.targets and not all(
                (self.repo / t.file).is_file() and t.new_block in (self.repo / t.file).read_text(encoding="utf-8")
                for t in m.targets if not t.disabled
            )
        ]
        if unapplied_mods:
            validator = ModValidator(repo_root=self.repo, mods_dir=self.mods_dir)
            sim_report = validator.simulate_composite_apply(ordered_mods)
            if not sim_report.valid:
                first_err = sim_report.errors[0].message if sim_report.errors else "Simulation conflict detected"
                for mod in ordered_mods:
                    res_base = {
                        "id": mod.id,
                        "name": mod.name,
                        "version": mod.version,
                        "targets_count": len(mod.targets),
                        "config_count": len(mod.config),
                        "user_config_count": len(mod.config),
                        "monitors_count": len(mod.monitors),
                        "mcp_servers_count": len(mod.mcp_servers),
                        "dependencies": mod.dependencies,
                    }
                    mod_errs = [e for e in sim_report.errors if e.mod_id == mod.id]
                    if mod_errs:
                        results.append({
                            **res_base,
                            "status": "failed_preflight_validation",
                            "verified": False,
                            "error": f"Pre-flight composite simulation failed: {mod_errs[0].message}",
                            "validation_errors": [e.to_dict() for e in mod_errs],
                        })
                    else:
                        results.append({
                            **res_base,
                            "status": "skipped_simulation_conflict",
                            "verified": False,
                            "reason": f"Blocked by composite simulation failure in active mods: {first_err}",
                        })
                return results

        # 1.5. Core Delta Snapshot before applying mods
        affected_files = set()
        mod_ids_to_apply = []
        for mod in ordered_mods:
            if not mod.enabled or not mod.targets:
                continue
            mod_ids_to_apply.append(mod.id)
            for t in mod.targets:
                if not t.disabled:
                    p = self.repo / t.file
                    if p.is_file():
                        affected_files.add(p)

        if affected_files and getattr(self, "snapshot_mgr", None):
            try:
                self.snapshot_mgr.create_snapshot(
                    files=list(affected_files),
                    description=f"Auto snapshot before applying: {', '.join(mod_ids_to_apply[:5])}",
                    trigger="pre_apply",
                    mod_ids=mod_ids_to_apply,
                    mods_dir=self.mods_dir,
                )
            except Exception as exc:
                sys.stderr.write(f"Warning: failed to create core snapshot before apply: {exc}\n")

        # Snapshot backup of pristine files before applying (legacy backup_mgr)
        files_to_backup = set()
        for mod in ordered_mods:
            if not mod.enabled or not mod.targets:
                continue
            for t in mod.targets:
                if not t.disabled:
                    p = self.repo / t.file
                    if p.is_file():
                        try:
                            c = p.read_text(encoding="utf-8")
                            if t.old_block in c and t.new_block not in c:
                                files_to_backup.add(p)
                        except Exception:
                            pass
        if files_to_backup:
            try:
                self.backup_mgr.create_backup(list(files_to_backup), self.repo)
            except Exception:
                pass

        # 2. Sequential application with Per-Mod Atomic Rollback:
        # Each mod applies independently. If any mod fails, it rolls back only its own
        # changes; other mods remain untouched.
        for mod in ordered_mods:
            res = self.apply_mod(mod, apply_changes=True, graph=graph, force=force)
            results.append(res)

        if apply_changes:
            try:
                update_commands_cache(ordered_mods, self.mods_dir)
                update_locales_cache(ordered_mods, self.mods_dir)
                update_mcp_cache(ordered_mods, self.mods_dir)
                register_mod_locales(ordered_mods, self.mods_dir)
            except Exception as exc:
                sys.stderr.write(f"Warning: failed to update caches: {exc}\n")

        return results


def handle_unpatch_cli(args, mods: List[Mod], runner: ModRunner, graph: Optional[ModDependencyGraph] = None) -> int:
    include_system = bool(getattr(args, "all_mods", False) or getattr(args, "include_system", False))
    target_mods = mods
    if getattr(args, "target", None) and not include_system:
        m = find_mod(args.target, mods)
        if not m:
            if getattr(args, "json", False):
                print(json.dumps({"error": f"Мод '{args.target}' не найден", "success": False, "status": "not_found"}, ensure_ascii=False))
            else:
                sys.stderr.write(f"Мод '{args.target}' не найден\n")
            return 1
        if is_system_mod(m, runner.mods_dir):
            sys.stderr.write(f"Мод '{args.target}' является системным компонентом движка. Для его удаления используйте --all или purge.\n")
            return 1
        target_mods = [m]

    report = runner.unpatch_all(target_mods, graph=graph, include_system=include_system)

    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("=" * 80)
    if not include_system:
        print("Все пользовательские моды (custom/) успешно сняты. Системные компоненты движка активны. (Для полного удаления системных модов используйте --all).")
    else:
        print("Все моды успешно сняты. Ядро Hermes возвращено к исходному состоянию.")
    print("=" * 80)
    print(f"Восстановлено файлов ядра: {report['restored_count']}")
    if report.get("restored_files"):
        for f in report["restored_files"]:
            print(f"  ✓ {f}")
    print("Кэш команд (.commands_cache.json) очищен.")
    print("Кэши скиллов и MCP сброшены.")
    return 0


def handle_purge_cli(args, mods: List[Mod], runner: ModRunner, graph: Optional[ModDependencyGraph] = None) -> int:
    confirmed = bool(getattr(args, "confirm", False) or getattr(args, "yes", False))
    if not confirmed:
        msg = "Это полностью снимет все моды, включая системные. Для подтверждения укажите --confirm"
        sys.stderr.write(f"{msg}\n")
        print(msg)
        return 1

    report = runner.unpatch_all(mods, graph=graph, include_system=True)

    # Очистка файлов кэша тестов и MCP
    for test_cache_p in [
        runner.mods_dir / ".test_cache.json",
        runner.mods_dir / "test_cache.json",
        runner.mods_dir / ".engine" / "test_cache.json",
        runner.mods_dir / ".mcp_cache.json",
    ]:
        if test_cache_p.exists():
            try:
                test_cache_p.unlink()
            except Exception:
                pass

    if getattr(args, "json", False):
        report["purged"] = True
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0

    print("=" * 80)
    print("Все моды (включая системные компоненты движка) полностью удалены. Ядро Hermes возвращено к чистому upstream.")
    print("=" * 80)
    print(f"Восстановлено файлов ядра: {report['restored_count']}")
    if report.get("restored_files"):
        for f in report["restored_files"]:
            print(f"  ✓ {f}")
    print("Кэш команд (.commands_cache.json) очищен.")
    print("Кэш тестов (test_cache.json) очищен.")
    print("Кэши скиллов и MCP сброшены.")
    return 0


def find_mod(target: str, mods: List[Mod]) -> Optional[Mod]:
    if not target:
        return None
    t = target.strip().lower()
    return next((m for m in mods if t in (m.name.lower(), m.id.lower(), m.path.stem.lower())), None)


def handle_config_cli(args, mods: List[Mod]) -> int:
    cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
    if getattr(args, "populate", False):
        target_mods = mods
        if args.target:
            mod = find_mod(args.target, mods)
            if not mod:
                if getattr(args, "json", False):
                    print(json.dumps({"error": f"Мод '{args.target}' не найден", "success": False, "status": "not_found"}, ensure_ascii=False))
                else:
                    sys.stderr.write(f"Мод '{args.target}' не найден.\n")
                return 1
            target_mods = [mod]
        res = cfg_mgr.populate_defaults(target_mods)
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            added_cfg_total = sum(len(keys) for keys in res["added_config"].values())
            added_sec_total = sum(len(keys) for keys in res["added_secrets"].values())
            print("✓ Автозаполнение настроек модов выполнено:")
            print(f"  • Добавлено параметров в config.yaml: {added_cfg_total}")
            for m_id, keys in res["added_config"].items():
                print(f"    - {m_id}: {', '.join(keys)}")
            print(f"  • Добавлено секретов в secrets.yaml: {added_sec_total}")
            for m_id, keys in res["added_secrets"].items():
                print(f"    - {m_id}: {', '.join(keys)}")
            if not added_cfg_total and not added_sec_total:
                print("  (Все параметры по умолчанию уже присутствуют в конфигурации)")
        return 0

    if not args.target:
        sys.stderr.write("Укажите имя мода: runner.py config <mod_name>\n")
        return 1

    mod = find_mod(args.target, mods)
    if not mod:
        if getattr(args, "json", False):
            print(json.dumps({"error": f"Мод '{args.target}' не найден", "success": False, "status": "not_found"}, ensure_ascii=False))
        else:
            sys.stderr.write(f"Мод '{args.target}' не найден.\n")
        return 1

    if not mod.config:
        print(f"Мод '{mod.id}' не объявляет секцию config.")
        return 0

    if args.set_option:
        key, sep, val = args.set_option.partition("=")
        if not sep:
            sys.stderr.write("Формат: --set <key>=<value>\n")
            return 1
        ok, msg = cfg_mgr.set_option(mod, key.strip(), val.strip())
        print(msg)
        return 0 if ok else 1

    if args.set_secret:
        key, sep, val = args.set_secret.partition("=")
        if not sep:
            sys.stderr.write("Формат: --set-secret <key>=<secret_value>\n")
            return 1
        ok, msg = cfg_mgr.set_secret(mod, key.strip(), val.strip())
        print(msg)
        return 0 if ok else 1

    masked = cfg_mgr.get_masked_config(mod)
    if args.json:
        out = {
            "mod_id": mod.id,
            "fields": {k: {"type": f.type, "title": f.title, "description": f.description, "sensitive": f.sensitive, "default": f.default, "options": f.options, "value": masked.get(k)} for k, f in mod.config.items()}
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print(f"Конфигурация для мода '{mod.id}' ({mod.name}):\n")
    print(f"{'ПАРАМЕТР':<20} {'ТИП':<10} {'СЕКРЕТ':<8} {'ЗНАЧЕНИЕ':<25} {'ОПИСАНИЕ'}")
    print("-" * 80)
    for k, f in mod.config.items():
        val = str(masked.get(k) if masked.get(k) is not None else "(не задано)")
        is_sec = "YES" if f.sensitive else "NO"
        print(f"{k:<20} {f.type:<10} {is_sec:<8} {val:<25} {f.description}")
    return 0


def handle_monitors_cli(args, mods: List[Mod]) -> int:
    mon_mgr = ModMonitorManager()
    cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
    sub = args.subaction or "list"

    if sub == "list":
        running = mon_mgr.get_running_monitors()
        print(f"{'МОД':<22} {'МОНИТОР':<18} {'СТАТУС':<10} {'PID':<8} {'ДОСТАВКА':<10} {'ОПИСАНИЕ'}")
        print("-" * 85)
        for mod in mods:
            for mon in mod.monitors:
                key = mon_mgr.get_monitor_key(mod.id, mon.id)
                info = running.get(key, {})
                status = info.get("status", "STOPPED")
                pid_str = str(info.get("pid", "-"))
                print(f"{mod.id:<22} {mon.id:<18} {status:<10} {pid_str:<8} {mon.delivery:<10} {mon.description}")
        return 0

    if sub in ("start", "stop"):
        target_mod_id = args.target
        target_mon_id = args.monitor_id
        if not target_mod_id:
            sys.stderr.write("Укажите mod-id: runner.py monitors start <mod-id> [monitor-id]\n")
            return 1

        mod = find_mod(target_mod_id, mods)
        if not mod:
            sys.stderr.write(f"Мод '{target_mod_id}' не найден.\n")
            return 1

        monitors_to_act = [m for m in mod.monitors if target_mon_id is None or m.id == target_mon_id]
        if not monitors_to_act:
            sys.stderr.write(f"Монитор '{target_mon_id}' не найден в моде '{mod.id}'.\n")
            return 1

        resolved_cfg = cfg_mgr.get_resolved_config(mod)
        for m in monitors_to_act:
            if sub == "start":
                ok, msg = mon_mgr.start_monitor(m, resolved_cfg)
            else:
                ok, msg = mon_mgr.stop_monitor(mod.id, m.id)
            print(msg)
        return 0

    if sub == "status":
        running = mon_mgr.get_running_monitors()
        if args.json:
            print(json.dumps(running, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(running, indent=2, ensure_ascii=False))
        return 0

    return 0


def handle_validate_cli(args, mods: List[Mod]) -> int:
    sfl = getattr(args, "strict_layout", None)
    validator = ModValidator(repo_root=HERMES_ROOT, mods_dir=Path(args.mods_dir), strict_folder_layout=sfl)
    if args.target:
        mod = find_mod(args.target, mods)
        if not mod:
            if getattr(args, "json", False):
                print(json.dumps({
                    "summary": {"total_mods_scanned": 1, "passed": 0, "failed": 1},
                    "results": [{"id": args.target, "passed": False, "issues": [{"severity": "error", "message": f"Мод '{args.target}' не найден"}]}],
                    "valid": False
                }, indent=2, ensure_ascii=False))
            else:
                sys.stderr.write(f"Мод '{args.target}' не найден.\n")
            return 1
        results = [validator.validate_mod(mod)]
    else:
        results = validator.validate_all(mods)

    if args.json:
        total = len(results)
        passed = sum(1 for r in results if r.valid)
        failed = total - passed
        total_errors = sum(len(r.errors) for r in results)
        total_warnings = sum(len(r.warnings) for r in results)
        out = {
            "valid": failed == 0,
            "total_mods": total,
            "passed_mods": passed,
            "failed_mods": failed,
            "total_errors": total_errors,
            "total_warnings": total_warnings,
            "results": [r.to_dict() for r in results],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(validator.format_report(results))

    has_errors = any(not r.valid for r in results)
    return 1 if has_errors else 0


def handle_mcp_cli(args, mods: List[Mod]) -> int:
    cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
    sub = args.subaction or (args.target if args.target in ("list", "sync") else "list")

    if sub == "sync":
        try:
            _mod_mcp_cache_clear()
            cache_file = update_mcp_cache(mods, runner.mods_dir)
            print(f"Синхронизация MCP серверов с кэшем модов выполнена ({cache_file.name}).")
            return 0
        except Exception as e:
            sys.stderr.write(f"Ошибка синхронизации MCP серверов: {e}\n")
            return 1

    servers_info = []
    for m in mods:
        if not m.mcp_servers:
            continue
        resolved = resolve_mod_mcp_servers(m, cfg_mgr)
        for srv_name, srv_cfg in resolved.items():
            servers_info.append({
                "mod_id": m.id,
                "mod_name": m.name,
                "server_name": srv_name,
                "mod_enabled": m.enabled,
                "command": srv_cfg.get("command", ""),
                "args": srv_cfg.get("args", []),
                "env": srv_cfg.get("env", {}),
            })

    if args.json:
        print(json.dumps(servers_info, indent=2, ensure_ascii=False))
        return 0

    if not servers_info:
        print("Ни один мод не декларирует MCP-серверы.")
        return 0

    print(f"Декларированные MCP-серверы в модах ({len(servers_info)}):\n")
    print(f"{'МОД':<26} {'СЕРВЕР':<20} {'СТАТУС':<10} {'КОМАНДА':<25} {'АРГУМЕНТЫ'}")
    print("-" * 105)
    for s in servers_info:
        status = "ENABLED" if s["mod_enabled"] else "DISABLED"
        cmd = str(s["command"])
        args_str = " ".join(str(a) for a in s["args"])
        print(f"{s['mod_id']:<26} {s['server_name']:<20} {status:<10} {cmd:<25} {args_str}")
    return 0


def handle_update_cli(args, mods: List[Mod]) -> int:
    updater = ModUpdater(
        mods_dir=Path(args.mods_dir),
        repo_root=HERMES_ROOT,
    )
    remote = getattr(args, "remote", "origin") or "origin"
    branch = getattr(args, "branch", "main") or "main"
    with_hermes = bool(getattr(args, "with_hermes", False) or getattr(args, "all_mods", False))

    res = updater.update(
        check=bool(args.check),
        dry_run=bool(args.dry_run),
        no_apply=bool(args.no_apply),
        force=bool(args.force),
        remote=remote,
        branch=branch,
        with_hermes=with_hermes,
    )

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return res.get("exit_code", 0)

    if with_hermes:
        if args.check:
            print("🔍 Проверка обновлений трёхфазного конвейера (--all):")
            eng_status = res.get("engine", {}).get("status", "unknown")
            print(f"  • Движок модов: {eng_status}")
            hermes_status = res.get("hermes", {}).get("status", "unknown")
            print(f"  • Ядро Hermes Agent: {hermes_status}")
            return res.get("exit_code", 0)

        if args.dry_run:
            print("🔍 Симуляция трёхфазного обновления (dry-run):")
            print("  [Фаза 1/3] Обновление движка модов: симуляция pull")
            print("  [Фаза 2/3] Обновление ядра Hermes: симуляция update")
            print("  [Фаза 3/3] Накатка модов: симуляция apply")
            return res.get("exit_code", 0)

        if res.get("status") == "completed":
            print(f"✓ Трёхфазное обновление успешно выполнено:")
            print(f"  [Фаза 1/3] ✓ Движок модов актуализирован (v{ENGINE_VERSION})")
            print(f"  [Фаза 2/3] ✓ Ядро Hermes Agent успешно обновлено")
            print(f"  [Фаза 3/3] ✓ Все моды наложены и верифицированы (runner.py apply --watchdog)")
            print("✓ Кэши команд, локализаций и MCP успешно перекомпилированы (Zero Downtime)")
            return 0
        else:
            err = res.get("error") or res.get("apply", {}).get("error") or "Ошибка трёхфазного обновления"
            print(f"❌ Ошибка трёхфазного обновления: {err}")
            return res.get("exit_code", 1)

    if args.check:
        status = res.get("status")
        if status == "up_to_date":
            print(f"✓ Экосистема модов актуальна (коммит: {res.get('local_commit', '-')}). Обновлений нет.")
        elif status == "updates_available":
            print(f"⚡ Доступны обновления: {res.get('behind')} коммит(ов) из {remote}/{branch}")
            print(f"  Локальный: {res.get('local_commit')} -> Удаленный: {res.get('remote_commit')}")
            print("  Для обновления выполните: python3 runner.py update")
        else:
            print(f"⚠️  Проверка обновлений: {res.get('message', res.get('error'))}")
        return res.get("exit_code", 0)

    if args.dry_run:
        print("🔍 Симуляция обновления (dry-run): файлы не изменяются")
        apply_sim = res.get("apply_simulation", {})
        for mr in apply_sim.get("mods_results", []):
            st = mr.get("status")
            print(f"  • {mr.get('id', mr.get('name'))}: {st}")
        return res.get("exit_code", 0)

    if args.no_apply:
        print("✓ Репозиторий модов успешно обновлен без применения патчей к Hermes Agent.")
        return res.get("exit_code", 0)

    if res.get("status") == "completed":
        print(f"✓ Обновление успешно выполнено (v{ENGINE_VERSION})")
        print("✓ Кэши команд, локализаций и MCP успешно перекомпилированы (Zero Downtime)")
        apply_info = res.get("apply", {})
        for mr in apply_info.get("mods_results", []):
            st = mr.get("status")
            print(f"  • {mr.get('id', mr.get('name'))}: {st} (verified: {mr.get('verified')})")
        return 0
    else:
        err = res.get("error") or res.get("apply", {}).get("error") or "Неизвестная ошибка при обновлении"
        print(f"❌ Ошибка обновления: {err}")
        return 1


def _update_manifest_enabled(path: Path, enabled: bool) -> None:
    """Non-destructively updates the 'enabled:' line in YAML preserving all comments and formatting."""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    val_str = "true" if enabled else "false"
    if re.search(r"(?m)^(\s*enabled:\s*)\w+", text):
        new_text = re.sub(r"(?m)^(\s*enabled:\s*)\w+", rf"\g<1>{val_str}", text)
    else:
        new_text = f"enabled: {val_str}\n" + text
    atomic_write_text(path, new_text)


def handle_diff_cli(args, mods: List[Mod]) -> int:
    import difflib
    use_color = not getattr(args, "no_color", False) and (
        getattr(args, "color", False) or sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"
    ) and not os.environ.get("NO_COLOR")

    RED = "\033[31m" if use_color else ""
    GREEN = "\033[32m" if use_color else ""
    CYAN = "\033[36m" if use_color else ""
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    if args.target:
        found = find_mod(args.target, mods)
        if not found:
            if getattr(args, "json", False):
                print(json.dumps({"error": f"Мод '{args.target}' не найден", "diffs": []}, indent=2, ensure_ascii=False))
            else:
                sys.stderr.write(f"Мод '{args.target}' не найден\n")
            return 1
        target_mods = [found]
    else:
        target_mods = [m for m in mods if m.enabled and m.targets]

    diff_entries = []

    for mod in target_mods:
        active_targets = [t for t in mod.targets if not t.disabled]
        if not active_targets:
            continue

        for idx, target in enumerate(active_targets):
            clean_file = target.file.strip().lstrip("/\\")
            target_path = HERMES_ROOT / clean_file

            if not target_path.exists():
                diff_entries.append({
                    "mod_id": mod.id,
                    "file": clean_file,
                    "status": "file_not_found",
                    "diff": "",
                })
                continue

            content = target_path.read_text(encoding="utf-8")
            if target.new_block in content:
                # Already applied: compute what was added relative to old_block
                orig_sim = content.replace(target.new_block, target.old_block, 1)
                diff_lines = list(difflib.unified_diff(
                    orig_sim.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{clean_file} (before mod)",
                    tofile=f"b/{clean_file} (applied: {mod.id})",
                    n=3,
                ))
                diff_entries.append({
                    "mod_id": mod.id,
                    "file": clean_file,
                    "status": "already_applied",
                    "diff": "".join(diff_lines),
                })
            elif target.old_block in content:
                # Pending application
                new_sim = content.replace(target.old_block, target.new_block, 1)
                diff_lines = list(difflib.unified_diff(
                    content.splitlines(keepends=True),
                    new_sim.splitlines(keepends=True),
                    fromfile=f"a/{clean_file} (current)",
                    tofile=f"b/{clean_file} (planned: {mod.id})",
                    n=3,
                ))
                diff_entries.append({
                    "mod_id": mod.id,
                    "file": clean_file,
                    "status": "pending",
                    "diff": "".join(diff_lines),
                })
            else:
                diff_entries.append({
                    "mod_id": mod.id,
                    "file": clean_file,
                    "status": "skipped_upstream_changed",
                    "diff": "",
                })

    if getattr(args, "json", False):
        print(json.dumps(diff_entries, indent=2, ensure_ascii=False))
        return 0

    if not diff_entries:
        print("Нет активных таргетов или изменений для отображения.")
        return 0

    for entry in diff_entries:
        m_id = entry["mod_id"]
        f_name = entry["file"]
        status = entry["status"]
        diff_str = entry["diff"]

        if status == "already_applied":
            badge = f"{GREEN}[ALREADY APPLIED]{RESET}"
        elif status == "pending":
            badge = f"{CYAN}[PENDING]{RESET}"
        elif status == "file_not_found":
            badge = f"{RED}[FILE NOT FOUND]{RESET}"
        else:
            badge = f"{RED}[UPSTREAM CHANGED]{RESET}"

        print(f"{BOLD}=== Mod:{RESET} {m_id} | {BOLD}File:{RESET} {f_name} {badge}")
        if diff_str:
            for line in diff_str.splitlines():
                if line.startswith("+++") or line.startswith("---"):
                    print(f"{BOLD}{line}{RESET}")
                elif line.startswith("@@"):
                    print(f"{CYAN}{line}{RESET}")
                elif line.startswith("+"):
                    print(f"{GREEN}{line}{RESET}")
                elif line.startswith("-"):
                    print(f"{RED}{line}{RESET}")
                else:
                    print(line)
            print()
        elif status == "file_not_found":
            print(f"  {RED}Целевой файл {f_name} отсутствует в {HERMES_ROOT}{RESET}\n")
        elif status == "skipped_upstream_changed":
            print(f"  {RED}Блок old_block не найден в {f_name} (исходный код ядра изменился){RESET}\n")

    return 0


def handle_doctor_cli(args, mods: List[Mod]) -> int:
    import shutil
    import stat

    use_color = not getattr(args, "no_color", False) and (
        getattr(args, "color", False) or sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"
    ) and not os.environ.get("NO_COLOR")

    GREEN = "\033[32m" if use_color else ""
    RED = "\033[31m" if use_color else ""
    YELLOW = "\033[33m" if use_color else ""
    BOLD = "\033[1m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    checks = []

    # 1. Python Environment
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 10)
    venv_exists = VENV_PYTHON.exists()
    checks.append({
        "name": "Python Environment",
        "status": "ok" if (py_ok and venv_exists) else ("warn" if py_ok else "fail"),
        "message": f"Python {py_ver} (interpreter: {sys.executable}, venv: {VENV_PYTHON if venv_exists else 'not found'})",
    })

    # 2. Git
    git_path = shutil.which("git")
    if git_path:
        is_repo = False
        branch = "unknown"
        try:
            is_repo = False
            branch = "main"
            if (MODS_DIR / ".git").exists():
                res = subprocess.run(["git", "-C", str(MODS_DIR), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and Path(res.stdout.strip()).resolve() == MODS_DIR.resolve():
                    is_repo = True
                    b_res = subprocess.run(["git", "-C", str(MODS_DIR), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=5)
                    branch = b_res.stdout.strip()
        except Exception:
            pass
        repo_info = f"repo: {MODS_DIR}, branch: {branch}" if is_repo else f"{MODS_DIR} (standalone deployment)"
        checks.append({
            "name": "Git CLI",
            "status": "ok",
            "message": f"{git_path} ({repo_info})",
        })
    else:
        checks.append({
            "name": "Git CLI",
            "status": "warn",
            "message": "git binary not found on PATH",
        })

    # 3. Hermes Core Presence & Writable
    from engine.updater import get_hermes_version
    hermes_exists = HERMES_ROOT.is_dir()
    hermes_writable = os.access(HERMES_ROOT, os.W_OK) if hermes_exists else False
    hermes_ver = get_hermes_version(HERMES_ROOT) if hermes_exists else "unknown"
    if hermes_exists and hermes_writable:
        checks.append({
            "name": "Hermes Core",
            "status": "ok",
            "message": f"{HERMES_ROOT} (v{hermes_ver}, writable)",
        })
    elif hermes_exists:
        checks.append({
            "name": "Hermes Core",
            "status": "fail",
            "message": f"{HERMES_ROOT} (v{hermes_ver}, read-only / not writable)",
        })
    else:
        checks.append({
            "name": "Hermes Core",
            "status": "fail",
            "message": f"{HERMES_ROOT} does not exist",
        })

    # 4. Secrets Storage & 0600 Permissions
    if SECRETS_FILE.exists():
        mode = stat.S_IMODE(SECRETS_FILE.stat().st_mode)
        if mode == 0o600:
            checks.append({
                "name": "Secrets Storage",
                "status": "ok",
                "message": f"{SECRETS_FILE} (permissions: 0600)",
            })
        else:
            checks.append({
                "name": "Secrets Storage",
                "status": "fail",
                "message": f"{SECRETS_FILE} has permissions {oct(mode)}, expected 0600! Fix with: chmod 0600 {SECRETS_FILE}",
            })
    else:
        checks.append({
            "name": "Secrets Storage",
            "status": "ok",
            "message": f"{SECRETS_FILE} (will be created with 0600 permissions on first use)",
        })

    # 5. Engine Integrity (.engine/)
    required_engine_files = [
        "__init__.py", "models.py", "version.py", "loader.py",
        "validator.py", "config_manager.py", "monitor_manager.py", "updater.py", "file_ops.py",
        "exporter.py", "circuit_breaker.py", "inspector.py", "pusher.py", "cron_manager.py"
    ]
    engine_missing = [f for f in required_engine_files if not (_engine_dir / f).exists()]
    engine_ast_errs = []
    for f in required_engine_files:
        fp = _engine_dir / f
        if fp.exists():
            try:
                import ast
                ast.parse(fp.read_text(encoding="utf-8"), filename=f)
            except SyntaxError as e:
                engine_ast_errs.append(f"{f}: {e.msg}")

    if not engine_missing and not engine_ast_errs:
        checks.append({
            "name": "Engine Integrity",
            "status": "ok",
            "message": f"{_engine_dir} ({len(required_engine_files)} modules valid, v{ENGINE_VERSION})",
        })
    else:
        err_msg = ""
        if engine_missing:
            err_msg += f"missing: {', '.join(engine_missing)}; "
        if engine_ast_errs:
            err_msg += f"syntax errors: {', '.join(engine_ast_errs)}"
        checks.append({
            "name": "Engine Integrity",
            "status": "fail",
            "message": err_msg.strip(),
        })

    # 6. Mod Manifests & Dependencies DAG
    graph = ModDependencyGraph(mods)
    cycle_error = None
    try:
        graph.detect_cycles()
    except Exception as exc:
        cycle_error = str(exc)

    if cycle_error:
        checks.append({
            "name": "Mod Manifests & Dependencies",
            "status": "fail",
            "message": f"Dependency cycle detected: {cycle_error}",
        })
    else:
        checks.append({
            "name": "Mod Manifests & Dependencies",
            "status": "ok",
            "message": f"{len(mods)} mods loaded, DAG clean (0 cycles)",
        })

    # 7. State & Commands Cache
    state_file = Path(args.mods_dir) / ".state.json"
    cache_file = Path(args.mods_dir) / ".commands_cache.json"
    cache_ok = cache_file.exists()
    checks.append({
        "name": "Commands Cache & State",
        "status": "ok" if cache_ok else "warn",
        "message": f"cache: {'present' if cache_ok else 'missing'}, state: {'present' if state_file.exists() else 'none'}",
    })

    # 8. Circuit Breaker
    try:
        from engine.circuit_breaker import get_circuit_breaker
        cb = get_circuit_breaker(args.mods_dir)
        statuses = cb.get_all_statuses()
        tripped_mods = [mid for mid, st in statuses.items() if st.get("state") == "OPEN"]
        if tripped_mods:
            checks.append({
                "name": "Circuit Breaker",
                "status": "warn",
                "message": f"tripped for {len(tripped_mods)} mod(s): {', '.join(tripped_mods)} (isolated)",
            })
        else:
            checks.append({
                "name": "Circuit Breaker",
                "status": "ok",
                "message": f"active (threshold: {cb.failure_threshold} err/{int(cb.window_seconds)}s, 0 tripped)",
            })
    except Exception as exc:
        checks.append({
            "name": "Circuit Breaker",
            "status": "ok",
            "message": "active",
        })

    if getattr(args, "json", False):
        print(json.dumps(checks, indent=2, ensure_ascii=False))
        return 0 if all(c["status"] in ("ok", "warn") for c in checks) else 1

    print(f"\n{BOLD}Hermes Mod Engine: Doctor Environment Diagnostics (v{ENGINE_VERSION}){RESET}")
    print("=" * 80)
    has_fail = False
    for c in checks:
        if c["status"] == "ok":
            badge = f"{GREEN}✓ [OK]{RESET}"
        elif c["status"] == "warn":
            badge = f"{YELLOW}⚠ [WARN]{RESET}"
        else:
            badge = f"{RED}✗ [FAIL]{RESET}"
            has_fail = True
        print(f"  {badge:<16} {BOLD}{c['name']:<30}{RESET} {c['message']}")

    print("=" * 80)
    if not has_fail:
        print(f"{GREEN}✓ All environment checks passed successfully.{RESET}\n")
        return 0
    else:
        print(f"{RED}✗ Some environment checks failed. Please fix the reported issues above.{RESET}\n")
        return 1


def handle_test_cli(args, mods: List[Mod], runner: ModRunner) -> int:
    use_color = not getattr(args, "no_color", False) and (
        getattr(args, "color", False) or sys.stdout.isatty() or os.environ.get("FORCE_COLOR") == "1"
    ) and not os.environ.get("NO_COLOR")

    RED = "\033[31m" if use_color else ""
    GREEN = "\033[32m" if use_color else ""
    YELLOW = "\033[33m" if use_color else ""
    BOLD = "\033[1m" if use_color else ""
    DIM = "\033[2m" if use_color else ""
    RESET = "\033[0m" if use_color else ""

    force_flag = bool(getattr(args, "force", False) or getattr(args, "no_cache", False))
    test_runner = ModTestRunner(repo_root=runner.repo, python_bin=runner.python_bin, mods_dir=runner.mods_dir, cache_mgr=runner.cache_mgr)

    # 1. Single mod test
    if args.target:
        target_mod = find_mod(args.target, mods)
        if not target_mod:
            if getattr(args, "json", False):
                print(json.dumps({"error": f"Мод '{args.target}' не найден", "success": False, "status": "not_found"}, indent=2, ensure_ascii=False))
            else:
                sys.stderr.write(f"Мод '{args.target}' не найден\n")
            return 1

        if not args.json:
            layout_desc = "Folder mod" if target_mod.is_folder else "Single-file mod"
            if target_mod.test_files:
                test_src = f"tests/ ({len(target_mod.test_files)} file{'s' if len(target_mod.test_files) > 1 else ''})"
            elif target_mod.inline_tests:
                test_src = f"inline tests ({len(target_mod.inline_tests)} case{'s' if len(target_mod.inline_tests) > 1 else ''})"
            elif target_mod.verify_cmd:
                test_src = "verify_cmd fallback"
            else:
                test_src = "none"

            cache_note = " [FORCE/NO-CACHE]" if force_flag else ""
            print("=" * 80)
            print(f" Testing mod: {BOLD}{target_mod.name}{RESET} [{target_mod.id}] (v{target_mod.version}){cache_note}")
            print(f" Layout: {layout_desc} | Test source: {test_src}")
            print("=" * 80)

        res = test_runner.run_test(
            target_mod,
            verbose=args.verbose,
            fail_fast=args.fail_fast,
            test_filter=args.test_filter,
            force=force_flag,
            use_cache=not force_flag,
        )

        if args.json:
            print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
            return 0 if res.status in ("passed", "no_tests", "skipped") else 1

        if res.status == "passed":
            cached_badge = " (cached)" if res.cached else ""
            print(f"\n{GREEN}✓ PASSED:{RESET}{cached_badge} {res.passed} passed in {res.duration:.2f}s (method: {res.method})")
            if res.test_files:
                for tf in res.test_files:
                    print(f"  • {tf}")
            if args.verbose and res.stdout:
                print("-" * 80)
                print(res.stdout)
            print("=" * 80)
            return 0
        elif res.status == "no_tests":
            print(f"\n{YELLOW}- NO TESTS:{RESET} У мода нет нативных тестов (tests/ подпапка или inline 'tests:' в YAML)")
            print("=" * 80)
            return 0
        elif res.status == "skipped":
            print(f"\n{YELLOW}↷ SKIPPED:{RESET} {res.error_message}")
            print("=" * 80)
            return 0
        else:
            print(f"\n{RED}✗ FAILED:{RESET} {res.failed} failed, {res.passed} passed, {res.errors} errors in {res.duration:.2f}s (method: {res.method})")
            if res.error_message:
                print("-" * 80)
                print(f"{RED}{res.error_message}{RESET}")
            print("=" * 80)
            return 1

    # 2. All mods test suite
    if not args.json:
        cache_note = " [FORCE/NO-CACHE]" if force_flag else ""
        print("=" * 80)
        print(f" {BOLD}Hermes Mods Test Suite{RESET} ({len(mods)} mods registered){cache_note}")
        print("=" * 80)

    summary = test_runner.run_all(
        mods,
        verbose=args.verbose,
        fail_fast=args.fail_fast,
        test_filter=args.test_filter,
        include_disabled=args.all_mods,
        force=force_flag,
        use_cache=not force_flag,
    )

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False))
        return 0 if summary.all_passed else 1

    for r in summary.results:
        if r.status == "passed":
            if r.cached:
                src_info = f"({r.passed} passed [cached])"
            else:
                src_info = f"({r.passed} passed in {r.duration:.2f}s)"
            print(f" {GREEN}[PASS]{RESET} {r.mod_id:<36} {src_info:<26} [{r.method}]")
        elif r.status in ("failed", "error"):
            fail_info = f"({r.failed} failed, {r.passed} passed in {r.duration:.2f}s)"
            print(f" {RED}[FAIL]{RESET} {r.mod_id:<36} {fail_info:<26} [{r.method}]")
            if r.error_message and (args.verbose or args.fail_fast):
                for line in r.error_message.strip().splitlines()[:15]:
                    print(f"        {line}")
        elif r.status == "skipped":
            print(f" {YELLOW}[SKIP]{RESET} {r.mod_id:<36} {'(skipped: disabled)':<26}")
        elif r.status == "no_tests":
            print(f" {DIM}[ - ]{RESET} {r.mod_id:<36} {'(no tests defined)':<26}")

    print("=" * 80)
    cached_info = f" ({summary.cached_mods} cached)" if summary.cached_mods else ""
    status_line = (
        f"RESULTS: {summary.tested_mods} tested{cached_info} | "
        f"{GREEN}{summary.total_tests_passed} passed{RESET} | "
        f"{RED if summary.total_tests_failed else GREEN}{summary.total_tests_failed} failed{RESET} | "
        f"{summary.no_tests_mods} without tests | "
        f"{summary.total_duration:.2f}s total"
    )
    print(f" {status_line}")
    if summary.all_passed:
        print(f" {GREEN}✓ ALL MOD TESTS PASSED{RESET}")
        print("=" * 80)
        return 0
    else:
        print(f" {RED}✗ SOME MOD TESTS FAILED{RESET}")
        print("=" * 80)
        return 1


IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def resolve_language(lang_arg: Optional[str] = None, config_path: Path = CONFIG_FILE) -> str:
    """Resolve language code with fallback: CLI arg -> config.yaml -> HERMES_LANGUAGE -> 'ru'.
    Supports any ISO language code (e.g. en, ru, es, de, zh, etc.).
    """
    if lang_arg:
        cleaned = lang_arg.strip().lower()
        if cleaned and cleaned != "auto":
            return cleaned

    # Check config.yaml
    cfg_p = Path(config_path) if config_path else CONFIG_FILE
    if cfg_p.exists():
        try:
            with open(cfg_p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if isinstance(data, dict):
                    display_sec = data.get("display")
                    if isinstance(display_sec, dict) and display_sec.get("language"):
                        cand = str(display_sec["language"]).strip().lower()
                        if cand and cand != "auto":
                            return cand
                    if data.get("language"):
                        cand = str(data["language"]).strip().lower()
                        if cand and cand != "auto":
                            return cand
        except Exception:
            pass

    # Check environment variable
    env_lang = os.environ.get("HERMES_LANGUAGE")
    if env_lang and env_lang.strip():
        cand = env_lang.strip().lower()
        if cand != "auto":
            return cand

    return "ru"


def handle_commands_cli(args, mods: List[Mod]) -> int:
    lang = resolve_language(getattr(args, "lang", "auto"), CONFIG_FILE)
    active_cmds: list[tuple[Mod, ModCommand]] = []
    for m in mods:
        if m.enabled:
            for c in m.commands:
                active_cmds.append((m, c))

    if args.json:
        result = []
        for m, c in active_cmds:
            d = c.to_dict()
            d["mod_id"] = m.id
            d["mod_name"] = m.get_name(lang)
            d["description"] = c.get_description(lang)
            d["category"] = c.category or "Custom Mods"
            d["busy_policy"] = c.busy_policy or "reject"
            result.append(d)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    col_cmd = engine_t("cli.commands.col.command", lang=lang, default="КОМАНДА" if lang == "ru" else "COMMAND")
    col_mod = engine_t("cli.commands.col.mod", lang=lang, default="МОД" if lang == "ru" else "MOD")
    col_cat = engine_t("cli.commands.col.category", lang=lang, default="КАТЕГОРИЯ" if lang == "ru" else "CATEGORY")
    col_mode = engine_t("cli.commands.col.mode", lang=lang, default="РЕЖИМ" if lang == "ru" else "MODE")
    col_args = engine_t("cli.commands.col.args", lang=lang, default="АРГУМЕНТЫ" if lang == "ru" else "ARGUMENTS")
    col_desc = engine_t("cli.commands.col.description", lang=lang, default="ОПИСАНИЕ" if lang == "ru" else "DESCRIPTION")

    hdr_count = engine_t("cli.commands.registered", lang=lang, count=len(active_cmds), default=f"Зарегистрировано слэш-команд: {len(active_cmds)}\n" if lang == "ru" else f"Registered slash commands: {len(active_cmds)}\n")
    print(hdr_count)
    print(f"{col_cmd:<20} {col_mod:<22} {col_cat:<16} {col_mode:<10} {col_args:<18} {col_desc}")
    print("-" * 110)
    for m, c in active_cmds:
        cmd_str = f"/{c.name}"
        desc = c.get_description(lang)
        cat = c.category or "Custom Mods"
        mode = c.busy_policy or "reject"
        args_hint = c.args_hint or ""
        print(f"{cmd_str:<20} {m.id:<22} {cat:<16} {mode:<10} {args_hint:<18} {desc}")
    return 0


def handle_info_cli(args, mods: List[Mod], mods_path: Path) -> int:
    lang = resolve_language(getattr(args, "lang", "auto"), CONFIG_FILE)
    cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
    mon_mgr = ModMonitorManager()
    state_mgr = StateManager(mods_path / ".state.json")
    overrides = state_mgr.load().get("overrides", {})

    target_id = args.target

    # Case 1: Overview catalog (no target_id)
    if not target_id:
        custom_mods = [m for m in mods if not is_system_mod(m, mods_path)]
        if getattr(args, "json", False) or getattr(args, "format", "text") == "json":
            result = []
            for m in custom_mods:
                is_ovr = m.id in overrides and "enabled" in overrides[m.id]
                result.append({
                    "id": m.id,
                    "name": m.get_name(lang),
                    "name_en": m.name,
                    "version": m.version,
                    "author": m.author or "User",
                    "enabled": m.enabled,
                    "status": ("ENABLED*" if is_ovr else "ENABLED") if m.enabled else ("DISABLED*" if is_ovr else "DISABLED"),
                    "type": "folder" if m.is_folder else "single",
                    "path": str(m.path),
                    "description": m.get_description(lang),
                    "description_en": m.description,
                    "targets_count": len(m.targets),
                    "commands_count": len(m.commands),
                    "config_count": len(m.config),
                    "monitors_count": len(m.monitors),
                    "skills_count": len(m.list_skills()) if m.skills_dir else 0,
                    "tests_count": m.tests_count,
                })
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        # Human-readable format strictly adhering to:
        # • Название мода — Описание на выбранном языке
        # Никаких (id) в скобках выводить не нужно! Чистый человекопонятный список.
        # Если мод отключен/не применен — показывать маркер [ОТКЛЮЧЕН] (или [DISABLED] для en/других).
        # Если работает штатно — статус не выводить.
        for m in custom_mods:
            title = m.get_name(lang)
            desc = m.get_description(lang)
            status_marker = ""
            if not m.enabled:
                status_tag = engine_t("cli.status.disabled", lang=lang, default="ОТКЛЮЧЕН" if lang == "ru" else "DISABLED")
                status_marker = f" [{status_tag}]"
            if desc:
                print(f"• {title}{status_marker} — {desc}")
            else:
                print(f"• {title}{status_marker}")
        return 0

    # Case 2: Detailed mod card for target_id
    mod = find_mod(target_id, mods)
    if not mod:
        err_msg = engine_t("cli.info.not_found", lang=lang, target=target_id, default=f"Мод '{target_id}' не найден" if lang == "ru" else f"Mod '{target_id}' not found")
        if getattr(args, "json", False):
            print(json.dumps({"error": err_msg, "success": False, "status": "not_found"}, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(f"{err_msg}\n")
        return 1

    masked_cfg = cfg_mgr.get_masked_config(mod) if mod.config else {}
    running_monitors = mon_mgr.get_running_monitors()

    # If --json or --format json: full structured JSON dump
    if getattr(args, "json", False) or getattr(args, "format", "text") == "json":
        is_ovr = mod.id in overrides and "enabled" in overrides[mod.id]
        targets_data = [
            {
                "file": t.file,
                "description": t.description,
                "disabled": t.disabled,
                "disabled_reason": t.disabled_reason,
            }
            for t in mod.targets
        ]
        commands_data = [
            {
                "name": c.name,
                "aliases": c.aliases,
                "args_hint": c.args_hint,
                "description": c.get_description(lang),
                "description_en": c.description,
                "category": c.category,
                "busy_policy": c.busy_policy,
                "handler_script": c.handler_script,
            }
            for c in mod.commands
        ]
        config_data = {
            k: {
                "type": f.type,
                "default": f.default,
                "sensitive": f.sensitive,
                "value": masked_cfg.get(k),
                "description": f.get_description(lang),
                "description_en": f.description,
                "required": f.required,
                "options": f.options,
                "min": f.min,
                "max": f.max,
            }
            for k, f in mod.config.items()
        }
        monitors_data = []
        for m_spec in mod.monitors:
            mon_key = mon_mgr.get_monitor_key(mod.id, m_spec.id)
            m_info = running_monitors.get(mon_key, {})
            monitors_data.append({
                "id": m_spec.id,
                "name": m_spec.name,
                "command": m_spec.command,
                "interval": m_spec.interval,
                "delivery": m_spec.delivery,
                "status": m_info.get("status", "STOPPED"),
                "pid": m_info.get("pid"),
                "description": m_spec.description,
            })
        skills_data = [s.name if s.is_dir() else s.parent.name for s in mod.list_skills()] if mod.skills_dir else []
        test_method = "native tests/" if mod.test_files else ("inline tests" if mod.inline_tests else "none")
        tests_data = {
            "count": mod.tests_count,
            "method": test_method,
            "files": [str(tf) for tf in mod.test_files],
            "inline_cases": len(mod.inline_tests),
        }

        full_info = {
            "id": mod.id,
            "name": mod.get_name(lang),
            "name_en": mod.name,
            "version": mod.version,
            "author": mod.author or "",
            "enabled": mod.enabled,
            "status": ("ENABLED*" if is_ovr else "ENABLED") if mod.enabled else ("DISABLED*" if is_ovr else "DISABLED"),
            "type": "folder" if mod.is_folder else "single",
            "path": str(mod.path),
            "description": mod.get_description(lang),
            "description_en": mod.description,
            "targets_count": len(mod.targets),
            "targets": targets_data,
            "commands": commands_data,
            "config": config_data,
            "monitors": monitors_data,
            "skills": skills_data,
            "tests": tests_data,
            "dependencies": mod.dependencies,
        }
        print(json.dumps(full_info, indent=2, ensure_ascii=False))
        return 0

    # Human-readable detailed card
    # Rules:
    # 1. No affected core files list in stdout (only in --json).
    # 2. No "Работает/Применен" status when normal. Only display status when disabled ([DISABLED] / [ОТКЛЮЧЕН]).
    # 3. Clean essence: Name, Version, Author (if specified), Description, slash-commands, config, monitors, skills, tests.
    is_folder = mod.is_folder
    if lang == "en":
        type_str = "Folder" if is_folder else "Single-file"
        lbl_mod = "Mod"
        lbl_author = "Author"
        lbl_status = "Status"
        lbl_type = "Type"
        lbl_path = "Path"
        lbl_desc = "Description"
        lbl_deps = "Dependencies"
        lbl_targets = "Targets"
        lbl_commands = "Commands"
        lbl_config = "Configuration"
        lbl_monitors = "Monitors"
        lbl_skills = "Skills"
        lbl_tests = "Tests"
    elif lang == "ru":
        type_str = "Папочный" if is_folder else "Однофайловый"
        lbl_mod = "Мод"
        lbl_author = "Автор"
        lbl_status = "Статус"
        lbl_type = "Тип"
        lbl_path = "Путь"
        lbl_desc = "Описание"
        lbl_deps = "Зависимости"
        lbl_targets = "Цели"
        lbl_commands = "Команды"
        lbl_config = "Конфигурация"
        lbl_monitors = "Мониторы"
        lbl_skills = "Скиллы"
        lbl_tests = "Тесты"
    else:
        type_key = "cli.info.type_folder" if is_folder else "cli.info.type_single"
        type_str = engine_t(type_key, lang=lang, default="Folder" if is_folder else "Single-file")
        lbl_mod = engine_t("cli.info.mod", lang=lang, default="Mod")
        lbl_author = engine_t("cli.info.author", lang=lang, default="Author")
        lbl_status = engine_t("cli.info.status", lang=lang, default="Status")
        lbl_type = engine_t("cli.info.type", lang=lang, default="Type")
        lbl_path = engine_t("cli.info.path", lang=lang, default="Path")
        lbl_desc = engine_t("cli.info.description", lang=lang, default="Description")
        lbl_deps = engine_t("cli.info.dependencies", lang=lang, default="Dependencies")
        lbl_targets = engine_t("cli.info.targets", lang=lang, default="Targets")
        lbl_commands = engine_t("cli.info.commands", lang=lang, default="Commands")
        lbl_config = engine_t("cli.info.config", lang=lang, default="Configuration")
        lbl_monitors = engine_t("cli.info.monitors", lang=lang, default="Monitors")
        lbl_skills = engine_t("cli.info.skills", lang=lang, default="Skills")
        lbl_tests = engine_t("cli.info.tests", lang=lang, default="Tests")

    mod_name = mod.get_name(lang)
    print(f"{lbl_mod}: {mod_name} ({mod.id}) v{mod.version}")
    if mod.author and mod.author.strip():
        print(f"{lbl_author}: {mod.author}")
    if not mod.enabled:
        tag_word = engine_t("cli.status.disabled", lang=lang, default="ОТКЛЮЧЕН" if lang == "ru" else "DISABLED")
        status_label = f"[{tag_word}]"
        print(f"{lbl_status}: {status_label}")
    print(f"{lbl_type}: {type_str}")
    print(f"{lbl_path}: {mod.path}")

    desc = mod.get_description(lang)
    if desc:
        print(f"\n{lbl_desc}:\n  {desc}")

    if mod.dependencies:
        print(f"\n{lbl_deps}:\n  • {', '.join(mod.dependencies)}")

    if mod.targets:
        print(f"\n{lbl_targets}: {len(mod.targets)}")

    if mod.commands:
        print(f"\n{lbl_commands}:")
        for c in mod.commands:
            cmd_desc = c.get_description(lang)
            alias_part = f" (псевдонимы: {', '.join('/' + a for a in c.aliases)})" if c.aliases else ""
            args_part = f" {c.args_hint}" if c.args_hint else ""
            policy_part = f" [режим: {c.busy_policy}]" if c.busy_policy != "reject" else ""
            print(f"  • /{c.name}{args_part}{alias_part}{policy_part} — {cmd_desc}")

    if mod.config:
        print(f"\n{lbl_config}:")
        for k, f in mod.config.items():
            val = masked_cfg.get(k)
            val_display = str(val) if val is not None else ("(не задано)" if lang == "ru" else "(not set)")
            sec_mark = ", секрет" if f.sensitive and lang == "ru" else (", secret" if f.sensitive else "")
            f_desc = f.get_description(lang)
            desc_part = f" — {f_desc}" if f_desc else ""
            curr_lbl = "текущее" if lang == "ru" else "current"
            print(f"  • {k} ({f.type}, default: {f.default}{sec_mark}, {curr_lbl}: {val_display}){desc_part}")

    if mod.monitors:
        print(f"\n{lbl_monitors}:")
        for m_spec in mod.monitors:
            mon_key = mon_mgr.get_monitor_key(mod.id, m_spec.id)
            m_info = running_monitors.get(mon_key, {})
            mon_status = m_info.get("status", "STOPPED")
            m_desc = m_spec.description
            desc_part = f" — {m_desc}" if m_desc else ""
            print(f"  • {m_spec.id} (интервал: {m_spec.interval}s, статус: {mon_status}){desc_part}")

    if mod.skills_dir:
        skills = mod.list_skills()
        if skills:
            print(f"\n{lbl_skills}:")
            for s in skills:
                s_name = s.name if s.is_dir() else s.parent.name
                print(f"  • {s_name}")

    if mod.has_tests:
        test_method = "native tests/" if mod.test_files else ("inline tests" if mod.inline_tests else "none")
        print(f"\n{lbl_tests}:")
        print(f"  • {mod.tests_count} ({test_method})")

    return 0


def handle_list_cli(args, mods: List[Mod], runner_or_path: Any) -> int:
    runner = runner_or_path if isinstance(runner_or_path, ModRunner) else ModRunner(mods_dir=runner_or_path)
    lang = resolve_language(getattr(args, "lang", "auto"), runner.config_file)
    status_filter = getattr(args, "status", "all") or "all"
    as_json = getattr(args, "json", False) or (getattr(args, "format", "text") == "json")

    inspector = runner.get_inspector()
    mods_data = inspector.list_mods(mods=mods, status_filter=status_filter, lang=lang)

    if as_json:
        print(json.dumps(mods_data, indent=2, ensure_ascii=False))
        return 0

    print(inspector.format_list_text(mods_data, status_filter=status_filter, lang=lang))
    return 0


def handle_status_cli(args, mods: List[Mod], runner_or_path: Any) -> int:
    runner = runner_or_path if isinstance(runner_or_path, ModRunner) else ModRunner(mods_dir=runner_or_path)
    lang = resolve_language(getattr(args, "lang", "auto"), runner.config_file)
    as_json = getattr(args, "json", False) or (getattr(args, "format", "text") == "json")

    inspector = runner.get_inspector()
    status_data = inspector.get_ecosystem_status(mods=mods, lang=lang)

    if as_json:
        print(json.dumps(status_data, indent=2, ensure_ascii=False))
        return 0 if status_data.get("healthy") else 1

    print(inspector.format_status_text(status_data, lang=lang))
    return 0 if status_data.get("healthy") else 1


def handle_logs_cli(args, mods: List[Mod], runner_or_path: Any) -> int:
    runner = runner_or_path if isinstance(runner_or_path, ModRunner) else ModRunner(mods_dir=runner_or_path)
    lang = resolve_language(getattr(args, "lang", "auto"), runner.config_file)
    target_id = args.target
    as_json = getattr(args, "json", False) or (getattr(args, "format", "text") == "json")
    lines_count = getattr(args, "lines", 50) or 50

    if not target_id:
        if as_json:
            print(json.dumps({
                "error": "Укажите mod_id для просмотра логов: runner.py logs <mod_id> [--lines N]",
                "success": False,
                "status": "missing_target"
            }, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write("Укажите mod_id для просмотра логов: runner.py logs <mod_id> [--lines N]\n")
        return 1

    mod = find_mod(target_id, mods)
    if not mod:
        if as_json:
            print(json.dumps({
                "error": f"Мод '{target_id}' не найден",
                "success": False,
                "status": "not_found"
            }, indent=2, ensure_ascii=False))
        else:
            sys.stderr.write(f"Мод '{target_id}' не найден\n")
        return 1

    inspector = runner.get_inspector()
    logs_data = inspector.get_mod_logs(mod, lines=lines_count, lang=lang)

    if as_json:
        print(json.dumps(logs_data, indent=2, ensure_ascii=False))
        return 0

    print(inspector.format_logs_text(logs_data, lang=lang))
    return 0


def handle_create_cli(args, mods_path: Path) -> int:
    mod_id = args.target
    if not mod_id:
        sys.stderr.write("Укажите mod_id для создания: runner.py create <mod_id> [--type single|folder]\n")
        return 1

    mod_id = mod_id.strip()
    if not IDENTIFIER_RE.match(mod_id):
        sys.stderr.write(f"Ошибка: некорректный mod_id '{mod_id}'. Разрешены только символы a-z, 0-9, '_' и '-'.\n")
        return 1

    custom_dir = mods_path / "custom"
    custom_dir.mkdir(parents=True, exist_ok=True)

    mod_type = getattr(args, "mod_type", "single") or "single"
    mod_title = mod_id.replace("-", " ").replace("_", " ").title()
    cmd_name = mod_id.replace("_", "-")

    if mod_type == "single":
        target_file = custom_dir / f"{mod_id}.yaml"
        if target_file.exists() or (custom_dir / f"{mod_id}.yml").exists() or (custom_dir / mod_id).exists():
            sys.stderr.write(f"Ошибка: мод '{mod_id}' уже существует в {custom_dir}\n")
            return 1

        content = f"""# ==============================================================================
# Hermes Single-File Mod: {mod_id}
# ==============================================================================
id: {mod_id}
name: {mod_title}
description: Scaffolded single-file mod {mod_id}
version: 1.0.0
author: User
enabled: true

# Declarative configuration options
config:
  example_setting:
    type: string
    description: Example configuration setting
    default: "default_value"
  secret_token:
    type: string
    description: Sensitive authentication token
    sensitive: true
    required: false

# Inline verification tests (dictionary format)
tests:
  test_sanity_check: |
    # Test logic executed by runner.py test
    assert True

# Declarative code patches against Hermes Agent source files
targets:
  - file: hermes_cli/commands.py
    description: Example code patch for {mod_id}
    old_block: |
      # Target block to match in Hermes core
    new_block: |
      # Replacement block
    disabled: true
    disabled_reason: "Template placeholder; customize and enable before applying"

# Declarative custom slash commands
commands:
  - name: {cmd_name}
    description: Example slash command for {mod_id}
    args_hint: "[text]"
    python: |
      print(f"Executed {mod_id} command with args: {{args}}")
    category: Custom Mods
    busy_policy: reject
"""
        target_file.write_text(content, encoding="utf-8")
        print(f"✓ Мод '{mod_id}' успешно создан: {target_file} (тип: single)")
        return 0

    elif mod_type == "folder":
        target_dir = custom_dir / mod_id
        if target_dir.exists() or (custom_dir / f"{mod_id}.yaml").exists() or (custom_dir / f"{mod_id}.yml").exists():
            sys.stderr.write(f"Ошибка: мод '{mod_id}' уже существует в {custom_dir}\n")
            return 1

        target_dir.mkdir(parents=True, exist_ok=True)
        tests_dir = target_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        mod_yaml_content = f"""# ==============================================================================
# Hermes Folder Mod: {mod_id}
# ==============================================================================
id: {mod_id}
name: {mod_title}
description: Scaffolded folder mod {mod_id}
version: 1.0.0
author: User
enabled: true
"""
        config_yaml_content = """# Declarative configuration options
example_setting:
  type: string
  description: Example configuration setting
  default: "default_value"
secret_token:
  type: string
  description: Sensitive authentication token
  sensitive: true
  required: false
"""
        commands_yaml_content = f"""# Declarative custom slash commands
- name: {cmd_name}
  description: Example slash command for {mod_id}
  args_hint: "[text]"
  python: |
    print(f"Executed {mod_id} command with args: {{args}}")
  category: Custom Mods
  busy_policy: reject
"""
        py_test_name = f"test_{mod_id.replace('-', '_')}.py"
        test_py_content = f'''"""Tests for {mod_id} mod."""
import pytest


def test_{mod_id.replace('-', '_')}_sanity():
    """Basic sanity test for {mod_id}."""
    assert True
'''

        (target_dir / "mod.yaml").write_text(mod_yaml_content, encoding="utf-8")
        (target_dir / "config.yaml").write_text(config_yaml_content, encoding="utf-8")
        (target_dir / "commands.yaml").write_text(commands_yaml_content, encoding="utf-8")
        (tests_dir / py_test_name).write_text(test_py_content, encoding="utf-8")

        print(f"✓ Мод '{mod_id}' успешно создан: {target_dir} (тип: folder)")
        return 0

    else:
        sys.stderr.write(f"Неизвестный тип мода '{mod_type}'. Допустимые: single, folder\n")
        return 1


def handle_export_cli(args, mods: List[Mod], mods_path: Path) -> int:
    try:
        from engine.exporter import ModExporter
    except ImportError:
        from .engine.exporter import ModExporter

    sfl = getattr(args, "strict_layout", None)
    validator = ModValidator(repo_root=HERMES_ROOT, mods_dir=mods_path, strict_folder_layout=sfl)
    exporter = ModExporter(mods_dir=mods_path, repo_root=HERMES_ROOT, validator=validator)

    out_path = getattr(args, "out_path", None)
    target_mod = getattr(args, "target", None)
    include_custom = getattr(args, "include_custom", False)
    no_secrets = getattr(args, "no_secrets", False)
    as_json = getattr(args, "json", False)

    result = exporter.export(
        mods=mods,
        out_path=out_path,
        target_mod_id=target_mod,
        include_custom=include_custom,
        no_secrets=no_secrets,
        validate_manifests=True,
    )

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.success else 1

    if not result.success:
        sys.stderr.write(f"❌ Ошибка экспорта: {result.error}\n")
        if result.validation_errors:
            sys.stderr.write("   Ошибки валидации манифестов:\n")
            for verr in result.validation_errors:
                m_id = verr.get("mod_id", "unknown")
                for iss in verr.get("issues", []):
                    if iss.get("severity") == "error":
                        sys.stderr.write(f"   • [{m_id}] {iss.get('category')}: {iss.get('message')}\n")
        return 1

    size_kb = result.archive_size / 1024
    print("✓ Валидация манифестов пройдена успешно")
    print(f"📦 Архив успешно создан: {result.archive_path} ({size_kb:.1f} KB)")
    print(f"   • Модов упаковано: {result.mods_count}")
    for m in result.mods:
        m_type = "папочный" if m.get("is_folder") else "однофайловый"
        print(f"     - {m.get('id')} (v{m.get('version')}, {m_type}, файлов: {m.get('files_count')})")
    print(f"   • Всего файлов в архиве: {len(result.files_included) + 1} (включая mod_export.json)")
    if result.files_excluded:
        print(f"   • Отфильтровано и исключено: {len(result.files_excluded)} (кэши, логи, секреты)")
    print("   🔒 Безопасность: кэши, secrets.yaml, *.db и логи исключены из архива")
    return 0


def handle_import_cli(args, mods_path: Path) -> int:
    try:
        from engine.exporter import ModImporter
    except ImportError:
        from .engine.exporter import ModImporter

    archive_target = getattr(args, "target", None)
    if not archive_target:
        if getattr(args, "json", False):
            print(json.dumps({"success": False, "error": "Укажите путь к архиву: runner.py import <archive.tar.gz>"}, ensure_ascii=False))
        else:
            sys.stderr.write("Укажите путь к архиву: runner.py import <archive.tar.gz>\n")
        return 1

    sfl = getattr(args, "strict_layout", None)
    validator = ModValidator(repo_root=HERMES_ROOT, mods_dir=mods_path, strict_folder_layout=sfl)
    importer = ModImporter(mods_dir=mods_path, repo_root=HERMES_ROOT, validator=validator)

    force_flag = bool(getattr(args, "force", False))
    as_json = getattr(args, "json", False)

    result = importer.import_archive(archive_path=archive_target, force=force_flag)

    if as_json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
        return 0 if result.success else 1

    if not result.success:
        sys.stderr.write(f"❌ Ошибка импорта: {'; '.join(result.errors)}\n")
        if result.validation.get("issues"):
            sys.stderr.write("   Ошибки валидации импортированных модов:\n")
            for verr in result.validation["issues"]:
                m_id = verr.get("mod_id", "unknown")
                for iss in verr.get("issues", []):
                    if iss.get("severity") == "error":
                        sys.stderr.write(f"   • [{m_id}] {iss.get('category')}: {iss.get('message')}\n")
        return 1

    print(f"📦 Распаковка архива: {result.archive_path}")
    print(f"✓ Извлечено файлов: {len(result.extracted_files)}")
    print("✓ Успешно импортированы моды:")
    for mid in result.imported_mods:
        print(f"  • {mid}")
    if result.skipped_files:
        print(f"ℹ️  Пропущено небезопасных/кэш файлов: {len(result.skipped_files)}")
    val_status = "PASSED" if result.validation.get("valid") else "WARNINGS/ERRORS"
    print(f"✓ Валидация после импорта: {val_status}")
    return 0


def handle_snapshot_cli(args, mods: List[Mod], runner: ModRunner) -> int:
    """Обработчик CLI-команд управления снапшотами ядра (create, restore, list, watchdog)."""
    subaction = args.args[0] if getattr(args, "args", None) and len(args.args) > 0 else "list"
    target_id = args.args[1] if getattr(args, "args", None) and len(args.args) > 1 else None

    # runner.py snapshot create [mod_id | files...]
    if subaction == "create":
        explicit_files = getattr(args, "files", None)
        desc = getattr(args, "snapshot_desc", None) or "Manual snapshot via CLI"
        target_mod_ids = []

        if not explicit_files:
            # Если передан аргумент runner.py snapshot create <mod_id>
            if target_id:
                m = find_mod(target_id, mods)
                if m:
                    target_mod_ids = [m.id]
                    explicit_files = [(runner.repo / t.file) for t in m.targets if not t.disabled]
            else:
                target_mod_ids = [m.id for m in mods if m.enabled]
                explicit_files = [
                    (runner.repo / t.file)
                    for m in mods
                    if m.enabled and m.targets
                    for t in m.targets
                    if not t.disabled
                ]

        try:
            snap = runner.snapshot_mgr.create_snapshot(
                files=explicit_files,
                description=desc,
                trigger="cli",
                mod_ids=target_mod_ids,
                mods_dir=runner.mods_dir,
            )
            if getattr(args, "json", False):
                print(json.dumps(snap.to_dict(), indent=2, ensure_ascii=False))
            else:
                print(f"✓ Создан дельта-снапшот ядра: {snap.id}")
                print(f"  Описание: {snap.description}")
                print(f"  Затронуто файлов ядра: {len(snap.files)}")
                for rel_path in sorted(snap.files.keys()):
                    rec = snap.files[rel_path]
                    flag = "сохранен" if rec.get("exists_before") else "новый (будет удален при откате)"
                    print(f"  • {rel_path} ({flag})")
            return 0
        except Exception as exc:
            if getattr(args, "json", False):
                print(json.dumps({"success": False, "error": str(exc)}, indent=2, ensure_ascii=False))
            else:
                sys.stderr.write(f"Ошибка создания снапшота ядра: {exc}\n")
            return 1

    elif subaction == "list":
        snaps = runner.snapshot_mgr.list_snapshots()
        if getattr(args, "json", False):
            print(json.dumps(snaps, indent=2, ensure_ascii=False))
        else:
            if not snaps:
                print("Снапшоты ядра отсутствуют.")
                return 0
            print(f"Всего снапшотов ядра: {len(snaps)}\n")
            print(f"{'ID':<34} {'ДАТА (UTC)':<20} {'ТРИГГЕР':<12} {'ФАЙЛОВ':<8} {'СТАТУС':<10} {'ОПИСАНИЕ'}")
            print("-" * 115)
            for s in snaps:
                created = s.get("created_at", "")[:19].replace("T", " ")
                trig = s.get("trigger", "manual")
                cnt = s.get("file_count", len(s.get("files", {})))
                status = s.get("status", "valid")
                desc = s.get("description", "")
                if len(desc) > 35:
                    desc = desc[:32] + "..."
                print(f"{s['id']:<34} {created:<20} {trig:<12} {cnt:<8} {status:<10} {desc}")
        return 0

    elif subaction == "restore":
        # runner.py snapshot restore [id]
        restore_id = target_id or (args.target if args.target and args.target != "restore" else "latest")
        res = runner.snapshot_mgr.restore_snapshot(restore_id)
        if getattr(args, "json", False):
            print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        else:
            if res.success:
                print(f"✓ Снапшот ядра '{res.snapshot_id}' успешно восстановлен:")
                print(f"  Восстановлено файлов: {len(res.restored_files)}")
                for f in res.restored_files:
                    print(f"  ✓ {f}")
                if res.deleted_files:
                    print(f"  Удалено вновь созданных файлов: {len(res.deleted_files)}")
                    for df in res.deleted_files:
                        print(f"  ✗ {df}")
            else:
                sys.stderr.write(f"Ошибка отката к снапшоту ядра '{res.snapshot_id}':\n")
                for err in res.errors:
                    sys.stderr.write(f"  ✗ {err}\n")
        return 0 if res.success else 1

    elif subaction in ("watchdog", "healthcheck"):
        return handle_watchdog_cli(args, runner)

    else:
        sys.stderr.write(f"Неизвестное действие snapshot: '{subaction}'. Доступны: create, restore, list, watchdog\n")
        return 1


def handle_watchdog_cli(args, runner: ModRunner) -> int:
    """Обработчик проверки шлюза и автоматического watchdog-отката."""
    service_name = getattr(args, "service", None) or "hermes-gateway"
    window = float(getattr(args, "window", None) or getattr(args, "watchdog_window", None) or 30.0)
    snap_id = args.args[1] if getattr(args, "args", None) and len(args.args) > 1 and args.args[1] not in ("create", "restore", "list", "watchdog") else None

    watchdog = GatewayWatchdog(service_name=service_name, snapshot_mgr=runner.snapshot_mgr)
    result = watchdog.monitor_and_rollback(
        snapshot_id=snap_id,
        window_seconds=window,
        poll_interval=1.0,
        auto_restore=True,
    )
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        if result.success:
            print(f"✓ Шлюз '{result.service_name}' успешно запущен и стабилен в течение {result.stable_seconds:.1f}с.")
        else:
            sys.stderr.write(f"✗ Ошибка мониторинга шлюза: {result.error}\n")
            if result.rolled_back and result.restored_files:
                sys.stderr.write(f"  Автоматический откат к снапшоту '{result.snapshot_id}' выполнен успешно:\n")
                for rf in result.restored_files:
                    sys.stderr.write(f"    • {rf}\n")
    return result.exit_code


def handle_circuit_breaker_cli(args, mods: List[Mod], runner: ModRunner) -> int:
    """CLI handler for runtime circuit breaker operations."""
    cb = get_circuit_breaker(runner.mods_dir)
    sub = getattr(args, "subaction", None) or getattr(args, "target", "status")

    if sub in ("status", "list"):
        statuses = cb.get_all_statuses()
        if getattr(args, "json", False):
            print(json.dumps(statuses, indent=2, ensure_ascii=False))
            return 0
        print("=" * 80)
        print("Hermes Mod Engine: Runtime Circuit Breaker Status")
        print("=" * 80)
        print(f"{'MOD ID':<30} {'STATUS':<12} {'FAILURES':<10} {'TRIPPED AT':<22} {'REASON'}")
        print("-" * 80)
        for mid, st in sorted(statuses.items()):
            state_str = st.get("state", "CLOSED")
            fails = st.get("failures_in_window", 0)
            tripped_at = st.get("tripped_at")
            tripped_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(tripped_at)) if tripped_at else "-"
            reason_str = (st.get("reason") or "-")[:30]
            print(f"{mid:<30} {state_str:<12} {fails:<10} {tripped_str:<22} {reason_str}")
        print("=" * 80)
        return 0

    if sub == "reset":
        target = args.monitor_id if getattr(args, "target", None) == "reset" else getattr(args, "target", None)
        if not target or target == "reset":
            sys.stderr.write("Укажите mod_id для сброса Circuit Breaker: runner.py circuit-breaker reset <mod_id>\n")
            return 1
        res = cb.reset(target, re_enable=True)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"✓ Circuit Breaker для мода '{target}' успешно сброшен (state: CLOSED).")
        return 0

    if sub == "trip":
        target = args.monitor_id if getattr(args, "target", None) == "trip" else getattr(args, "target", None)
        if not target or target == "trip":
            sys.stderr.write("Укажите mod_id для изоляции: runner.py circuit-breaker trip <mod_id>\n")
            return 1
        reason = getattr(args, "reason", None) or "Manual operator trip via CLI"
        res = cb.isolate_mod(target, reason=reason)
        if getattr(args, "json", False):
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(f"⚠️ [CIRCUIT BREAKER] Мод '{target}' изолирован (state: OPEN).")
        return 0

    if sub == "failures":
        target = args.monitor_id if getattr(args, "target", None) == "failures" else getattr(args, "target", None)
        if not target or target == "failures":
            sys.stderr.write("Укажите mod_id: runner.py circuit-breaker failures <mod_id>\n")
            return 1
        fails = cb.get_failures(target, window_only=False)
        if getattr(args, "json", False):
            print(json.dumps(fails, indent=2, ensure_ascii=False))
        else:
            print(f"История инцидентов для '{target}': {len(fails)} записей")
            for f in fails:
                t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.get("timestamp", 0)))
                print(f"  • [{t_str}] {f.get('error_type')}: {f.get('error')}")
        return 0

    sys.stderr.write(f"Неизвестное действие circuit-breaker: {sub}. Допустимые: status, reset, trip, failures\n")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Mod Runner")
    parser.add_argument("action", nargs="?", default="apply", choices=["apply", "verify", "test", "list", "enable", "disable", "config", "monitors", "validate", "mcp", "commands", "update", "push", "diff", "doctor", "unpatch", "rollback", "purge", "uninstall-all", "info", "create", "export", "pack", "import", "snapshot", "watchdog", "circuit-breaker", "status", "logs", "cron"])
    parser.add_argument("args", nargs="*", help="Subaction or target identifiers (e.g. mod-id, monitor-id)")
    parser.add_argument("--message", "-m", default=None, help="Commit message for git push (auto-generated conventional commit if omitted)")
    parser.add_argument("--install-hook", action="store_true", help="Install or update .git/hooks/post-commit auto-push hook")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format: text or json")
    parser.add_argument("--status", choices=["applied", "disabled", "all"], default="all", help="Filter mods by status: applied, disabled, all")
    parser.add_argument("--lines", "-n", dest="lines", type=int, default=50, help="Number of log lines to show (default: 50)")
    parser.add_argument("--subaction", choices=["list", "start", "stop", "status"], help="Subaction for monitors")
    parser.add_argument("--lang", default="auto", help="Language code for localization (e.g. en, ru, es, de, or 'auto')")
    parser.add_argument("--type", dest="mod_type", default="single", choices=["single", "folder"], help="Scaffolding layout type: single (file) or folder (default: single)")
    parser.add_argument("--set", dest="set_option", help="Set non-sensitive config: --set key=value")
    parser.add_argument("--set-secret", dest="set_secret", help="Set sensitive secret: --set-secret key=value")
    parser.add_argument("--populate", action="store_true", help="Populate default configuration values for mods in config.yaml and secrets.yaml")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose test and command execution output")
    parser.add_argument("-x", "--fail-fast", action="store_true", help="Stop tests immediately on first failure")
    parser.add_argument("-k", "--filter", dest="test_filter", help="Filter tests by name expression")
    parser.add_argument("--all", dest="all_mods", action="store_true", help="Run tests for all mods including disabled, include system mods in unpatch, or run full three-phase update in update action")
    parser.add_argument("--with-hermes", action="store_true", help="Run full three-phase update (engine pre-update -> Hermes core update -> mods reapply)")
    parser.add_argument("--system", dest="include_system", action="store_true", help="Include system engine mods in unpatch/rollback")
    parser.add_argument("--confirm", action="store_true", help="Confirm destructive purge of all mods including system core mods")
    parser.add_argument("-y", "--yes", action="store_true", help="Assume yes / confirm dangerous actions without prompting")
    parser.add_argument("--no-deps", action="store_true", help="Do not auto-enable dependencies or resolve dependency closure")
    parser.add_argument("--write-manifest", action="store_true", help="Also persist enabled/disabled status into YAML manifest")
    parser.add_argument("--circuit-breaker", action="store_true", help="Disable mod via Circuit Breaker and rollback patches from core")
    parser.add_argument("--reason", default=None, help="Reason for circuit breaker tripping / disabling")
    parser.add_argument("--no-color", action="store_true", help="Disable colored output in diff and doctor")
    parser.add_argument("--color", action="store_true", help="Force colored output in diff and doctor")
    parser.add_argument("--mods-dir", default=str(MODS_DIR), help="Mods directory")
    parser.add_argument("--out", dest="out_path", help="Output archive path for export (e.g. archive.tar.gz)")
    parser.add_argument("--include-custom", action="store_true", help="Include custom mods when exporting all mods")
    parser.add_argument("--no-secrets", action="store_true", default=False, help="Strictly enforce exclusion of secrets, keys, and credentials")
    parser.add_argument("--check", action="store_true", help="Check for available updates without applying")
    parser.add_argument("--dry-run", action="store_true", help="Simulate update and apply without modifying files")
    parser.add_argument("--no-apply", action="store_true", help="Update mod repo only, do not apply patches to Hermes Agent")
    parser.add_argument("--force", action="store_true", help="Force update, test execution or verification (bypasses cache)")
    parser.add_argument("--no-cache", action="store_true", help="Bypass test and verification cache, forcing fresh cold test runs")
    parser.add_argument("--strict-layout", dest="strict_layout", action="store_true", default=None, help="Enforce strict folder layout (separate files for targets, commands.yaml, config, monitors)")
    parser.add_argument("--no-strict-layout", dest="strict_layout", action="store_false", help="Disable strict folder layout enforcement")
    parser.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    parser.add_argument("--branch", default="main", help="Git branch name (default: main)")
    parser.add_argument("--desc", "--description", dest="snapshot_desc", default=None, help="Description for core snapshot")
    parser.add_argument("--files", nargs="*", default=None, help="Explicit file paths for core snapshot")
    parser.add_argument("--window", type=float, default=30.0, help="Watchdog observation window in seconds (default: 30s)")
    parser.add_argument("--service", default="hermes-gateway", help="Systemd service name for watchdog (default: hermes-gateway)")
    parser.add_argument("--watchdog", action="store_true", help="Run gateway watchdog healthcheck after apply and auto-rollback on failure")
    parser.add_argument("--watchdog-window", type=float, default=30.0, help="Observation window for --watchdog after apply (default: 30s)")
    parser.add_argument("--schedule", default="0 4 * * *", help="Cron schedule expression for auto-update (default: '0 4 * * *')")
    parser.add_argument("--python", dest="python_bin", default=None, help="Explicit python interpreter path for cron jobs")
    args = parser.parse_args()

    # Map positional extra args
    args.target = args.args[0] if len(args.args) > 0 else None
    args.monitor_id = args.args[1] if len(args.args) > 1 else None
    if len(args.args) > 2 and args.action == "monitors":
        # e.g. runner.py monitors start <mod_id> <monitor_id>
        args.subaction = args.args[0]
        args.target = args.args[1]
        args.monitor_id = args.args[2]

    mods_path = Path(args.mods_dir)
    mods = ModLoader.load_all(mods_path)
    runner = ModRunner(mods_dir=mods_path)
    graph = ModDependencyGraph(mods)

    # Always ensure commands cache is fresh for active mods (only when enabled and not unpatching/purging/exporting/snapshot/watchdog/circuit-breaker)
    if runner.is_mods_enabled() and args.action not in ("unpatch", "rollback", "purge", "uninstall-all", "export", "pack", "import", "snapshot", "watchdog", "circuit-breaker"):
        try:
            update_commands_cache(mods, mods_path)
            update_locales_cache(mods, mods_path)
            update_mcp_cache(mods, mods_path)
        except Exception:
            pass

    if args.action == "circuit-breaker":
        return handle_circuit_breaker_cli(args, mods, runner)

    if args.action == "snapshot":
        return handle_snapshot_cli(args, mods, runner)

    if args.action == "watchdog":
        return handle_watchdog_cli(args, runner)

    if args.action in ("unpatch", "rollback"):
        return handle_unpatch_cli(args, mods, runner, graph)

    if args.action in ("purge", "uninstall-all"):
        return handle_purge_cli(args, mods, runner, graph)

    if args.action == "create":
        return handle_create_cli(args, mods_path)

    if args.action in ("export", "pack"):
        return handle_export_cli(args, mods, mods_path)

    if args.action == "import":
        return handle_import_cli(args, mods_path)

    if args.action == "info":
        return handle_info_cli(args, mods, mods_path)

    if args.action == "commands":
        return handle_commands_cli(args, mods)

    if args.action == "diff":
        return handle_diff_cli(args, mods)

    if args.action == "doctor":
        return handle_doctor_cli(args, mods)

    if args.action == "test":
        return handle_test_cli(args, mods, runner)

    if args.action == "list":
        return handle_list_cli(args, mods, runner)

    if args.action == "status":
        return handle_status_cli(args, mods, runner)

    if args.action == "logs":
        return handle_logs_cli(args, mods, runner)

    if args.action == "update":
        return handle_update_cli(args, mods)

    if args.action == "push":
        from engine.pusher import handle_push_cli
        return handle_push_cli(args, mods)

    if args.action == "cron":
        from engine.cron_manager import handle_cron_cli
        return handle_cron_cli(args, mods)

    if args.action == "mcp":
        return handle_mcp_cli(args, mods)

    if args.action == "validate":
        return handle_validate_cli(args, mods)

    if args.action == "config":
        return handle_config_cli(args, mods)

    if args.action == "monitors":
        # Support syntax: runner.py monitors list, runner.py monitors start <mod> <mon>
        if args.target in ("list", "start", "stop", "status"):
            args.subaction = args.target
            args.target = args.monitor_id
            args.monitor_id = None
        return handle_monitors_cli(args, mods)

    if args.action in ("enable", "disable"):
        if not args.target:
            sys.stderr.write("Укажите имя мода для включения/отключения\n")
            return 1
        found = find_mod(args.target, mods)
        if not found:
            if getattr(args, "json", False):
                print(json.dumps({"error": f"Мод '{args.target}' не найден", "success": False}, indent=2, ensure_ascii=False))
            else:
                sys.stderr.write(f"Мод '{args.target}' не найден\n")
            return 1

        state_mgr = StateManager(mods_path / ".state.json")

        if args.action == "enable":
            missing = graph.get_missing_dependencies(found)
            if missing:
                sys.stderr.write(
                    f"⚠️  Предупреждение: мод '{found.id}' требует отсутствующие зависимости: {', '.join(missing)}!\n"
                )

            auto_enabled = []
            if not args.no_deps:
                try:
                    closure = graph.get_dependency_closure([found])
                except CycleDetectionError as exc:
                    sys.stderr.write(f"Ошибка: обнаружен цикл зависимостей: {exc}\n")
                    return 1

                for dep in closure:
                    if dep.id != found.id and not dep.enabled:
                        state_mgr.set_mod_override(dep.id, {"enabled": True})
                        dep.enabled = True
                        if getattr(args, "write_manifest", False):
                            _update_manifest_enabled(dep.path, True)
                        auto_enabled.append(dep.id)
                        print(f"  ↪ Автоматически включена зависимость: '{dep.id}'")
            else:
                disabled = graph.get_disabled_dependencies(found)
                if disabled:
                    d_names = [d.id for d in disabled]
                    sys.stderr.write(
                        f"⚠️  Предупреждение: зависимость(и) отключены: {', '.join(d_names)}. "
                        f"Используйте без --no-deps для их автоматического включения.\n"
                    )

            state_mgr.set_mod_override(found.id, {
                "enabled": True,
                "disabled_circuit_breaker": False,
                "disabled_reason": None,
                "circuit_breaker_tripped_at": None,
            })
            found.enabled = True
            found.disabled_circuit_breaker = False
            found.disabled_reason = None
            try:
                cb = get_circuit_breaker(mods_path)
                cb.reset(found.id)
            except Exception:
                pass
            if getattr(args, "write_manifest", False):
                _update_manifest_enabled(found.path, True)

            try:
                MODS_DIR.touch()
            except OSError:
                pass
            _mod_mcp_cache_clear()
            try:
                update_commands_cache(mods, mods_path)
                update_locales_cache(mods, mods_path)
                update_mcp_cache(mods, mods_path)
            except Exception:
                pass

            extra = f" (также включены зависимости: {', '.join(auto_enabled)})" if auto_enabled else ""
            print(f"Мод '{found.id}' включен: enabled = True{extra}")
            return 0

        if args.action == "disable":
            is_cb = bool(getattr(args, "circuit_breaker", False))
            cb_reason = getattr(args, "reason", None) or ("Circuit breaker trip: repeated runtime errors" if is_cb else None)

            if is_cb and is_system_mod(found, runner.mods_dir):
                msg = f"⚠️ Предупреждение: мод '{found.id}' является системным компонентом ядра. Откат отменен для сохранения стабильности шлюза."
                if getattr(args, "json", False):
                    print(json.dumps({"error": msg, "success": False, "status": "protected_system_mod"}, ensure_ascii=False))
                else:
                    sys.stderr.write(f"{msg}\n")
                return 1

            active_dependents = graph.get_dependents(found, enabled_only=True)
            if active_dependents:
                dep_ids = [d.id for d in active_dependents]
                sys.stderr.write(
                    f"⚠️  Предупреждение: от мода '{found.id}' зависят другие активные моды: {', '.join(dep_ids)}!\n"
                    f"   Отключение '{found.id}' может нарушить их работу.\n"
                )

            overrides_data = {"enabled": False}
            if is_cb:
                now_ts = time.time()
                overrides_data["disabled_circuit_breaker"] = True
                overrides_data["disabled_reason"] = cb_reason
                overrides_data["circuit_breaker_tripped_at"] = now_ts
                found.disabled_circuit_breaker = True
                found.disabled_reason = cb_reason

            state_mgr.set_mod_override(found.id, overrides_data)
            found.enabled = False
            if getattr(args, "write_manifest", False):
                _update_manifest_enabled(found.path, False)

            unpatch_report = None
            if is_cb:
                # 1. Automatic rollback from Hermes core
                unpatch_report = runner.unpatch_all([found], graph=graph, include_system=False)
                # 2. Stop running monitors
                try:
                    from engine.monitor_manager import ModMonitorManager
                    mon_mgr = ModMonitorManager()
                    running = mon_mgr.get_running_monitors()
                    for key, info in running.items():
                        if info.get("mod_id") == found.id and info.get("status") == "RUNNING":
                            mon_id = info.get("monitor_id")
                            if mon_id:
                                mon_mgr.stop_monitor(found.id, mon_id)
                except Exception:
                    pass
                # 3. Update circuit breaker state file
                try:
                    cb = get_circuit_breaker(mods_path)
                    cb.isolate_mod(found.id, reason=cb_reason)
                except Exception:
                    pass

            try:
                MODS_DIR.touch()
            except OSError:
                pass
            _mod_mcp_cache_clear()
            try:
                update_commands_cache(mods, mods_path)
                update_locales_cache(mods, mods_path)
            except Exception:
                pass

            if is_cb:
                if getattr(args, "json", False):
                    res = {
                        "success": True,
                        "mod_id": found.id,
                        "enabled": False,
                        "disabled_circuit_breaker": True,
                        "status": "disabled_circuit_breaker",
                        "reason": cb_reason,
                        "unpatch_report": unpatch_report,
                    }
                    print(json.dumps(res, indent=2, ensure_ascii=False))
                    return 0
                warning_msg = f"⚠️ [CIRCUIT BREAKER] Мод '{found.id}' изолирован: помечен как disabled_circuit_breaker, патчи откатаны из ядра."
                print(warning_msg)
                if unpatch_report and unpatch_report.get("restored_files"):
                    print(f"  ↪ Восстановлено файлов ядра: {unpatch_report['restored_count']}")
                    for rf in unpatch_report["restored_files"]:
                        print(f"    ✓ {rf}")
            else:
                if getattr(args, "json", False):
                    print(json.dumps({"success": True, "mod_id": found.id, "enabled": False, "status": "disabled"}, indent=2, ensure_ascii=False))
                    return 0
                print(f"Мод '{found.id}' переключен: enabled = False")
            return 0

    try:
        graph.detect_cycles()
    except CycleDetectionError as exc:
        sys.stderr.write(f"Ошибка: обнаружен цикл зависимостей: {exc}\n")
        return 1

    target_mods = mods
    if args.target:
        m = find_mod(args.target, mods)
        if not m:
            if getattr(args, "json", False):
                print(json.dumps([{"id": args.target, "name": args.target, "error": f"Мод '{args.target}' не найден", "status": "not_found", "verified": False}], indent=2, ensure_ascii=False))
            else:
                sys.stderr.write(f"Мод '{args.target}' не найден\n")
            return 1
        if args.no_deps:
            target_mods = [m]
        else:
            target_mods = graph.get_dependency_closure([m])
    else:
        target_mods = graph.topological_sort(mods)

    apply_flag = (args.action == "apply") and (os.environ.get("HERMES_MOD_APPLY", "1") == "1")
    if apply_flag:
        try:
            cfg_mgr = ModConfigManager(CONFIG_FILE, SECRETS_FILE)
            cfg_mgr.populate_defaults(mods)
        except Exception as exc:
            sys.stderr.write(f"Предупреждение: не удалось автозаполнить настройки модов: {exc}\n")

    force_flag = bool(getattr(args, "force", False) or getattr(args, "no_cache", False))
    results = runner.run_all(target_mods, apply_changes=apply_flag, graph=graph, force=force_flag)

    if apply_flag and not runner.is_mods_enabled() and not args.json:
        sys.stderr.write("⚠️  Мастер-рубильник mods.enabled: false в config.yaml!\n")
        sys.stderr.write("   Наложение модов заблокировано. Код ядра возвращен к исходному состоянию.\n\n")

    print(json.dumps(results, indent=2, ensure_ascii=False))

    failed = any(
        r.get("status") in (
            "failed", "failed_verification", "failed_verification_rolled_back",
            "failed_exception_rolled_back", "skipped_missing_dependency", "skipped_disabled_dependency",
            "failed_preflight_validation", "skipped_engine_outdated", "skipped_hermes_outdated",
            "blocked_killswitch",
        )
        for r in results
    )

    # Watchdog integration: if --watchdog is passed (or env HERMES_MODS_AUTO_WATCHDOG=1) and apply succeeded
    if apply_flag and not failed and (getattr(args, "watchdog", False) or os.environ.get("HERMES_MODS_AUTO_WATCHDOG") == "1"):
        watchdog_window = float(getattr(args, "watchdog_window", 30.0))
        watchdog = GatewayWatchdog(service_name=getattr(args, "service", "hermes-gateway"), snapshot_mgr=runner.snapshot_mgr)
        wd_res = watchdog.monitor_and_rollback(
            snapshot_id=None,  # Use latest snapshot created before apply
            window_seconds=watchdog_window,
            auto_restore=True,
        )
        if not wd_res.success:
            failed = True
            if not getattr(args, "json", False):
                sys.stderr.write(f"\n⚠️  WATCHDOG: {wd_res.error}\n")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
