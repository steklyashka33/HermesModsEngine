"""Tests for Generic Section Loader (without hardcoding) and Mod Context with Path Auto-resolution."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from engine.loader import (
    _load_folder_mod_sections,
    parse_mod_manifest,
    resolve_mod_mcp_servers,
    update_commands_cache,
)
from engine.models import Mod, MonitorSpec
from engine.monitor_manager import ModMonitorManager
from engine.validator import ModValidator
from gateway.mod_commands import ModCommandRegistry
from runner import ModRunner


class TestGenericSectionLoader:
    """Tests for dynamic/native discovery of manifest sections."""

    def test_generic_section_loader_modular_layout(self, tmp_path: Path):
        """Test that any section (standard or future) loads from inline, <sec>.yaml, or <sec>/."""
        mod_dir = tmp_path / "comprehensive-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        # 1. Base mod.yaml with metadata and an inline custom section
        manifest = {
            "id": "comprehensive-mod",
            "name": "Comprehensive Mod",
            "version": "2.1.0",
            "author": "Architect",
            "description": "Demonstrating generic section loading",
            "enabled": True,
            "inline_notes": ["note 1", "note 2"],
        }
        (mod_dir / "mod.yaml").write_text(yaml.dump(manifest), encoding="utf-8")

        # 2. Standalone file sections
        # user_config.yaml (dict section)
        (mod_dir / "user_config.yaml").write_text(
            yaml.dump({
                "api_key": {"type": "string", "sensitive": True, "required": True},
                "retries": {"type": "int", "default": 5},
            }),
            encoding="utf-8",
        )

        # telemetry.yaml (custom dict section)
        (mod_dir / "telemetry.yaml").write_text(
            yaml.dump({
                "endpoint": "https://telemetry.local/v1",
                "sample_rate": 0.5,
            }),
            encoding="utf-8",
        )

        # 3. Directory sections
        # targets/ directory (standard list section)
        targets_dir = mod_dir / "targets"
        targets_dir.mkdir()
        (targets_dir / "part1.yaml").write_text(
            yaml.dump([{"file": "core.py", "old_block": "a = 1", "new_block": "a = 2"}]),
            encoding="utf-8",
        )
        (targets_dir / "part2.yaml").write_text(
            yaml.dump([{"file": "util.py", "old_block": "b = 1", "new_block": "b = 2"}]),
            encoding="utf-8",
        )

        # commands.yaml file (standard list section)
        (mod_dir / "commands.yaml").write_text(
            yaml.dump([{
                "name": "status",
                "description": "Show status",
                "shell": "echo ok",
            }]),
            encoding="utf-8",
        )

        # hooks/ directory (custom list section)
        hooks_dir = mod_dir / "hooks"
        hooks_dir.mkdir()
        (hooks_dir / "01_pre.yaml").write_text(
            yaml.dump([{"event": "pre_apply", "script": "scripts/pre.sh"}]),
            encoding="utf-8",
        )
        (hooks_dir / "02_post.yaml").write_text(
            yaml.dump([{"event": "post_apply", "script": "scripts/post.sh"}]),
            encoding="utf-8",
        )

        # Non-section folders like scripts/ or data/ should NOT be treated as sections
        (mod_dir / "scripts").mkdir()
        (mod_dir / "scripts" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
        (mod_dir / "data").mkdir()
        (mod_dir / "data" / "db.sqlite").write_text("", encoding="utf-8")

        # Compile in-memory manifest
        compiled = _load_folder_mod_sections(mod_dir)
        assert compiled["id"] == "comprehensive-mod"
        assert len(compiled["targets"]) == 2
        assert {t["file"] for t in compiled["targets"]} == {"core.py", "util.py"}
        assert "api_key" in compiled["config"]
        assert "retries" in compiled["config"]
        assert "api_key" in compiled["user_config"]
        assert "retries" in compiled["user_config"]
        assert len(compiled["commands"]) == 1
        assert compiled["commands"][0]["name"] == "status"
        assert len(compiled["hooks"]) == 2
        assert compiled["hooks"][0]["event"] == "pre_apply"
        assert compiled["hooks"][1]["event"] == "post_apply"
        assert compiled["telemetry"]["endpoint"] == "https://telemetry.local/v1"
        assert "scripts" not in compiled
        assert "data" not in compiled

        # Parse into Mod object
        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="comprehensive-mod")
        assert mod is not None
        assert mod.is_folder is True
        assert len(mod.targets) == 2
        assert len(mod.commands) == 1
        assert "api_key" in mod.config
        assert "api_key" in mod.user_config
        assert mod.sections["telemetry"]["sample_rate"] == 0.5
        assert len(mod.sections["hooks"]) == 2

    def test_strict_folder_layout_with_arbitrary_sections(self, tmp_path: Path):
        """Test that strict_folder_layout rejects bloated mod.yaml with inline custom sections."""
        mod_dir = tmp_path / "inline-bloated-mod"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text(
            yaml.dump({
                "id": "inline-bloated-mod",
                "name": "Bloated Mod",
                "version": "1.0.0",
                "hooks": [{"event": "pre_apply", "script": "run.sh"}],
                "telemetry": {"enabled": True},
            }),
            encoding="utf-8",
        )

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="inline-bloated-mod")
        assert mod is not None

        # Under lenient validator: passes
        validator_lenient = ModValidator(repo_root=tmp_path, mods_dir=tmp_path, strict_folder_layout=False)
        res_lenient = validator_lenient.validate_mod(mod)
        assert res_lenient.valid is True

        # Under strict_folder_layout: fails because hooks and telemetry are defined inline in mod.yaml
        validator_strict = ModValidator(repo_root=tmp_path, mods_dir=tmp_path, strict_folder_layout=True)
        res_strict = validator_strict.validate_mod(mod)
        assert res_strict.valid is False
        layout_errors = [e for e in res_strict.errors if e.category == "mod_layout"]
        assert len(layout_errors) == 2
        assert any("hooks" in e.message for e in layout_errors)
        assert any("telemetry" in e.message for e in layout_errors)


class TestModContextAndPathResolution:
    """Tests for child process context (env, ctx) and path auto-resolution."""

    def test_gateway_mod_commands_full_context_and_path_resolution(self, tmp_path: Path):
        """Verify python and shell handlers receive complete ctx and env with paths auto-resolved."""
        async def _run():
            mods_dir = tmp_path / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            mod_dir = mods_dir / "runner-mod"
            mod_dir.mkdir(parents=True, exist_ok=True)

            scripts_dir = mod_dir / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            data_dir = mod_dir / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            sample_db = data_dir / "db.sqlite"
            sample_db.write_text("DATABASE_CONTENT", encoding="utf-8")

            # 1. Python handler script
            py_handler = scripts_dir / "handler.py"
            py_handler.write_text(
                """def handle(ctx):
        resolved_db = ctx["resolve_path"]("data/db.sqlite")
        import os
        db_exists = os.path.exists(resolved_db)
        return f"MOD_ID={ctx['mod_id']}|VER={ctx['mod_version']}|CFG_TOKEN={ctx['config'].get('token')}|LEGACY={ctx['user_config'].get('token')}|DB={db_exists}"
    """,
                encoding="utf-8",
            )

            # 2. Shell handler script
            sh_handler = scripts_dir / "shell_handler.sh"
            sh_handler.write_text(
                """#!/bin/sh
    cat data/db.sqlite
    echo " ENV_ID=$HERMES_MOD_ID ENV_VER=$HERMES_MOD_VERSION CFG_TOKEN=$HERMES_MOD_CONFIG_TOKEN"
    """,
                encoding="utf-8",
            )
            os.chmod(sh_handler, 0o755)

            # 3. Create mod manifest with config and commands
            mod_manifest = {
                "id": "runner-mod",
                "name": "Runner Mod",
                "version": "3.4.5",
                "enabled": True,
                "config": {
                    "token": {"type": "string", "sensitive": True},
                    "log_level": {"type": "string", "default": "DEBUG"},
                },
                "commands": [
                    {
                        "name": "pycmd",
                        "description": "Python command",
                        "handler_script": "scripts/handler.py",
                    },
                    {
                        "name": "shcmd",
                        "description": "Shell command",
                        "handler_script": "scripts/shell_handler.sh",
                    },
                    {
                        "name": "inlinepy",
                        "description": "Inline python",
                        "python": "f'INLINE_PY: {mod_id} v{mod_version} token={config.get(\"token\")} legacy={user_config.get(\"token\")}'",
                    },
                    {
                        "name": "inlinesh",
                        "description": "Inline shell",
                        "shell": "echo -n \"INLINE_SH: $HERMES_MOD_ID v$HERMES_MOD_VERSION CFG=$HERMES_MOD_CONFIG_LOG_LEVEL $(cat data/db.sqlite)\"",
                    },
                ],
            }
            (mod_dir / "mod.yaml").write_text(yaml.dump(mod_manifest), encoding="utf-8")

            # Set up config.yaml and secrets.json in hermes home
            hermes_home = tmp_path / ".hermes"
            hermes_home.mkdir(parents=True, exist_ok=True)
            secrets_file = hermes_home / "secrets.json"
            secrets_file.write_text(
                json.dumps({"mods": {"runner-mod": {"token": "secret-xyz-123"}}}),
                encoding="utf-8",
            )
            config_file = hermes_home / "config.yaml"
            config_file.write_text(
                yaml.dump({"mods": {"runner-mod": {"log_level": "INFO"}}}),
                encoding="utf-8",
            )

            # Parse mod and update cache
            mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="runner-mod")
            assert mod is not None
            update_commands_cache([mod], mods_dir=mods_dir)

            # Initialize registry with mock environment
            orig_env = dict(os.environ)
            os.environ["HERMES_HOME"] = str(hermes_home)
            os.environ["HERMES_CONFIG_FILE"] = str(config_file)
            os.environ["HERMES_SECRETS_FILE"] = str(secrets_file)

            try:
                registry = ModCommandRegistry(mods_dir=mods_dir)

                # Test Python script execution
                mock_event = MagicMock()
                mock_event.get_command_args.return_value = ""
                mock_source = MagicMock()
                res_py = await registry.dispatch(mock_event, mock_source, "pycmd")
                assert "MOD_ID=runner-mod" in res_py
                assert "VER=3.4.5" in res_py
                assert "CFG_TOKEN=secret-xyz-123" in res_py
                assert "LEGACY=secret-xyz-123" in res_py
                assert "DB=True" in res_py

                # Test Shell script execution
                res_sh = await registry.dispatch(mock_event, mock_source, "shcmd")
                assert "DATABASE_CONTENT" in res_sh
                assert "ENV_ID=runner-mod" in res_sh
                assert "ENV_VER=3.4.5" in res_sh
                assert "CFG_TOKEN=secret-xyz-123" in res_sh

                # Test Inline Python
                res_inline_py = await registry.dispatch(mock_event, mock_source, "inlinepy")
                assert res_inline_py == "INLINE_PY: runner-mod v3.4.5 token=secret-xyz-123 legacy=secret-xyz-123"

                # Test Inline Shell
                res_inline_sh = await registry.dispatch(mock_event, mock_source, "inlinesh")
                assert "INLINE_SH: runner-mod v3.4.5 CFG=INFO DATABASE_CONTENT" in res_inline_sh

            finally:
                os.environ.clear()
                os.environ.update(orig_env)

        asyncio.run(_run())

    def test_monitors_context_and_path_resolution(self, tmp_path: Path):
        """Verify monitor specs receive full environment and variable substitutions."""
        mod_dir = tmp_path / "monitor-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        spec = MonitorSpec(
            id="watch1",
            command="python3 ${mod_dir}/scripts/watcher.py --token ${config.api_key} --legacy ${user_config.api_key}",
            name="Watcher 1",
            mod_id="monitor-mod",
            mod_dir=mod_dir,
            mod_version="2.0.1",
        )

        mgr = ModMonitorManager(cache_dir=tmp_path / "cache")
        cmd_resolved, env = mgr.resolve_monitor_command(spec, {"api_key": "sec_token_999", "port": 8080})

        assert str(mod_dir) in cmd_resolved
        assert "sec_token_999" in cmd_resolved
        assert env["HERMES_MOD_ID"] == "monitor-mod"
        assert env["HERMES_MOD_VERSION"] == "2.0.1"
        assert env["HERMES_MOD_DIR"] == str(mod_dir)
        assert env["HERMES_MOD_CONFIG_API_KEY"] == "sec_token_999"
        assert env["HERMES_MOD_CONFIG_PORT"] == "8080"

    def test_mcp_servers_context_and_path_resolution(self, tmp_path: Path):
        """Verify declarative MCP servers auto-resolve cwd, relative paths, and env context."""
        mod_dir = tmp_path / "mcp-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)
        srv_file = mod_dir / "server.py"
        srv_file.write_text("print('mcp')\n", encoding="utf-8")

        mod = Mod(
            id="mcp-mod",
            name="MCP Mod",
            version="1.5.0",
            targets=[],
            path=mod_dir / "mod.yaml",
            mod_dir=mod_dir,
            mcp_servers={
                "custom-server": {
                    "command": "server.py",
                    "args": ["--mode", "daemon", "server.py"],
                    "env": {"EXTRA": "1"},
                }
            },
        )

        mock_cfg_mgr = MagicMock()
        mock_cfg_mgr.get_resolved_config.return_value = {"param": "val_123"}

        resolved = resolve_mod_mcp_servers(mod, config_mgr=mock_cfg_mgr)
        srv = resolved["custom-server"]

        assert srv["cwd"] == str(mod_dir)
        # Relative command pointing to server.py should be auto-resolved to absolute path
        assert srv["command"] == str(srv_file.resolve())
        # Argument 'server.py' pointing to file should be auto-resolved to absolute path
        assert srv["args"][2] == str(srv_file.resolve())
        # Environment must contain full mod context
        assert srv["env"]["HERMES_MOD_ID"] == "mcp-mod"
        assert srv["env"]["HERMES_MOD_DIR"] == str(mod_dir)
        assert srv["env"]["HERMES_MOD_VERSION"] == "1.5.0"
        assert srv["env"]["HERMES_MOD_CONFIG_PARAM"] == "val_123"

    def test_runner_verify_cmd_context_and_path_resolution(self, tmp_path: Path):
        """Verify runner run_verify executes with mod context and resolved working directory."""
        mod_dir = tmp_path / "verify-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)
        scripts_dir = mod_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        verify_script = scripts_dir / "test_check.py"
        verify_script.write_text(
            """import os, sys
assert os.environ.get("HERMES_MOD_ID") == "verify-mod"
assert os.environ.get("HERMES_MOD_VERSION") == "2.2.0"
assert os.environ.get("HERMES_MOD_DIR") != ""
assert os.path.exists("scripts/test_check.py")
print("PASS_VERIFY")
""",
            encoding="utf-8",
        )

        mod = Mod(
            id="verify-mod",
            name="Verify Mod",
            version="2.2.0",
            targets=[],
            path=mod_dir / "mod.yaml",
            mod_dir=mod_dir,
            verify_cmd="python3 scripts/test_check.py",
        )

        runner = ModRunner(repo_root=tmp_path)
        ok, out = runner.run_verify(mod.verify_cmd, mod.mod_dir, mod=mod)
        assert ok is True
        assert "PASS_VERIFY" in out


class TestGenericLoaderStressAndSecurity:
    """Stress testing edge cases and security boundaries of Generic Section Loader and Mod Context."""

    def test_resolve_path_security_path_traversal(self, tmp_path: Path):
        """Ensure resolve_path strictly prevents path traversal outside mod_dir."""
        async def _run():
            mods_dir = tmp_path / "mods"
            mods_dir.mkdir(parents=True, exist_ok=True)
            mod_dir = mods_dir / "secure-mod"
            mod_dir.mkdir(parents=True, exist_ok=True)

            scripts_dir = mod_dir / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)

            py_handler = scripts_dir / "security_check.py"
            py_handler.write_text(
                """def handle(ctx):
        resolve_fn = ctx["resolve_path"]
        results = []
        # 1. Valid local file
        valid = resolve_fn("scripts/security_check.py")
        results.append("VALID_OK" if valid.endswith("security_check.py") else "VALID_FAIL")

        # 2. Path traversal attempts
        for attack in ("../../etc/passwd", "../../../secrets.json", "/etc/shadow", "/../../root"):
            try:
                resolve_fn(attack)
                results.append(f"LEAK:{attack}")
            except ValueError:
                results.append("BLOCKED")
        return "|".join(results)
    """,
                encoding="utf-8",
            )

            mod_manifest = {
                "id": "secure-mod",
                "name": "Secure Mod",
                "version": "1.0.0",
                "enabled": True,
                "commands": [
                    {
                        "name": "checksec",
                        "description": "Security test",
                        "handler_script": "scripts/security_check.py",
                    }
                ],
            }
            (mod_dir / "mod.yaml").write_text(yaml.dump(mod_manifest), encoding="utf-8")

            mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="secure-mod")
            assert mod is not None
            update_commands_cache([mod], mods_dir=mods_dir)

            registry = ModCommandRegistry(mods_dir=mods_dir)
            mock_event = MagicMock()
            mock_event.get_command_args.return_value = ""
            mock_source = MagicMock()

            res = await registry.dispatch(mock_event, mock_source, "checksec")
            assert "VALID_OK" in res
            assert "LEAK" not in res
            # All 4 attack vectors must be BLOCKED
            assert res.count("BLOCKED") == 4

        asyncio.run(_run())

    def test_generic_loader_empty_files_and_mixed_types(self, tmp_path: Path):
        """Ensure empty files, comments-only files, and mixed list/dict items don't crash."""
        mod_dir = tmp_path / "edge-case-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        # Base mod.yaml with None fields
        (mod_dir / "mod.yaml").write_text(
            yaml.dump({
                "id": "edge-case-mod",
                "name": "Edge Case Mod",
                "version": "1.0.0",
                "targets": None,
                "monitors": None,
                "commands": None,
                "custom_list": ["item1"],
                "custom_dict": {"k1": "v1"},
            }),
            encoding="utf-8",
        )

        # Empty top-level files
        (mod_dir / "targets.yaml").write_text("", encoding="utf-8")
        (mod_dir / "user_config.yaml").write_text("# Comments only\n", encoding="utf-8")
        (mod_dir / "empty_section.yaml").write_text("", encoding="utf-8")

        # targets/ directory with empty & single-dict files
        t_dir = mod_dir / "targets"
        t_dir.mkdir()
        (t_dir / "01_empty.yaml").write_text("", encoding="utf-8")
        (t_dir / "02_valid.yaml").write_text(
            yaml.dump([{"file": "core.py", "old_block": "a = 1", "new_block": "a = 2"}]),
            encoding="utf-8",
        )
        (t_dir / "03_single_dict.yaml").write_text(
            yaml.dump({"file": "util.py", "old_block": "b = 1", "new_block": "b = 2"}),
            encoding="utf-8",
        )
        (t_dir / "04_invalid.txt").write_text("ignored non-yaml", encoding="utf-8")

        # user_config/ directory with empty file and valid config
        uc_dir = mod_dir / "user_config"
        uc_dir.mkdir()
        (uc_dir / "01_empty.yaml").write_text("", encoding="utf-8")
        (uc_dir / "02_cfg.yaml").write_text(
            yaml.dump({"poll-interval": {"type": "int", "default": 60}}),
            encoding="utf-8",
        )

        compiled = _load_folder_mod_sections(mod_dir)
        assert len(compiled.get("targets", [])) == 2
        assert {t["file"] for t in compiled["targets"]} == {"core.py", "util.py"}
        assert "poll-interval" in compiled.get("config", {})
        assert "poll-interval" in compiled.get("user_config", {})
        assert compiled.get("custom_list") == ["item1"]
        assert compiled.get("custom_dict") == {"k1": "v1"}

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="edge-case-mod")
        assert mod is not None
        assert len(mod.targets) == 2
        assert "poll-interval" in mod.config
        assert "poll-interval" in mod.user_config

    def test_canonical_config_yaml_and_dir_loading(self, tmp_path: Path):
        """Ensure folder mods load config from config.yaml and config/ directory."""
        mod_dir = tmp_path / "canonical-cfg-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)
        (mod_dir / "mod.yaml").write_text(yaml.dump({
            "id": "canonical-cfg-mod",
            "name": "Canonical Config Mod",
        }), encoding="utf-8")

        # config/ directory with split files
        cfg_dir = mod_dir / "config"
        cfg_dir.mkdir()
        (cfg_dir / "network.yaml").write_text(yaml.dump({
            "timeout": {"type": "int", "default": 45}
        }), encoding="utf-8")
        (cfg_dir / "auth.yaml").write_text(yaml.dump({
            "secret_key": {"type": "string", "sensitive": True}
        }), encoding="utf-8")

        compiled = _load_folder_mod_sections(mod_dir)
        assert "timeout" in compiled.get("config", {})
        assert "secret_key" in compiled.get("config", {})
        assert "timeout" in compiled.get("user_config", {})

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="canonical-cfg-mod")
        assert mod is not None
        assert mod.config["timeout"].default == 45
        assert mod.user_config["timeout"].default == 45
        assert mod.config["secret_key"].sensitive is True

    def test_generic_loader_skips_non_manifest_directories(self, tmp_path: Path):
        """Ensure non-section directories (assets, bin) with binary/script files are excluded."""
        mod_dir = tmp_path / "directory-filter-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        (mod_dir / "mod.yaml").write_text(
            yaml.dump({"id": "directory-filter-mod", "name": "Filter Mod", "version": "1.0.0"}),
            encoding="utf-8",
        )

        # Assets folder with images only
        assets_dir = mod_dir / "assets"
        assets_dir.mkdir()
        (assets_dir / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (assets_dir / "icon.ico").write_bytes(b"\x00\x00\x01\x00")

        # Bin folder with shell script only
        bin_dir = mod_dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "tool.sh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")

        compiled = _load_folder_mod_sections(mod_dir)
        assert "assets" not in compiled
        assert "bin" not in compiled

    def test_generic_loader_stem_name_and_id_inference(self, tmp_path: Path):
        """Commands and monitors missing explicit 'name'/'id' infer it from filename stem."""
        mod_dir = tmp_path / "stem-infer-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        (mod_dir / "mod.yaml").write_text(
            yaml.dump({"id": "stem-infer-mod", "name": "Infer Mod", "version": "1.0.0"}),
            encoding="utf-8",
        )

        # commands.yaml file
        (mod_dir / "commands.yaml").write_text(
            yaml.dump([{"name": "my_action", "description": "Action", "shell": "echo ok"}]),
            encoding="utf-8",
        )

        # monitors/ directory: file without 'id' key
        mon_dir = mod_dir / "monitors"
        mon_dir.mkdir()
        (mon_dir / "daemon_watcher.yaml").write_text(
            yaml.dump({"command": "python3 watcher.py", "interval": 10}),
            encoding="utf-8",
        )

        compiled = _load_folder_mod_sections(mod_dir)
        assert len(compiled.get("commands", [])) == 1
        assert compiled["commands"][0]["name"] == "my_action"

        assert len(compiled.get("monitors", [])) == 1
        assert compiled["monitors"][0]["id"] == "daemon_watcher"

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="stem-infer-mod")
        assert mod is not None
        assert len(mod.commands) == 1
        assert mod.commands[0].name == "my_action"
        assert len(mod.monitors) == 1
        assert mod.monitors[0].id == "daemon_watcher"

    def test_generic_loader_dict_declarations(self, tmp_path: Path):
        """Verify targets, commands, and monitors declared as mappings (dicts) parse correctly."""
        mod_dir = tmp_path / "dict-manifest-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "id": "dict-manifest-mod",
            "name": "Dict Mod",
            "version": "1.0.0",
            "targets": {
                "t1": {"file": "core.py", "old_block": "x=1", "new_block": "x=2"},
                "t2": {"file": "api.py", "old_block": "y=1", "new_block": "y=2"},
            },
            "commands": {
                "ping": {"description": "Ping test", "shell": "echo pong"},
                "pong": {"description": "Pong test", "shell": "echo ping"},
            },
            "monitors": {
                "health_mon": {"command": "python3 health.py", "interval": 5},
            },
        }
        (mod_dir / "mod.yaml").write_text(yaml.dump(manifest), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="dict-manifest-mod")
        assert mod is not None
        assert len(mod.targets) == 2
        assert len(mod.commands) == 2
        assert {c.name for c in mod.commands} == {"ping", "pong"}
        assert len(mod.monitors) == 1
        assert mod.monitors[0].id == "health_mon"

    def test_mcp_servers_path_traversal_prevention(self, tmp_path: Path):
        """MCP server relative paths must not escape mod_dir via .. traversal."""
        mod_dir = tmp_path / "mcp-sec-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        mod = Mod(
            id="mcp-sec-mod",
            name="MCP Sec Mod",
            version="1.0.0",
            targets=[],
            path=mod_dir / "mod.yaml",
            mod_dir=mod_dir,
            mcp_servers={
                "traversal-server": {
                    "command": "../../outside.sh",
                    "cwd": "../../../outside_dir",
                    "args": ["../../outside_arg"],
                }
            },
        )

        resolved = resolve_mod_mcp_servers(mod)
        srv = resolved["traversal-server"]
        # Must not resolve to external outside_dir
        assert srv["cwd"] == str(mod_dir.resolve())
        # Command and args must not be rewritten to external absolute path
        assert srv["command"] == "../../outside.sh"
        assert srv["args"] == ["../../outside_arg"]

    def test_environment_variable_sanitization(self, tmp_path: Path):
        """User config keys with hyphens or dots must be sanitized to valid POSIX env vars."""
        mod_dir = tmp_path / "env-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)

        spec = MonitorSpec(
            id="env_watcher",
            command="python3 watcher.py",
            mod_id="env-mod",
            mod_dir=mod_dir,
        )

        mgr = ModMonitorManager(cache_dir=tmp_path / "cache")
        _, env = mgr.resolve_monitor_command(spec, {
            "poll-interval": 120,
            "api.version": "v2",
            "nested-key-test": "active",
        })

        assert env["HERMES_MOD_CONFIG_POLL_INTERVAL"] == "120"
        assert env["HERMES_MOD_CONFIG_API_VERSION"] == "v2"
        assert env["HERMES_MOD_CONFIG_NESTED_KEY_TEST"] == "active"
