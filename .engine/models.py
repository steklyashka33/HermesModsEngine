"""Data models for Hermes Mod Engine (Targets, Config, Monitors)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .i18n import t as engine_t, normalize_language_id

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass
class ModTarget:
    file: str
    old_block: str
    new_block: str
    description: str = ""
    disabled: bool = False
    disabled_reason: str = ""


@dataclass
class ConfigField:
    name: str
    type: str  # string, int, bool, file, dir
    description: str = ""
    description_ru: str = ""
    title: str = ""
    title_ru: str = ""
    required: bool = False
    default: Any = None
    sensitive: bool = False
    options: Optional[List[Any]] = None
    min: Optional[int] = None
    max: Optional[int] = None
    multiple: bool = False
    i18n: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.i18n, dict):
            self.i18n = {}
        if self.description_ru:
            ru_entry = self.i18n.setdefault("ru", {})
            if isinstance(ru_entry, dict) and "description" not in ru_entry:
                ru_entry["description"] = self.description_ru
        elif "ru" in self.i18n:
            ru_val = self.i18n["ru"]
            if isinstance(ru_val, dict) and "description" in ru_val:
                self.description_ru = str(ru_val["description"])
            elif isinstance(ru_val, str):
                self.description_ru = ru_val
        if self.title_ru:
            ru_entry = self.i18n.setdefault("ru", {})
            if isinstance(ru_entry, dict) and "title" not in ru_entry:
                ru_entry["title"] = self.title_ru
        elif "ru" in self.i18n:
            ru_val = self.i18n["ru"]
            if isinstance(ru_val, dict) and "title" in ru_val:
                self.title_ru = str(ru_val["title"])

    def get_description(self, lang: str = "ru") -> str:
        """Return localized description with fallback to English source of truth."""
        norm_lang = normalize_language_id(lang) or lang
        if self.i18n and norm_lang in self.i18n:
            entry = self.i18n[norm_lang]
            if isinstance(entry, dict) and entry.get("description"):
                return str(entry["description"])
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()
        if norm_lang == "ru" and self.description_ru:
            return self.description_ru
        if getattr(self, "mod_id", None):
            key = f"mods.{self.mod_id}.config.{self.name}.description"
            val = engine_t(key, lang=norm_lang, default=None)
            if val and val != key:
                return val
        return self.description or ""

    def get_title(self, lang: str = "ru") -> str:
        """Return localized title with fallback to English source of truth."""
        norm_lang = normalize_language_id(lang) or lang
        if self.i18n and norm_lang in self.i18n:
            entry = self.i18n[norm_lang]
            if isinstance(entry, dict) and (entry.get("title") or entry.get("name")):
                return str(entry.get("title") or entry.get("name"))
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()
        if norm_lang == "ru" and self.title_ru:
            return self.title_ru
        if getattr(self, "mod_id", None):
            key = f"mods.{self.mod_id}.config.{self.name}.title"
            val = engine_t(key, lang=norm_lang, default=None)
            if val and val != key:
                return val
        return self.title or self.name or ""

    @property
    def locales(self) -> Dict[str, Any]:
        """Backward-compatibility alias for i18n."""
        return self.i18n

    @locales.setter
    def locales(self, val: Dict[str, Any]) -> None:
        self.i18n = val

    def validate_value(self, value: Any) -> tuple[bool, Any, str]:
        """Validate and coerce value against field specification."""
        if value is None or value == "":
            if self.required:
                return False, None, f"Field '{self.name}' is required."
            return True, self.default, ""

        norm_type = str(self.type).strip().lower()
        if norm_type in ("int", "integer"):
            if isinstance(value, bool):
                return False, None, f"Field '{self.name}' must be an integer, got boolean {value}."
            try:
                coerced = int(value)
            except (ValueError, TypeError):
                return False, None, f"Field '{self.name}' must be an integer, got {value!r}."
            if self.min is not None and coerced < self.min:
                return False, None, f"Field '{self.name}' value {coerced} is below minimum {self.min}."
            if self.max is not None and coerced > self.max:
                return False, None, f"Field '{self.name}' value {coerced} is above maximum {self.max}."
            if self.options and coerced not in self.options:
                return False, None, f"Field '{self.name}' value {coerced} not in allowed options {self.options}."
            return True, coerced, ""

        elif norm_type in ("float", "number"):
            if isinstance(value, bool):
                return False, None, f"Field '{self.name}' must be a float, got boolean {value}."
            try:
                coerced = float(value)
            except (ValueError, TypeError):
                return False, None, f"Field '{self.name}' must be a float, got {value!r}."
            if self.min is not None and coerced < self.min:
                return False, None, f"Field '{self.name}' value {coerced} is below minimum {self.min}."
            if self.max is not None and coerced > self.max:
                return False, None, f"Field '{self.name}' value {coerced} is above maximum {self.max}."
            if self.options and coerced not in self.options:
                return False, None, f"Field '{self.name}' value {coerced} not in allowed options {self.options}."
            return True, coerced, ""

        elif norm_type in ("bool", "boolean"):
            if isinstance(value, bool):
                return True, value, ""
            s = str(value).strip().lower()
            if s in ("true", "1", "yes", "on", "y"):
                return True, True, ""
            if s in ("false", "0", "no", "off", "n"):
                return True, False, ""
            return False, None, f"Field '{self.name}' must be a boolean (true/false), got {value!r}."

        elif norm_type in ("file", "dir"):
            s = str(value).strip()
            p = Path(s).expanduser()
            if not p.exists():
                return False, None, f"Path for '{self.name}' does not exist: {s}"
            if norm_type == "file" and not p.is_file():
                return False, None, f"Path for '{self.name}' is not a regular file: {s}"
            if norm_type == "dir" and not p.is_dir():
                return False, None, f"Path for '{self.name}' is not a directory: {s}"
            return True, str(p.resolve()), ""

        elif norm_type in ("list", "array"):
            if isinstance(value, list):
                return True, value, ""
            if isinstance(value, (tuple, set)):
                return True, list(value), ""
            if isinstance(value, str):
                s = value.strip()
                if s.startswith("[") and s.endswith("]"):
                    import json
                    try:
                        parsed = json.loads(s)
                        if isinstance(parsed, list):
                            return True, parsed, ""
                    except Exception:
                        pass
                items = [x.strip() for x in s.split(",") if x.strip()]
                return True, items, ""
            return False, None, f"Field '{self.name}' must be a list, got {type(value).__name__}."

        else:  # string / text
            if isinstance(value, (dict, list)):
                return False, None, f"Field '{self.name}' must be a string, got {type(value).__name__}."
            s = str(value).strip()
            if self.options and s not in [str(o) for o in self.options]:
                return False, None, f"Field '{self.name}' value {s!r} not in allowed options {self.options}."
            return True, s, ""


@dataclass
class MonitorSpec:
    id: str
    command: str
    name: str = ""
    description: str = ""
    when: str = "always"  # always, on-demand
    delivery: str = "chat"  # chat, agent, log
    interval: int = 5  # check/heartbeat interval (seconds)
    restart: str = "on-failure"  # no, on-failure, always
    max_restarts: int = 5
    mod_id: str = ""
    mod_dir: Optional[Path] = None
    mod_version: str = "1.0.0"


@dataclass
class ModCommand:
    name: str
    description: str = ""
    description_ru: str = ""
    args_hint: str = ""
    handler_script: Optional[str] = None  # path to .py or .sh script
    python: Optional[str] = None          # inline python snippet
    shell: Optional[str] = None           # inline shell command
    entrypoint: str = "handle"
    aliases: List[str] = field(default_factory=list)
    category: str = "Custom Mods"
    busy_policy: str = "reject"           # reject | dispatch
    required_args: bool = False
    mod_id: str = ""
    mod_dir: Optional[Path] = None
    mod_version: str = "1.0.0"
    i18n: Dict[str, Any] = field(default_factory=dict)
    timeout: Optional[float] = None

    def __post_init__(self):
        if self.name:
            self.name = self.name.strip().lstrip("/").lower()
        if isinstance(self.aliases, str):
            self.aliases = [self.aliases]
        self.aliases = [
            str(a).strip().lstrip("/").lower()
            for a in self.aliases
            if str(a).strip()
        ]
        if not isinstance(self.i18n, dict):
            self.i18n = {}
        if self.description_ru:
            ru_entry = self.i18n.setdefault("ru", {})
            if isinstance(ru_entry, dict) and "description" not in ru_entry:
                ru_entry["description"] = self.description_ru
        elif "ru" in self.i18n:
            ru_val = self.i18n["ru"]
            if isinstance(ru_val, dict) and "description" in ru_val:
                self.description_ru = str(ru_val["description"])
            elif isinstance(ru_val, str):
                self.description_ru = ru_val
        if self.timeout is not None:
            try:
                self.timeout = float(self.timeout)
            except (ValueError, TypeError):
                self.timeout = None

    def get_description(self, lang: str = "ru") -> str:
        """Return localized description with fallback to English source of truth."""
        norm_lang = normalize_language_id(lang) or lang
        if self.i18n and norm_lang in self.i18n:
            entry = self.i18n[norm_lang]
            if isinstance(entry, dict) and entry.get("description"):
                return str(entry["description"])
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()

        if norm_lang == "ru" and self.description_ru:
            return self.description_ru

        if self.mod_id:
            mod_key = f"mods.{self.mod_id}.commands.{self.name}.description"
            val = engine_t(mod_key, lang=norm_lang, default=None)
            if val and val != mod_key:
                return val

        if self.name:
            slash_key = f"slash.{self.name}.description"
            val = engine_t(slash_key, lang=norm_lang, default=None)
            if val and val != slash_key:
                return val

        return self.description or ""

    def get_title(self, lang: str = "ru") -> str:
        """Return localized title with fallback to English source of truth."""
        if self.i18n and lang in self.i18n:
            entry = self.i18n[lang]
            if isinstance(entry, dict) and (entry.get("title") or entry.get("name")):
                return str(entry.get("title") or entry.get("name"))
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()
        return self.name

    def resolve_handler_path(self) -> Optional[Path]:
        """Resolve handler_script against mod_dir if relative."""
        if not self.handler_script:
            return None
        p = Path(self.handler_script)
        if not p.is_absolute():
            base_dir = self.mod_dir
            if not base_dir and hasattr(self, "mod_path") and self.mod_path:
                mp = Path(self.mod_path)
                base_dir = mp.parent if mp.is_file() else mp
            if base_dir:
                p = (Path(base_dir) / p).resolve()
        return p

    @property
    def resolved_handler_path(self) -> Optional[Path]:
        """Auto-resolve handler path relative to mod directory."""
        return self.resolve_handler_path()

    @property
    def locales(self) -> Dict[str, Any]:
        """Backward-compatibility alias for i18n."""
        return self.i18n

    @locales.setter
    def locales(self, val: Dict[str, Any]) -> None:
        self.i18n = val

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for registry / cache (.commands_cache.json)."""
        resolved_script = str(self.resolve_handler_path()) if self.handler_script else None
        return {
            "name": self.name,
            "description": self.description,
            "description_ru": self.description_ru,
            "args_hint": self.args_hint,
            "handler_script": str(self.handler_script) if self.handler_script else None,
            "handler_file": resolved_script,
            "python": self.python,
            "shell": self.shell,
            "entrypoint": self.entrypoint,
            "aliases": self.aliases,
            "category": self.category,
            "busy_policy": self.busy_policy,
            "required_args": self.required_args,
            "mod_id": self.mod_id,
            "mod_dir": str(self.mod_dir) if self.mod_dir else None,
            "mod_version": self.mod_version,
            "i18n": self.i18n,
            "timeout": self.timeout,
        }


class CycleDetectionError(Exception):
    """Raised when cyclic dependencies are detected in the mod dependency graph."""

    def __init__(self, cycle: List[str], message: str = ""):
        self.cycle = cycle
        msg = message or f"Cyclic dependency detected: {' -> '.join(cycle)}"
        super().__init__(msg)


@dataclass
class Mod:
    name: str
    targets: List[ModTarget]
    path: Path
    id: str = ""
    description: str = ""
    description_ru: str = ""
    title_ru: str = ""
    author: str = ""
    version: str = "1.0.0"
    min_hermes_version: Optional[str] = None
    min_engine_version: Optional[str] = None
    enabled: bool = True
    disabled_circuit_breaker: bool = False
    disabled_reason: Optional[str] = None
    verify_cmd: Optional[str] = None
    verify_timeout: int = 45
    mod_dir: Optional[Path] = None
    config: Dict[str, ConfigField] = field(default_factory=dict)
    monitors: List[MonitorSpec] = field(default_factory=list)
    commands: List[ModCommand] = field(default_factory=list)
    skills_dir: Optional[Path] = None
    dependencies: List[str] = field(default_factory=list)
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    i18n: Dict[str, Any] = field(default_factory=dict)
    i18n_surfaces: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    has_inline_targets: bool = False
    has_inline_commands: bool = False
    has_inline_config: bool = False
    has_inline_monitors: bool = False
    inline_sections: Set[str] = field(default_factory=set)
    sections: Dict[str, Any] = field(default_factory=dict)
    tests_dir: Optional[Path] = None
    test_files: List[Path] = field(default_factory=list)
    inline_tests: List[Dict[str, Any]] = field(default_factory=list)

    def get_description(self, lang: str = "ru") -> str:
        """Return localized description with fallback to English source of truth."""
        norm_lang = normalize_language_id(lang) or lang
        if self.i18n and norm_lang in self.i18n:
            entry = self.i18n[norm_lang]
            if isinstance(entry, dict) and entry.get("description"):
                return str(entry["description"])
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()

        if norm_lang == "ru" and self.description_ru:
            return self.description_ru

        if self.id:
            val = engine_t(f"mods.{self.id}.description", lang=norm_lang, default=None)
            if val and val != f"mods.{self.id}.description":
                return val

        return self.description or ""

    def get_title(self, lang: str = "ru") -> str:
        """Return localized title with fallback to English source of truth."""
        norm_lang = normalize_language_id(lang) or lang
        if self.i18n and norm_lang in self.i18n:
            entry = self.i18n[norm_lang]
            if isinstance(entry, dict) and (entry.get("title") or entry.get("name")):
                return str(entry.get("title") or entry.get("name"))
            elif isinstance(entry, str) and entry.strip():
                return entry.strip()

        if norm_lang == "ru" and getattr(self, "title_ru", ""):
            return self.title_ru

        if self.id:
            val = engine_t(f"mods.{self.id}.name", lang=norm_lang, default=None)
            if val and val != f"mods.{self.id}.name":
                return val

        return self.name

    def get_name(self, lang: str = "ru") -> str:
        return self.get_title(lang)

    @property
    def user_config(self) -> Dict[str, ConfigField]:
        """Backward-compatibility alias for config."""
        return self.config

    @user_config.setter
    def user_config(self, val: Dict[str, ConfigField]) -> None:
        self.config = val

    @property
    def locales(self) -> Dict[str, Any]:
        """Backward-compatibility alias for i18n."""
        return self.i18n

    @locales.setter
    def locales(self, val: Dict[str, Any]) -> None:
        self.i18n = val

    def resolve_path(self, relative_path: str | Path) -> Path:
        """Resolve a relative path against this mod's directory (or mod file parent)."""
        p = Path(relative_path)
        if p.is_absolute():
            return p
        base = self.mod_dir or (self.path.parent if self.path else Path.cwd())
        return (base / p).resolve()

    @property
    def has_inline_user_config(self) -> bool:
        """Backward-compatibility alias for has_inline_config."""
        return self.has_inline_config

    @has_inline_user_config.setter
    def has_inline_user_config(self, val: bool) -> None:
        self.has_inline_config = val

    @property
    def has_tests(self) -> bool:
        """Return True if mod defines native folder tests or inline YAML tests."""
        return bool(self.test_files or self.inline_tests)

    @property
    def tests_count(self) -> int:
        return len(self.test_files) + len(self.inline_tests)

    @property
    def is_folder(self) -> bool:
        """Return True if this mod is a folder-based mod with its own directory."""
        return bool(self.mod_dir and self.mod_dir.is_dir())

    @property
    def has_skills(self) -> bool:
        return bool(self.skills_dir and self.skills_dir.is_dir())

    @property
    def has_commands(self) -> bool:
        return bool(self.commands)

    @property
    def has_mcp_servers(self) -> bool:
        return bool(self.mcp_servers)

    def list_mcp_servers(self) -> List[str]:
        return sorted(self.mcp_servers.keys())

    def list_skills(self) -> List[Path]:
        """Return list of SKILL.md paths inside this mod's skills_dir."""
        if not self.skills_dir or not self.skills_dir.is_dir():
            return []
        skills = []
        try:
            base_resolved = self.skills_dir.resolve()
            for p in self.skills_dir.rglob("SKILL.md"):
                try:
                    if p.is_file() and p.resolve().is_relative_to(base_resolved):
                        skills.append(p)
                except OSError:
                    continue
        except OSError:
            return []
        return sorted(skills)

    def __post_init__(self):
        # name and id are always identical — one single mod name
        if not self.name and self.id:
            self.name = self.id
        elif not self.id and self.name:
            self.id = self.name
        if isinstance(self.dependencies, str):
            self.dependencies = [self.dependencies]
        self.dependencies = [str(d).strip() for d in self.dependencies if str(d).strip()]
        if not isinstance(self.mcp_servers, dict):
            self.mcp_servers = {}
        if not isinstance(self.i18n, dict):
            self.i18n = {}
        if not isinstance(self.i18n_surfaces, dict):
            self.i18n_surfaces = {}
        if self.description_ru:
            ru_entry = self.i18n.setdefault("ru", {})
            if isinstance(ru_entry, dict) and "description" not in ru_entry:
                ru_entry["description"] = self.description_ru
        elif "ru" in self.i18n:
            ru_val = self.i18n["ru"]
            if isinstance(ru_val, dict) and "description" in ru_val:
                self.description_ru = str(ru_val["description"])
            elif isinstance(ru_val, str):
                self.description_ru = ru_val
        if getattr(self, "title_ru", ""):
            ru_entry = self.i18n.setdefault("ru", {})
            if isinstance(ru_entry, dict) and "title" not in ru_entry:
                ru_entry["title"] = self.title_ru
        elif "ru" in self.i18n:
            ru_val = self.i18n["ru"]
            if isinstance(ru_val, dict) and ("title" in ru_val or "name" in ru_val):
                self.title_ru = str(ru_val.get("title") or ru_val.get("name"))
        for cmd in self.commands:
            if not cmd.mod_dir and self.mod_dir:
                cmd.mod_dir = self.mod_dir
            elif not cmd.mod_dir and self.path and self.is_folder:
                cmd.mod_dir = self.path.parent

        # Auto-propagate translations from i18n to commands and config
        for lang_k, lang_tree in self.i18n.items():
            if not isinstance(lang_tree, dict):
                continue
            norm_lang = str(lang_k).strip().lower().replace("_", "-")

            cmd_translations = lang_tree.get("commands") or lang_tree.get("slash") or {}
            if isinstance(cmd_translations, dict):
                for cmd in self.commands:
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
                for field_name, cf in self.config.items():
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


_orig_mod_init = Mod.__init__


def _compat_mod_init(self, *args, **kwargs):
    if "user_config" in kwargs and "config" not in kwargs:
        kwargs["config"] = kwargs.pop("user_config")
    elif "user_config" in kwargs:
        kwargs.pop("user_config")
    if "has_inline_user_config" in kwargs and "has_inline_config" not in kwargs:
        kwargs["has_inline_config"] = kwargs.pop("has_inline_user_config")
    elif "has_inline_user_config" in kwargs:
        kwargs.pop("has_inline_user_config")
    if "locales" in kwargs and "i18n" not in kwargs:
        kwargs["i18n"] = kwargs.pop("locales")
    elif "locales" in kwargs:
        loc = kwargs.pop("locales")
        if isinstance(loc, dict) and isinstance(kwargs.get("i18n"), dict):
            merged = dict(loc)
            merged.update(kwargs["i18n"])
            kwargs["i18n"] = merged
    _orig_mod_init(self, *args, **kwargs)


Mod.__init__ = _compat_mod_init

