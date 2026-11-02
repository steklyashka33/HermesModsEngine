"""Tests for declarative mod slash commands (ModCommand, ModCommandRegistry, Gateway integration)."""
from __future__ import annotations

import asyncio
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
import yaml

# Read engine sources once at module import time (before HomeIOGuard activates during test execution)
_mods_dir = Path(__file__).resolve().parents[3]
if not (_mods_dir / ".engine").is_dir():
    _mods_dir = Path(os.environ.get("HERMES_MODS_DIR", Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "mods"))
_engine_dir = _mods_dir / ".engine"

_ENGINE_INIT = (_engine_dir / "__init__.py").read_text(encoding="utf-8")
_MODELS_PY = (_engine_dir / "models.py").read_text(encoding="utf-8")
_LOADER_PY = (_engine_dir / "loader.py").read_text(encoding="utf-8")
_CONFIG_MGR_PY = (_engine_dir / "config_manager.py").read_text(encoding="utf-8")
_MONITOR_MGR_PY = (_engine_dir / "monitor_manager.py").read_text(encoding="utf-8")
_I18N_PY = (_engine_dir / "i18n.py").read_text(encoding="utf-8") if (_engine_dir / "i18n.py").exists() else ""

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
if str(HERMES_ROOT) not in sys.path and HERMES_ROOT.is_dir():
    sys.path.insert(0, str(HERMES_ROOT))

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.mod_commands import ModCommandRegistry
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli.commands import is_gateway_known_command, resolve_command


@pytest.fixture
def test_engine(tmp_path):
    """Fixture to provide engine package without triggering HomeIOGuard."""
    pkg_dir = tmp_path / "test_engine_pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(_ENGINE_INIT, encoding="utf-8")
    (pkg_dir / "models.py").write_text(_MODELS_PY, encoding="utf-8")
    (pkg_dir / "loader.py").write_text(_LOADER_PY, encoding="utf-8")
    (pkg_dir / "config_manager.py").write_text(_CONFIG_MGR_PY, encoding="utf-8")
    (pkg_dir / "monitor_manager.py").write_text(_MONITOR_MGR_PY, encoding="utf-8")
    if _I18N_PY:
        (pkg_dir / "i18n.py").write_text(_I18N_PY, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "test_engine_pkg",
        str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["test_engine_pkg"] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def _make_source(chat_id: str = "c1", user_id: str = "u1") -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str, source: SessionSource | None = None) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source or _make_source(),
        message_id="m1",
        internal=False,
    )


def test_mod_command_model_and_parsing(test_engine):
    """ModCommand dataclass and parse_mod_command validation."""
    ModCommand = test_engine.ModCommand
    parse_mod_command = test_engine.parse_mod_command

    # 1. Valid full spec
    raw = {
        "name": "my-tool",
        "description": "My tool description",
        "description_ru": "Описание моего инструмента",
        "args_hint": "[options] <target>",
        "handler_script": "scripts/tool.py",
        "python": "result = args.upper()",
        "shell": "echo $HERMES_COMMAND_ARGS",
        "aliases": ["tool_alias", "/mt"],
        "category": "Tools",
        "busy_policy": "dispatch",
        "required_args": True,
    }
    cmd = parse_mod_command(raw, mod_id="test-mod", mod_dir=Path("/tmp/test-mod"))
    assert cmd is not None
    assert cmd.name == "my-tool"
    assert cmd.description == "My tool description"
    assert cmd.description_ru == "Описание моего инструмента"
    assert cmd.args_hint == "[options] <target>"
    assert cmd.aliases == ["tool_alias", "mt"]  # normalized without leading slash
    assert cmd.category == "Tools"
    assert cmd.busy_policy == "dispatch"
    assert cmd.required_args is True
    assert cmd.resolve_handler_path() == Path("/tmp/test-mod/scripts/tool.py")

    d = cmd.to_dict()
    assert d["name"] == "my-tool"
    assert d["handler_file"] == "/tmp/test-mod/scripts/tool.py"

    # 2. Invalid names
    assert parse_mod_command({"name": ""}, "m", None) is None
    assert parse_mod_command({"name": "tool with spaces"}, "m", None) is None
    assert parse_mod_command({"name": "a" * 33}, "m", None) is None
    assert parse_mod_command({"name": "UPPERCASE_CMD"}, "m", None) is not None  # normalized to lowercase
    assert parse_mod_command({"name": "UPPERCASE_CMD"}, "m", None).name == "uppercase_cmd"


def test_commands_discovery_and_cache_generation(tmp_path, test_engine):
    """Manifest loading and cache generation with update_commands_cache."""
    parse_mod_manifest = test_engine.parse_mod_manifest
    update_commands_cache = test_engine.update_commands_cache

    mod_dir = tmp_path / "sample-folder-mod"
    mod_dir.mkdir()
    (mod_dir / "scripts").mkdir()
    (mod_dir / "scripts" / "hello.py").write_text("def handle(ctx):\n    return f'Hello {ctx[\"args\"]}'\n")

    manifest_yaml = """
id: sample-folder-mod
name: Sample Folder Mod
enabled: true
commands:
  - name: folder-cmd
    description: Inline command in manifest
    handler_script: scripts/hello.py
    aliases: [fcmd]
"""
    manifest_file = mod_dir / "mod.yaml"
    manifest_file.write_text(manifest_yaml)

    # External commands in commands.yaml
    (mod_dir / "commands.yaml").write_text("""
- name: extra-cmd
  description: Command from commands.yaml file
  python: "f'EXTRA: {args}'"
""")

    mod = parse_mod_manifest(manifest_file, mod_dir=mod_dir, fallback_id="sample-folder-mod")
    assert mod is not None
    assert len(mod.commands) == 2
    cmd_names = {c.name for c in mod.commands}
    assert cmd_names == {"folder-cmd", "extra-cmd"}

    # Test cache generation
    cache_path = update_commands_cache([mod], mods_dir=tmp_path)
    assert cache_path.exists()
    cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert "commands" in cache_data
    assert "folder-cmd" in cache_data["commands"]
    assert "extra-cmd" in cache_data["commands"]
    assert cache_data["commands"]["folder-cmd"]["handler_file"] == str((mod_dir / "scripts" / "hello.py").resolve())


@pytest.mark.asyncio
async def test_mod_command_registry_dispatch_python(tmp_path):
    """Dispatching python script handlers (sync and async)."""
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    # Async handler
    async_py = scripts_dir / "async_cmd.py"
    async_py.write_text("""
async def handle(ctx):
    return f"ASYNC_OK: {ctx['command']} args={ctx['args']} chat={ctx['chat_id']}"
""")

    # Sync handler
    sync_py = scripts_dir / "sync_cmd.py"
    sync_py.write_text("""
def handle(ctx):
    return f"SYNC_OK: {ctx['args']}"
""")

    # Failing handler
    fail_py = scripts_dir / "fail_cmd.py"
    fail_py.write_text("""
def handle(ctx):
    raise ValueError("Test error in handler")
""")

    cache_data = {
        "version": "1.0",
        "commands": {
            "async-test": {
                "name": "async-test",
                "description": "Async handler test",
                "handler_file": str(async_py),
                "entrypoint": "handle",
                "aliases": ["atest"],
                "required_args": False,
            },
            "sync-test": {
                "name": "sync-test",
                "description": "Sync handler test",
                "handler_file": str(sync_py),
                "entrypoint": "handle",
                "required_args": True,
                "args_hint": "<name>",
            },
            "fail-test": {
                "name": "fail-test",
                "description": "Failing handler test",
                "handler_file": str(fail_py),
                "entrypoint": "handle",
            },
        }
    }
    (tmp_path / ".commands_cache.json").write_text(json.dumps(cache_data))

    registry = ModCommandRegistry(mods_dir=tmp_path)

    # 1. Test async dispatch
    event1 = _make_event("/async-test foo bar")
    res1 = await registry.dispatch(event1, event1.source, "async-test")
    assert res1 == "ASYNC_OK: async-test args=foo bar chat=c1"

    # 2. Test alias lookup
    event_alias = _make_event("/atest foo")
    res_alias = await registry.dispatch(event_alias, event_alias.source, "atest")
    assert res_alias == "ASYNC_OK: async-test args=foo chat=c1"

    # 3. Test required args validation
    event_no_args = _make_event("/sync-test")
    res_no_args = await registry.dispatch(event_no_args, event_no_args.source, "sync-test")
    assert "ℹ️ Использование: /sync-test <name>" in res_no_args

    # 4. Test sync dispatch with args
    event_args = _make_event("/sync-test Alice")
    res_args = await registry.dispatch(event_args, event_args.source, "sync-test")
    assert res_args == "SYNC_OK: Alice"

    # 5. Test error isolation (does not crash, returns warning)
    event_fail = _make_event("/fail-test")
    res_fail = await registry.dispatch(event_fail, event_fail.source, "fail-test")
    assert "⚠️ Ошибка выполнения /fail-test" in res_fail
    assert "Test error in handler" in res_fail


@pytest.mark.asyncio
async def test_mod_command_registry_dispatch_inline_python_and_shell(tmp_path):
    """Dispatching inline python and shell commands."""
    cache_data = {
        "version": "1.0",
        "commands": {
            "inline-py": {
                "name": "inline-py",
                "description": "Inline python test",
                "python": "f'PY_EVAL:{args.upper()}'",
            },
            "inline-py-stmt": {
                "name": "inline-py-stmt",
                "description": "Inline python statement test",
                "python": "x = args * 2\nresult = f'STMT:{x}'",
            },
            "inline-sh": {
                "name": "inline-sh",
                "description": "Inline shell test",
                "shell": "echo SHELL_RESULT: $HERMES_COMMAND_ARGS",
            },
        }
    }
    (tmp_path / ".commands_cache.json").write_text(json.dumps(cache_data))

    registry = ModCommandRegistry(mods_dir=tmp_path)

    # 1. Inline python eval
    ev_py = _make_event("/inline-py hello")
    res_py = await registry.dispatch(ev_py, ev_py.source, "inline-py")
    assert res_py == "PY_EVAL:HELLO"

    # 2. Inline python statement
    ev_stmt = _make_event("/inline-py-stmt abc")
    res_stmt = await registry.dispatch(ev_stmt, ev_stmt.source, "inline-py-stmt")
    assert res_stmt == "STMT:abcabc"

    # 3. Inline shell
    ev_sh = _make_event("/inline-sh world")
    res_sh = await registry.dispatch(ev_sh, ev_sh.source, "inline-sh")
    assert res_sh == "SHELL_RESULT: world"


@pytest.mark.asyncio
async def test_gateway_runner_mod_command_dispatch_integration(tmp_path):
    """Full gateway dispatch path via _hm_dispatch_idle_commands and slash_commands help."""
    from gateway.run import GatewayRunner

    cache_data = {
        "version": "1.0",
        "commands": {
            "echo-mod": {
                "name": "echo-mod",
                "description": "Echo command for gateway test",
                "description_ru": "Эхо-команда для шлюза",
                "args_hint": "<msg>",
                "python": "f'ECHO: {args}'",
                "aliases": ["emod"],
            }
        }
    }
    (tmp_path / ".commands_cache.json").write_text(json.dumps(cache_data))

    # Initialize ModCommandRegistry with tmp_path
    reg = ModCommandRegistry.get_instance(mods_dir=tmp_path)
    reg.mods_dir = tmp_path
    reg.cache_file = tmp_path / ".commands_cache.json"
    reg.refresh()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._voice_mode = {}
    runner._draining = False
    runner._check_slash_access = MagicMock(return_value=None)
    runner._route_stop_epoch = MagicMock(return_value=0)
    runner._hm_quick_commands = MagicMock(return_value={})
    runner._telegramized_command_reply = lambda event, text: text

    # Test is_gateway_known_command
    assert is_gateway_known_command("echo-mod") is True
    assert is_gateway_known_command("emod") is True
    assert is_gateway_known_command("unknown-cmd-xyz") is False

    # Test resolve_command
    cmd_def = resolve_command("echo-mod")
    assert cmd_def is not None
    assert cmd_def.name == "echo-mod"
    assert cmd_def.execute == "mod_command"

    # Test idle dispatch
    ev = _make_event("/echo-mod testing 1 2 3")
    handled, reply = await runner._hm_dispatch_idle_commands(ev, ev.source, "test_session_key")
    assert handled is True
    assert reply == "ECHO: testing 1 2 3"

    # Test commands registry
    assert resolve_command("echo-mod") is not None
    assert is_gateway_known_command("echo-mod") is True
