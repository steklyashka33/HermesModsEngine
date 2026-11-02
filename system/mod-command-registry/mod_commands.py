"""ModCommandRegistry: declarative slash command registry and dispatcher for Hermes mods."""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
import logging
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("gateway.mod_commands")

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(HERMES_HOME / "mods")))


def _t(key: str, lang: Optional[str] = None, **kwargs: Any) -> str:
    """Safely translate key using agent.i18n.t with fallback to local i18n catalogs."""
    active_lang = lang
    if not active_lang:
        try:
            from agent.i18n import get_language
            active_lang = get_language() or "en"
        except Exception:
            try:
                from agent.i18n import get_active_language
                active_lang = get_active_language() or "en"
            except Exception:
                active_lang = "en"

    try:
        from agent.i18n import t
        val = t(key, lang=active_lang, **kwargs)
        if val and val != key:
            return val
    except Exception:
        pass

    try:
        mod_dir = Path(__file__).resolve().parent
        for target_lang in (active_lang, "en"):
            i18n_file = mod_dir / "i18n" / f"{target_lang}.yaml"
            if i18n_file.is_file():
                import yaml
                data = yaml.safe_load(i18n_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    parts = key.split(".")
                    cur = data
                    for p in parts:
                        if isinstance(cur, dict) and p in cur:
                            cur = cur[p]
                        else:
                            cur = None
                            break
                    if isinstance(cur, str):
                        return cur.format(**kwargs).strip() if kwargs else cur.strip()
    except Exception:
        pass

    try:
        return key.format(**kwargs).strip() if kwargs else key
    except Exception:
        return key


class ModCommandRegistry:
    """Registry for discovering, resolving and dispatching slash commands defined by Hermes mods."""

    _instance: Optional[ModCommandRegistry] = None

    def __init__(self, mods_dir: Optional[Path] = None):
        self.mods_dir = mods_dir or Path(os.environ.get("HERMES_MODS_DIR", DEFAULT_MODS_DIR))
        self.cache_file = self.mods_dir / ".commands_cache.json"
        self._commands: Dict[str, Dict[str, Any]] = {}
        self._last_mtime: float = -1.0
        self._reload_lock = asyncio.Lock()
        self._sync_reload()

    @classmethod
    def get_instance(cls, mods_dir: Optional[Path] = None) -> ModCommandRegistry:
        if cls._instance is None:
            cls._instance = cls(mods_dir)
        return cls._instance

    def _sync_reload(self) -> None:
        """Synchronously check and reload .commands_cache.json if mtime changed."""
        try:
            if not self.cache_file.exists():
                self._commands = {}
                self._last_mtime = -1.0
                return
            mtime = self.cache_file.stat().st_mtime
            if mtime <= self._last_mtime:
                return
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                raw_cmds = data.get("commands", {})
                if isinstance(raw_cmds, dict):
                    self._commands = {str(k).lower(): v for k, v in raw_cmds.items() if isinstance(v, dict)}
                elif isinstance(data.get("items"), list):
                    self._commands = {
                        str(item.get("name")).lower(): item
                        for item in data["items"]
                        if isinstance(item, dict) and item.get("name")
                    }
                self._last_mtime = mtime
                logger.debug("ModCommandRegistry: loaded %d commands from %s", len(self._commands), self.cache_file)
        except Exception as exc:
            logger.warning("ModCommandRegistry: failed reading cache %s: %s", self.cache_file, exc)

    def refresh(self) -> None:
        """Force reload commands from cache."""
        self._last_mtime = -1.0
        self._sync_reload()

    def get_commands(self) -> Dict[str, Dict[str, Any]]:
        self._sync_reload()
        return dict(self._commands)

    def get_command_entries(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """Return list of (name, description, meta_dict) for hermes_cli.commands integration."""
        self._sync_reload()
        out = []
        for name, meta in self._commands.items():
            desc = meta.get("description", "")
            out.append((name, desc, meta))
        return out

    def get_command(self, name: str | None) -> Optional[Dict[str, Any]]:
        """Look up command by exact name, normalized name, or alias."""
        if not name:
            return None
        self._sync_reload()
        clean = name.strip().lstrip("/").lower()
        if not clean:
            return None

        # 1. Exact match
        if clean in self._commands:
            return self._commands[clean]

        # 2. Check underscore <-> hyphen
        variants = {clean, clean.replace("_", "-"), clean.replace("-", "_")}
        for v in variants:
            if v in self._commands:
                return self._commands[v]

        # 3. Check aliases
        for cmd_name, meta in self._commands.items():
            aliases = meta.get("aliases") or []
            if isinstance(aliases, (list, tuple)):
                norm_aliases = {str(a).strip().lstrip("/").lower() for a in aliases if a}
                alias_variants = set(norm_aliases)
                for a in norm_aliases:
                    alias_variants.add(a.replace("_", "-"))
                    alias_variants.add(a.replace("-", "_"))
                if variants & alias_variants:
                    return meta

        return None

    def is_known_command(self, name: str | None) -> bool:
        """Return True if command or alias exists in registry."""
        return self.get_command(name) is not None

    def resolve_command_def(self, name: str | None):
        """Construct CommandDef for hermes_cli.commands.resolve_command."""
        meta = self.get_command(name)
        if not meta:
            return None
        try:
            from hermes_cli.commands import CommandDef
            aliases = tuple(meta.get("aliases") or ())
            return CommandDef(
                name=meta.get("name", name),
                description=meta.get("description", ""),
                category=meta.get("category", "Custom Mods"),
                aliases=aliases,
                args_hint=meta.get("args_hint", ""),
                busy_policy=meta.get("busy_policy", "reject"),
                execute="mod_command",
            )
        except Exception:
            return None

    def format_help_lines(self, allowed: Optional[Iterable[str]] = None) -> List[str]:
        """Format help entries according to formatting standards: /{name} {args} — {desc}."""
        self._sync_reload()
        allowed_set = set(allowed) if allowed is not None else None
        lines = []

        # Check active locale
        active_lang = "ru"
        try:
            from agent.i18n import get_language
            active_lang = get_language() or "ru"
        except Exception:
            try:
                from agent.i18n import get_active_language
                active_lang = get_active_language() or "ru"
            except Exception:
                active_lang = "ru"

        for name in sorted(self._commands.keys()):
            meta = self._commands[name]
            if allowed_set is not None and name not in allowed_set:
                continue
            desc = ""
            try:
                from agent.i18n import t
                for key in (f"slash.{name}.description", f"slash.command.{name}.description"):
                    val = t(key, lang=active_lang)
                    if val and val != key:
                        desc = val
                        break
            except Exception:
                pass
            if not desc:
                i18n_meta = meta.get("i18n", {})
                if isinstance(i18n_meta, dict) and active_lang in i18n_meta:
                    l_entry = i18n_meta[active_lang]
                    if isinstance(l_entry, dict) and l_entry.get("description"):
                        desc = str(l_entry["description"])
            if not desc and active_lang == "ru" and meta.get("description_ru"):
                desc = meta["description_ru"]
            if not desc:
                desc = meta.get("description", "")

            args = meta.get("args_hint", "").strip()
            args_str = f" {args}" if args else ""

            # Check aliases
            aliases = [f"/{a}" for a in (meta.get("aliases") or []) if a != name]
            alias_label = _t("mods.command_registry.alias", lang=active_lang)
            alias_note = f" ({alias_label}: {', '.join(aliases)})" if aliases else ""

            lines.append(f"/{name}{args_str} — {desc}{alias_note}")
        return lines

    def _resolve_mod_config(self, mod_id: str, mod_dir: Optional[str] = None) -> Dict[str, Any]:
        """Load merged configuration for mod_id (manifest defaults + config.yaml + secrets.json)."""
        if not mod_id:
            return {}
        try:
            defaults: Dict[str, Any] = {}
            if mod_dir:
                m_dir = Path(mod_dir)
                for cand in (m_dir / "config.yaml", m_dir / "config.yml", m_dir / "user_config.yaml", m_dir / "user_config.yml", m_dir / "mod.yaml", m_dir / f"{m_dir.name}.yaml"):
                    if cand.is_file():
                        try:
                            import yaml
                            with open(cand, "r", encoding="utf-8") as f:
                                ydata = yaml.safe_load(f)
                            if isinstance(ydata, dict):
                                cfg_section = ydata.get("config") or ydata.get("user_config") or ydata.get("config_schema") or (ydata if cand.stem in ("config", "user_config") else None)
                                if isinstance(cfg_section, dict):
                                    for k, v in cfg_section.items():
                                        if isinstance(v, dict) and "default" in v:
                                            defaults[str(k)] = v["default"]
                        except Exception:
                            pass
                # Also check modular config/ and user_config/ directory
                for d_name in ("config", "user_config"):
                    uc_dir = m_dir / d_name
                    if uc_dir.is_dir():
                        for subf in sorted(uc_dir.iterdir()):
                            if subf.suffix in (".yaml", ".yml", ".json"):
                                try:
                                    import yaml
                                    with open(subf, "r", encoding="utf-8") as f:
                                        ydata = yaml.safe_load(f)
                                    if isinstance(ydata, dict):
                                        cfg_section = ydata.get("config") or ydata.get("user_config") or ydata
                                        if isinstance(cfg_section, dict):
                                            for k, v in cfg_section.items():
                                                if isinstance(v, dict) and "default" in v:
                                                    defaults[str(k)] = v["default"]
                                except Exception:
                                    pass
            else:
                for cand in (
                    self.mods_dir / f"{mod_id}.yaml",
                    self.mods_dir / f"{mod_id}.yml",
                    self.mods_dir / "system" / f"{mod_id}.yaml",
                    self.mods_dir / "custom" / f"{mod_id}.yaml",
                ):
                    if cand.is_file():
                        try:
                            import yaml
                            with open(cand, "r", encoding="utf-8") as f:
                                ydata = yaml.safe_load(f)
                            if isinstance(ydata, dict):
                                cfg_section = ydata.get("config") or ydata.get("user_config") or ydata.get("config_schema")
                                if isinstance(cfg_section, dict):
                                    for k, v in cfg_section.items():
                                        if isinstance(v, dict) and "default" in v:
                                            defaults[str(k)] = v["default"]
                        except Exception:
                            pass
                        break

            try:
                from hermes_constants import get_hermes_home
                home = get_hermes_home()
            except Exception:
                home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))

            config_file = Path(os.environ.get("HERMES_CONFIG_FILE", home / "config.yaml"))
            secrets_env = os.environ.get("HERMES_SECRETS_FILE")
            if secrets_env:
                sec_candidates = [Path(secrets_env)]
            else:
                sec_candidates = [home / "secrets.yaml", home / "secrets.json"]

            merged = dict(defaults)
            if config_file.is_file():
                try:
                    import yaml
                    with open(config_file, "r", encoding="utf-8") as f:
                        cfg_data = yaml.safe_load(f)
                    if isinstance(cfg_data, dict):
                        mods_cfg = cfg_data.get("mods", {})
                        if isinstance(mods_cfg, dict):
                            m_opts = mods_cfg.get(mod_id) or mods_cfg.get(mod_id.replace("-", "_")) or {}
                            if isinstance(m_opts, dict):
                                for k, v in m_opts.items():
                                    if k != "user_config":
                                        merged[k] = v
                                    elif isinstance(v, dict):
                                        merged.update(v)
                except Exception:
                    pass

            for sc in sec_candidates:
                if sc.is_file():
                    try:
                        if sc.suffix == ".json":
                            with open(sc, "r", encoding="utf-8") as f:
                                sec_data = json.load(f)
                        else:
                            import yaml
                            with open(sc, "r", encoding="utf-8") as f:
                                sec_data = yaml.safe_load(f)
                        if isinstance(sec_data, dict):
                            mods_sec = sec_data.get("mods", {})
                            if isinstance(mods_sec, dict):
                                m_sec = mods_sec.get(mod_id) or mods_sec.get(mod_id.replace("-", "_")) or {}
                                if isinstance(m_sec, dict):
                                    merged.update(m_sec)
                    except Exception:
                        pass

            return merged
        except Exception:
            return {}

    _resolve_mod_user_config = _resolve_mod_config

    def _can_execute_mod(self, mod_id: str) -> bool:
        """Check if mod can execute (fail-open) reading status directly without hard engine dependency."""
        if not mod_id:
            return True
        # 1. Direct check of .state.json
        try:
            state_file = self.mods_dir / ".state.json"
            if state_file.is_file():
                with open(state_file, "r", encoding="utf-8") as f:
                    s_data = json.load(f)
                if isinstance(s_data, dict):
                    mod_state = (
                        s_data.get("overrides", {}).get(mod_id)
                        or s_data.get("mods", {}).get(mod_id)
                    )
                    if isinstance(mod_state, dict):
                        if mod_state.get("disabled_circuit_breaker") or mod_state.get("circuit_breaker_tripped"):
                            return False
        except Exception:
            pass

        # 2. Direct check of .circuit_breaker.json
        try:
            cb_file = self.mods_dir / ".circuit_breaker.json"
            if cb_file.is_file():
                with open(cb_file, "r", encoding="utf-8") as f:
                    cb_data = json.load(f)
                if isinstance(cb_data, dict):
                    status = cb_data.get("statuses", {}).get(mod_id, {})
                    if isinstance(status, dict) and status.get("state") == "OPEN":
                        return False
        except Exception:
            pass

        # 3. Safe fallback attempt via dynamic engine inspection if present / fail-open
        try:
            cb_mod = sys.modules.get("engine.circuit_breaker")
            if cb_mod is None:
                import importlib
                cb_mod = importlib.import_module("engine.circuit_breaker")
            if cb_mod and hasattr(cb_mod, "get_circuit_breaker"):
                return cb_mod.get_circuit_breaker(self.mods_dir).can_execute(mod_id)
        except Exception:
            pass

        return True

    def _record_circuit_breaker_error(
        self,
        mod_id: str,
        error: str,
        error_type: str = "ExecutionError",
        details: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Record runtime mod error safely fail-open without hard engine dependency."""
        if not mod_id:
            return False
        try:
            cb_mod = sys.modules.get("engine.circuit_breaker")
            if cb_mod is None:
                import importlib
                cb_mod = importlib.import_module("engine.circuit_breaker")
            if cb_mod and hasattr(cb_mod, "get_circuit_breaker"):
                cb = cb_mod.get_circuit_breaker(self.mods_dir)
                return bool(cb.record_error(mod_id, error=error, error_type=error_type, details=details or {}))
        except Exception:
            pass
        return False

    async def dispatch(self, event: Any, source: Any, command_name: str) -> Optional[str]:
        """Dispatch execution of a mod command: handles python scripts, inline python, and shell."""
        meta = self.get_command(command_name)
        if not meta:
            return None

        canonical_name = meta.get("name", command_name)
        args = ""
        if hasattr(event, "get_command_args"):
            args = event.get_command_args().strip()
        elif hasattr(event, "text") and event.text:
            parts = event.text.strip().split(None, 1)
            args = parts[1].strip() if len(parts) > 1 else ""

        if meta.get("required_args") and not args:
            hint = meta.get("args_hint", "").strip()
            return _t("mods.command_registry.usage", canonical_name=canonical_name, hint=hint).strip()

        mod_id = meta.get("mod_id", "")
        # Circuit Breaker check: do not execute if mod is tripped/disabled by circuit breaker
        if not self._can_execute_mod(mod_id):
            return _t("mods.command_registry.circuit_breaker_disabled", mod_id=mod_id)

        mod_dir = meta.get("mod_dir")
        mod_version = meta.get("mod_version", "1.0.0")
        mod_config = self._resolve_mod_config(mod_id, mod_dir)

        ctx = {
            "event": event,
            "source": source,
            "command": canonical_name,
            "args": args,
            "chat_id": getattr(source, "chat_id", None),
            "user_id": getattr(source, "user_id", None),
            "thread_id": getattr(source, "thread_id", None),
            "mod_id": mod_id,
            "mod_dir": str(mod_dir) if mod_dir else None,
            "mod_version": mod_version,
            "config": mod_config,
            "user_config": mod_config,
        }
        base_dir = Path(mod_dir).resolve() if mod_dir else self.mods_dir.resolve()

        def _safe_resolve_path(rel: Any) -> str:
            if not rel:
                return str(base_dir)
            rel_path = Path(str(rel))
            if rel_path.is_absolute():
                raise ValueError(f"Absolute paths not allowed in resolve_path: '{rel}'")
            target = (base_dir / rel_path).resolve()
            try:
                if not target.is_relative_to(base_dir):
                    raise ValueError(f"Path traversal detected: '{rel}' escapes base directory '{base_dir}'")
            except (ValueError, AttributeError):
                if not str(target).startswith(str(base_dir)):
                    raise ValueError(f"Path traversal detected: '{rel}' escapes base directory '{base_dir}'")
            return str(target)

        ctx["resolve_path"] = _safe_resolve_path

        try:
            # 1. Handler script execution (Python or Shell script file)
            handler_path_raw = meta.get("handler_file") or meta.get("handler_script")
            if handler_path_raw:
                handler_path = Path(handler_path_raw)
                if not handler_path.is_absolute():
                    mod_dir = meta.get("mod_dir")
                    if mod_dir:
                        handler_path = (Path(mod_dir) / handler_path).resolve()
                    else:
                        handler_path = (self.mods_dir / handler_path).resolve()

                if not handler_path.exists():
                    return _t("mods.command_registry.handler_not_found", handler_path=str(handler_path))

                if handler_path.suffix == ".py":
                    return await self._execute_python_script(handler_path, meta, ctx)
                else:
                    return await self._execute_shell_script(handler_path, meta, ctx, args)

            # 2. Inline Python code execution
            if meta.get("python"):
                return await self._execute_inline_python(meta["python"], ctx)

            # 3. Inline Shell command execution
            if meta.get("shell"):
                return await self._execute_inline_shell(meta["shell"], ctx, args)

            return _t("mods.command_registry.no_handler_configured", canonical_name=canonical_name)

        except asyncio.TimeoutError:
            logger.warning("Mod command /%s timed out after 30s", canonical_name)
            tripped = self._record_circuit_breaker_error(
                mod_id,
                error=f"Timeout after 30s on /{canonical_name}",
                error_type="TimeoutError",
                details={"command": canonical_name},
            )
            if tripped:
                return _t(
                    "mods.command_registry.timeout_isolated",
                    canonical_name=canonical_name,
                    mod_id=mod_id,
                )
            return _t(
                "mods.command_registry.timeout",
                canonical_name=canonical_name,
            )
        except Exception as exc:
            logger.warning("Mod command /%s failed: %s", canonical_name, exc, exc_info=True)
            tripped = self._record_circuit_breaker_error(
                mod_id,
                error=str(exc),
                error_type=type(exc).__name__,
                details={"command": canonical_name},
            )
            if tripped:
                return _t(
                    "mods.command_registry.error_isolated",
                    canonical_name=canonical_name,
                    exc=str(exc),
                    mod_id=mod_id,
                )
            return _t(
                "mods.command_registry.error",
                canonical_name=canonical_name,
                exc=str(exc),
            )

    async def _execute_python_script(self, script_path: Path, meta: Dict[str, Any], ctx: Dict[str, Any]) -> Optional[str]:
        script_dir = str(script_path.parent.resolve())
        mod_dir = meta.get("mod_dir")
        for p in (script_dir, mod_dir):
            if p and p not in sys.path:
                sys.path.insert(0, p)

        mod_id = meta.get("mod_id", "custom")
        cmd_name = meta.get("name", "cmd")
        module_name = f"hermes_mod_cmd_{mod_id}_{cmd_name}".replace("-", "_")

        spec = importlib.util.spec_from_file_location(module_name, str(script_path))
        if spec is None or spec.loader is None:
            return _t("mods.command_registry.module_spec_error", script_path=str(script_path))

        py_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(py_module)

        entrypoint = meta.get("entrypoint", "handle")
        fn = getattr(py_module, entrypoint, None)
        if fn is None:
            # Fallbacks
            for alt in ("handle", "main", "run", "execute"):
                fn = getattr(py_module, alt, None)
                if fn is not None:
                    break

        if not callable(fn):
            return _t("mods.command_registry.entrypoint_not_found", script_name=script_path.name, entrypoint=entrypoint)

        # Check signature for calling conventions
        sig = inspect.signature(fn)
        params_count = len(sig.parameters)

        def _call_sync():
            if params_count == 0:
                return fn()
            try:
                return fn(ctx)
            except TypeError:
                return fn(ctx["args"])

        if inspect.iscoroutinefunction(fn):
            if params_count == 0:
                res = await asyncio.wait_for(fn(), timeout=30.0)
            else:
                try:
                    res = await asyncio.wait_for(fn(ctx), timeout=30.0)
                except TypeError:
                    res = await asyncio.wait_for(fn(ctx["args"]), timeout=30.0)
        else:
            res = await asyncio.wait_for(asyncio.to_thread(_call_sync), timeout=30.0)

        return str(res) if res is not None else ""

    async def _execute_shell_script(self, script_path: Path, meta: Dict[str, Any], ctx: Dict[str, Any], args: str) -> str:
        env = os.environ.copy()
        env["HERMES_COMMAND_NAME"] = ctx["command"]
        env["HERMES_COMMAND_ARGS"] = args
        env["HERMES_CHAT_ID"] = str(ctx.get("chat_id") or "")
        env["HERMES_USER_ID"] = str(ctx.get("user_id") or "")
        env["HERMES_MOD_ID"] = str(ctx.get("mod_id") or "")
        env["HERMES_MOD_DIR"] = str(ctx.get("mod_dir") or "")
        env["HERMES_MOD_VERSION"] = str(ctx.get("mod_version") or "")

        for k, v in (ctx.get("config") or ctx.get("user_config", {})).items():
            if v is not None:
                clean_k = re.sub(r"[^A-Z0-9_]", "_", str(k).upper())
                env[f"HERMES_MOD_CONFIG_{clean_k}"] = str(v)
                env[f"HERMES_MOD_OPTION_{clean_k}"] = str(v)

        cwd = Path(ctx["mod_dir"]) if ctx.get("mod_dir") and Path(ctx["mod_dir"]).exists() else self.mods_dir

        cmd_args = shlex.split(args) if args else []
        proc = await asyncio.create_subprocess_exec(
            str(script_path), *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        out_text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            return _t("mods.command_registry.script_error", code=proc.returncode, error=err_text or out_text)
        return out_text

    async def _execute_inline_python(self, code: str, ctx: Dict[str, Any]) -> str:
        local_scope = {
            "ctx": ctx,
            "event": ctx.get("event"),
            "source": ctx.get("source"),
            "args": ctx.get("args", ""),
            "command": ctx.get("command", ""),
            "chat_id": ctx.get("chat_id"),
            "user_id": ctx.get("user_id"),
            "mod_id": ctx.get("mod_id"),
            "mod_dir": ctx.get("mod_dir"),
            "mod_version": ctx.get("mod_version"),
            "config": ctx.get("config") or ctx.get("user_config", {}),
            "user_config": ctx.get("user_config") or ctx.get("config", {}),
            "resolve_path": ctx.get("resolve_path"),
        }

        # Try evaluating as expression first
        try:
            expr_ast = compile(code.strip(), "<mod_inline_python>", "eval")
            res = eval(expr_ast, {"__builtins__": __builtins__}, local_scope)
            return str(res) if res is not None else ""
        except SyntaxError:
            pass

        # Execute as statements, capturing stdout and 'result' variable
        stdout_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf):
            stmt_ast = compile(code, "<mod_inline_python>", "exec")
            exec(stmt_ast, {"__builtins__": __builtins__}, local_scope)

        if "result" in local_scope and local_scope["result"] is not None:
            return str(local_scope["result"])
        return stdout_buf.getvalue().strip()

    async def _execute_inline_shell(self, cmd_str: str, ctx: Dict[str, Any], args: str) -> str:
        env = os.environ.copy()
        env["HERMES_COMMAND_NAME"] = ctx["command"]
        env["HERMES_COMMAND_ARGS"] = args
        env["HERMES_CHAT_ID"] = str(ctx.get("chat_id") or "")
        env["HERMES_USER_ID"] = str(ctx.get("user_id") or "")
        env["HERMES_MOD_ID"] = str(ctx.get("mod_id") or "")
        env["HERMES_MOD_DIR"] = str(ctx.get("mod_dir") or "")
        env["HERMES_MOD_VERSION"] = str(ctx.get("mod_version") or "")

        mod_dir_val = str(ctx.get("mod_dir") or self.mods_dir)
        cmd_str = cmd_str.replace("${MOD_DIR}", mod_dir_val).replace("${mod_dir}", mod_dir_val)

        for k, v in (ctx.get("config") or ctx.get("user_config", {})).items():
            if v is not None:
                clean_k = re.sub(r"[^A-Z0-9_]", "_", str(k).upper())
                env[f"HERMES_MOD_CONFIG_{clean_k}"] = str(v)
                env[f"HERMES_MOD_OPTION_{clean_k}"] = str(v)
                cmd_str = cmd_str.replace(f"${{config.{k}}}", str(v)).replace(f"${{user_config.{k}}}", str(v))

        cwd = Path(ctx["mod_dir"]) if ctx.get("mod_dir") and Path(ctx["mod_dir"]).exists() else self.mods_dir

        proc = await asyncio.create_subprocess_shell(
            cmd_str,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        out_text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            return _t("mods.command_registry.shell_error", code=proc.returncode, error=err_text or out_text)
        return out_text
