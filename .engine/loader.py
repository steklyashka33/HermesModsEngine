"""Parser and loader for Hermes Mod manifests with config and monitors."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import yaml

from .models import ConfigField, Mod, ModTarget, MonitorSpec, ModCommand, CycleDetectionError
try:
    from .file_ops import FileLock, atomic_write_text
except ImportError:
    class FileLock:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def atomic_write_text(p, content, encoding="utf-8"):
        Path(p).write_text(content, encoding=encoding)

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(HERMES_HOME / "mods")))


def is_valid_hermes_root(path: Optional[Path | str]) -> bool:
    """Проверяет, является ли указанный путь директорией ядра Hermes Agent."""
    if not path:
        return False
    try:
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            return False
        # Маркеры ядра Hermes Agent:
        if (p / "hermes_cli").is_dir():
            return True
        if (p / "agent").is_dir() and (p / "gateway").is_dir():
            return True
        if (p / "run_agent.py").is_file() or (p / "hermes_bootstrap.py").is_file():
            return True
        pyproject = p / "pyproject.toml"
        if pyproject.is_file():
            try:
                content = pyproject.read_text(encoding="utf-8", errors="ignore")
                if "hermes-agent" in content or "hermes_agent" in content:
                    return True
            except OSError:
                pass
    except (OSError, PermissionError):
        pass
    return False


def detect_hermes_root(prefer_env: bool = True) -> Path:
    """Динамическое определение корня ядра Hermes Agent (Zero Path Assumptions).

    Приоритет:
    1. Переменные окружения: HERMES_ROOT, HERMES_ROOT_DIR, HERMES_AGENT_PATH.
    2. Стандартные системные и пользовательские директории:
       - /usr/local/lib/hermes-agent
       - ~/.local/lib/hermes-agent, ~/.local/share/hermes-agent, ~/.local/hermes-agent
       - sys.prefix / lib / hermes-agent, sys.prefix / share / hermes-agent
       - site-packages / hermes-agent / hermes_agent
       - /usr/lib/hermes-agent, /opt/hermes-agent, /opt/hermes
    3. Автоматический поиск через интроспекцию модулей (import hermes_cli).
    4. Безопасный фоллбэк: /usr/local/lib/hermes-agent (или ~/.local/lib/hermes-agent для non-root).
    """
    # 1. Переменные окружения
    if prefer_env:
        for var_name in ("HERMES_ROOT", "HERMES_ROOT_DIR", "HERMES_AGENT_PATH"):
            val = os.environ.get(var_name, "").strip()
            if val:
                resolved = Path(val).expanduser().resolve()
                return resolved

    # 2. Проверка системных и пользовательских путей
    home = Path.home()
    candidates: List[Path] = [
        Path("/usr/local/lib/hermes-agent"),
        home / ".local" / "lib" / "hermes-agent",
        home / ".local" / "share" / "hermes-agent",
        home / ".local" / "hermes-agent",
        Path(sys.prefix) / "lib" / "hermes-agent",
        Path(sys.prefix) / "share" / "hermes-agent",
        Path(sys.prefix) / "hermes-agent",
        Path("/usr/lib/hermes-agent"),
        Path("/opt/hermes-agent"),
        Path("/opt/hermes"),
    ]

    # site-packages интроспекция
    try:
        import site
        site_dirs = []
        if hasattr(site, "getsitepackages"):
            site_dirs.extend(site.getsitepackages())
        if hasattr(site, "getusersitepackages"):
            u_site = site.getusersitepackages()
            if isinstance(u_site, str):
                site_dirs.append(u_site)
        for s in site_dirs:
            sp = Path(s)
            candidates.append(sp / "hermes-agent")
            candidates.append(sp / "hermes_agent")
            candidates.append(sp / "hermes")
    except Exception:
        pass

    for cand in candidates:
        if is_valid_hermes_root(cand):
            return cand.resolve()

    # 3. Интроспекция импорта hermes_cli
    for mod_name in ("hermes_cli", "hermes_agent", "hermes"):
        try:
            import importlib.util
            spec = importlib.util.find_spec(mod_name)
            if spec and spec.origin:
                origin = Path(spec.origin).resolve()
                cur = origin.parent
                for _ in range(4):
                    if is_valid_hermes_root(cur):
                        return cur.resolve()
                    cur = cur.parent
        except Exception:
            pass

    # 4. Фоллбэк без падений
    default_usr = Path("/usr/local/lib/hermes-agent")
    if default_usr.exists():
        return default_usr.resolve()

    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return (home / ".local" / "lib" / "hermes-agent").resolve()

    return default_usr


HERMES_ROOT = detect_hermes_root()


def parse_config_field(name: str, raw: dict) -> ConfigField:
    t = str(raw.get("type", "string")).lower()
    raw_i18n = raw.get("i18n") or raw.get("locales") or {}
    i18n: Dict[str, Any] = dict(raw_i18n) if isinstance(raw_i18n, dict) else {}
    desc_ru = str(
        raw.get("description_ru")
        or (i18n.get("ru", {}).get("description") if isinstance(i18n.get("ru"), dict) else (i18n.get("ru") if isinstance(i18n.get("ru"), str) else ""))
        or ""
    ).strip()
    title_ru = str(
        raw.get("title_ru")
        or (i18n.get("ru", {}).get("title") if isinstance(i18n.get("ru"), dict) else "")
        or ""
    ).strip()
    return ConfigField(
        name=name,
        type=t,
        description=str(raw.get("description", "")),
        description_ru=desc_ru,
        title=str(raw.get("title", name)),
        title_ru=title_ru,
        required=bool(raw.get("required", False)),
        default=raw.get("default"),
        sensitive=bool(raw.get("sensitive", False)),
        options=raw.get("options"),
        min=raw.get("min"),
        max=raw.get("max"),
        multiple=bool(raw.get("multiple", False)),
        i18n=i18n,
    )


def parse_monitor_spec(raw: dict, mod_id: str, mod_dir: Optional[Path], mod_version: str = "1.0.0") -> MonitorSpec:
    m_id = str(raw.get("id") or raw.get("name") or "monitor")
    return MonitorSpec(
        id=m_id,
        command=str(raw.get("command", "")),
        name=str(raw.get("name") or m_id),
        description=str(raw.get("description", "")),
        when=str(raw.get("when", "always")),
        delivery=str(raw.get("delivery", "chat")),
        interval=int(raw.get("interval", 5)),
        restart=str(raw.get("restart", "on-failure")),
        max_restarts=int(raw.get("max_restarts", 5)),
        mod_id=mod_id,
        mod_dir=mod_dir,
        mod_version=mod_version,
    )


COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def parse_mod_command(raw: dict, mod_id: str, mod_dir: Optional[Path], mod_version: str = "1.0.0") -> Optional[ModCommand]:
    """Parse declarative slash command specification from a mod manifest or file."""
    if not isinstance(raw, dict):
        return None
    raw_name = str(raw.get("name") or "").strip().lstrip("/").lower()
    if not raw_name or len(raw_name) > 32 or not COMMAND_NAME_RE.match(raw_name):
        return None

    raw_aliases = raw.get("aliases") or []
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    aliases: List[str] = [
        str(a).strip().lstrip("/").lower()
        for a in raw_aliases
        if str(a).strip() and COMMAND_NAME_RE.match(str(a).strip().lstrip("/").lower())
    ]

    handler_script = (
        raw.get("handler_script")
        or raw.get("handler")
        or raw.get("script")
        or raw.get("handler_file")
    )
    python_code = raw.get("python") or raw.get("inline_python")
    shell_cmd = raw.get("shell") or raw.get("inline_shell")

    # If 'command' is provided and handler_script/python/shell not explicitly set, determine type
    if not handler_script and not python_code and not shell_cmd and "command" in raw:
        cmd_candidate = str(raw["command"]).strip()
        if cmd_candidate.endswith((".py", ".sh")):
            handler_script = cmd_candidate
        else:
            shell_cmd = cmd_candidate

    entrypoint = str(raw.get("entrypoint") or raw.get("entry_point") or "handle").strip()
    category = str(raw.get("category") or "Custom Mods").strip()
    busy_policy = str(raw.get("busy_policy") or "reject").strip().lower()
    if busy_policy not in ("reject", "dispatch", "interrupt_then_dispatch"):
        busy_policy = "reject"
    required_args = bool(raw.get("required_args", False))
    desc = str(raw.get("description", "")).strip()

    raw_i18n = raw.get("i18n") or raw.get("locales") or {}
    i18n: Dict[str, Any] = dict(raw_i18n) if isinstance(raw_i18n, dict) else {}
    desc_ru = str(
        raw.get("description_ru")
        or (i18n.get("ru", {}).get("description") if isinstance(i18n.get("ru"), dict) else (i18n.get("ru") if isinstance(i18n.get("ru"), str) else ""))
        or ""
    ).strip()

    raw_timeout = raw.get("timeout")
    cmd_timeout: Optional[float] = None
    if raw_timeout is not None:
        try:
            cmd_timeout = float(raw_timeout)
        except (ValueError, TypeError):
            cmd_timeout = None

    return ModCommand(
        name=raw_name,
        description=desc,
        description_ru=desc_ru,
        args_hint=str(raw.get("args_hint", "")).strip(),
        handler_script=str(handler_script).strip() if handler_script else None,
        python=str(python_code).strip() if python_code else None,
        shell=str(shell_cmd).strip() if shell_cmd else None,
        entrypoint=entrypoint or "handle",
        aliases=aliases,
        category=category or "Custom Mods",
        busy_policy=busy_policy,
        required_args=required_args,
        mod_id=mod_id,
        mod_dir=mod_dir,
        mod_version=mod_version,
        i18n=i18n,
        timeout=cmd_timeout,
    )


METADATA_SCALARS: Set[str] = {
    "id", "name", "version", "author", "tier", "description", "description_ru", "title_ru",
    "enabled", "min_hermes_version", "min_engine_version", "verify_cmd", "verify_timeout"
}

METADATA_FIELDS: Set[str] = {
    "id", "name", "version", "author", "tier", "description", "description_ru", "title_ru",
    "enabled", "min_hermes_version", "min_engine_version", "verify_cmd", "verify_timeout",
    "tests", "test", "dependencies", "depends_on", "i18n", "locales"
}


def parse_inline_tests(raw: Any) -> List[Dict[str, Any]]:
    """Parse inline test snippets from manifest 'tests' or 'test' section."""
    tests: List[Dict[str, Any]] = []
    if not raw:
        return tests

    if isinstance(raw, str):
        content = raw.strip()
        if content:
            tests.append({"name": "test_inline", "code": content})
    elif isinstance(raw, list):
        for idx, item in enumerate(raw, 1):
            if isinstance(item, dict):
                t_name = str(item.get("name") or f"test_{idx}")
                t_code = str(item.get("code") or item.get("test") or item.get("script") or "").strip()
                if t_code:
                    tests.append({"name": t_name, "code": t_code})
            elif isinstance(item, str) and item.strip():
                tests.append({"name": f"test_{idx}", "code": item.strip()})
    elif isinstance(raw, dict):
        for k, v in raw.items():
            t_name = str(k).strip()
            if isinstance(v, str) and v.strip():
                tests.append({"name": t_name, "code": v.strip()})
            elif isinstance(v, dict):
                t_code = str(v.get("code") or v.get("test") or v.get("script") or "").strip()
                if t_code:
                    tests.append({"name": t_name, "code": t_code})
    return tests

NON_SECTION_DIRS: Set[str] = {
    "skills", "scripts", "handlers", "data", "logs", "docs", "templates",
    "tests", ".git", ".github", "__pycache__", ".pytest_cache", ".ruff_cache",
    "venv", ".venv", "node_modules", ".engine", ".archive", "cache", "build", "dist",
    "assets", "bin", "lib", "vendor", "static", "media", "tmp", "fixtures",
    "commands",
}

KNOWN_LIST_SECTIONS: Set[str] = {
    "targets", "commands", "monitors", "dependencies", "depends_on", "hooks"
}

KNOWN_DICT_SECTIONS: Set[str] = {
    "config", "user_config", "config_schema", "mcp_servers", "mcpServers", "telemetry", "updates", "i18n", "locales"
}

SECTION_ALIASES: Dict[str, str] = {
    "user_config": "config",
    "config_schema": "config",
    "mcpServers": "mcp_servers",
    "depends_on": "dependencies",
    "test": "tests",
    "locales": "i18n",
}


def _safe_read_yaml_or_json(path: Path) -> Any:
    """Helper to safely read YAML or JSON file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            if path.suffix == ".json":
                return json.load(f)
            return yaml.safe_load(f)
    except Exception as exc:
        sys.stderr.write(f"Warning: failed to read {path}: {exc}\n")
        return None


def _deep_merge_dict(dest: Dict[str, Any], src: Dict[str, Any]) -> None:
    """Recursively merge src dictionary into dest dictionary."""
    for k, v in src.items():
        if k in dest and isinstance(dest[k], dict) and isinstance(v, dict):
            _deep_merge_dict(dest[k], v)
        else:
            dest[k] = v


def reduce_mod_sections(mod_dir: Optional[Path], raw_manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Smart Unified Section Reducer for single-file and folder mods.

    Fully abstract, generic, and extensible reducer for ANY section X:
    - In single-file mod: declared inline as X: ...
    - In folder mod: can be declared inline in mod.yaml, in X.yaml, or split across X/*.yaml
    - Automatically discovers candidate sections from manifest keys, files, and subdirectories
    - Merges and normalizes aliases without section-specific hardcoding
    """
    compiled: Dict[str, Any] = {}
    if raw_manifest is not None:
        compiled.update(raw_manifest)
    elif mod_dir is not None:
        mod_dir = Path(mod_dir)
        for cand in ("mod.yaml", "mod.yml", f"{mod_dir.name}.yaml", f"{mod_dir.name}.yml"):
            mf = mod_dir / cand
            if mf.is_file():
                content = _safe_read_yaml_or_json(mf)
                if isinstance(content, dict):
                    compiled.update(content)
                break

    manifest_names = {"mod.yaml", "mod.yml"}
    if mod_dir is not None:
        mod_dir = Path(mod_dir)
        manifest_names.update({f"{mod_dir.name}.yaml", f"{mod_dir.name}.yml"})

    # Discover candidate sections dynamically
    candidate_sections: Set[str] = set()

    # 1. From base manifest (excluding scalar metadata)
    for k in compiled.keys():
        if k not in METADATA_SCALARS:
            candidate_sections.add(k)

    # 2. From top-level files in folder mod
    if mod_dir and mod_dir.is_dir():
        for f in sorted(mod_dir.iterdir()):
            if f.name.startswith((".", "_")) or f.name.endswith(".disabled"):
                continue
            if f.is_file():
                if f.name in manifest_names or f.name in ("secrets.yaml", "secrets.yml", "secrets.json"):
                    continue
                if f.suffix in (".yaml", ".yml"):
                    candidate_sections.add(f.stem)
                elif f.name in ("mcp.json", "mcp_servers.json"):
                    candidate_sections.add("mcp_servers")

    # 3. From top-level subdirectories in folder mod
    if mod_dir and mod_dir.is_dir():
        for d in sorted(mod_dir.iterdir()):
            if d.name.startswith((".", "_")) or d.name.endswith(".disabled"):
                continue
            if d.is_dir() and d.name not in NON_SECTION_DIRS:
                candidate_sections.add(d.name)

    # Map candidate sections to canonical names using SECTION_ALIASES
    canonical_sections: List[str] = []
    for s in sorted(candidate_sections):
        canonical = SECTION_ALIASES.get(s, s)
        if canonical not in canonical_sections:
            canonical_sections.append(canonical)

    for sec in canonical_sections:
        sec_aliases = [sec] + [k for k, v in SECTION_ALIASES.items() if v == sec]
        sorted_aliases = sorted(sec_aliases, key=lambda a: 1 if a == sec else 0)

        # Determine whether section represents a list or dict
        is_list = False
        is_dict = False

        if sec in KNOWN_LIST_SECTIONS:
            is_list = True
        elif sec in KNOWN_DICT_SECTIONS:
            is_dict = True
        else:
            for alias in sorted_aliases:
                if alias in compiled:
                    if isinstance(compiled[alias], list):
                        is_list = True
                        break
                    elif isinstance(compiled[alias], dict):
                        is_dict = True
                        break

            if not is_list and not is_dict and mod_dir:
                probe_file = None
                for alias in sorted_aliases:
                    for ext in (".yaml", ".yml", ".json"):
                        cand = mod_dir / f"{alias}{ext}"
                        if cand.is_file():
                            probe_file = cand
                            break
                    if probe_file:
                        break

                if probe_file:
                    c = _safe_read_yaml_or_json(probe_file)
                    if isinstance(c, list):
                        is_list = True
                    elif isinstance(c, dict):
                        if any(alias in c and isinstance(c[alias], list) for alias in sorted_aliases):
                            is_list = True
                        else:
                            is_dict = True
                else:
                    for alias in sorted_aliases:
                        sec_dir = mod_dir / alias
                        if sec_dir.is_dir() and alias not in NON_SECTION_DIRS:
                            for subf in sorted(sec_dir.iterdir()):
                                if subf.name.startswith((".", "_")) or subf.name.endswith(".disabled"):
                                    continue
                                if subf.suffix in (".yaml", ".yml", ".json"):
                                    c = _safe_read_yaml_or_json(subf)
                                    if isinstance(c, list):
                                        is_list = True
                                        break
                                    elif isinstance(c, dict):
                                        if any(a in c and isinstance(c[a], list) for a in sorted_aliases):
                                            is_list = True
                                            break
                                        elif any(k in c for k in ("file", "name", "id", "command")):
                                            is_list = True
                                            break
                                        else:
                                            is_dict = True
                                            break
                            if is_list or is_dict:
                                break
                if not is_list and not is_dict:
                    is_list = True

        # Process LIST section
        if is_list:
            merged_list: List[Any] = []
            # 1. Inline items
            for alias in sorted_aliases:
                inline_val = compiled.get(alias)
                if isinstance(inline_val, list):
                    merged_list.extend(inline_val)
                elif isinstance(inline_val, dict):
                    if any(k in inline_val for k in ("file", "name", "id", "command")):
                        merged_list.append(inline_val)
                    else:
                        for item_k, item_v in inline_val.items():
                            if isinstance(item_v, dict):
                                item_dict = dict(item_v)
                                if sec == "targets" and "description" not in item_dict:
                                    item_dict["description"] = f"[{item_k}]"
                                if sec == "commands" and "name" not in item_dict:
                                    item_dict["name"] = str(item_k)
                                if sec == "monitors" and "id" not in item_dict and "name" not in item_dict:
                                    item_dict["id"] = str(item_k)
                                merged_list.append(item_dict)
                            else:
                                merged_list.append(item_v)
                elif isinstance(inline_val, str):
                    merged_list.append(inline_val)

            # 2. External single file in folder mod: <sec>.yaml / <sec>.yml / <alias>.yaml
            if mod_dir:
                for alias in sorted_aliases:
                    for ext in (".yaml", ".yml"):
                        ext_f = mod_dir / f"{alias}{ext}"
                        if ext_f.is_file():
                            c = _safe_read_yaml_or_json(ext_f)
                            if isinstance(c, list):
                                for item in c:
                                    if sec == "targets" and isinstance(item, dict) and "description" not in item:
                                        item["description"] = f"[{ext_f.name}]"
                                    merged_list.append(item)
                            elif isinstance(c, dict):
                                sub_list = None
                                for a in sorted_aliases:
                                    if a in c:
                                        sub_list = c[a]
                                        break
                                if isinstance(sub_list, list):
                                    for item in sub_list:
                                        if sec == "targets" and isinstance(item, dict) and "description" not in item:
                                            item["description"] = f"[{ext_f.name}]"
                                        merged_list.append(item)
                                elif any(k in c for k in ("file", "name", "id", "command")) or sec in ("targets", "commands", "monitors"):
                                    if sec == "targets" and "description" not in c:
                                        c["description"] = f"[{ext_f.name}]"
                                    if sec == "commands" and "name" not in c:
                                        c["name"] = ext_f.stem
                                    if sec == "monitors" and "id" not in c and "name" not in c:
                                        c["id"] = ext_f.stem
                                    merged_list.append(c)

            # 3. External directory in folder mod: <sec>/*.yaml (commands directory is strictly forbidden)
            if mod_dir and sec != "commands":
                for alias in sorted_aliases:
                    sec_dir = mod_dir / alias
                    if sec_dir.is_dir() and alias not in NON_SECTION_DIRS:
                        for subf in sorted(sec_dir.iterdir()):
                            if subf.name.startswith((".", "_")) or subf.name.endswith(".disabled"):
                                continue
                            if subf.suffix in (".yaml", ".yml", ".json"):
                                c = _safe_read_yaml_or_json(subf)
                                if isinstance(c, list):
                                    for item in c:
                                        if sec == "targets" and isinstance(item, dict) and "description" not in item:
                                            item["description"] = f"[{subf.stem}]"
                                        merged_list.append(item)
                                elif isinstance(c, dict):
                                    sub_list = None
                                    for a in sorted_aliases:
                                        if a in c:
                                            sub_list = c[a]
                                            break
                                    if isinstance(sub_list, list):
                                        for item in sub_list:
                                            if sec == "targets" and isinstance(item, dict) and "description" not in item:
                                                item["description"] = f"[{subf.stem}]"
                                            merged_list.append(item)
                                    elif any(k in c for k in ("file", "name", "id", "command")) or sec in ("targets", "commands", "monitors"):
                                        if sec == "targets" and "description" not in c:
                                            c["description"] = f"[{subf.stem}]"
                                        if sec == "commands" and "name" not in c:
                                            c["name"] = subf.stem
                                        if sec == "monitors" and "id" not in c and "name" not in c:
                                            c["id"] = subf.stem
                                        merged_list.append(c)

            compiled[sec] = merged_list
            for a in sec_aliases:
                compiled[a] = merged_list

        # Process DICT section
        else:
            merged_dict: Dict[str, Any] = {}
            surfaces: Dict[str, Dict[str, Any]] = {}

            # 1. Directory splitting in folder mod: <alias>/*.yaml
            if mod_dir:
                for alias in sorted_aliases:
                    sec_dir = mod_dir / alias
                    if sec_dir.is_dir() and alias not in NON_SECTION_DIRS:
                        for subf in sorted(sec_dir.iterdir()):
                            if subf.name.startswith((".", "_")) or subf.name.endswith(".disabled"):
                                continue
                            if subf.is_file() and subf.suffix in (".yaml", ".yml", ".json"):
                                c = _safe_read_yaml_or_json(subf)
                                if isinstance(c, dict):
                                    stem = subf.stem
                                    surface = "core"
                                    key = stem
                                    if "." in stem:
                                        parts = stem.split(".", 1)
                                        key = parts[0].strip().lower().replace("_", "-")
                                        surface = parts[1].strip().lower()
                                    else:
                                        key = key.strip().lower().replace("_", "-")

                                    if key in c and isinstance(c[key], dict) and (len(c) == 1 or not any(k in c for k in ("name", "description", "title", "type"))):
                                        c = c[key]

                                    is_single_entry = (
                                        bool(re.match(r"^[a-z]{2,3}(-[a-z0-9]+)?$", key))
                                        or any(k in c for k in ("type", "command", "delivery", "old_block", "new_block", "status_label"))
                                        or (any(k in c for k in ("name", "title", "description")) and not any(isinstance(v, dict) and "type" in v for v in c.values()))
                                    )

                                    if is_single_entry:
                                        if surface == "core":
                                            if key not in merged_dict or not isinstance(merged_dict[key], dict):
                                                merged_dict[key] = {}
                                            merged_dict[key].update(c)
                                        else:
                                            if surface not in surfaces:
                                                surfaces[surface] = {}
                                            if key not in surfaces[surface]:
                                                surfaces[surface][key] = {}
                                            surfaces[surface][key].update(c)
                                    else:
                                        _deep_merge_dict(merged_dict, c)

            # 2. External single file in folder mod: <alias>.yaml / <alias>.yml / mcp.json
            if mod_dir:
                cand_files = []
                for alias in sorted_aliases:
                    cand_files.extend([f"{alias}.yaml", f"{alias}.yml"])
                    if sec == "mcp_servers":
                        cand_files.extend(["mcp_servers.json", "mcp.json"])
                for cf_name in cand_files:
                    ext_f = mod_dir / cf_name
                    if ext_f.is_file():
                        c = _safe_read_yaml_or_json(ext_f)
                        if isinstance(c, dict):
                            for a in sorted_aliases:
                                if a in c and isinstance(c[a], dict):
                                    c = c[a]
                                    break
                            _deep_merge_dict(merged_dict, c)

            # 3. Base manifest inline declarations (e.g. inline config:, i18n:, etc.)
            for alias in sorted_aliases:
                inline_val = compiled.get(alias)
                if isinstance(inline_val, dict):
                    _deep_merge_dict(merged_dict, inline_val)

            compiled[sec] = merged_dict
            for a in sec_aliases:
                compiled[a] = merged_dict
            if surfaces:
                compiled[f"{sec}_surfaces"] = surfaces

    return compiled


def _load_folder_mod_sections(mod_dir: Path, base_manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Dynamically scan mod_dir and compile all sections into a unified in-memory manifest dict.

    Backward-compatible wrapper around reduce_mod_sections.
    """
    return reduce_mod_sections(mod_dir=mod_dir, raw_manifest=base_manifest)


def parse_mod_manifest(path: Path, mod_dir: Optional[Path] = None, fallback_id: str = "") -> Optional[Mod]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw_data = yaml.safe_load(f)
        if not isinstance(raw_data, dict):
            return None

        is_folder_mod = bool(mod_dir and mod_dir.is_dir() and path.parent == mod_dir)

        if is_folder_mod:
            inline_sections = {
                k for k, v in raw_data.items()
                if k not in METADATA_FIELDS and v is not None and v != [] and v != {}
            }
            has_inline_targets = bool(raw_data.get("targets"))
            has_inline_commands = bool(raw_data.get("commands"))
            has_inline_config = bool(raw_data.get("config") or raw_data.get("user_config") or raw_data.get("config_schema"))
            has_inline_monitors = bool(raw_data.get("monitors"))
            compiled = reduce_mod_sections(mod_dir, raw_data)
        else:
            inline_sections = set()
            has_inline_targets = bool(raw_data.get("targets"))
            has_inline_commands = bool(raw_data.get("commands"))
            has_inline_config = bool(raw_data.get("config") or raw_data.get("user_config") or raw_data.get("config_schema"))
            has_inline_monitors = bool(raw_data.get("monitors"))
            compiled = reduce_mod_sections(None, raw_data)

        mod_id = str(compiled.get("id") or fallback_id)
        name = str(compiled.get("name") or mod_id)
        enabled = bool(compiled.get("enabled", True))
        version = str(compiled.get("version", "1.0.0"))

        # Targets
        targets: List[ModTarget] = []
        raw_targets = compiled.get("targets", [])
        targets_list = []
        if isinstance(raw_targets, dict):
            for tk, tv in raw_targets.items():
                if isinstance(tv, dict):
                    td = dict(tv)
                    if "description" not in td:
                        td["description"] = f"[{tk}]"
                    targets_list.append(td)
                else:
                    targets_list.append(tv)
        elif isinstance(raw_targets, list):
            for item in raw_targets:
                if isinstance(item, dict) and not any(k in item for k in ("file", "old_block", "new_block")):
                    for tk, tv in item.items():
                        if isinstance(tv, dict):
                            td = dict(tv)
                            if "description" not in td:
                                td["description"] = f"[{tk}]"
                            targets_list.append(td)
                        else:
                            targets_list.append(tv)
                else:
                    targets_list.append(item)

        for t in targets_list:
            if isinstance(t, dict) and "file" in t and "old_block" in t and "new_block" in t:
                targets.append(
                    ModTarget(
                        file=str(t["file"]).strip(),
                        old_block=str(t["old_block"]),
                        new_block=str(t["new_block"]),
                        description=str(t.get("description", "")),
                        disabled=bool(t.get("disabled", False)),
                        disabled_reason=str(t.get("disabled_reason", "")),
                    )
                )

        # Config
        config: Dict[str, ConfigField] = {}
        raw_cfg = compiled.get("config") or compiled.get("user_config") or compiled.get("config_schema") or {}
        if isinstance(raw_cfg, dict):
            for k, v in raw_cfg.items():
                if isinstance(v, dict):
                    config[k] = parse_config_field(k, v)

        # Monitors
        monitors: List[MonitorSpec] = []
        raw_mons = compiled.get("monitors") or []
        if isinstance(raw_mons, dict):
            for m_name, m_spec in raw_mons.items():
                if isinstance(m_spec, dict):
                    m_dict = dict(m_spec)
                    if "id" not in m_dict and "name" not in m_dict:
                        m_dict["id"] = str(m_name)
                    if "command" in m_dict:
                        monitors.append(parse_monitor_spec(m_dict, mod_id, mod_dir, mod_version=version))
        elif isinstance(raw_mons, list):
            for m in raw_mons:
                if isinstance(m, dict) and "command" in m:
                    monitors.append(parse_monitor_spec(m, mod_id, mod_dir, mod_version=version))

        # Commands
        commands: List[ModCommand] = []
        raw_cmds = compiled.get("commands") or []
        if isinstance(raw_cmds, dict):
            for c_name, c_spec in raw_cmds.items():
                if isinstance(c_spec, dict):
                    c_dict = dict(c_spec)
                    if "name" not in c_dict:
                        c_dict["name"] = str(c_name)
                    c_obj = parse_mod_command(c_dict, mod_id=mod_id, mod_dir=mod_dir, mod_version=version)
                    if c_obj:
                        commands.append(c_obj)
        elif isinstance(raw_cmds, list):
            for c in raw_cmds:
                if isinstance(c, dict):
                    c_obj = parse_mod_command(c, mod_id=mod_id, mod_dir=mod_dir, mod_version=version)
                    if c_obj:
                        commands.append(c_obj)

        # MCP servers
        raw_mcp = compiled.get("mcp_servers") or compiled.get("mcpServers") or {}
        mcp_servers: Dict[str, Dict[str, Any]] = {}
        if isinstance(raw_mcp, dict):
            for k, v in raw_mcp.items():
                if isinstance(v, dict):
                    mcp_servers[str(k)] = dict(v)

        # Dependencies
        raw_deps = compiled.get("dependencies") or compiled.get("depends_on") or []
        if isinstance(raw_deps, str):
            raw_deps = [raw_deps]
        dependencies: List[str] = [str(d).strip() for d in raw_deps if str(d).strip()]

        # Skills
        skills_dir: Optional[Path] = None
        if is_folder_mod:
            candidate_skills = mod_dir / "skills"
            if candidate_skills.is_dir():
                try:
                    if candidate_skills.resolve().is_relative_to(mod_dir.resolve()):
                        skills_dir = candidate_skills
                except OSError:
                    pass

        # Tests discovery
        tests_dir: Optional[Path] = None
        test_files: List[Path] = []
        raw_tests = compiled.get("tests") or compiled.get("test")
        inline_tests = parse_inline_tests(raw_tests)

        if is_folder_mod:
            # Look for native tests/ directory inside folder mod
            candidate_tests = mod_dir / "tests"
            if candidate_tests.is_dir():
                try:
                    if candidate_tests.resolve().is_relative_to(mod_dir.resolve()):
                        tests_dir = candidate_tests
                        for p in candidate_tests.rglob("*.py"):
                            if p.is_file() and (p.name.startswith("test_") or p.name.endswith("_test.py")):
                                test_files.append(p)
                except OSError:
                    pass
            # Also check single test.py or tests.py in folder mod root
            for single_test in ("test.py", "tests.py"):
                single_p = mod_dir / single_test
                if single_p.is_file() and single_p not in test_files:
                    test_files.append(single_p)
            test_files.sort()

        raw_i18n = compiled.get("i18n") or compiled.get("locales") or {}
        i18n: Dict[str, Any] = dict(raw_i18n) if isinstance(raw_i18n, dict) else {}
        desc_ru = str(
            compiled.get("description_ru")
            or (i18n.get("ru", {}).get("description") if isinstance(i18n.get("ru"), dict) else (i18n.get("ru") if isinstance(i18n.get("ru"), str) else ""))
            or ""
        ).strip()
        if desc_ru:
            if "ru" not in i18n:
                i18n["ru"] = {"description": desc_ru}
            elif isinstance(i18n["ru"], dict) and "description" not in i18n["ru"]:
                i18n["ru"]["description"] = desc_ru

        title_ru = str(
            compiled.get("title_ru")
            or (i18n.get("ru", {}).get("title") or i18n.get("ru", {}).get("name") if isinstance(i18n.get("ru"), dict) else "")
            or ""
        ).strip()
        if title_ru:
            if "ru" not in i18n:
                i18n["ru"] = {"title": title_ru}
            elif isinstance(i18n["ru"], dict) and "title" not in i18n["ru"] and "name" not in i18n["ru"]:
                i18n["ru"]["title"] = title_ru

        # Auto-propagate translations from i18n to commands and config
        for lang_k, lang_tree in i18n.items():
            if not isinstance(lang_tree, dict):
                continue
            norm_lang = str(lang_k).strip().lower().replace("_", "-")

            cmd_translations = lang_tree.get("commands") or lang_tree.get("slash") or {}
            if isinstance(cmd_translations, dict):
                for cmd in commands:
                    if cmd.name in cmd_translations and isinstance(cmd_translations[cmd.name], dict):
                        cmd_entry = cmd_translations[cmd.name]
                        if norm_lang not in cmd.i18n:
                            cmd.i18n[norm_lang] = {}
                        if isinstance(cmd.i18n[norm_lang], dict) and "description" not in cmd.i18n[norm_lang] and "description" in cmd_entry:
                            cmd.i18n[norm_lang]["description"] = str(cmd_entry["description"])
                            if norm_lang == "ru" and not cmd.description_ru:
                                cmd.description_ru = str(cmd_entry["description"])

            cfg_translations = lang_tree.get("config") or {}
            if isinstance(cfg_translations, dict):
                for field_name, cf in config.items():
                    if field_name in cfg_translations and isinstance(cfg_translations[field_name], dict):
                        f_entry = cfg_translations[field_name]
                        if norm_lang not in cf.i18n:
                            cf.i18n[norm_lang] = {}
                        if isinstance(cf.i18n[norm_lang], dict):
                            if "description" not in cf.i18n[norm_lang] and "description" in f_entry:
                                cf.i18n[norm_lang]["description"] = str(f_entry["description"])
                                if norm_lang == "ru" and not cf.description_ru:
                                    cf.description_ru = str(f_entry["description"])
                            if "title" not in cf.i18n[norm_lang] and "title" in f_entry:
                                cf.i18n[norm_lang]["title"] = str(f_entry["title"])
                                if norm_lang == "ru" and not cf.title_ru:
                                    cf.title_ru = str(f_entry["title"])

        verify_timeout = int(compiled.get("verify_timeout", 45))

        return Mod(
            id=mod_id,
            name=name,
            targets=targets,
            path=path,
            description=str(compiled.get("description", "")),
            description_ru=desc_ru,
            title_ru=title_ru,
            author=str(compiled.get("author", "")),
            version=version,
            min_hermes_version=compiled.get("min_hermes_version"),
            min_engine_version=compiled.get("min_engine_version"),
            enabled=enabled,
            verify_cmd=compiled.get("verify_cmd"),
            verify_timeout=verify_timeout,
            mod_dir=mod_dir,
            config=config,
            monitors=monitors,
            commands=commands,
            skills_dir=skills_dir,
            dependencies=dependencies,
            mcp_servers=mcp_servers,
            i18n=i18n,
            i18n_surfaces=compiled.get("i18n_surfaces", {}),
            has_inline_targets=has_inline_targets,
            has_inline_commands=has_inline_commands,
            has_inline_config=has_inline_config,
            has_inline_monitors=has_inline_monitors,
            inline_sections=inline_sections,
            sections=compiled,
            tests_dir=tests_dir,
            test_files=test_files,
            inline_tests=inline_tests,
        )
    except Exception as exc:
        sys.stderr.write(f"Error parsing mod at {path}: {exc}\n")
        return None


class ModLoader:
    """Обнаруживает и парсит однофайловые моды и моды-папки."""

    EXCLUDED_DIRS = {
        "templates", "docs", "tests", "skills", ".engine", ".git", ".github",
        "__pycache__", ".pytest_cache", ".ruff_cache", ".archive", ".venv", "venv", "node_modules", "build", "dist"
    }
    EXCLUDED_FILES = {
        "secrets.yaml", "secrets.yml", "secrets.json",
        "config.yaml", "config.yml",
    }

    @classmethod
    def _scan_directory(cls, directory: Path, mods: Dict[str, Mod]) -> None:
        if not directory.exists() or not directory.is_dir():
            return

        # 1. Сканируем моды-папки
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith((".", "_")) or entry.name.endswith(".disabled") or entry.name in cls.EXCLUDED_DIRS:
                continue

            if entry.is_dir():
                manifest = entry / "mod.yaml"
                if not manifest.exists():
                    manifest = entry / f"{entry.name}.yaml"
                if manifest.exists():
                    mod = parse_mod_manifest(manifest, mod_dir=entry, fallback_id=entry.name)
                    if mod and mod.id not in mods:
                        mods[mod.id] = mod

        # 2. Сканируем однофайловые моды (*.yaml, *.yml)
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith((".", "_")) or entry.name.endswith(".disabled") or entry.name in cls.EXCLUDED_FILES:
                continue

            if entry.is_file() and entry.suffix in (".yaml", ".yml"):
                mod_id = entry.stem
                if mod_id in mods:
                    continue
                mod = parse_mod_manifest(entry, mod_dir=None, fallback_id=mod_id)
                if mod and mod.id not in mods:
                    mods[mod.id] = mod

    @classmethod
    def load_all(cls, mods_dir: Optional[Path] = None) -> List[Mod]:
        if mods_dir is None:
            mods_dir = DEFAULT_MODS_DIR
        mods_dir = Path(mods_dir)
        if not mods_dir.exists():
            return []

        mods: Dict[str, Mod] = {}

        # 1. system/ поддиректория (системные моды)
        system_dir = mods_dir / "system"
        if system_dir.is_dir():
            cls._scan_directory(system_dir, mods)

        # 2. custom/ поддиректория (пользовательские моды)
        custom_dir = mods_dir / "custom"
        if custom_dir.is_dir():
            cls._scan_directory(custom_dir, mods)

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

        # Корень mods_dir больше НЕ опрашивается на наличие модов (полная изоляция)
        return sorted(mods.values(), key=lambda m: m.id)

    @classmethod
    def build_graph(cls, mods: Optional[List[Mod]] = None, mods_dir: Optional[Path] = None) -> ModDependencyGraph:
        if mods is None:
            mods = cls.load_all(mods_dir)
        return ModDependencyGraph(mods)

    @classmethod
    def get_active_skills_dirs(cls, mods_dir: Optional[Path] = None) -> List[Path]:
        return get_enabled_mod_skills_dirs(mods_dir)


def update_commands_cache(mods: List[Mod], mods_dir: Optional[Path] = None) -> Path:
    """Generate/update .commands_cache.json with all valid commands from active mods."""
    if mods_dir is None:
        mods_dir = DEFAULT_MODS_DIR
    mods_dir = Path(mods_dir)
    cache_path = mods_dir / ".commands_cache.json"

    try:
        from .config_manager import ModConfigManager
        if not ModConfigManager().is_mods_enabled():
            try:
                with FileLock(cache_path):
                    atomic_write_text(cache_path, "{}\n")
            except Exception:
                try:
                    cache_path.write_text("{}\n", encoding="utf-8")
                except Exception:
                    pass
            return cache_path
    except Exception:
        pass

    active_mods = [m for m in mods if m.enabled]
    commands_map: Dict[str, Dict[str, Any]] = {}
    items: List[Dict[str, Any]] = []

    for mod in active_mods:
        for cmd in mod.commands:
            c_dict = cmd.to_dict()
            commands_map[cmd.name] = c_dict
            items.append(c_dict)

    cache_data = {
        "version": "1.0",
        "generated_at": time.time(),
        "_meta": {
            "active_mods_count": len(active_mods),
            "commands_count": len(commands_map),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "commands": commands_map,
        "items": items,
    }

    try:
        with FileLock(cache_path):
            atomic_write_text(cache_path, json.dumps(cache_data, indent=2, ensure_ascii=False))
    except Exception as exc:
        sys.stderr.write(f"Warning: failed to update {cache_path}: {exc}\n")

    return cache_path


def update_mcp_cache(mods: Optional[List[Mod]] = None, mods_dir: Optional[Path] = None) -> Path:
    """Generate/update .mcp_cache.json with all valid MCP servers from active mods."""
    if mods_dir is None:
        mods_dir = DEFAULT_MODS_DIR
    mods_dir = Path(mods_dir)
    cache_path = mods_dir / ".mcp_cache.json"

    try:
        from .config_manager import ModConfigManager
        if not ModConfigManager().is_mods_enabled():
            try:
                with FileLock(cache_path):
                    atomic_write_text(cache_path, "{}\n")
            except Exception:
                try:
                    cache_path.write_text("{}\n", encoding="utf-8")
                except Exception:
                    pass
            return cache_path
    except Exception:
        pass

    try:
        servers = get_enabled_mod_mcp_servers(mods_dir)
    except Exception:
        servers = {}

    cache_data = {
        "version": "1.0",
        "generated_at": time.time(),
        "_meta": {
            "servers_count": len(servers),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "mcp_servers": servers,
    }

    try:
        with FileLock(cache_path):
            atomic_write_text(cache_path, json.dumps(cache_data, indent=2, ensure_ascii=False))
    except Exception as exc:
        try:
            cache_path.write_text(json.dumps(cache_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc2:
            sys.stderr.write(f"Warning: failed to update {cache_path}: {exc2}\n")

    return cache_path


from .i18n import (
    flatten_i18n_dict,
    compile_mod_locales,
    update_locales_cache,
    get_enabled_mod_locales,
    register_mod_locales,
)


def get_enabled_mod_skills_dirs(mods_dir: Optional[Path] = None) -> List[Path]:
    """Scan mods_dir for enabled folder mods containing a skills/ directory."""
    try:
        from .config_manager import ModConfigManager
        if not ModConfigManager().is_mods_enabled():
            return []
    except Exception:
        pass

    if mods_dir is None:
        try:
            from hermes_constants import get_hermes_home
            mods_dir = get_hermes_home() / "mods"
        except Exception:
            mods_dir = DEFAULT_MODS_DIR
    mods_dir = Path(mods_dir)
    if not mods_dir.exists():
        return []

    mods = ModLoader.load_all(mods_dir)
    return [m.skills_dir for m in mods if m.enabled and m.skills_dir and m.skills_dir.is_dir()]


def interpolate_mcp_config(
    config: Dict[str, Any],
    mod_dir: Optional[Path | str] = None,
    config_values: Optional[Dict[str, Any]] = None,
    user_config_values: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Substitute ${mod_dir}, ${MOD_DIR}, ${PLUGIN_ROOT}, ${config.<key>}, and ${user_config.<key>} in MCP server config.
    Recursively interpolates strings in command, args, env, cwd and arbitrary config fields.
    """
    mod_dir_str = str(mod_dir or "")
    cfg_values = config_values if config_values is not None else (user_config_values or {})

    def _replace_cfg(match: re.Match) -> str:
        k = match.group(1)
        val = cfg_values.get(k)
        if val is None:
            val = cfg_values.get(k.lower())
        return "" if val is None else str(val)

    def _interpolate(val: Any) -> Any:
        if isinstance(val, str):
            res = val.replace("${mod_dir}", mod_dir_str)
            res = res.replace("${MOD_DIR}", mod_dir_str)
            res = res.replace("${PLUGIN_ROOT}", mod_dir_str)
            res = re.sub(r"\$\{(?:config|user_config)\.([A-Za-z0-9_-]+)\}", _replace_cfg, res)
            return res
        elif isinstance(val, list):
            return [_interpolate(item) for item in val]
        elif isinstance(val, dict):
            return {k: _interpolate(v) for k, v in val.items()}
        return val

    return _interpolate(config)


def resolve_mod_mcp_servers(
    mod: Mod,
    config_mgr: Optional[Any] = None,
) -> Dict[str, Dict[str, Any]]:
    """Resolve and interpolate MCP servers declared by a mod.
    Auto-resolves relative paths (cwd, command, script args) against mod_dir
    and injects complete mod context (HERMES_MOD_DIR, HERMES_MOD_ID, HERMES_MOD_VERSION, HERMES_MOD_CONFIG_<KEY>).
    """
    if not mod.mcp_servers:
        return {}

    resolved_cfg: Dict[str, Any] = {}
    if config_mgr is not None and hasattr(config_mgr, "get_resolved_config"):
        resolved_cfg = config_mgr.get_resolved_config(mod) or {}
    elif mod.config:
        from .config_manager import ModConfigManager
        config_mgr = ModConfigManager()
        resolved_cfg = config_mgr.get_resolved_config(mod) or {}

    mod_dir_val = mod.mod_dir if mod.mod_dir else (mod.path.parent if mod.path else None)

    resolved_servers: Dict[str, Dict[str, Any]] = {}
    for name, srv_cfg in mod.mcp_servers.items():
        if isinstance(srv_cfg, dict):
            interpolated = interpolate_mcp_config(
                srv_cfg,
                mod_dir=mod_dir_val,
                config_values=resolved_cfg,
                user_config_values=resolved_cfg,
            )
            if not isinstance(interpolated, dict):
                continue
            cfg = dict(interpolated)

            # Auto-resolve relative paths if mod directory exists
            if mod_dir_val and mod_dir_val.is_dir():
                base_dir = mod_dir_val.resolve()
                # Auto-resolve cwd
                if "cwd" not in cfg or not cfg["cwd"]:
                    cfg["cwd"] = str(base_dir)
                else:
                    cwd_p = Path(cfg["cwd"])
                    if not cwd_p.is_absolute():
                        try:
                            resolved_cwd = (base_dir / cwd_p).resolve()
                            if resolved_cwd.is_relative_to(base_dir):
                                cfg["cwd"] = str(resolved_cwd)
                            else:
                                cfg["cwd"] = str(base_dir)
                        except (ValueError, OSError):
                            cfg["cwd"] = str(base_dir)
                    else:
                        try:
                            if not cwd_p.resolve().is_relative_to(base_dir):
                                cfg["cwd"] = str(base_dir)
                        except (ValueError, OSError):
                            cfg["cwd"] = str(base_dir)

                # Auto-resolve relative command if pointing to a file inside mod_dir
                if "command" in cfg and cfg["command"]:
                    cmd_path = Path(cfg["command"])
                    if not cmd_path.is_absolute():
                        try:
                            resolved_cmd = (base_dir / cmd_path).resolve()
                            if resolved_cmd.is_relative_to(base_dir) and resolved_cmd.exists():
                                cfg["command"] = str(resolved_cmd)
                        except (ValueError, OSError):
                            pass

                # Auto-resolve relative args pointing to files in mod_dir
                if "args" in cfg and isinstance(cfg["args"], list):
                    new_args = []
                    for arg in cfg["args"]:
                        if isinstance(arg, str) and not arg.startswith("-"):
                            arg_p = Path(arg)
                            if not arg_p.is_absolute():
                                try:
                                    resolved_arg = (base_dir / arg_p).resolve()
                                    if resolved_arg.is_relative_to(base_dir) and resolved_arg.exists():
                                        new_args.append(str(resolved_arg))
                                    else:
                                        new_args.append(arg)
                                except (ValueError, OSError):
                                    new_args.append(arg)
                            else:
                                new_args.append(arg)
                        else:
                            new_args.append(arg)
                    cfg["args"] = new_args

            # Inject complete mod context environment variables
            srv_env = dict(cfg.get("env", {})) if isinstance(cfg.get("env"), dict) else {}
            srv_env["HERMES_MOD_DIR"] = str(mod_dir_val or "")
            srv_env["HERMES_MOD_ID"] = mod.id
            srv_env["HERMES_MOD_VERSION"] = mod.version
            for k, v in resolved_cfg.items():
                if v is not None:
                    clean_k = re.sub(r"[^A-Z0-9_]", "_", str(k).upper())
                    srv_env[f"HERMES_MOD_CONFIG_{clean_k}"] = str(v)
                    srv_env[f"HERMES_MOD_OPTION_{clean_k}"] = str(v)
            cfg["env"] = srv_env

            resolved_servers[name] = cfg
    return resolved_servers


_MOD_MCP_CACHE: Dict[tuple, Dict[str, Dict[str, Any]]] = {}


def _mod_mcp_cache_clear() -> None:
    """Clear in-process cache of resolved mod MCP servers."""
    _MOD_MCP_CACHE.clear()


def _get_mods_mcp_cache_key(
    mods_dir: Path,
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
) -> tuple:
    """Compute mtime signature of mods_dir, manifest files, config.yaml, and secrets.json."""
    try:
        entries: list[Any] = [mods_dir.stat().st_mtime_ns]
        if config_path:
            try:
                if config_path.is_file():
                    entries.append(("config", config_path.stat().st_mtime_ns))
            except Exception:
                pass
        if secrets_path:
            try:
                if secrets_path.is_file():
                    entries.append(("secrets", secrets_path.stat().st_mtime_ns))
            except Exception:
                pass

        for e in sorted(mods_dir.iterdir()):
            if e.name.startswith((".", "_")) or e.name.endswith(".disabled"):
                continue
            if e.is_dir():
                m_manifest = e / "mod.yaml"
                if not m_manifest.exists():
                    m_manifest = e / f"{e.name}.yaml"
                m_mtime = m_manifest.stat().st_mtime_ns if m_manifest.exists() else 0
                mcp_mtime = 0
                for mc in ("mcp_servers.yaml", "mcp_servers.yml", "mcp_servers.json", "mcp.json"):
                    mf = e / mc
                    if mf.exists():
                        mcp_mtime = max(mcp_mtime, mf.stat().st_mtime_ns)
                entries.append((e.name, m_mtime, mcp_mtime))
            elif e.is_file() and e.suffix in (".yaml", ".yml"):
                entries.append((e.name, e.stat().st_mtime_ns))
        return tuple(entries)
    except OSError:
        return (0,)


def get_enabled_mod_mcp_servers(
    mods_dir: Optional[Path] = None,
    config_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
) -> Dict[str, Dict[str, Any]]:
    """Scan mods_dir for enabled mods and return their resolved MCP server configurations.
    Thread-safe and cached by filesystem mtime.
    """
    if mods_dir is None:
        mods_dir = DEFAULT_MODS_DIR
    mods_dir = Path(mods_dir)

    if not mods_dir.exists():
        return {}

    from .config_manager import CONFIG_FILE, SECRETS_FILE, ModConfigManager

    if config_path is None:
        cand = mods_dir.parent / "config.yaml"
        cfg_file = cand if cand.is_file() else (CONFIG_FILE if CONFIG_FILE.is_file() else None)
    else:
        cfg_file = config_path

    if cfg_file is not None:
        try:
            if not ModConfigManager(config_path=cfg_file).is_mods_enabled():
                return {}
        except Exception:
            pass

    if secrets_path is None:
        cand_sec = mods_dir.parent / "secrets.yaml"
        if not cand_sec.is_file():
            legacy_cand = mods_dir.parent / "secrets.json"
            if legacy_cand.is_file():
                cand_sec = legacy_cand
        sec_file = cand_sec if cand_sec.is_file() else (SECRETS_FILE if SECRETS_FILE.is_file() else None)
    else:
        sec_file = secrets_path

    cache_key = _get_mods_mcp_cache_key(mods_dir, cfg_file, sec_file)
    cached = _MOD_MCP_CACHE.get(cache_key)
    if cached is not None:
        return {k: dict(v) for k, v in cached.items()}

    cfg_mgr = ModConfigManager(cfg_file or Path("/dev/null"), sec_file or Path("/dev/null"))
    all_servers: Dict[str, Dict[str, Any]] = {}

    try:
        # Load all mods in mods_dir
        mods = ModLoader.load_all(mods_dir)
        for mod in mods:
            if mod.enabled and mod.mcp_servers:
                resolved = resolve_mod_mcp_servers(mod, cfg_mgr)
                for s_name, s_cfg in resolved.items():
                    if s_name in all_servers:
                        sys.stderr.write(f"Warning: MCP server '{s_name}' from mod '{mod.id}' conflicts with another mod; skipping\n")
                    else:
                        all_servers[s_name] = s_cfg

        _MOD_MCP_CACHE[cache_key] = {k: dict(v) for k, v in all_servers.items()}
    except Exception as exc:
        sys.stderr.write(f"Warning: error discovering mod MCP servers: {exc}\n")

    return {k: dict(v) for k, v in all_servers.items()}


class ModDependencyGraph:
    """Directed Acyclic Graph (DAG) of mod dependencies with cycle detection and topological sorting."""

    def __init__(self, mods: List[Mod]):
        self.mods: Dict[str, Mod] = {m.id: m for m in mods}
        # Build normalized index for lookup by id, name, stem, or alias
        self._lookup: Dict[str, str] = {}
        for m in mods:
            self._lookup[m.id.lower()] = m.id
            self._lookup[m.name.lower()] = m.id
            self._lookup[m.path.stem.lower()] = m.id

    @staticmethod
    def normalize_spec(spec: str) -> str:
        """Extract canonical name from dependency specifier (e.g. 'foo@1.0.0' or 'foo>=1.0' -> 'foo')."""
        s = spec.strip()
        parts = re.split(r"[@>=<]", s)
        return parts[0].strip().lower() if parts else s.lower()

    def resolve_mod(self, spec: str) -> Optional[Mod]:
        """Resolve a dependency spec to a loaded Mod instance."""
        norm = self.normalize_spec(spec)
        mod_id = self._lookup.get(norm)
        if mod_id and mod_id in self.mods:
            return self.mods[mod_id]
        return None

    def get_dependencies(self, mod: Mod) -> List[Mod]:
        """Return resolved direct dependencies for a mod."""
        resolved = []
        for dep_spec in mod.dependencies:
            dep_mod = self.resolve_mod(dep_spec)
            if dep_mod and dep_mod not in resolved:
                resolved.append(dep_mod)
        return resolved

    def get_missing_dependencies(self, mod: Mod) -> List[str]:
        """Return list of dependency specifiers that cannot be resolved to any loaded mod."""
        missing = []
        for dep_spec in mod.dependencies:
            if not self.resolve_mod(dep_spec):
                missing.append(dep_spec)
        return missing

    def get_disabled_dependencies(self, mod: Mod) -> List[Mod]:
        """Return list of resolved direct dependencies that are currently disabled."""
        disabled = []
        for dep_mod in self.get_dependencies(mod):
            if not dep_mod.enabled:
                disabled.append(dep_mod)
        return disabled

    def get_dependents(self, mod: Mod, enabled_only: bool = False) -> List[Mod]:
        """Return list of mods that directly depend on the given mod (reverse dependencies)."""
        dependents = []
        for other in self.mods.values():
            if enabled_only and not other.enabled:
                continue
            if other.id == mod.id:
                continue
            for dep_spec in other.dependencies:
                dep_mod = self.resolve_mod(dep_spec)
                if dep_mod and dep_mod.id == mod.id:
                    dependents.append(other)
                    break
        return sorted(dependents, key=lambda m: m.id)

    def detect_cycles(self, subset: Optional[List[Mod]] = None) -> None:
        """Detect cyclic dependencies in the graph. Raises CycleDetectionError if a cycle is found."""
        target_ids = {m.id for m in subset} if subset is not None else set(self.mods.keys())
        visited: Set[str] = set()
        rec_stack: List[str] = []
        rec_set: Set[str] = set()

        def dfs(node_id: str):
            visited.add(node_id)
            rec_stack.append(node_id)
            rec_set.add(node_id)

            mod = self.mods.get(node_id)
            if mod:
                for dep_spec in mod.dependencies:
                    dep_mod = self.resolve_mod(dep_spec)
                    if not dep_mod:
                        continue
                    dep_id = dep_mod.id
                    if dep_id in rec_set:
                        # Cycle detected!
                        idx = rec_stack.index(dep_id)
                        cycle_path = rec_stack[idx:] + [dep_id]
                        raise CycleDetectionError(cycle=cycle_path)
                    if dep_id not in visited:
                        dfs(dep_id)

            rec_stack.pop()
            rec_set.remove(node_id)

        for mod_id in sorted(target_ids):
            if mod_id not in visited and mod_id in self.mods:
                dfs(mod_id)

    def topological_sort(self, mods: Optional[List[Mod]] = None) -> List[Mod]:
        """
        Sort mods in topological order: base mods (dependencies) appear before dependent mods.
        Raises CycleDetectionError if a cycle is detected.
        """
        target_mods = mods if mods is not None else list(self.mods.values())
        target_ids = {m.id for m in target_mods}

        # Check cycles first
        self.detect_cycles(target_mods)

        order: List[Mod] = []
        visited: Set[str] = set()

        def _mod_tier_key(m: Mod) -> Tuple[int, str]:
            is_sys = 0 if ((m.author or "").strip().lower() == "engine" or (m.path and "system" in m.path.parts)) else 1
            return (is_sys, m.id)

        def visit(node_id: str):
            if node_id in visited:
                return
            visited.add(node_id)

            mod = self.mods.get(node_id)
            if mod:
                # Visit dependencies first (dependencies come before dependent in apply order)
                deps = []
                for dep_spec in mod.dependencies:
                    dep_mod = self.resolve_mod(dep_spec)
                    if dep_mod and dep_mod.id in target_ids:
                        deps.append(dep_mod)
                deps.sort(key=_mod_tier_key)
                for dep_mod in deps:
                    visit(dep_mod.id)

                order.append(mod)

        for mod in sorted(target_mods, key=_mod_tier_key):
            visit(mod.id)

        return order

    def get_dependency_closure(self, mods: List[Mod]) -> List[Mod]:
        """
        Return the transitive dependency closure for the given mods,
        sorted in topological order (base dependencies first).
        """
        closure_ids: Set[str] = set()

        def collect(mod: Mod):
            closure_ids.add(mod.id)
            for dep_mod in self.get_dependencies(mod):
                if dep_mod.id not in closure_ids:
                    collect(dep_mod)

        for m in mods:
            collect(m)

        closure_mods = [self.mods[mid] for mid in closure_ids if mid in self.mods]
        return self.topological_sort(closure_mods)
