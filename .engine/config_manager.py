"""Manager for mod config and secure secrets storage."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import yaml
from .models import Mod, ConfigField

try:
    from ruamel.yaml import YAML
    from ruamel.yaml.comments import CommentedMap
except ImportError:
    YAML = None
    CommentedMap = dict

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
CONFIG_FILE = Path(os.environ.get("HERMES_CONFIG_FILE", str(HERMES_HOME / "config.yaml")))
SECRETS_FILE = Path(os.environ.get("HERMES_SECRETS_FILE", str(CONFIG_FILE.parent / "secrets.yaml")))


class SecretsStorage:
    """Secure YAML storage for mod secrets with 0600 permissions."""

    def __init__(self, path: Path = SECRETS_FILE):
        self.path = Path(path)

    def _migrate_from_json_if_needed(self) -> None:
        """Migrate legacy secrets.json to secrets.yaml if found."""
        legacy_candidates = [
            self.path.parent / "secrets.json",
            self.path.with_suffix(".json"),
        ]
        for legacy in legacy_candidates:
            if legacy.is_file() and legacy != self.path:
                try:
                    data = {}
                    try:
                        with open(legacy, "r", encoding="utf-8") as f:
                            raw = json.load(f)
                            if isinstance(raw, dict):
                                data = raw
                    except Exception:
                        pass
                    # If target secrets.yaml does not exist or is empty, write migrated data
                    if not self.path.exists() or self.path.stat().st_size == 0:
                        content = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
                        atomic_write_text(self.path, content)
                        try:
                            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
                        except OSError:
                            pass
                    # Delete legacy json and its lock file
                    try:
                        legacy.unlink()
                    except OSError:
                        pass
                    lock_file = legacy.parent / f".{legacy.name}.lock"
                    if lock_file.exists():
                        try:
                            lock_file.unlink()
                        except OSError:
                            pass
                except Exception as exc:
                    import sys
                    sys.stderr.write(f"Warning: secrets.json migration failed: {exc}\n")

    def _ensure_dir_and_perms(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate_from_json_if_needed()
        if not self.path.exists():
            atomic_write_text(self.path, "{}\n")
        # Ensure 0600 (owner read/write only)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _load_unlocked(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            self._migrate_from_json_if_needed()
        if not self.path.exists():
            return {}
        try:
            content = self.path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def load_all(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            self._migrate_from_json_if_needed()
        if not self.path.exists():
            return {}
        try:
            with FileLock(self.path):
                return self._load_unlocked()
        except Exception:
            return self._load_unlocked()

    def get_mod_secrets(self, mod_id: str) -> Dict[str, str]:
        all_secrets = self.load_all()
        mods_sec = all_secrets.get("mods")
        if not isinstance(mods_sec, dict):
            return {}
        mod_sec = mods_sec.get(mod_id, {})
        return dict(mod_sec) if isinstance(mod_sec, dict) else {}

    def set_mod_secret(self, mod_id: str, key: str, value: str) -> None:
        with FileLock(self.path):
            self._ensure_dir_and_perms()
            all_secrets = self._load_unlocked()
            mods_sec = all_secrets.setdefault("mods", {})
            if not isinstance(mods_sec, dict):
                mods_sec = {}
                all_secrets["mods"] = mods_sec
            mod_dict = mods_sec.setdefault(mod_id, {})
            if not isinstance(mod_dict, dict):
                mod_dict = {}
                mods_sec[mod_id] = mod_dict
            mod_dict[key] = str(value)
            content = yaml.safe_dump(all_secrets, allow_unicode=True, default_flow_style=False, sort_keys=False)
            atomic_write_text(self.path, content)
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    def remove_mod_secret(self, mod_id: str, key: str) -> bool:
        with FileLock(self.path):
            if not self.path.exists():
                return False
            all_secrets = self._load_unlocked()
            mods_sec = all_secrets.get("mods")
            if not isinstance(mods_sec, dict):
                return False
            mod_sec = mods_sec.get(mod_id, {})
            if isinstance(mod_sec, dict) and key in mod_sec:
                del mod_sec[key]
                content = yaml.safe_dump(all_secrets, allow_unicode=True, default_flow_style=False, sort_keys=False)
                atomic_write_text(self.path, content)
                try:
                    os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
                return True
            return False


def _create_yaml_parser() -> Any:
    if YAML is None:
        return None
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


class _PopulateDefaultsDescriptor:
    """Descriptor enabling populate_defaults to be called as either classmethod or instance method."""

    def __init__(self, func):
        self.func = func
        self.__doc__ = func.__doc__
        self.__name__ = func.__name__

    def __get__(self, instance, owner=None):
        if instance is None:
            def _classmethod_wrapper(
                mods: Iterable[Mod] | Mod,
                config_path: Optional[Union[Path, str]] = None,
            ) -> Dict[str, Any]:
                target_path = Path(config_path) if config_path is not None else CONFIG_FILE
                mgr = owner(config_path=target_path)
                return mgr._populate_defaults_impl(mods, config_path=target_path)

            _classmethod_wrapper.__doc__ = self.__doc__
            _classmethod_wrapper.__name__ = self.__name__
            return _classmethod_wrapper
        else:
            def _method_wrapper(
                mods: Iterable[Mod] | Mod,
                config_path: Optional[Union[Path, str]] = None,
            ) -> Dict[str, Any]:
                target_path = Path(config_path) if config_path is not None else instance.config_path
                return instance._populate_defaults_impl(mods, config_path=target_path)

            _method_wrapper.__doc__ = self.__doc__
            _method_wrapper.__name__ = self.__name__
            return _method_wrapper


class ModConfigManager:
    """Handles reading, writing, and validating mod config."""

    def __init__(self, config_path: Path = CONFIG_FILE, secrets_path: Optional[Path] = None):
        self.config_path = Path(config_path)
        sec_path = Path(secrets_path) if secrets_path is not None else (self.config_path.parent / "secrets.yaml")
        self.secrets = SecretsStorage(sec_path)

    def load_raw_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def is_mods_enabled(self) -> bool:
        """Return whether mods are globally enabled (mods.enabled in config.yaml, default True)."""
        raw = self.load_raw_config()
        mods_sec = raw.get("mods")
        if isinstance(mods_sec, dict):
            val = mods_sec.get("enabled")
            if val is not None:
                return bool(val)
        elif isinstance(mods_sec, bool):
            return mods_sec
        return True

    def get_mod_stored_options(self, mod_id: str, mod_name: str = "") -> Dict[str, Any]:
        raw = self.load_raw_config()
        mods_sec = raw.get("mods", {})
        if not isinstance(mods_sec, dict):
            return {}

        # Check by mod_id first, then mod_name
        mod_cfg = mods_sec.get(mod_id)
        if not isinstance(mod_cfg, dict) and mod_name:
            mod_cfg = mods_sec.get(mod_name)

        if isinstance(mod_cfg, dict):
            # If user wrote nested config: or user_config:, extract it for compatibility
            nested_cfg = None
            if "config" in mod_cfg and isinstance(mod_cfg["config"], dict):
                nested_cfg = mod_cfg["config"]
            elif "user_config" in mod_cfg and isinstance(mod_cfg["user_config"], dict):
                nested_cfg = mod_cfg["user_config"]

            if nested_cfg is not None:
                merged = dict(nested_cfg)
                # Add flat keys as well
                for k, v in mod_cfg.items():
                    if k not in ("config", "user_config"):
                        merged[k] = v
                return merged
            # Pure flat structure: mods.<name>.<key> = value
            return dict(mod_cfg)
        return {}

    def get_resolved_config(self, mod: Mod) -> Dict[str, Any]:
        """Merge default, config.yaml (non-sensitive), and secrets.json (sensitive)."""
        stored_opts = self.get_mod_stored_options(mod.id, getattr(mod, "name", ""))
        mod_secrets = self.secrets.get_mod_secrets(mod.id)
        if not mod_secrets and hasattr(mod, "name") and mod.name:
            mod_secrets = self.secrets.get_mod_secrets(mod.name)
        resolved: Dict[str, Any] = {}

        for key, field in mod.config.items():
            if field.sensitive:
                if key in mod_secrets:
                    resolved[key] = mod_secrets[key]
                elif field.default is not None:
                    resolved[key] = field.default
                else:
                    resolved[key] = None
            else:
                if key in stored_opts:
                    resolved[key] = stored_opts[key]
                elif field.default is not None:
                    resolved[key] = field.default
                else:
                    resolved[key] = None

        return resolved

    def get_masked_config(self, mod: Mod) -> Dict[str, Any]:
        """Return resolved config with sensitive values masked."""
        resolved = self.get_resolved_config(mod)
        masked = {}
        for key, val in resolved.items():
            field = mod.config.get(key)
            if field and field.sensitive and val is not None:
                masked[key] = "||********||"
            else:
                masked[key] = val
        return masked

    def set_option(self, mod: Mod, key: str, val_str: Any) -> Tuple[bool, str]:
        """Set a non-sensitive configuration value via hermes config set or config.yaml."""
        if key not in mod.config:
            return False, f"Unknown config key '{key}' for mod '{mod.id}'."
        field = mod.config[key]
        if field.sensitive:
            return False, f"Key '{key}' is marked sensitive! Use --set-secret instead."

        ok, coerced, err = field.validate_value(val_str)
        if not ok:
            return False, err

        config_key = f"mods.{mod.id}.{key}"
        # Use hermes config set
        cmd = ["hermes", "config", "set", config_key, str(coerced)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                return True, f"✓ Set {config_key} = {coerced}"
        except Exception:
            pass

        # Fallback to direct safe YAML write if hermes CLI is unavailable
        raw = self.load_raw_config()
        mods_sec = raw.setdefault("mods", {})
        mod_sec = mods_sec.setdefault(mod.id, {})
        mod_sec[key] = coerced
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(raw, f, sort_keys=False, allow_unicode=True)
        return True, f"✓ Set {config_key} = {coerced} (direct)"

    def set_secret(self, mod: Mod, key: str, secret_val: str) -> Tuple[bool, str]:
        """Set a sensitive secret in secure storage."""
        if key not in mod.config:
            return False, f"Unknown config key '{key}' for mod '{mod.id}'."
        field = mod.config[key]
        if not field.sensitive:
            return False, f"Key '{key}' is not sensitive. Use --set instead."

        ok, coerced, err = field.validate_value(secret_val)
        if not ok:
            return False, err

        self.secrets.set_mod_secret(mod.id, key, str(coerced))
        return True, f"✓ Saved secret for '{mod.id}.{key}' in secure storage."

    def _populate_defaults_impl(
        self,
        mods: Iterable[Mod] | Mod,
        config_path: Optional[Union[Path, str]] = None,
    ) -> Dict[str, Any]:
        """Populate default options for mods non-destructively in config.yaml and secrets.yaml."""
        if isinstance(mods, Mod):
            mod_list = [mods]
        elif hasattr(mods, "__iter__"):
            mod_list = list(mods)
        else:
            mod_list = []

        target_cfg = Path(config_path) if config_path is not None else self.config_path
        if target_cfg == self.config_path:
            sec_mgr = self.secrets
        else:
            sec_mgr = SecretsStorage(target_cfg.parent / "secrets.yaml")

        result: Dict[str, Any] = {
            "config_updated": False,
            "secrets_updated": False,
            "added_config": {},
            "added_secrets": {},
        }

        # 1. Process sensitive parameters -> secrets.yaml (permissions 0600)
        with FileLock(sec_mgr.path):
            sec_mgr._ensure_dir_and_perms()
            all_secrets = sec_mgr._load_unlocked()
            mods_sec = all_secrets.setdefault("mods", {})
            if not isinstance(mods_sec, dict):
                mods_sec = {}
                all_secrets["mods"] = mods_sec

            secrets_changed = False
            for mod in mod_list:
                if not getattr(mod, "config", None):
                    continue
                for key, field in mod.config.items():
                    if not getattr(field, "sensitive", False):
                        continue
                    mod_dict = mods_sec.setdefault(mod.id, {})
                    if not isinstance(mod_dict, dict):
                        mod_dict = {}
                        mods_sec[mod.id] = mod_dict
                    if key not in mod_dict:
                        val = "" if getattr(field, "default", None) is None else str(field.default)
                        mod_dict[key] = val
                        result["added_secrets"].setdefault(mod.id, []).append(key)
                        secrets_changed = True

            # Clean up any empty mod dict sections in secrets.yaml
            for k in list(mods_sec.keys()):
                v = mods_sec[k]
                if isinstance(v, dict) and len(v) == 0:
                    del mods_sec[k]
                    secrets_changed = True

            if secrets_changed:
                result["secrets_updated"] = True
                content = yaml.safe_dump(all_secrets, allow_unicode=True, default_flow_style=False, sort_keys=False)
                atomic_write_text(sec_mgr.path, content)
                try:
                    os.chmod(sec_mgr.path, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass

        # 2. Process non-sensitive parameters -> config.yaml
        with FileLock(target_cfg):
            raw_text = ""
            if target_cfg.exists():
                try:
                    raw_text = target_cfg.read_text(encoding="utf-8")
                except Exception:
                    raw_text = ""

            yaml_parser = _create_yaml_parser()
            if yaml_parser is not None:
                if raw_text.strip():
                    try:
                        data = yaml_parser.load(raw_text)
                    except Exception:
                        data = CommentedMap()
                else:
                    data = CommentedMap()
            else:
                data = yaml.safe_load(raw_text) if raw_text.strip() else {}

            if data is None or not isinstance(data, dict):
                data = CommentedMap() if CommentedMap is not dict else {}

            if "mods" not in data or not isinstance(data["mods"], dict):
                data["mods"] = CommentedMap() if CommentedMap is not dict else {}
            mods_node = data["mods"]

            config_changed = False

            # Clean up pre-existing empty dict sections under mods: (e.g. mod_id: {})
            # Preserving system flags (e.g. enabled: true, strict_folder_layout: false)
            for k in list(mods_node.keys()):
                v = mods_node[k]
                if isinstance(v, dict) and len(v) == 0:
                    del mods_node[k]
                    config_changed = True

            for mod in mod_list:
                mod_cfg = getattr(mod, "config", None)
                if not mod_cfg or not isinstance(mod_cfg, dict):
                    continue

                # ONLY mods with at least one non-sensitive parameter may be written to config.yaml
                non_sensitive_fields = {
                    key: field
                    for key, field in mod_cfg.items()
                    if not getattr(field, "sensitive", False)
                }
                if not non_sensitive_fields:
                    continue

                for key, field in non_sensitive_fields.items():
                    # Check if mod.id is in mods_node
                    if mod.id not in mods_node or not isinstance(mods_node[mod.id], dict):
                        # Check if mod.name is present and already has key
                        if (
                            getattr(mod, "name", None)
                            and mod.name in mods_node
                            and isinstance(mods_node[mod.name], dict)
                            and key in mods_node[mod.name]
                        ):
                            continue
                        mods_node[mod.id] = CommentedMap() if CommentedMap is not dict else {}

                    mod_node = mods_node[mod.id]
                    if not isinstance(mod_node, CommentedMap) and CommentedMap is not dict:
                        mod_node = CommentedMap(mod_node)
                        mods_node[mod.id] = mod_node

                    # Check if key is already specified
                    if key in mod_node:
                        # Existing user config! Never overwrite!
                        continue

                    # Key does not exist: populate default
                    mod_node[key] = field.default
                    result["added_config"].setdefault(mod.id, []).append(key)
                    config_changed = True

                    # Add description comment strictly in English (field.description)
                    desc = str(getattr(field, "description", "") or "").strip()
                    if desc and hasattr(mod_node, "yaml_set_comment_before_after_key"):
                        clean_lines = [
                            l.strip().lstrip("#").strip() if l.strip().startswith("#") else l.strip()
                            for l in desc.splitlines()
                        ]
                        clean_desc = "\n".join(clean_lines).strip()
                        if clean_desc:
                            mod_node.yaml_set_comment_before_after_key(key, before=clean_desc, indent=4)

            # Final safety check: ensure no empty mod dictionaries (mod_id: {}) remain in mods_node
            for k in list(mods_node.keys()):
                v = mods_node[k]
                if isinstance(v, dict) and len(v) == 0:
                    del mods_node[k]
                    config_changed = True

            if config_changed:
                result["config_updated"] = True
                if yaml_parser is not None:
                    buf = io.StringIO()
                    yaml_parser.dump(data, buf)
                    atomic_write_text(target_cfg, buf.getvalue())
                else:
                    content = yaml.safe_dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)
                    atomic_write_text(target_cfg, content)

        return result

    @_PopulateDefaultsDescriptor
    def populate_defaults(self, mods: Iterable[Mod] | Mod, config_path: Path = CONFIG_FILE) -> Dict[str, Any]:
        """Populate default options for mods non-destructively in config.yaml and secrets.yaml."""
        pass
