"""Hermes Mod Engine: Inspector & Diagnostics subsystem.

Provides inspection, listing, status reporting, and log retrieval
for Hermes Agent mods.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from .models import Mod, ModTarget, CycleDetectionError
    from .loader import ModLoader, ModDependencyGraph
    from .config_manager import ModConfigManager, SecretsStorage
    from .monitor_manager import ModMonitorManager
    from .version import ENGINE_VERSION
    from .updater import get_hermes_version, StateManager
    from .i18n import engine_i18n, t as engine_t, resolve_language, normalize_language_id
except ImportError:
    from models import Mod, ModTarget, CycleDetectionError
    from loader import ModLoader, ModDependencyGraph
    from config_manager import ModConfigManager, SecretsStorage
    from monitor_manager import ModMonitorManager
    from version import ENGINE_VERSION
    from updater import get_hermes_version, StateManager
    from i18n import engine_i18n, t as engine_t, resolve_language, normalize_language_id

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", HERMES_HOME / "mods"))
try:
    from .loader import detect_hermes_root
    HERMES_ROOT = detect_hermes_root()
except Exception:
    try:
        from loader import detect_hermes_root
        HERMES_ROOT = detect_hermes_root()
    except Exception:
        HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
CONFIG_FILE = Path(os.environ.get("HERMES_CONFIG", HERMES_HOME / "config.yaml"))
SECRETS_FILE = Path(os.environ.get("HERMES_SECRETS", HERMES_HOME / "secrets.yaml"))
DEFAULT_LOG_DIR = HERMES_HOME / "logs"


def is_system_mod(mod: Mod, mods_dir: Optional[Path] = None) -> bool:
    """Returns True if the mod is a Tier 0 system/engine mod."""
    if (mod.author or "").strip().lower() == "engine":
        return True
    mod_path = Path(mod.path)
    base_dir = Path(mods_dir) if mods_dir else MODS_DIR
    try:
        rel = mod_path.resolve().relative_to(base_dir.resolve())
        return "system" in rel.parts
    except Exception:
        return "system" in str(mod_path).split(os.sep)


def read_file_tail(file_path: Path, max_lines: int = 1000, buffer_size: int = 65536) -> List[str]:
    """Efficiently read the last max_lines from a file."""
    if not file_path.is_file():
        return []
    try:
        file_size = file_path.stat().st_size
        if file_size == 0:
            return []
        if file_size < 1024 * 1024:
            text = file_path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            return lines[-max_lines:]

        with open(file_path, "rb") as f:
            lines: List[str] = []
            offset = file_size
            while offset > 0 and len(lines) <= max_lines:
                read_size = min(buffer_size, offset)
                offset -= read_size
                f.seek(offset)
                chunk = f.read(read_size)
                chunk_str = chunk.decode("utf-8", errors="replace")
                chunk_lines = chunk_str.splitlines(True)
                if lines and chunk_lines:
                    lines[0] = chunk_lines[-1] + lines[0]
                    lines = chunk_lines[:-1] + lines
                else:
                    lines = chunk_lines + lines
                if len(lines) > max_lines:
                    break
            return [l.rstrip("\r\n") for l in lines[-max_lines:]]
    except Exception:
        return []


class ModInspector:
    """Subsystem for querying mod metadata, applied state, health, and logs."""

    def __init__(
        self,
        mods_dir: Optional[Path] = None,
        repo_root: Optional[Path] = None,
        config_file: Optional[Path] = None,
        secrets_file: Optional[Path] = None,
        log_dir: Optional[Path] = None,
    ):
        hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
        self.mods_dir = Path(mods_dir) if mods_dir else Path(os.environ.get("HERMES_MODS_DIR", hermes_home / "mods"))
        self.repo_root = Path(repo_root) if repo_root else Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
        self.config_file = Path(config_file) if config_file else Path(os.environ.get("HERMES_CONFIG", hermes_home / "config.yaml"))
        self.secrets_file = Path(secrets_file) if secrets_file else Path(os.environ.get("HERMES_SECRETS", hermes_home / "secrets.yaml"))
        self.log_dir = Path(log_dir) if log_dir else Path(os.environ.get("HERMES_LOG_DIR", hermes_home / "logs"))
        self.config_mgr = ModConfigManager(self.config_file, self.secrets_file)
        self.monitor_mgr = ModMonitorManager()

    def get_mod_status(self, mod: Mod) -> str:
        """Determines mod application status: applied, disabled, not_applied, or diverged."""
        if not mod.enabled:
            return "disabled"

        active_targets = [t for t in mod.targets if not t.disabled]
        if not active_targets:
            return "applied"

        all_applied = True
        all_not_applied = True
        has_diverged = False

        for t in active_targets:
            target_path = self.repo_root / t.file
            if not target_path.is_file():
                has_diverged = True
                all_applied = False
                all_not_applied = False
                continue

            try:
                content = target_path.read_text(encoding="utf-8")
            except Exception:
                has_diverged = True
                all_applied = False
                all_not_applied = False
                continue

            if t.new_block in content:
                all_not_applied = False
            elif t.old_block in content:
                all_applied = False
            else:
                has_diverged = True
                all_applied = False
                all_not_applied = False

        if all_applied:
            return "applied"
        if all_not_applied:
            return "not_applied"
        return "diverged" if has_diverged else "not_applied"

    def list_mods(
        self,
        mods: Optional[List[Mod]] = None,
        status_filter: str = "all",
        lang: str = "auto",
    ) -> List[Dict[str, Any]]:
        """Returns structured metadata for mods, filtered by status."""
        if mods is None:
            mods = ModLoader.load_all(self.mods_dir)

        state_mgr = StateManager(self.mods_dir / ".state.json")
        overrides = state_mgr.load().get("overrides", {})

        result: List[Dict[str, Any]] = []
        filter_val = (status_filter or "all").strip().lower()

        for m in mods:
            mod_status = self.get_mod_status(m)

            # Apply filter
            if filter_val == "applied" and mod_status != "applied":
                continue
            if filter_val == "disabled" and mod_status != "disabled":
                continue

            is_ovr = m.id in overrides and "enabled" in overrides[m.id]
            is_sys = is_system_mod(m, self.mods_dir)

            item = {
                "id": m.id,
                "name": m.get_name(lang),
                "name_en": m.name,
                "version": m.version,
                "author": m.author or ("Engine" if is_sys else "User"),
                "description": m.get_description(lang),
                "description_en": m.description,
                "status": mod_status,
                "enabled": m.enabled,
                "is_system": is_sys,
                "targets": len(m.targets),
                "targets_count": len(m.targets),
                "commands": len(m.commands),
                "commands_count": len(m.commands),
                "config": len(m.config),
                "config_count": len(m.config),
                "user_config": len(m.config),
                "user_config_count": len(m.config),
                "monitors": len(m.monitors),
                "monitors_count": len(m.monitors),
                "skills": len(m.list_skills()) if m.skills_dir else 0,
                "skills_count": len(m.list_skills()) if m.skills_dir else 0,
                "mcp_servers": len(m.mcp_servers),
                "mcp_servers_count": len(m.mcp_servers),
                "tests": m.tests_count,
                "tests_count": m.tests_count,
                "skills_dir": str(m.skills_dir) if m.skills_dir else None,
                "dependencies": m.dependencies,
                "overridden": is_ovr,
                "path": str(m.path),
            }
            result.append(item)

        return result

    def format_list_text(
        self,
        mods_data: List[Dict[str, Any]],
        status_filter: str = "all",
        lang: str = "auto",
        use_color: bool = True,
    ) -> str:
        """Formats list output in a clean, human-readable view."""
        total = len(mods_data)
        applied_cnt = sum(1 for m in mods_data if m.get("status") == "applied")
        disabled_cnt = sum(1 for m in mods_data if m.get("status") == "disabled")
        not_applied_cnt = sum(1 for m in mods_data if m.get("status") in ("not_applied", "diverged"))

        filter_val = (status_filter or "all").strip().lower()
        title_extra = ""
        if filter_val == "applied":
            title_extra = f" [filter: applied ({total})]"
        elif filter_val == "disabled":
            title_extra = f" [filter: disabled ({total})]"

        lines: List[str] = []
        lines.append("=" * 80)
        lines.append(f" Hermes Mods ({total} total | {applied_cnt} applied | {disabled_cnt} disabled){title_extra}")
        lines.append("=" * 80)

        if not mods_data:
            empty_msg = engine_t("cli.list.empty", lang=lang, default="  (нет модов, соответствующих критерию)" if lang == "ru" else "  (no mods matching filter)")
            lines.append(empty_msg)
            lines.append("=" * 80)
            return "\n".join(lines)

        for m in mods_data:
            st = m.get("status", "not_applied")
            name = m.get("name") or m.get("id")
            ver = m.get("version", "1.0.0")
            mid = m.get("id")
            desc = m.get("description") or ""

            if st == "applied":
                badge = "🟢"
                status_tag = ""
            elif st == "disabled":
                badge = "⚪"
                tag_word = engine_t("cli.status.disabled", lang=lang, default="ОТКЛЮЧЕН" if lang == "ru" else "DISABLED")
                status_tag = f" [{tag_word}]"
            elif st == "not_applied":
                badge = "🟡"
                tag_word = engine_t("cli.status.not_applied", lang=lang, default="НЕ ПРИМЕНЕН" if lang == "ru" else "NOT APPLIED")
                status_tag = f" [{tag_word}]"
            else:
                badge = "🔴"
                tag_word = engine_t("cli.status.diverged", lang=lang, default="РАСХОЖДЕНИЕ" if lang == "ru" else "DIVERGED")
                status_tag = f" [{tag_word}]"

            lines.append(f"{badge} {name} v{ver} ({mid}){status_tag}")
            if desc:
                lines.append(f"   {desc}")
            lines.append("")

        if lines[-1] == "":
            lines.pop()

        lines.append("=" * 80)
        return "\n".join(lines)

    def get_mod_info(self, mod: Mod, lang: str = "auto") -> Dict[str, Any]:
        """Returns detailed metadata dictionary for a specific mod."""
        state_mgr = StateManager(self.mods_dir / ".state.json")
        overrides = state_mgr.load().get("overrides", {})
        is_ovr = mod.id in overrides and "enabled" in overrides[mod.id]
        is_sys = is_system_mod(mod, self.mods_dir)
        mod_status = self.get_mod_status(mod)
        masked_cfg = self.config_mgr.get_masked_config(mod) if mod.config else {}
        running_monitors = self.monitor_mgr.get_running_monitors()

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
            mon_key = self.monitor_mgr.get_monitor_key(mod.id, m_spec.id)
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

        return {
            "id": mod.id,
            "name": mod.get_name(lang),
            "name_en": mod.name,
            "version": mod.version,
            "author": mod.author or ("Engine" if is_sys else "User"),
            "enabled": mod.enabled,
            "status": mod_status,
            "display_status": ("ENABLED*\"" if is_ovr else "ENABLED") if mod.enabled else ("DISABLED*" if is_ovr else "DISABLED"),
            "type": "folder" if mod.is_folder else "single",
            "path": str(mod.path),
            "description": mod.get_description(lang),
            "description_en": mod.description,
            "dependencies": mod.dependencies,
            "targets_count": len(mod.targets),
            "targets": targets_data,
            "commands": commands_data,
            "config": config_data,
            "monitors": monitors_data,
            "skills": skills_data,
            "tests": tests_data,
        }

    def format_info_text(self, info: Dict[str, Any], lang: str = "auto") -> str:
        """Formats detailed mod card strictly adhering to clean output conventions."""
        lines: List[str] = []
        is_folder = info.get("type") == "folder"
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

        mod_name = info.get("name") or info.get("id")

        lines.append(f"{lbl_mod}: {mod_name} ({info['id']}) v{info.get('version', '1.0.0')}")
        if info.get("author") and str(info["author"]).strip():
            lines.append(f"{lbl_author}: {info['author']}")

        if not info.get("enabled"):
            status_word = engine_t("cli.status.disabled", lang=lang, default="ОТКЛЮЧЕН" if lang == "ru" else "DISABLED")
            status_label = f"[{status_word}]"
            lines.append(f"{lbl_status}: {status_label}")

        lines.append(f"{lbl_type}: {type_str}")
        lines.append(f"{lbl_path}: {info['path']}")

        desc = info.get("description")
        if desc:
            lines.append(f"\n{lbl_desc}:\n  {desc}")

        deps = info.get("dependencies") or []
        if deps:
            lines.append(f"\n{lbl_deps}:\n  • {', '.join(deps)}")

        if info.get("targets_count", 0) > 0:
            lines.append(f"\n{lbl_targets}: {info['targets_count']}")

        commands = info.get("commands") or []
        if commands:
            lines.append(f"\n{lbl_commands}:")
            for c in commands:
                cmd_desc = c.get("description") or ""
                aliases = c.get("aliases") or []
                alias_part = f" (псевдонимы: {', '.join('/' + a for a in aliases)})" if aliases else ""
                args_part = f" {c['args_hint']}" if c.get("args_hint") else ""
                policy = c.get("busy_policy", "reject")
                policy_part = f" [режим: {policy}]" if policy != "reject" else ""
                lines.append(f"  • /{c['name']}{args_part}{alias_part}{policy_part} — {cmd_desc}")

        config = info.get("config") or {}
        if config:
            lines.append(f"\n{lbl_config}:")
            for k, f in config.items():
                val = f.get("value")
                val_display = str(val) if val is not None else ("(не задано)" if lang == "ru" else "(not set)")
                sec_mark = ", секрет" if f.get("sensitive") and lang == "ru" else (", secret" if f.get("sensitive") else "")
                f_desc = f.get("description") or ""
                desc_part = f" — {f_desc}" if f_desc else ""
                curr_lbl = "текущее" if lang == "ru" else "current"
                lines.append(f"  • {k} ({f.get('type')}, default: {f.get('default')}{sec_mark}, {curr_lbl}: {val_display}){desc_part}")

        monitors = info.get("monitors") or []
        if monitors:
            lines.append(f"\n{lbl_monitors}:")
            for m_spec in monitors:
                mon_status = m_spec.get("status", "STOPPED")
                m_desc = m_spec.get("description") or ""
                desc_part = f" — {m_desc}" if m_desc else ""
                lines.append(f"  • {m_spec['id']} (интервал: {m_spec.get('interval')}s, статус: {mon_status}){desc_part}")

        skills = info.get("skills") or []
        if skills:
            lines.append(f"\n{lbl_skills}:")
            for s in skills:
                lines.append(f"  • {s}")

        tests = info.get("tests") or {}
        if tests.get("count", 0) > 0:
            method = tests.get("method", "inline tests")
            lines.append(f"\n{lbl_tests}:")
            lines.append(f"  • {tests.get('count')} ({method})")

        return "\n".join(lines)

    def get_ecosystem_status(
        self,
        mods: Optional[List[Mod]] = None,
        lang: str = "auto",
    ) -> Dict[str, Any]:
        """Calculates comprehensive activity and health summary of the mods ecosystem."""
        if mods is None:
            mods = ModLoader.load_all(self.mods_dir)

        master_enabled = self.config_mgr.is_mods_enabled()
        graph = ModDependencyGraph(mods)

        # DAG and dependency validation
        dag_clean = True
        dag_error = None
        try:
            graph.detect_cycles()
        except CycleDetectionError as exc:
            dag_clean = False
            dag_error = str(exc)

        missing_deps: Dict[str, List[str]] = {}
        disabled_deps: Dict[str, List[str]] = {}
        for m in mods:
            if m.enabled:
                miss = graph.get_missing_dependencies(m)
                if miss:
                    missing_deps[m.id] = miss
                dis = [d.id for d in graph.get_disabled_dependencies(m)]
                if dis:
                    disabled_deps[m.id] = dis

        # Core health
        core_exists = self.repo_root.is_dir()
        core_writable = os.access(self.repo_root, os.W_OK) if core_exists else False
        core_version = get_hermes_version(self.repo_root) if core_exists else "unknown"

        # Secrets health
        secrets_exists = self.secrets_file.exists()
        secrets_valid = True
        secrets_perm = "missing"
        if secrets_exists:
            mode = stat.S_IMODE(self.secrets_file.stat().st_mode)
            secrets_perm = oct(mode)
            secrets_valid = (mode == 0o600)

        # Runtime subsystems
        running_monitors = self.monitor_mgr.get_running_monitors()
        running_cnt = sum(1 for m in running_monitors.values() if m.get("status") == "RUNNING")
        total_monitors = sum(len(m.monitors) for m in mods if m.enabled)
        active_commands = sum(len(m.commands) for m in mods if m.enabled)
        active_mcp = sum(len(m.mcp_servers) for m in mods if m.enabled)
        active_skills = sum(len(m.list_skills()) for m in mods if m.enabled and m.skills_dir)

        # Mod classifications
        applied_mods: List[str] = []
        disabled_mods: List[str] = []
        not_applied_mods: List[str] = []
        diverged_mods: List[str] = []
        system_mods_cnt = 0
        custom_mods_cnt = 0

        for m in mods:
            if is_system_mod(m, self.mods_dir):
                system_mods_cnt += 1
            else:
                custom_mods_cnt += 1

            st = self.get_mod_status(m)
            if st == "applied":
                applied_mods.append(m.id)
            elif st == "disabled":
                disabled_mods.append(m.id)
            elif st == "not_applied":
                not_applied_mods.append(m.id)
            else:
                diverged_mods.append(m.id)

        # Overall health rating
        if not master_enabled:
            overall_status = "DISABLED"
            healthy = False
        elif not dag_clean or not_applied_mods or diverged_mods or missing_deps or not secrets_valid:
            overall_status = "DEGRADED"
            healthy = False
        else:
            overall_status = "HEALTHY"
            healthy = True

        return {
            "overall_status": overall_status,
            "healthy": healthy,
            "engine_version": ENGINE_VERSION,
            "master_enabled": master_enabled,
            "counts": {
                "total": len(mods),
                "enabled": len(mods) - len(disabled_mods),
                "applied": len(applied_mods),
                "disabled": len(disabled_mods),
                "not_applied": len(not_applied_mods),
                "diverged": len(diverged_mods),
                "system": system_mods_cnt,
                "custom": custom_mods_cnt,
            },
            "runtime": {
                "commands": active_commands,
                "monitors_running": running_cnt,
                "monitors_total": total_monitors,
                "mcp_servers": active_mcp,
                "skills": active_skills,
            },
            "integrity": {
                "dag_clean": dag_clean,
                "dag_error": dag_error,
                "missing_dependencies": missing_deps,
                "disabled_dependencies": disabled_deps,
                "secrets_valid": secrets_valid,
                "secrets_permissions": secrets_perm,
                "core_version": core_version,
                "core_writable": core_writable,
            },
            "mod_ids": {
                "applied": applied_mods,
                "disabled": disabled_mods,
                "not_applied": not_applied_mods,
                "diverged": diverged_mods,
            },
        }

    def format_status_text(self, status: Dict[str, Any], lang: str = "auto") -> str:
        """Formats the ecosystem status into a comprehensive readable dashboard."""
        st = status.get("overall_status", "UNKNOWN")
        if st == "HEALTHY":
            health_badge = "🟢 HEALTHY"
        elif st == "DEGRADED":
            health_badge = "🟡 DEGRADED"
        else:
            health_badge = "🔴 DISABLED"

        master_en = status.get("master_enabled", True)
        master_badge = "🟢 ACTIVE (mods.enabled: true)" if master_en else "🔴 DISABLED (mods.enabled: false)"

        c = status.get("counts", {})
        r = status.get("runtime", {})
        integ = status.get("integrity", {})
        mod_ids = status.get("mod_ids", {})

        lines: List[str] = []
        lines.append("=" * 80)
        lines.append(" Hermes Mods Ecosystem Status")
        lines.append("=" * 80)
        lines.append(f"Overall Health:       {health_badge}")
        lines.append(f"Master Killswitch:    {master_badge}")
        lines.append(f"Engine Version:       v{status.get('engine_version', ENGINE_VERSION)}")
        lines.append("")
        lines.append("📊 Mods Overview:")
        lines.append(f"  • Total registered: {c.get('total', 0)} ({c.get('custom', 0)} custom, {c.get('system', 0)} system)")
        lines.append(f"  • Applied & Active: {c.get('applied', 0)}")
        lines.append(f"  • Disabled:         {c.get('disabled', 0)}")
        lines.append(f"  • Not Applied:      {c.get('not_applied', 0)}")
        if c.get("diverged", 0) > 0:
            lines.append(f"  • Diverged/Conflict:{c.get('diverged', 0)}")

        lines.append("")
        lines.append("⚙️ Runtime Subsystems:")
        lines.append(f"  • Slash Commands:   {r.get('commands', 0)} registered")
        lines.append(f"  • Monitors:         {r.get('monitors_running', 0)} running ({r.get('monitors_total', 0)} defined)")
        lines.append(f"  • MCP Servers:      {r.get('mcp_servers', 0)} active")
        lines.append(f"  • Folder Skills:    {r.get('skills', 0)} active")

        lines.append("")
        lines.append("🛡️ System Integrity:")
        dag_msg = "Clean (0 cycles)" if integ.get("dag_clean") else f"Cycle detected ({integ.get('dag_error')})"
        lines.append(f"  • Dependency DAG:   {dag_msg}")
        sec_msg = "Valid (0600)" if integ.get("secrets_valid") else f"Invalid ({integ.get('secrets_permissions')})"
        lines.append(f"  • Secrets Storage:  {sec_msg}")
        core_msg = f"v{integ.get('core_version', 'unknown')} ({'writable' if integ.get('core_writable') else 'read-only'})"
        lines.append(f"  • Hermes Core:      {core_msg}")

        if mod_ids.get("disabled"):
            lines.append("")
            lines.append(f"ℹ️  Disabled mods ({len(mod_ids['disabled'])}): {', '.join(mod_ids['disabled'])}")

        if mod_ids.get("not_applied"):
            lines.append("")
            lines.append(f"⚠️  Not applied mods ({len(mod_ids['not_applied'])}): {', '.join(mod_ids['not_applied'])}")

        if integ.get("missing_dependencies"):
            lines.append("")
            lines.append("⚠️  Missing dependencies:")
            for mid, miss in integ["missing_dependencies"].items():
                lines.append(f"  • {mid} -> {', '.join(miss)}")

        lines.append("=" * 80)
        return "\n".join(lines)

    def get_mod_logs(
        self,
        mod: Mod,
        lines: int = 50,
        log_dir: Optional[Path] = None,
        check_journal: bool = True,
        lang: str = "auto",
    ) -> Dict[str, Any]:
        """Collects the most recent log entries pertaining to a specific mod."""
        lines_limit = max(1, lines)
        active_log_dir = Path(log_dir) if log_dir else self.log_dir

        # Build search terms for this mod
        search_terms: Set[str] = {mod.id.lower(), mod.id.replace("-", "_").lower()}
        for c in mod.commands:
            search_terms.add(f"/{c.name}".lower())
            for a in c.aliases:
                search_terms.add(f"/{a}".lower())
        for m_spec in mod.monitors:
            search_terms.add(m_spec.id.lower())

        sources_checked: List[str] = []
        collected_lines: List[str] = []

        # 1. Check mod monitor logs
        monitor_log_dir = Path.home() / ".hermes" / "cache" / "monitors" / mod.id / "logs"
        if monitor_log_dir.is_dir():
            for lf in sorted(monitor_log_dir.glob("*.log")):
                sources_checked.append(str(lf))
                for line in read_file_tail(lf, max_lines=lines_limit * 2):
                    collected_lines.append(line)

        # 2. Check Hermes log files dynamically
        hermes_log_files: List[Path] = []
        if active_log_dir.is_dir():
            for f in sorted(active_log_dir.glob("*.log")):
                if f.is_file():
                    hermes_log_files.append(f)
        parent_log_dir = active_log_dir.parent
        if parent_log_dir.is_dir() and parent_log_dir != active_log_dir:
            for f in sorted(parent_log_dir.glob("*.log")):
                if f.is_file() and f not in hermes_log_files:
                    hermes_log_files.append(f)

        term_patterns = [re.compile(re.escape(term), re.IGNORECASE) for term in search_terms]

        for hlf in hermes_log_files:
            if hlf.is_file():
                sources_checked.append(str(hlf))
                tail_lines = read_file_tail(hlf, max_lines=2000)
                for line in tail_lines:
                    line_lower = line.lower()
                    if any(p.search(line_lower) for p in term_patterns):
                        collected_lines.append(line)

        # 3. Check systemd journal if requested
        disable_journal = os.environ.get("HERMES_NO_JOURNAL", "").lower() in ("1", "true", "yes")
        if check_journal and not disable_journal and shutil.which("journalctl"):
            sources_checked.append("journalctl -u hermes-gateway")
            try:
                res = subprocess.run(
                    ["journalctl", "-u", "hermes-gateway", "-n", "500", "--no-pager"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                if res.returncode == 0 and res.stdout:
                    for line in res.stdout.splitlines():
                        line_lower = line.lower()
                        if any(p.search(line_lower) for p in term_patterns):
                            collected_lines.append(line)
            except Exception:
                pass

        # Deduplicate consecutive identical lines while preserving order
        deduped: List[str] = []
        for line in collected_lines:
            if not deduped or deduped[-1] != line:
                deduped.append(line)

        selected_lines = deduped[-lines_limit:]

        return {
            "mod_id": mod.id,
            "mod_name": mod.get_name(lang),
            "version": mod.version,
            "lines_count": len(selected_lines),
            "lines": selected_lines,
            "sources_checked": sources_checked,
            "search_terms": sorted(list(search_terms)),
        }

    def format_logs_text(self, logs_data: Dict[str, Any], lang: str = "auto") -> str:
        """Formats mod log entries into human-readable text output."""
        lines_list = logs_data.get("lines", [])
        mod_name = logs_data.get("mod_name", logs_data.get("mod_id"))
        mod_id = logs_data.get("mod_id")

        lines: List[str] = []
        lines.append("=" * 80)
        lines.append(f" Логи мода: {mod_name} ({mod_id}) [показано строк: {len(lines_list)}]" if lang == "ru" else f" Logs for mod: {mod_name} ({mod_id}) [lines shown: {len(lines_list)}]")
        lines.append("=" * 80)

        if not lines_list:
            if lang == "ru":
                lines.append(f"ℹ️  Записей логов для мода '{mod_id}' не найдено.")
                lines.append("   Проверенные источники:")
            else:
                lines.append(engine_t("cli.logs.empty", lang=lang, mod_id=mod_id, default=f"ℹ️  No log entries found for mod '{mod_id}'."))
                lines.append(engine_t("cli.logs.checked_sources", lang=lang, default="   Checked sources:"))
            for src in logs_data.get("sources_checked", []):
                lines.append(f"   • {src}")
            lines.append("=" * 80)
            return "\n".join(lines)

        for l in lines_list:
            lines.append(l)

        lines.append("=" * 80)
        return "\n".join(lines)
