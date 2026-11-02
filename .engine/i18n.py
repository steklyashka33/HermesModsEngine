"""Hermes Mod Engine Unified i18n Subsystem.
Single unified localization engine for Hermes mods, engine internals, and CLI interfaces.
Supports the 8 official Hermes languages:
  en - English (Base / Default)
  ru - Russian
  es - Spanish
  fr - French
  zh - Chinese (Simplified)
  ar - Arabic
  de - German
  pt - Portuguese
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

OFFICIAL_LANGUAGES: tuple[str, ...] = ("en", "es", "fr", "zh", "ar", "ru", "de", "pt")
DEFAULT_LANGUAGE: str = "en"
DEFAULT_CLI_LANGUAGE: str = "ru"

# Mapping aliases and regional language tags to canonical 8 languages or base codes
LANGUAGE_ALIASES: dict[str, str] = {
    "english": "en",
    "en-us": "en",
    "en-gb": "en",
    "russian": "ru",
    "русский": "ru",
    "ru-ru": "ru",
    "spanish": "es",
    "español": "es",
    "es-es": "es",
    "es-mx": "es",
    "es-ar": "es",
    "french": "fr",
    "français": "fr",
    "fr-fr": "fr",
    "fr-ca": "fr",
    "chinese": "zh",
    "mandarin": "zh",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-sg": "zh",
    "arabic": "ar",
    "العربية": "ar",
    "ar-sa": "ar",
    "ar-eg": "ar",
    "ar-ae": "ar",
    "german": "de",
    "deutsch": "de",
    "de-de": "de",
    "de-at": "de",
    "de-ch": "de",
    "portuguese": "pt",
    "português": "pt",
    "pt-pt": "pt",
    "pt-br": "pt",
    "brazilian": "pt",
}

_LANG_ID_RE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})*$")


def normalize_language_id(value: Any) -> str:
    """Canonical spelling of a language id: lowercase, '_' -> '-', trimmed."""
    if not value or not isinstance(value, str):
        return ""
    val = value.strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(val, val)


def is_language_id(value: Any) -> bool:
    """Check if value looks like a valid BCP 47 language code."""
    norm = normalize_language_id(value)
    return bool(_LANG_ID_RE.match(norm))


def is_supported_language(value: Any) -> bool:
    """Check if normalized language is in the official 8 languages."""
    return normalize_language_id(value) in OFFICIAL_LANGUAGES


def resolve_language(
    lang_arg: Optional[str] = None,
    config_path: Optional[Path] = None,
    default: str = "ru",
) -> str:
    """Resolve language code with fallback cascade:
    1. CLI argument (--lang <code/auto>)
    2. config.yaml (display.language or language)
    3. HERMES_LANGUAGE env var
    4. fallback default ('ru')
    """
    if lang_arg:
        cleaned = str(lang_arg).strip().lower().replace("_", "-")
        if cleaned not in ("auto", "none", ""):
            return cleaned

    # 2. Try config.yaml
    if config_path and Path(config_path).is_file():
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if isinstance(data, dict):
                display_sec = data.get("display")
                if isinstance(display_sec, dict) and display_sec.get("language"):
                    cand = str(display_sec["language"]).strip().lower().replace("_", "-")
                    if cand:
                        return cand
                if data.get("language"):
                    cand = str(data["language"]).strip().lower().replace("_", "-")
                    if cand:
                        return cand
        except Exception:
            pass

    # 3. Try environment variable HERMES_LANGUAGE
    env_lang = os.environ.get("HERMES_LANGUAGE")
    if env_lang and env_lang.strip():
        cand = env_lang.strip().lower().replace("_", "-")
        if cand:
            return cand

    return default


def flatten_i18n_dict(node: Any, prefix: str = "", out: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Recursively flatten nested dictionary into {dotted.key: text}."""
    flat: Dict[str, str] = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            k_str = str(k).strip()
            flatten_i18n_dict(v, f"{prefix}.{k_str}" if prefix else k_str, flat)
    elif isinstance(node, str):
        if prefix:
            flat[prefix] = node
    elif node is not None:
        if prefix:
            flat[prefix] = str(node)
    return flat


# Built-in 8-language translations for Engine CLI messages, labels, and status badges
ENGINE_CLI_CATALOG: Dict[str, Dict[str, str]] = {
    "en": {
        "cli.status.applied": "APPLIED",
        "cli.status.disabled": "DISABLED",
        "cli.status.not_applied": "NOT APPLIED",
        "cli.status.diverged": "DIVERGED",
        "cli.status.healthy": "HEALTHY",
        "cli.status.degraded": "DEGRADED",
        "cli.status.stopped": "STOPPED",
        "cli.status.running": "RUNNING",
        "cli.list.empty": "  (no mods matching filter)",
        "cli.list.header": "Hermes Mods ({total} total | {applied} applied | {disabled} disabled)",
        "cli.commands.registered": "Registered slash commands: {count}\n",
        "cli.commands.col.command": "COMMAND",
        "cli.commands.col.mod": "MOD",
        "cli.commands.col.category": "CATEGORY",
        "cli.commands.col.mode": "MODE",
        "cli.commands.col.args": "ARGUMENTS",
        "cli.commands.col.description": "DESCRIPTION",
        "cli.info.mod": "Mod",
        "cli.info.author": "Author",
        "cli.info.status": "Status",
        "cli.info.type": "Type",
        "cli.info.type_folder": "Folder",
        "cli.info.type_single": "Single-file",
        "cli.info.path": "Path",
        "cli.info.description": "Description",
        "cli.info.dependencies": "Dependencies",
        "cli.info.targets": "Targets",
        "cli.info.commands": "Commands",
        "cli.info.config": "Configuration",
        "cli.info.monitors": "Monitors",
        "cli.info.skills": "Skills",
        "cli.info.tests": "Tests",
        "cli.info.current": "current",
        "cli.info.secret": "secret",
        "cli.info.not_set": "(not set)",
        "cli.info.aliases": "aliases",
        "cli.info.mode": "mode",
        "cli.info.not_found": "Mod '{target}' not found",
        "cli.status.title": "Hermes Mods Ecosystem Status",
        "cli.status.overall_health": "Overall Health",
        "cli.status.master_killswitch": "Master Killswitch",
        "cli.status.engine_version": "Engine Version",
        "cli.status.mods_overview": "Mods Overview",
        "cli.status.runtime_subsystems": "Runtime Subsystems",
        "cli.status.system_integrity": "System Integrity",
        "cli.status.total_registered": "Total registered",
        "cli.status.applied_active": "Applied & Active",
        "cli.status.disabled_count": "Disabled",
        "cli.status.not_applied_count": "Not Applied",
        "cli.status.diverged_count": "Diverged/Conflict",
        "cli.status.slash_commands": "Slash Commands",
        "cli.status.monitors": "Monitors",
        "cli.status.mcp_servers": "MCP Servers",
        "cli.status.folder_skills": "Folder Skills",
        "cli.status.dependency_dag": "Dependency DAG",
        "cli.status.secrets_storage": "Secrets Storage",
        "cli.status.hermes_core": "Hermes Core",
        "cli.status.dag_clean": "Clean (0 cycles)",
        "cli.status.secrets_valid": "Valid (0600)",
        "cli.status.writable": "writable",
        "cli.status.read_only": "read-only",
        "cli.logs.header": "Logs for mod: {mod_name} ({mod_id}) [lines shown: {count}]",
        "cli.logs.empty": "ℹ️  No log entries found for mod '{mod_id}'.",
        "cli.logs.checked_sources": "   Checked sources:",
        "cli.cron.title": "HermesModsEngine Auto-Update Cron Status",
        "cli.cron.installed": "ACTIVE (Scheduled in user crontab)",
        "cli.cron.not_installed": "NOT INSTALLED (Inactive)",
        "cli.cron.schedule": "Schedule",
        "cli.cron.command": "Command",
        "cli.cron.log_file": "Log File",
        "cli.cron.recent_logs": "Recent Log Output:",
    },
    "ru": {
        "cli.status.applied": "ПРИМЕНЕН",
        "cli.status.disabled": "ОТКЛЮЧЕН",
        "cli.status.not_applied": "НЕ ПРИМЕНЕН",
        "cli.status.diverged": "РАСХОЖДЕНИЕ",
        "cli.status.healthy": "ИСПРАВНО",
        "cli.status.degraded": "ДЕГРАДИРОВАНО",
        "cli.status.stopped": "ОСТАНОВЛЕН",
        "cli.status.running": "РАБОТАЕТ",
        "cli.list.empty": "  (нет модов, соответствующих критерию)",
        "cli.list.header": "Hermes Mods ({total} total | {applied} applied | {disabled} disabled)",
        "cli.commands.registered": "Зарегистрировано слэш-команд: {count}\n",
        "cli.commands.col.command": "КОМАНДА",
        "cli.commands.col.mod": "МОД",
        "cli.commands.col.category": "КАТЕГОРИЯ",
        "cli.commands.col.mode": "РЕЖИМ",
        "cli.commands.col.args": "АРГУМЕНТЫ",
        "cli.commands.col.description": "ОПИСАНИЕ",
        "cli.info.mod": "Мод",
        "cli.info.author": "Автор",
        "cli.info.status": "Статус",
        "cli.info.type": "Тип",
        "cli.info.type_folder": "Папочный",
        "cli.info.type_single": "Однофайловый",
        "cli.info.path": "Путь",
        "cli.info.description": "Описание",
        "cli.info.dependencies": "Зависимости",
        "cli.info.targets": "Цели",
        "cli.info.commands": "Команды",
        "cli.info.config": "Конфигурация",
        "cli.info.monitors": "Мониторы",
        "cli.info.skills": "Скиллы",
        "cli.info.tests": "Тесты",
        "cli.info.current": "текущее",
        "cli.info.secret": "секрет",
        "cli.info.not_set": "(не задано)",
        "cli.info.aliases": "псевдонимы",
        "cli.info.mode": "режим",
        "cli.info.not_found": "Мод '{target}' не найден",
        "cli.status.title": "Hermes Mods Ecosystem Status",
        "cli.status.overall_health": "Overall Health",
        "cli.status.master_killswitch": "Master Killswitch",
        "cli.status.engine_version": "Engine Version",
        "cli.status.mods_overview": "Mods Overview",
        "cli.status.runtime_subsystems": "Runtime Subsystems",
        "cli.status.system_integrity": "System Integrity",
        "cli.status.total_registered": "Total registered",
        "cli.status.applied_active": "Applied & Active",
        "cli.status.disabled_count": "Disabled",
        "cli.status.not_applied_count": "Not Applied",
        "cli.status.diverged_count": "Diverged/Conflict",
        "cli.status.slash_commands": "Slash Commands",
        "cli.status.monitors": "Monitors",
        "cli.status.mcp_servers": "MCP Servers",
        "cli.status.folder_skills": "Folder Skills",
        "cli.status.dependency_dag": "Dependency DAG",
        "cli.status.secrets_storage": "Secrets Storage",
        "cli.status.hermes_core": "Hermes Core",
        "cli.status.dag_clean": "Clean (0 cycles)",
        "cli.status.secrets_valid": "Valid (0600)",
        "cli.status.writable": "writable",
        "cli.status.read_only": "read-only",
        "cli.logs.header": " Логи мода: {mod_name} ({mod_id}) [показано строк: {count}]",
        "cli.logs.empty": "ℹ️  Записей логов для мода '{mod_id}' не найдено.",
        "cli.logs.checked_sources": "   Проверенные источники:",
        "cli.cron.title": "Статус автообновления HermesModsEngine (Cron)",
        "cli.cron.installed": "АКТИВНО (Запланировано в crontab)",
        "cli.cron.not_installed": "НЕ УСТАНОВЛЕНО (Неактивно)",
        "cli.cron.schedule": "Расписание",
        "cli.cron.command": "Команда",
        "cli.cron.log_file": "Файл лога",
        "cli.cron.recent_logs": "Последние записи лога:",
    },
    "es": {
        "cli.status.applied": "APLICADO",
        "cli.status.disabled": "DESACTIVADO",
        "cli.status.not_applied": "NO APLICADO",
        "cli.status.diverged": "DISCREPANCIA",
        "cli.status.healthy": "SALUDABLE",
        "cli.status.degraded": "DEGRADADO",
        "cli.status.stopped": "DETENIDO",
        "cli.status.running": "EN EJECUCIÓN",
        "cli.list.empty": "  (no hay mods que coincidan con el filtro)",
        "cli.list.header": "Hermes Mods ({total} total | {applied} aplicados | {disabled} desactivados)",
        "cli.commands.registered": "Comandos slash registrados: {count}\n",
        "cli.commands.col.command": "COMANDO",
        "cli.commands.col.mod": "MOD",
        "cli.commands.col.category": "CATEGORÍA",
        "cli.commands.col.mode": "MODO",
        "cli.commands.col.args": "ARGUMENTOS",
        "cli.commands.col.description": "DESCRIPCIÓN",
        "cli.info.mod": "Mod",
        "cli.info.author": "Autor",
        "cli.info.status": "Estado",
        "cli.info.type": "Tipo",
        "cli.info.type_folder": "Carpeta",
        "cli.info.type_single": "Archivo único",
        "cli.info.path": "Ruta",
        "cli.info.description": "Descripción",
        "cli.info.dependencies": "Dependencias",
        "cli.info.targets": "Objetivos",
        "cli.info.commands": "Comandos",
        "cli.info.config": "Configuración",
        "cli.info.monitors": "Monitores",
        "cli.info.skills": "Habilidades",
        "cli.info.tests": "Pruebas",
        "cli.info.current": "actual",
        "cli.info.secret": "secreto",
        "cli.info.not_set": "(no establecido)",
        "cli.info.aliases": "alias",
        "cli.info.mode": "modo",
        "cli.info.not_found": "Mod '{target}' no encontrado",
        "cli.status.title": "Estado del ecosistema de mods de Hermes",
        "cli.status.overall_health": "Estado general",
        "cli.status.master_killswitch": "Interruptor maestro",
        "cli.status.engine_version": "Versión del motor",
        "cli.status.mods_overview": "Resumen de mods",
        "cli.status.runtime_subsystems": "Subsistemas en ejecución",
        "cli.status.system_integrity": "Integridad del sistema",
        "cli.status.total_registered": "Total registrados",
        "cli.status.applied_active": "Aplicados y activos",
        "cli.status.disabled_count": "Desactivados",
        "cli.status.not_applied_count": "No aplicados",
        "cli.status.diverged_count": "Discrepancia/Conflicto",
        "cli.status.slash_commands": "Comandos slash",
        "cli.status.monitors": "Monitores",
        "cli.status.mcp_servers": "Servidores MCP",
        "cli.status.folder_skills": "Habilidades de carpeta",
        "cli.status.dependency_dag": "DAG de dependencias",
        "cli.status.secrets_storage": "Almacenamiento de secretos",
        "cli.status.hermes_core": "Núcleo de Hermes",
        "cli.status.dag_clean": "Limpio (0 ciclos)",
        "cli.status.secrets_valid": "Válido (0600)",
        "cli.status.writable": "escribible",
        "cli.status.read_only": "solo lectura",
        "cli.logs.header": "Registros del mod: {mod_name} ({mod_id}) [líneas mostradas: {count}]",
        "cli.logs.empty": "ℹ️  No se encontraron registros para el mod '{mod_id}'.",
        "cli.logs.checked_sources": "   Fuentes comprobadas:",
        "cli.cron.title": "Estado de actualización automática HermesModsEngine (Cron)",
        "cli.cron.installed": "ACTIVO (Programado en crontab de usuario)",
        "cli.cron.not_installed": "NO INSTALADO (Inactivo)",
        "cli.cron.schedule": "Horario",
        "cli.cron.command": "Comando",
        "cli.cron.log_file": "Archivo de registro",
        "cli.cron.recent_logs": "Salida de registro reciente:",
    },
    "fr": {
        "cli.status.applied": "APPLIQUÉ",
        "cli.status.disabled": "DÉSACTIVÉ",
        "cli.status.not_applied": "NON APPLIQUÉ",
        "cli.status.diverged": "DIVERGENCE",
        "cli.status.healthy": "SAIN",
        "cli.status.degraded": "DÉGRADÉ",
        "cli.status.stopped": "ARRÊTÉ",
        "cli.status.running": "EN COURS",
        "cli.list.empty": "  (aucun mod correspondant au filtre)",
        "cli.list.header": "Hermes Mods ({total} total | {applied} appliqués | {disabled} désactivés)",
        "cli.commands.registered": "Commandes slash enregistrées: {count}\n",
        "cli.commands.col.command": "COMMANDE",
        "cli.commands.col.mod": "MOD",
        "cli.commands.col.category": "CATÉGORIE",
        "cli.commands.col.mode": "MODE",
        "cli.commands.col.args": "ARGUMENTS",
        "cli.commands.col.description": "DESCRIPTION",
        "cli.info.mod": "Mod",
        "cli.info.author": "Auteur",
        "cli.info.status": "Statut",
        "cli.info.type": "Type",
        "cli.info.type_folder": "Dossier",
        "cli.info.type_single": "Fichier unique",
        "cli.info.path": "Chemin",
        "cli.info.description": "Description",
        "cli.info.dependencies": "Dépendances",
        "cli.info.targets": "Cibles",
        "cli.info.commands": "Commandes",
        "cli.info.config": "Configuration",
        "cli.info.monitors": "Moniteurs",
        "cli.info.skills": "Compétences",
        "cli.info.tests": "Tests",
        "cli.info.current": "actuel",
        "cli.info.secret": "secret",
        "cli.info.not_set": "(non défini)",
        "cli.info.aliases": "alias",
        "cli.info.mode": "mode",
        "cli.info.not_found": "Mod '{target}' introuvable",
        "cli.status.title": "État de l'écosystème des mods Hermes",
        "cli.status.overall_health": "Santé globale",
        "cli.status.master_killswitch": "Interrupteur général",
        "cli.status.engine_version": "Version du moteur",
        "cli.status.mods_overview": "Aperçu des mods",
        "cli.status.runtime_subsystems": "Sous-systèmes en exécution",
        "cli.status.system_integrity": "Intégrité du système",
        "cli.status.total_registered": "Total enregistrés",
        "cli.status.applied_active": "Appliqués et actifs",
        "cli.status.disabled_count": "Désactivés",
        "cli.status.not_applied_count": "Non appliqués",
        "cli.status.diverged_count": "Divergence/Conflit",
        "cli.status.slash_commands": "Commandes slash",
        "cli.status.monitors": "Moniteurs",
        "cli.status.mcp_servers": "Serveurs MCP",
        "cli.status.folder_skills": "Compétences de dossier",
        "cli.status.dependency_dag": "DAG des dépendances",
        "cli.status.secrets_storage": "Stockage des secrets",
        "cli.status.hermes_core": "Noyau Hermes",
        "cli.status.dag_clean": "Propre (0 cycle)",
        "cli.status.secrets_valid": "Valide (0600)",
        "cli.status.writable": "inscriptible",
        "cli.status.read_only": "lecture seule",
        "cli.logs.header": "Journaux du mod: {mod_name} ({mod_id}) [lignes affichées: {count}]",
        "cli.logs.empty": "ℹ️  Aucun journal trouvé pour le mod '{mod_id}'.",
        "cli.logs.checked_sources": "   Sources vérifiées:",
        "cli.cron.title": "Statut de mise à jour automatique HermesModsEngine (Cron)",
        "cli.cron.installed": "ACTIF (Planifié dans le crontab utilisateur)",
        "cli.cron.not_installed": "NON INSTALLÉ (Inactif)",
        "cli.cron.schedule": "Calendrier",
        "cli.cron.command": "Commande",
        "cli.cron.log_file": "Fichier journal",
        "cli.cron.recent_logs": "Sortie récente du journal:",
    },
    "zh": {
        "cli.status.applied": "已应用",
        "cli.status.disabled": "已禁用",
        "cli.status.not_applied": "未应用",
        "cli.status.diverged": "存在分歧",
        "cli.status.healthy": "健康",
        "cli.status.degraded": "降级",
        "cli.status.stopped": "已停止",
        "cli.status.running": "运行中",
        "cli.list.empty": "  (没有匹配筛选条件的模组)",
        "cli.list.header": "Hermes 模组 ({total} 总计 | {applied} 已应用 | {disabled} 已禁用)",
        "cli.commands.registered": "已注册斜杠命令: {count}\n",
        "cli.commands.col.command": "命令",
        "cli.commands.col.mod": "模组",
        "cli.commands.col.category": "类别",
        "cli.commands.col.mode": "模式",
        "cli.commands.col.args": "参数",
        "cli.commands.col.description": "描述",
        "cli.info.mod": "模组",
        "cli.info.author": "作者",
        "cli.info.status": "状态",
        "cli.info.type": "类型",
        "cli.info.type_folder": "文件夹",
        "cli.info.type_single": "单文件",
        "cli.info.path": "路径",
        "cli.info.description": "描述",
        "cli.info.dependencies": "依赖项",
        "cli.info.targets": "目标",
        "cli.info.commands": "命令",
        "cli.info.config": "配置",
        "cli.info.monitors": "监视器",
        "cli.info.skills": "技能",
        "cli.info.tests": "测试",
        "cli.info.current": "当前值",
        "cli.info.secret": "机密",
        "cli.info.not_set": "(未设置)",
        "cli.info.aliases": "别名",
        "cli.info.mode": "模式",
        "cli.info.not_found": "未找到模组 '{target}'",
        "cli.status.title": "Hermes 模组生态系统状态",
        "cli.status.overall_health": "总体健康状态",
        "cli.status.master_killswitch": "总开关",
        "cli.status.engine_version": "引擎版本",
        "cli.status.mods_overview": "模组概览",
        "cli.status.runtime_subsystems": "运行中子系统",
        "cli.status.system_integrity": "系统完整性",
        "cli.status.total_registered": "总注册数",
        "cli.status.applied_active": "已应用且激活",
        "cli.status.disabled_count": "已禁用",
        "cli.status.not_applied_count": "未应用",
        "cli.status.diverged_count": "分歧/冲突",
        "cli.status.slash_commands": "斜杠命令",
        "cli.status.monitors": "监视器",
        "cli.status.mcp_servers": "MCP 服务器",
        "cli.status.folder_skills": "文件夹技能",
        "cli.status.dependency_dag": "依赖项有向图 (DAG)",
        "cli.status.secrets_storage": "机密存储",
        "cli.status.hermes_core": "Hermes 核心",
        "cli.status.dag_clean": "无环 (0 循环)",
        "cli.status.secrets_valid": "有效 (0600)",
        "cli.status.writable": "可写",
        "cli.status.read_only": "只读",
        "cli.logs.header": "模组日志: {mod_name} ({mod_id}) [显示行数: {count}]",
        "cli.logs.empty": "ℹ️  未找到模组 '{mod_id}' 的日志记录。",
        "cli.logs.checked_sources": "   已检查来源:",
        "cli.cron.title": "HermesModsEngine 自动更新计划状态 (Cron)",
        "cli.cron.installed": "激活 (已配置在用户 crontab 中)",
        "cli.cron.not_installed": "未安装 (未激活)",
        "cli.cron.schedule": "计划周期",
        "cli.cron.command": "执行命令",
        "cli.cron.log_file": "日志文件",
        "cli.cron.recent_logs": "最新日志输出:",
    },
    "ar": {
        "cli.status.applied": "مطبق",
        "cli.status.disabled": "معطل",
        "cli.status.not_applied": "غير مطبق",
        "cli.status.diverged": "تباعد",
        "cli.status.healthy": "سليم",
        "cli.status.degraded": "متدهور",
        "cli.status.stopped": "متوقف",
        "cli.status.running": "قيد التشغيل",
        "cli.list.empty": "  (لا توجد نماذج مطابقة للفلتر)",
        "cli.list.header": "Hermes Mods ({total} إجمالي | {applied} مطبق | {disabled} معطل)",
        "cli.commands.registered": "أوامر الشرطة المائلة المسجلة: {count}\n",
        "cli.commands.col.command": "الأمر",
        "cli.commands.col.mod": "النموذج",
        "cli.commands.col.category": "الفئة",
        "cli.commands.col.mode": "الوضع",
        "cli.commands.col.args": "المعطيات",
        "cli.commands.col.description": "الوصف",
        "cli.info.mod": "النموذج",
        "cli.info.author": "المؤلف",
        "cli.info.status": "الحالة",
        "cli.info.type": "النوع",
        "cli.info.type_folder": "مجلد",
        "cli.info.type_single": "ملف فردي",
        "cli.info.path": "المسار",
        "cli.info.description": "الوصف",
        "cli.info.dependencies": "التبعيات",
        "cli.info.targets": "الأهداف",
        "cli.info.commands": "الأوامر",
        "cli.info.config": "التكوين",
        "cli.info.monitors": "المراقبون",
        "cli.info.skills": "المهارات",
        "cli.info.tests": "الاختبارات",
        "cli.info.current": "الحالي",
        "cli.info.secret": "سري",
        "cli.info.not_set": "(غير محدد)",
        "cli.info.aliases": "الأسماء المستعارة",
        "cli.info.mode": "الوضع",
        "cli.info.not_found": "النموذج '{target}' غير موجود",
        "cli.status.title": "حالة نظام نماذج Hermes",
        "cli.status.overall_health": "الحالة العامة",
        "cli.status.master_killswitch": "مفتاح الإيقاف الرئيسي",
        "cli.status.engine_version": "إصدار المحرك",
        "cli.status.mods_overview": "نظرة عامة على النماذج",
        "cli.status.runtime_subsystems": "الأنظمة الفرعية قيد التشغيل",
        "cli.status.system_integrity": "سلامة النظام",
        "cli.status.total_registered": "إجمالي المسجل",
        "cli.status.applied_active": "المطبقة والنشطة",
        "cli.status.disabled_count": "معطل",
        "cli.status.not_applied_count": "غير مطبق",
        "cli.status.diverged_count": "متباعد/تعارض",
        "cli.status.slash_commands": "أوامر الشرطة المائلة",
        "cli.status.monitors": "المراقبون",
        "cli.status.mcp_servers": "خوادم MCP",
        "cli.status.folder_skills": "مهارات المجلد",
        "cli.status.dependency_dag": "مخطط التبعيات (DAG)",
        "cli.status.secrets_storage": "تخزين الأسرار",
        "cli.status.hermes_core": "نواة Hermes",
        "cli.status.dag_clean": "نظيف (0 دورات)",
        "cli.status.secrets_valid": "صالح (0600)",
        "cli.status.writable": "قابل للكتابة",
        "cli.status.read_only": "للقراءة فقط",
        "cli.logs.header": "سجلات النموذج: {mod_name} ({mod_id}) [السطور المعروضة: {count}]",
        "cli.logs.empty": "ℹ️  لم يتم العثور على سجلات للنموذج '{mod_id}'.",
        "cli.logs.checked_sources": "   المصادر التي تم فحصها:",
        "cli.cron.title": "حالة التحديث التلقائي لـ HermesModsEngine (Cron)",
        "cli.cron.installed": "نشط (مجدول في crontab المستخدم)",
        "cli.cron.not_installed": "غير مثبت (غير نشط)",
        "cli.cron.schedule": "الجدول الزمني",
        "cli.cron.command": "الأمر",
        "cli.cron.log_file": "ملف السجل",
        "cli.cron.recent_logs": "سجل المخرجات الأخير:",
    },
    "de": {
        "cli.status.applied": "ANGEWENDET",
        "cli.status.disabled": "DEAKTIVIERT",
        "cli.status.not_applied": "NICHT ANGEWENDET",
        "cli.status.diverged": "ABWEICHEND",
        "cli.status.healthy": "GESUND",
        "cli.status.degraded": "BEEINTRÄCHTIGT",
        "cli.status.stopped": "GESTOPPT",
        "cli.status.running": "LÄUFT",
        "cli.list.empty": "  (keine Mods entsprechen dem Filter)",
        "cli.list.header": "Hermes Mods ({total} gesamt | {applied} angewendet | {disabled} deaktiviert)",
        "cli.commands.registered": "Registrierte Slash-Befehle: {count}\n",
        "cli.commands.col.command": "BEFEHL",
        "cli.commands.col.mod": "MOD",
        "cli.commands.col.category": "KATEGORIE",
        "cli.commands.col.mode": "MODUS",
        "cli.commands.col.args": "ARGUMENTE",
        "cli.commands.col.description": "BESCHREIBUNG",
        "cli.info.mod": "Mod",
        "cli.info.author": "Autor",
        "cli.info.status": "Status",
        "cli.info.type": "Typ",
        "cli.info.type_folder": "Ordner",
        "cli.info.type_single": "Einzeldatei",
        "cli.info.path": "Pfad",
        "cli.info.description": "Beschreibung",
        "cli.info.dependencies": "Abhängigkeiten",
        "cli.info.targets": "Ziele",
        "cli.info.commands": "Befehle",
        "cli.info.config": "Konfiguration",
        "cli.info.monitors": "Monitore",
        "cli.info.skills": "Fähigkeiten",
        "cli.info.tests": "Tests",
        "cli.info.current": "aktuell",
        "cli.info.secret": "Geheimnis",
        "cli.info.not_set": "(nicht gesetzt)",
        "cli.info.aliases": "Aliase",
        "cli.info.mode": "Modus",
        "cli.info.not_found": "Mod '{target}' nicht gefunden",
        "cli.status.title": "Status des Hermes Mod-Ökosystems",
        "cli.status.overall_health": "Gesamtzustand",
        "cli.status.master_killswitch": "Hauptschalter",
        "cli.status.engine_version": "Engine-Version",
        "cli.status.mods_overview": "Mod-Übersicht",
        "cli.status.runtime_subsystems": "Laufzeit-Subsysteme",
        "cli.status.system_integrity": "Systemintegrität",
        "cli.status.total_registered": "Gesamt registriert",
        "cli.status.applied_active": "Angewendet & Aktiv",
        "cli.status.disabled_count": "Deaktiviert",
        "cli.status.not_applied_count": "Nicht angewendet",
        "cli.status.diverged_count": "Abweichend/Konflikt",
        "cli.status.slash_commands": "Slash-Befehle",
        "cli.status.monitors": "Monitore",
        "cli.status.mcp_servers": "MCP-Server",
        "cli.status.folder_skills": "Ordner-Fähigkeiten",
        "cli.status.dependency_dag": "Abhängigkeits-DAG",
        "cli.status.secrets_storage": "Geheimnisspeicher",
        "cli.status.hermes_core": "Hermes-Kern",
        "cli.status.dag_clean": "Sauber (0 Zyklen)",
        "cli.status.secrets_valid": "Gültig (0600)",
        "cli.status.writable": "beschreibbar",
        "cli.status.read_only": "schreibgeschützt",
        "cli.logs.header": "Protokolle für Mod: {mod_name} ({mod_id}) [angezeigte Zeilen: {count}]",
        "cli.logs.empty": "ℹ️  Keine Protokolleinträge für Mod '{mod_id}' gefunden.",
        "cli.logs.checked_sources": "   Überprüfte Quellen:",
        "cli.cron.title": "HermesModsEngine Status der automatischen Aktualisierung (Cron)",
        "cli.cron.installed": "AKTIV (Im Benutzer-Crontab geplant)",
        "cli.cron.not_installed": "NICHT INSTALLIERT (Inaktiv)",
        "cli.cron.schedule": "Zeitplan",
        "cli.cron.command": "Befehl",
        "cli.cron.log_file": "Protokolldatei",
        "cli.cron.recent_logs": "Aktuelle Protokollausgabe:",
    },
    "pt": {
        "cli.status.applied": "APLICADO",
        "cli.status.disabled": "DESATIVADO",
        "cli.status.not_applied": "NÃO APLICADO",
        "cli.status.diverged": "DIVERGENTE",
        "cli.status.healthy": "SAUDÁVEL",
        "cli.status.degraded": "DEGRADADO",
        "cli.status.stopped": "PARADO",
        "cli.status.running": "EM EXECUÇÃO",
        "cli.list.empty": "  (nenhum mod correspondente ao filtro)",
        "cli.list.header": "Hermes Mods ({total} total | {applied} aplicados | {disabled} desativados)",
        "cli.commands.registered": "Comandos slash registrados: {count}\n",
        "cli.commands.col.command": "COMANDO",
        "cli.commands.col.mod": "MOD",
        "cli.commands.col.category": "CATEGORIA",
        "cli.commands.col.mode": "MODO",
        "cli.commands.col.args": "ARGUMENTOS",
        "cli.commands.col.description": "DESCRIÇÃO",
        "cli.info.mod": "Mod",
        "cli.info.author": "Autor",
        "cli.info.status": "Estado",
        "cli.info.type": "Tipo",
        "cli.info.type_folder": "Pasta",
        "cli.info.type_single": "Arquivo único",
        "cli.info.path": "Caminho",
        "cli.info.description": "Descrição",
        "cli.info.dependencies": "Dependências",
        "cli.info.targets": "Alvos",
        "cli.info.commands": "Comandos",
        "cli.info.config": "Configuração",
        "cli.info.monitors": "Monitores",
        "cli.info.skills": "Habilidades",
        "cli.info.tests": "Testes",
        "cli.info.current": "atual",
        "cli.info.secret": "segredo",
        "cli.info.not_set": "(não definido)",
        "cli.info.aliases": "apelidos",
        "cli.info.mode": "modo",
        "cli.info.not_found": "Mod '{target}' não encontrado",
        "cli.status.title": "Status do ecossistema de mods do Hermes",
        "cli.status.overall_health": "Saúde geral",
        "cli.status.master_killswitch": "Interruptor geral",
        "cli.status.engine_version": "Versão do motor",
        "cli.status.mods_overview": "Visão geral dos mods",
        "cli.status.runtime_subsystems": "Subsistemas em execução",
        "cli.status.system_integrity": "Integridade do sistema",
        "cli.status.total_registered": "Total registrados",
        "cli.status.applied_active": "Aplicados e ativos",
        "cli.status.disabled_count": "Desativados",
        "cli.status.not_applied_count": "Não aplicados",
        "cli.status.diverged_count": "Divergente/Conflito",
        "cli.status.slash_commands": "Comandos slash",
        "cli.status.monitors": "Monitores",
        "cli.status.mcp_servers": "Servidores MCP",
        "cli.status.folder_skills": "Habilidades de pasta",
        "cli.status.dependency_dag": "DAG de dependências",
        "cli.status.secrets_storage": "Armazenamento de segredos",
        "cli.status.hermes_core": "Núcleo do Hermes",
        "cli.status.dag_clean": "Limpo (0 ciclos)",
        "cli.status.secrets_valid": "Válido (0600)",
        "cli.status.writable": "gravável",
        "cli.status.read_only": "somente leitura",
        "cli.logs.header": "Logs do mod: {mod_name} ({mod_id}) [linhas mostradas: {count}]",
        "cli.logs.empty": "ℹ️  Nenhum registro de log encontrado para o mod '{mod_id}'.",
        "cli.logs.checked_sources": "   Fontes verificadas:",
        "cli.cron.title": "Status da atualização automática HermesModsEngine (Cron)",
        "cli.cron.installed": "ATIVO (Agendado no crontab do usuário)",
        "cli.cron.not_installed": "NÃO INSTALADO (Inativo)",
        "cli.cron.schedule": "Agendamento",
        "cli.cron.command": "Comando",
        "cli.cron.log_file": "Arquivo de log",
        "cli.cron.recent_logs": "Saída recente de log:",
    },
}


def compile_mod_locales(mods: List[Any]) -> Dict[str, Dict[str, Dict[str, str]]]:
    """Compile localization catalogs from all enabled mods into {lang: {surface: {dotted_key: text}}}.

    Order of precedence:
    1. Explicit translation keys declared in mod.i18n (or locales/*.yaml)
    2. Synthesized mod keys: mods.<mod_id>.name, mods.<mod_id>.description
    3. Synthesized slash command keys: slash.<cmd.name>.description, slash.command.<cmd.name>.description, mods.<mod_id>.commands.<cmd.name>.description
    4. Synthesized config keys: mods.<mod_id>.config.<key>.title, mods.<mod_id>.config.<key>.description
    5. Surface-specific catalogs from mod.i18n_surfaces (tui, desktop)
    """
    catalogs: Dict[str, Dict[str, Dict[str, str]]] = {}

    def ensure_catalog(lang_id: str, surface: str = "core") -> Dict[str, str]:
        l_norm = normalize_language_id(lang_id) or "en"
        if l_norm not in catalogs:
            catalogs[l_norm] = {}
        if surface not in catalogs[l_norm]:
            catalogs[l_norm][surface] = {}
        return catalogs[l_norm][surface]

    for mod in mods:
        if not getattr(mod, "enabled", True):
            continue

        mod_i18n = getattr(mod, "i18n", {}) or {}
        if not isinstance(mod_i18n, dict):
            continue

        for lang_raw, lang_tree in mod_i18n.items():
            if not isinstance(lang_tree, dict):
                continue
            lang = normalize_language_id(lang_raw)
            if not lang:
                continue
            cat = ensure_catalog(lang, "core")

            tree_to_flatten = lang_tree
            if lang in lang_tree and isinstance(lang_tree[lang], dict) and (
                len(lang_tree) == 1 or not any(k in lang_tree for k in ("name", "description", "title"))
            ):
                tree_to_flatten = lang_tree[lang]

            flat_tree = flatten_i18n_dict(tree_to_flatten)
            cat.update(flat_tree)

            mod_title = None
            if hasattr(mod, "get_title"):
                mod_title = mod.get_title(lang)
            elif isinstance(lang_tree, dict):
                mod_title = lang_tree.get("name") or lang_tree.get("title")
            if mod_title and f"mods.{mod.id}.name" not in cat:
                cat[f"mods.{mod.id}.name"] = mod_title

            mod_desc = None
            if hasattr(mod, "get_description"):
                mod_desc = mod.get_description(lang)
            elif isinstance(lang_tree, dict):
                mod_desc = lang_tree.get("description")
            if mod_desc and f"mods.{mod.id}.description" not in cat:
                cat[f"mods.{mod.id}.description"] = mod_desc

            for cmd in getattr(mod, "commands", []):
                cmd_desc = None
                cmd_trans = lang_tree.get("commands", {}).get(cmd.name, {}) if isinstance(lang_tree.get("commands"), dict) else {}
                if isinstance(cmd_trans, dict) and cmd_trans.get("description"):
                    cmd_desc = str(cmd_trans["description"])
                if not cmd_desc and hasattr(cmd, "get_description"):
                    cmd_desc = cmd.get_description(lang)
                if cmd_desc:
                    if f"slash.{cmd.name}.description" not in cat:
                        cat[f"slash.{cmd.name}.description"] = cmd_desc
                    if f"slash.command.{cmd.name}.description" not in cat:
                        cat[f"slash.command.{cmd.name}.description"] = cmd_desc
                    if f"mods.{mod.id}.commands.{cmd.name}.description" not in cat:
                        cat[f"mods.{mod.id}.commands.{cmd.name}.description"] = cmd_desc

            for cf_name, cf in getattr(mod, "config", {}).items():
                cf_title = None
                cf_desc = None
                if hasattr(cf, "get_title"):
                    cf_title = cf.get_title(lang)
                if hasattr(cf, "get_description"):
                    cf_desc = cf.get_description(lang)
                if cf_title and f"mods.{mod.id}.config.{cf_name}.title" not in cat:
                    cat[f"mods.{mod.id}.config.{cf_name}.title"] = cf_title
                if cf_desc and f"mods.{mod.id}.config.{cf_name}.description" not in cat:
                    cat[f"mods.{mod.id}.config.{cf_name}.description"] = cf_desc

        i18n_surfaces = getattr(mod, "i18n_surfaces", {})
        if isinstance(i18n_surfaces, dict):
            for surface, lang_dict in i18n_surfaces.items():
                if isinstance(lang_dict, dict):
                    for lang_raw, surf_tree in lang_dict.items():
                        if isinstance(surf_tree, dict):
                            lang = normalize_language_id(lang_raw)
                            if lang:
                                cat = ensure_catalog(lang, surface)
                                cat.update(flatten_i18n_dict(surf_tree))

    return catalogs


def get_locales_cache_path(mods_dir: Optional[Path] = None) -> Path:
    """Get absolute path to .locales_cache.json."""
    if mods_dir is None:
        env_mods = os.environ.get("HERMES_MODS_DIR")
        if env_mods:
            return Path(env_mods) / ".locales_cache.json"
        return Path(__file__).resolve().parent.parent / ".locales_cache.json"
    return Path(mods_dir) / ".locales_cache.json"


def update_locales_cache(mods: List[Any], mods_dir: Optional[Path] = None) -> Path:
    """Generate and write atomic .locales_cache.json from enabled mods."""
    cache_path = get_locales_cache_path(mods_dir)

    try:
        from .config_manager import ModConfigManager
        if not ModConfigManager().is_mods_enabled():
            try:
                from .file_ops import FileLock, atomic_write_text
                with FileLock(cache_path):
                    atomic_write_text(cache_path, "{}\n")
            except Exception:
                cache_path.write_text("{}\n", encoding="utf-8")
            return cache_path
    except Exception:
        pass

    active_mods = [m for m in mods if getattr(m, "enabled", True)]
    compiled = compile_mod_locales(active_mods)
    all_langs = sorted(compiled.keys())

    cache_data = {
        "version": "2.0",
        "generated_at": time.time(),
        "_meta": {
            "active_mods_count": len(active_mods),
            "languages": all_langs,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "catalogs": compiled,
    }

    try:
        from .file_ops import FileLock, atomic_write_text
        with FileLock(cache_path):
            atomic_write_text(cache_path, json.dumps(cache_data, indent=2, ensure_ascii=False))
    except Exception:
        cache_path.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # Invalidate in-memory engine resolver
    engine_i18n.reset_cache()

    # Invalidate Hermes agent i18n core cache if available in environment
    try:
        from agent.i18n import reset_language_cache
        reset_language_cache()
    except Exception:
        pass

    return cache_path


def get_enabled_mod_locales(
    mods_dir: Optional[Path] = None, lang: str = "ru", surface: str = "core"
) -> Dict[str, str]:
    """Get merged translations for (lang, surface) from cache or compiled mods."""
    cache_path = get_locales_cache_path(mods_dir)
    lang_id = normalize_language_id(lang) or "en"

    if cache_path.is_file():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            catalogs = data.get("catalogs", {})
            if isinstance(catalogs, dict):
                lang_data = catalogs.get(lang_id, {})
                if isinstance(lang_data, dict):
                    if surface in lang_data and isinstance(lang_data[surface], dict):
                        return dict(lang_data[surface])
                    elif surface == "core" and not any(s in lang_data for s in ("core", "tui", "desktop")):
                        return {str(k): str(v) for k, v in lang_data.items() if isinstance(v, str)}
        except Exception:
            pass

    try:
        from .loader import ModLoader
        mods = ModLoader.load_all(cache_path.parent)
        compiled = compile_mod_locales([m for m in mods if getattr(m, "enabled", True)])
        lang_data = compiled.get(lang_id, {})
        return dict(lang_data.get(surface, {}))
    except Exception:
        return {}


def register_mod_locales(mods: Optional[List[Any]] = None, mods_dir: Optional[Path] = None) -> int:
    """Register all active mod translation catalogs directly into agent.i18n_layers if present."""
    try:
        from agent.i18n_layers import register_pack, SURFACES
    except ImportError:
        return 0

    if mods is None:
        try:
            from .loader import ModLoader
            mods = ModLoader.load_all(mods_dir or get_locales_cache_path().parent)
        except Exception:
            return 0

    active_mods = [m for m in mods if getattr(m, "enabled", True)]
    compiled = compile_mod_locales(active_mods)
    count = 0
    for lang, surfaces in compiled.items():
        for surface, messages in surfaces.items():
            if surface in SURFACES and messages:
                try:
                    register_pack(lang, surface, messages, source="mods_engine")
                    count += 1
                except Exception as exc:
                    sys.stderr.write(f"Warning: failed to register mod pack {lang}/{surface}: {exc}\n")
    return count


class EngineI18nResolver:
    """Unified in-memory and on-disk i18n resolver for Hermes Mods engine and CLI."""

    def __init__(self, mods_dir: Optional[Path] = None):
        self._mods_dir = mods_dir
        self._lock = threading.Lock()
        self._cache_mtime: float = -1.0
        self._cached_catalogs: Dict[str, Dict[str, str]] = {}
        self._cached_languages: List[str] = list(OFFICIAL_LANGUAGES)

    def _get_cache_path(self) -> Path:
        return get_locales_cache_path(self._mods_dir)

    def reset_cache(self) -> None:
        with self._lock:
            self._cache_mtime = -1.0
            self._cached_catalogs.clear()

    def _ensure_loaded(self) -> None:
        cache_path = self._get_cache_path()
        file_mtime = -1.0
        if cache_path.is_file():
            try:
                file_mtime = cache_path.stat().st_mtime
            except OSError:
                pass

        with self._lock:
            if file_mtime > self._cache_mtime:
                self._cached_catalogs.clear()
                self._cache_mtime = file_mtime
                if cache_path.is_file():
                    try:
                        with open(cache_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        raw_cats = data.get("catalogs", {})
                        if isinstance(raw_cats, dict):
                            for lang, surfaces in raw_cats.items():
                                norm_l = normalize_language_id(lang)
                                if isinstance(surfaces, dict):
                                    core_cat = surfaces.get("core", {})
                                    if isinstance(core_cat, dict):
                                        self._cached_catalogs[norm_l] = {str(k): str(v) for k, v in core_cat.items()}
                                    elif not any(s in surfaces for s in ("core", "tui", "desktop")):
                                        self._cached_catalogs[norm_l] = {
                                            str(k): str(v) for k, v in surfaces.items() if isinstance(v, str)
                                        }
                    except Exception as exc:
                        logger.debug("Could not read %s: %s", cache_path, exc)

    def t(
        self,
        key: str,
        lang: Optional[str] = None,
        default: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Translate a key into target language with hierarchical fallbacks:
        1. Mod-defined translations for `lang` (from .locales_cache.json)
        2. Built-in Engine CLI catalog for `lang`
        3. Mod-defined English translations (`en`)
        4. Built-in Engine CLI English catalog (`en`)
        5. `default` parameter or `key` itself.
        Supports keyword formatting kwargs.
        """
        self._ensure_loaded()
        target_lang = normalize_language_id(lang) if lang else "en"
        if not target_lang:
            target_lang = "en"

        value: Optional[str] = None

        with self._lock:
            # 1. Check mod catalog for target_lang
            mod_cat = self._cached_catalogs.get(target_lang)
            if mod_cat and key in mod_cat:
                value = mod_cat[key]

            # 2. Check built-in engine CLI catalog for target_lang
            if value is None:
                cli_cat = ENGINE_CLI_CATALOG.get(target_lang)
                if cli_cat and key in cli_cat:
                    value = cli_cat[key]

            # 3. Fallback to English mod catalog
            if value is None and target_lang != "en":
                en_mod_cat = self._cached_catalogs.get("en")
                if en_mod_cat and key in en_mod_cat:
                    value = en_mod_cat[key]

            # 4. Fallback to English CLI catalog
            if value is None and target_lang != "en":
                en_cli_cat = ENGINE_CLI_CATALOG.get("en")
                if en_cli_cat and key in en_cli_cat:
                    value = en_cli_cat[key]

        # 5. Final fallback to default or key
        if value is None:
            value = default if default is not None else key

        if not kwargs:
            return value

        try:
            return value.format(**kwargs)
        except Exception:
            return value

    def get_catalog(self, lang: str, surface: str = "core") -> Dict[str, str]:
        """Get flattened catalog for (lang, surface). Merges CLI strings and mod overlays."""
        self._ensure_loaded()
        norm_l = normalize_language_id(lang) or "en"
        merged: Dict[str, str] = {}

        # 1. Base CLI catalog
        if surface == "core" and norm_l in ENGINE_CLI_CATALOG:
            merged.update(ENGINE_CLI_CATALOG[norm_l])

        # 2. Mod catalog overrides
        with self._lock:
            mod_cat = self._cached_catalogs.get(norm_l, {})
            merged.update(mod_cat)

        return merged


# Global engine singleton
engine_i18n = EngineI18nResolver()


def t(key: str, lang: Optional[str] = None, default: Optional[str] = None, **kwargs: Any) -> str:
    """Convenience helper for engine_i18n.t."""
    return engine_i18n.t(key, lang=lang, default=default, **kwargs)


def get_language() -> str:
    """Get active language code."""
    return resolve_language()


get_active_language = get_language

__all__ = [
    "OFFICIAL_LANGUAGES",
    "DEFAULT_LANGUAGE",
    "DEFAULT_CLI_LANGUAGE",
    "LANGUAGE_ALIASES",
    "normalize_language_id",
    "is_language_id",
    "is_supported_language",
    "resolve_language",
    "flatten_i18n_dict",
    "compile_mod_locales",
    "get_locales_cache_path",
    "update_locales_cache",
    "get_enabled_mod_locales",
    "register_mod_locales",
    "ENGINE_CLI_CATALOG",
    "EngineI18nResolver",
    "engine_i18n",
    "t",
    "get_language",
    "get_active_language",
]
