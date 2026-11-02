"""Handler for /mods and /mod Telegram slash commands."""
from __future__ import annotations

import html
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gateway.mod_manager")

_I18N_CACHE: Dict[str, Dict[str, str]] = {}


def _resolve_mods_dir(ctx: Optional[Dict[str, Any]] = None) -> Path:
    if isinstance(ctx, dict) and ctx.get("mod_dir"):
        m_dir = Path(ctx["mod_dir"]).resolve()
        if m_dir.parent.name in ("system", "custom"):
            cand = m_dir.parent.parent
            if (cand / ".engine").is_dir():
                return cand
    env_dir = os.environ.get("HERMES_MODS_DIR")
    if env_dir and Path(env_dir).is_dir():
        return Path(env_dir).resolve()
    default_dir = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "mods"
    if default_dir.is_dir():
        return default_dir.resolve()
    return default_dir


def _resolve_mod_dir(ctx: Optional[Dict[str, Any]] = None) -> Path:
    if isinstance(ctx, dict) and ctx.get("mod_dir"):
        cand = Path(ctx["mod_dir"]).resolve()
        if cand.is_dir():
            return cand
    return Path(__file__).resolve().parent.parent


def _init_engine(mods_dir: Path) -> None:
    engine_dir = mods_dir / ".engine"
    if str(engine_dir) not in sys.path:
        sys.path.insert(0, str(engine_dir))
    if str(mods_dir) not in sys.path:
        sys.path.insert(0, str(mods_dir))


def _simple_yaml_parse(text: str) -> Dict[str, Any]:
    """Pure-python fallback parser for basic YAML dictionaries."""
    result: Dict[str, Any] = {}
    stack: List[Tuple[int, Dict[str, Any]]] = [(-1, result)]
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        current_dict = stack[-1][1]
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip()
            if not v:
                new_dict: Dict[str, Any] = {}
                current_dict[k] = new_dict
                stack.append((indent, new_dict))
            else:
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                current_dict[k] = v
    return result


def _safe_load_yaml(file_path: Path) -> Dict[str, Any]:
    try:
        import yaml
        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
    except ImportError:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return _simple_yaml_parse(f.read())
        except Exception:
            pass
    except Exception as exc:
        logger.debug("Failed to read %s: %s", file_path, exc)
    return {}


def _flatten_i18n(node: Any, prefix: str = "", out: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    flat = {} if out is None else out
    if isinstance(node, dict):
        for k, v in node.items():
            sub = f"{prefix}.{k}" if prefix else str(k)
            _flatten_i18n(v, sub, flat)
    elif isinstance(node, str):
        if prefix:
            flat[prefix] = node
    elif node is not None:
        if prefix:
            flat[prefix] = str(node)
    return flat


def _load_mod_catalog(mod_dir: Path, lang: str) -> Dict[str, str]:
    norm_lang = (lang or "ru").strip().lower().replace("_", "-")
    cache_key = f"{mod_dir}:{norm_lang}"
    if cache_key in _I18N_CACHE:
        return _I18N_CACHE[cache_key]

    catalog: Dict[str, str] = {}
    i18n_dir = mod_dir / "i18n"
    if i18n_dir.is_dir():
        target_file = i18n_dir / f"{norm_lang}.yaml"
        if not target_file.is_file() and "-" in norm_lang:
            target_file = i18n_dir / f"{norm_lang.split('-')[0]}.yaml"
        if not target_file.is_file():
            target_file = i18n_dir / "en.yaml"

        if target_file.is_file():
            raw = _safe_load_yaml(target_file)
            _flatten_i18n(raw, "", catalog)

    _I18N_CACHE[cache_key] = catalog
    return catalog


def _tr(mod_dir: Path, key: str, lang: str = "ru", default: str = "", **kwargs: Any) -> str:
    norm_lang = (lang or "ru").strip().lower().replace("_", "-")
    val: Optional[str] = None

    # 1. Mod's own collocated i18n catalog
    cat = _load_mod_catalog(mod_dir, norm_lang)
    if key in cat:
        val = cat[key]

    # 2. Try agent.i18n.t if available
    if val is None:
        try:
            from agent.i18n import t as agent_t
            res = agent_t(key, lang=norm_lang)
            if res != key:
                val = res
        except Exception:
            pass

    # 3. Try engine_i18n if available
    if val is None:
        try:
            from engine.i18n import engine_i18n
            res = engine_i18n.t(key, lang=norm_lang)
            if res != key:
                val = res
        except Exception:
            pass

    # 4. Fallback to English mod catalog
    if val is None and norm_lang != "en":
        en_cat = _load_mod_catalog(mod_dir, "en")
        if key in en_cat:
            val = en_cat[key]

    if val is None:
        val = default if default else key

    if kwargs:
        try:
            return val.format(**kwargs)
        except Exception:
            return val
    return val


def _detect_language(ctx: Any = None) -> str:
    if isinstance(ctx, dict) and ctx.get("lang"):
        cand = str(ctx["lang"]).strip().lower().replace("_", "-")
        if cand:
            return cand
    try:
        from agent.i18n import get_active_language
        cand = get_active_language()
        if cand:
            return cand.strip().lower().replace("_", "-")
    except Exception:
        pass
    try:
        from engine.i18n import get_active_language
        cand = get_active_language()
        if cand:
            return cand.strip().lower().replace("_", "-")
    except Exception:
        pass
    env_lang = os.environ.get("HERMES_LANGUAGE")
    if env_lang:
        return env_lang.strip().lower().replace("_", "-")
    return "ru"


def find_mod(target: str, mods: List[Any]) -> Optional[Any]:
    if not target:
        return None
    t = target.strip().lower()
    for m in mods:
        m_id = str(getattr(m, "id", "")).lower()
        m_name = str(getattr(m, "name", "")).lower()
        stem = Path(getattr(m, "path", "")).stem.lower()
        if t in (
            m_id,
            m_name,
            stem,
            m_id.replace("-", "_"),
            m_id.replace("_", "-"),
        ):
            return m
    return None


def handle_list(mods_dir: Path, lang: str = "ru", mod_dir: Optional[Path] = None) -> str:
    """1. /mods or /mods list: concise list of custom mods and hidden details for system mods."""
    _init_engine(mods_dir)
    from runner import ModRunner
    from engine.loader import ModLoader
    from engine.inspector import is_system_mod

    effective_mod_dir = mod_dir or (mods_dir / "system" / "mod-manager")
    tr = lambda key, default="", **kw: _tr(effective_mod_dir, key, lang=lang, default=default, **kw)

    runner = ModRunner(mods_dir=mods_dir)
    inspector = runner.get_inspector()
    mods = ModLoader.load_all(mods_dir)

    custom_mods = []
    system_mods = []

    for m in mods:
        if is_system_mod(m, mods_dir):
            system_mods.append(m)
        else:
            custom_mods.append(m)

    custom_mods.sort(key=lambda x: x.name.lower())
    system_mods.sort(key=lambda x: x.name.lower())

    header = tr("slash_mods.custom_mods_header", "🧩 <b>Активные кастомные моды:</b>\n")
    lines = [header]

    for m in custom_mods:
        name = m.get_name(lang)
        desc = m.get_description(lang)
        is_active = (m.enabled and inspector.get_mod_status(m) == "applied")
        if is_active:
            name_part = f"<b>{html.escape(name)}</b>"
        else:
            name_part = f"<s><b>{html.escape(name)}</b></s>"
        if desc:
            lines.append(f"• {name_part} — {html.escape(desc)}")
        else:
            lines.append(f"• {name_part}")

    summary_title = tr("slash_mods.system_mods_summary", "Системные моды (Tier 0)")
    sys_lines = [f"\n<details><summary>{summary_title}</summary>"]
    for m in system_mods:
        name = m.get_name(lang)
        desc = m.get_description(lang)
        is_active = (m.enabled and inspector.get_mod_status(m) == "applied")
        if is_active:
            name_part = f"<b>{html.escape(name)}</b>"
        else:
            name_part = f"<s><b>{html.escape(name)}</b></s>"
        if desc:
            sys_lines.append(f"• {name_part} — {html.escape(desc)}")
        else:
            sys_lines.append(f"• {name_part}")
    sys_lines.append("</details>")

    return "\n".join(lines) + "\n" + "\n".join(sys_lines)


def handle_info(mods_dir: Path, mod_id: str, lang: str = "ru", mod_dir: Optional[Path] = None) -> str:
    """2. /mods info <mod_id>: detailed mod card."""
    _init_engine(mods_dir)
    from runner import ModRunner
    from engine.loader import ModLoader
    from engine.inspector import is_system_mod

    effective_mod_dir = mod_dir or (mods_dir / "system" / "mod-manager")
    tr = lambda key, default="", **kw: _tr(effective_mod_dir, key, lang=lang, default=default, **kw)

    runner = ModRunner(mods_dir=mods_dir)
    inspector = runner.get_inspector()
    mods = ModLoader.load_all(mods_dir)

    mod = find_mod(mod_id, mods)
    if not mod:
        return tr("slash_mods.not_found", "⚠️ Мод '{mod_id}' не найден. Проверьте список через /mods", mod_id=html.escape(mod_id))

    info = inspector.get_mod_info(mod, lang=lang)

    name = info.get("name") or mod.name
    version = info.get("version", "1.0.0")
    author = info.get("author") or ("Engine" if is_system_mod(mod, mods_dir) else "User")
    status = tr("slash_mods.info.status_applied", "активен") if (mod.enabled and info.get("status") == "applied") else tr("slash_mods.info.status_disabled", "отключен")
    type_str = tr("slash_mods.info.type_folder", "Папочный") if mod.is_folder else tr("slash_mods.info.type_single", "Однофайловый")
    desc = info.get("description") or mod.description or tr("slash_mods.info.no_description", "нет описания")

    deps = info.get("dependencies") or []
    deps_str = ", ".join(deps) if deps else tr("slash_mods.info.no_dependencies", "нет")

    lines = [
        f"📦 <b>{html.escape(name)}</b> (<code>{mod.id}</code>)",
        f"• <b>{tr('slash_mods.info.version', 'Версия:')}</b> v{version}",
        f"• <b>{tr('slash_mods.info.author', 'Автор:')}</b> {html.escape(author)}",
        f"• <b>{tr('slash_mods.info.status', 'Статус:')}</b> {status}",
        f"• <b>{tr('slash_mods.info.type', 'Тип:')}</b> {type_str}",
        f"• <b>{tr('slash_mods.info.dependencies', 'Зависимости:')}</b> {html.escape(deps_str)}",
        f"\n📝 <b>{tr('slash_mods.info.description', 'Описание:')}</b>\n{html.escape(desc)}",
    ]

    targets = info.get("targets") or []
    if targets:
        lines.append(f"\n🎯 <b>{tr('slash_mods.info.targets', 'Файлы / Таргеты ({count}):', count=len(targets))}</b>")
        for t in targets:
            f = t.get("file", "")
            tdesc = t.get("description", "")
            desc_part = f" — {html.escape(tdesc)}" if tdesc else ""
            lines.append(f"• <code>{html.escape(f)}</code>{desc_part}")
    else:
        lines.append(f"\n🎯 <b>{tr('slash_mods.info.targets_none', 'Файлы / Таргеты: нет (декларативный мод)')}</b>")

    commands = info.get("commands") or []
    if commands:
        lines.append(f"\n⚡ <b>{tr('slash_mods.info.commands', 'Команды ({count}):', count=len(commands))}</b>")
        for c in commands:
            cname = c.get("name", "")
            aliases = c.get("aliases") or []
            alias_part = f" ({tr('slash_mods.info.aliases', 'алиасы:')} {', '.join('/' + a for a in aliases)})" if aliases else ""
            args_hint = f" {c['args_hint']}" if c.get("args_hint") else ""
            cdesc = c.get("description") or ""
            desc_part = f" — {html.escape(cdesc)}" if cdesc else ""
            lines.append(f"• <code>/{cname}{args_hint}</code>{alias_part}{desc_part}")

    config = info.get("config") or {}
    if config:
        lines.append(f"\n⚙️ <b>{tr('slash_mods.info.config', 'Конфигурация ({count}):', count=len(config))}</b>")
        for k, f in config.items():
            f_type = f.get("type", "string")
            f_def = f.get("default")
            f_desc = f.get("description") or ""
            desc_part = f" — {html.escape(f_desc)}" if f_desc else ""
            lines.append(f"• <code>{html.escape(k)}</code> ({f_type}, default: {f_def}){desc_part}")

    monitors = info.get("monitors") or []
    if monitors:
        lines.append(f"\n📡 <b>{tr('slash_mods.info.monitors', 'Мониторы ({count}):', count=len(monitors))}</b>")
        for m_spec in monitors:
            m_id = m_spec.get("id", "")
            m_status = m_spec.get("status", "STOPPED")
            lines.append(f"• <code>{html.escape(m_id)}</code> ({tr('slash_mods.info.monitors_status', 'статус:')} {m_status})")

    skills = info.get("skills") or []
    if skills:
        lines.append(f"\n🧠 <b>{tr('slash_mods.info.skills', 'Скиллы ({count}):', count=len(skills))}</b>")
        for s in skills:
            lines.append(f"• <code>{html.escape(s)}</code>")

    tests = info.get("tests") or {}
    if tests.get("count", 0) > 0:
        method = tests.get("method", "native tests")
        lines.append(f"\n🧪 <b>{tr('slash_mods.info.tests', 'Тесты:')}</b> {tests.get('count')} ({method})")

    return "\n".join(lines)


def handle_status(mods_dir: Path, lang: str = "ru", mod_dir: Optional[Path] = None) -> str:
    """3. /mods status: ecosystem health summary."""
    _init_engine(mods_dir)
    from runner import ModRunner
    from engine.loader import ModLoader

    effective_mod_dir = mod_dir or (mods_dir / "system" / "mod-manager")
    tr = lambda key, default="", **kw: _tr(effective_mod_dir, key, lang=lang, default=default, **kw)

    runner = ModRunner(mods_dir=mods_dir)
    inspector = runner.get_inspector()
    mods = ModLoader.load_all(mods_dir)
    status_data = inspector.get_ecosystem_status(mods=mods, lang=lang)

    overall = status_data.get("overall_status", "UNKNOWN")
    healthy = status_data.get("healthy", False)
    status_icon = "🟢" if healthy else ("🟡" if overall == "DEGRADED" else "🔴")

    master_enabled = status_data.get("master_enabled", True)
    master_icon = "🟢" if master_enabled else "🔴"
    master_text = tr("slash_mods.status.master_enabled", "Включен (mods.enabled: true)") if master_enabled else tr("slash_mods.status.master_disabled", "Отключен (mods.enabled: false)")

    counts = status_data.get("counts", {})
    total = counts.get("total", len(mods))
    applied = counts.get("applied", 0)
    disabled = counts.get("disabled", 0)
    not_applied = counts.get("not_applied", 0)
    diverged = counts.get("diverged", 0)
    system_cnt = counts.get("system", 0)
    custom_cnt = counts.get("custom", 0)

    runtime = status_data.get("runtime", {})
    integrity = status_data.get("integrity", {})

    dag_status = tr("slash_mods.status.dag_clean", "Clean (0 циклов)") if integrity.get("dag_clean") else tr("slash_mods.status.dag_error", "ОШИБКА: обнаружены циклы")
    secrets_status = tr("slash_mods.status.secrets_valid", "Valid (0600)") if integrity.get("secrets_valid") else tr("slash_mods.status.secrets_error", "ОШИБКА прав доступа")
    core_mode = tr("slash_mods.status.writable", "writable") if integrity.get("core_writable") else tr("slash_mods.status.read_only", "read-only")

    lines = [
        f"🛡️ <b>{tr('slash_mods.status.title', 'Экосистема модов Hermes:')}</b> {status_icon} <b>{overall}</b>",
        f"🔌 <b>{tr('slash_mods.status.master_killswitch', 'Мастер-рубильник:')}</b> {master_icon} {master_text}",
        f"⚙️ <b>{tr('slash_mods.status.engine_version', 'Версия движка:')}</b> v{status_data.get('engine_version', '1.4.0')}",
        "",
        f"📊 <b>{tr('slash_mods.status.mods_overview', 'Сводка модов:')}</b>",
        tr("slash_mods.status.total_registered", "Всего зарегистрировано: {total} ({custom_cnt} custom, {system_cnt} system)", total=total, custom_cnt=custom_cnt, system_cnt=system_cnt),
        tr("slash_mods.status.applied_active", "Применено и активно: {applied}/{total}", applied=applied, total=total),
        tr("slash_mods.status.disabled_count", "Отключено: {disabled}", disabled=disabled),
    ]
    if not_applied:
        lines.append(tr("slash_mods.status.not_applied_count", "Не применено: {not_applied}", not_applied=not_applied))
    if diverged:
        lines.append(tr("slash_mods.status.diverged_count", "Расхождений (diverged): {diverged}", diverged=diverged))

    lines.extend([
        "",
        f"⚙️ <b>{tr('slash_mods.status.runtime_subsystems', 'Подсистемы рантайма:')}</b>",
        tr("slash_mods.status.commands", "Слэш-команды: {count} зарегистрировано", count=runtime.get('commands', 0)),
        tr("slash_mods.status.monitors", "Фоновые мониторы: {running} запущено ({total} всего)", running=runtime.get('monitors_running', 0), total=runtime.get('monitors_total', 0)),
        tr("slash_mods.status.mcp_servers", "MCP-серверы: {count} активно", count=runtime.get('mcp_servers', 0)),
        tr("slash_mods.status.skills", "Скиллы модов: {count} активно", count=runtime.get('skills', 0)),
        "",
        f"🔒 <b>{tr('slash_mods.status.system_integrity', 'Целостность системы:')}</b>",
        tr("slash_mods.status.dag", "Граф зависимостей: {status}", status=dag_status),
        tr("slash_mods.status.secrets", "Хранилище секретов: {status}", status=secrets_status),
        tr("slash_mods.status.core", "Ядро Hermes: v{version} ({mode})", version=integrity.get('core_version', '0.22.5'), mode=core_mode),
    ])

    return "\n".join(lines)


def handle_logs(mods_dir: Path, mod_id: str, lines_count: int = 50, lang: str = "ru", mod_dir: Optional[Path] = None) -> str:
    """4. /mods logs <mod_id> [N]: mod logs under spoiler."""
    _init_engine(mods_dir)
    from runner import ModRunner
    from engine.loader import ModLoader

    effective_mod_dir = mod_dir or (mods_dir / "system" / "mod-manager")
    tr = lambda key, default="", **kw: _tr(effective_mod_dir, key, lang=lang, default=default, **kw)

    runner = ModRunner(mods_dir=mods_dir)
    inspector = runner.get_inspector()
    mods = ModLoader.load_all(mods_dir)

    mod = find_mod(mod_id, mods)
    if not mod:
        return tr("slash_mods.not_found", "⚠️ Мод '{mod_id}' не найден. Проверьте список через /mods", mod_id=html.escape(mod_id))

    logs_data = inspector.get_mod_logs(mod, lines=lines_count, lang=lang)
    lines_list = logs_data.get("lines", [])

    if not lines_list:
        return tr("slash_mods.logs.empty", "ℹ️ Записей логов для мода '<b>{mod_id}</b>' не найдено.", mod_id=html.escape(mod.id))

    joined_lines = "\n".join(lines_list)
    if len(joined_lines) > 3000:
        truncated_part = joined_lines[-3000:]
        nl = truncated_part.find("\n")
        if nl != -1:
            truncated_part = truncated_part[nl + 1:]
        joined_lines = tr("slash_mods.logs.truncated", "[...первые строки логов усечены для Telegram...]\n") + truncated_part

    escaped_logs = html.escape(joined_lines)

    header = tr(
        "slash_mods.logs.header",
        "📋 <b>Логи мода {mod_name} (<code>{mod_id}</code>) [показано строк: {lines_count}]:</b>\n\n",
        mod_name=html.escape(mod.name),
        mod_id=mod.id,
        lines_count=len(lines_list),
    )
    summary = tr(
        "slash_mods.logs.summary",
        "Показать логи ({lines_count} строк)",
        lines_count=len(lines_list),
    )

    return (
        f"{header}"
        f"<details><summary>{summary}</summary>\n"
        f"<tg-spoiler>\n"
        f"<pre>\n{escaped_logs}\n</pre>\n"
        f"</tg-spoiler>\n"
        f"</details>"
    )


def handle(ctx: Any = None) -> str:
    """Main entrypoint for mod commands dispatch."""
    raw_args = ""
    mod_dir_cand = None
    if isinstance(ctx, dict):
        raw_args = ctx.get("args", "") or ""
        if ctx.get("mod_dir"):
            mod_dir_cand = Path(ctx["mod_dir"]).resolve()
    elif isinstance(ctx, str):
        raw_args = ctx
    elif hasattr(ctx, "get_command_args"):
        raw_args = ctx.get_command_args() or ""
    elif hasattr(ctx, "text"):
        parts = str(ctx.text).split(None, 1)
        raw_args = parts[1] if len(parts) > 1 else ""

    raw_args = raw_args.strip()
    mods_dir = _resolve_mods_dir(ctx if isinstance(ctx, dict) else None)
    mod_dir = mod_dir_cand or _resolve_mod_dir(ctx if isinstance(ctx, dict) else None)

    lang = _detect_language(ctx)

    parts = raw_args.split()
    if "--lang" in parts:
        idx = parts.index("--lang")
        if idx + 1 < len(parts):
            cand = parts[idx + 1].strip().lower().replace("_", "-")
            if cand not in ("auto", "none", ""):
                lang = cand
            parts.pop(idx + 1)
            parts.pop(idx)

    tr = lambda key, default="", **kw: _tr(mod_dir, key, lang=lang, default=default, **kw)

    if not parts:
        return handle_list(mods_dir, lang=lang, mod_dir=mod_dir)

    subcmd = parts[0].lower()

    if subcmd == "list":
        return handle_list(mods_dir, lang=lang, mod_dir=mod_dir)

    if subcmd == "status":
        return handle_status(mods_dir, lang=lang, mod_dir=mod_dir)

    if subcmd == "info":
        if len(parts) < 2:
            return tr("slash_mods.usage_info", "ℹ️ Использование: /mods info <mod_id>")
        mod_id = parts[1]
        return handle_info(mods_dir, mod_id, lang=lang, mod_dir=mod_dir)

    if subcmd == "logs":
        if len(parts) < 2:
            return tr("slash_mods.logs.usage", "ℹ️ Использование: /mods logs <mod_id> [N]")
        mod_id = parts[1]
        lines_count = 50
        if len(parts) > 2 and parts[2].isdigit():
            lines_count = max(1, min(200, int(parts[2])))
        return handle_logs(mods_dir, mod_id, lines_count=lines_count, lang=lang, mod_dir=mod_dir)

    # Check if subcmd itself is a known mod_id
    _init_engine(mods_dir)
    from engine.loader import ModLoader
    mods = ModLoader.load_all(mods_dir)
    matched = find_mod(subcmd, mods)
    if matched:
        return handle_info(mods_dir, matched.id, lang=lang, mod_dir=mod_dir)

    help_title = tr("slash_mods.help.title", "ℹ️ <b>Управление и инспекция модов Hermes (/mods, /mod):</b>\n\n")
    help_list = tr("slash_mods.help.list", "• <code>/mods</code> или <code>/mods list</code> — список активных кастомных и системных модов")
    help_info = tr("slash_mods.help.info", "• <code>/mods info &lt;mod_id&gt;</code> — подробная карточка мода (версия, автор, цели, команды)")
    help_status = tr("slash_mods.help.status", "• <code>/mods status</code> — статус и здоровье экосистемы модов")
    help_logs = tr("slash_mods.help.logs", "• <code>/mods logs &lt;mod_id&gt; [N]</code> — последние логи и ошибки мода под спойлером")

    return f"{help_title}{help_list}\n{help_info}\n{help_status}\n{help_logs}"
