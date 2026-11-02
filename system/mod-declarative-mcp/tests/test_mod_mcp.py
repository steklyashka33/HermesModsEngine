"""Unit tests for declarative MCP servers in Hermes Mods."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

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


@pytest.fixture
def test_engine(tmp_path):
    """Isolated mod engine package inside tmp_path."""
    pkg_dir = tmp_path / "test_engine_pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(_ENGINE_INIT, encoding="utf-8")
    (pkg_dir / "models.py").write_text(_MODELS_PY, encoding="utf-8")
    (pkg_dir / "loader.py").write_text(_LOADER_PY, encoding="utf-8")
    (pkg_dir / "config_manager.py").write_text(_CONFIG_MGR_PY, encoding="utf-8")
    (pkg_dir / "monitor_manager.py").write_text(_MONITOR_MGR_PY, encoding="utf-8")
    if _I18N_PY:
        (pkg_dir / "i18n.py").write_text(_I18N_PY, encoding="utf-8")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "test_engine_pkg",
        str(pkg_dir / "__init__.py"),
        submodule_search_locations=[str(pkg_dir)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["test_engine_pkg"] = pkg
    spec.loader.exec_module(pkg)
    pkg._mod_mcp_cache_clear()
    return pkg


@pytest.fixture
def hermes_mcp_env(tmp_path):
    """Isolated HERMES_HOME with mods/ and .engine/ to satisfy HomeIOGuard."""
    home = tmp_path / ".hermes"
    home.mkdir()
    mods_dir = home / "mods"
    mods_dir.mkdir()
    engine_dir = mods_dir / ".engine"
    engine_dir.mkdir()

    (engine_dir / "__init__.py").write_text(_ENGINE_INIT, encoding="utf-8")
    (engine_dir / "models.py").write_text(_MODELS_PY, encoding="utf-8")
    (engine_dir / "loader.py").write_text(_LOADER_PY, encoding="utf-8")
    (engine_dir / "config_manager.py").write_text(_CONFIG_MGR_PY, encoding="utf-8")
    (engine_dir / "monitor_manager.py").write_text(_MONITOR_MGR_PY, encoding="utf-8")

    # Minimal config.yaml
    (home / "config.yaml").write_text("mcp_servers: {}\n", encoding="utf-8")

    return home


class TestDeclarativeMcpManifestParsing:
    """Test parsing of mcp_servers and Claude Code mcpServers declarations."""

    def test_single_file_mod_mcp_servers(self, tmp_path, test_engine):
        manifest_file = tmp_path / "custom-mcp.yaml"
        manifest_data = {
            "id": "custom-mcp",
            "name": "Custom MCP Mod",
            "version": "1.0.0",
            "enabled": True,
            "mcp_servers": {
                "sqlite-srv": {
                    "command": "python3",
                    "args": ["${mod_dir}/server.py", "--db", "${user_config.db_path}"],
                    "env": {"DEBUG": "1"},
                }
            },
        }
        manifest_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

        mod = test_engine.parse_mod_manifest(manifest_file, mod_dir=None, fallback_id="custom-mcp")
        assert mod is not None
        assert mod.has_mcp_servers is True
        assert "sqlite-srv" in mod.mcp_servers
        assert mod.mcp_servers["sqlite-srv"]["command"] == "python3"
        assert len(mod.mcp_servers["sqlite-srv"]["args"]) == 3
        assert mod.list_mcp_servers() == ["sqlite-srv"]

    def test_claude_code_mcp_servers_key(self, tmp_path, test_engine):
        """Claude Code plugins declare mcpServers (camelCase)."""
        manifest_file = tmp_path / "claude-mod.yaml"
        manifest_data = {
            "id": "claude-mod",
            "name": "Claude Mod",
            "version": "1.0.0",
            "enabled": True,
            "mcpServers": {
                "claude-srv": {
                    "command": "node",
                    "args": ["${PLUGIN_ROOT}/index.js"],
                    "env": {"NODE_ENV": "production"},
                }
            },
        }
        manifest_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

        mod = test_engine.parse_mod_manifest(manifest_file, mod_dir=None, fallback_id="claude-mod")
        assert mod is not None
        assert mod.has_mcp_servers is True
        assert "claude-srv" in mod.mcp_servers
        assert mod.mcp_servers["claude-srv"]["command"] == "node"

    def test_folder_mod_external_mcp_json(self, tmp_path, test_engine):
        """Claude Code mcp.json inside folder mod directory."""
        mod_dir = tmp_path / "folder-mcp"
        mod_dir.mkdir(parents=True)
        manifest_data = {
            "id": "folder-mcp",
            "name": "Folder MCP",
            "version": "1.0.0",
            "enabled": True,
        }
        (mod_dir / "mod.yaml").write_text(yaml.dump(manifest_data), encoding="utf-8")

        mcp_json = {
            "mcpServers": {
                "ext-srv": {
                    "command": "python3",
                    "args": ["${mod_dir}/ext.py"],
                }
            }
        }
        (mod_dir / "mcp.json").write_text(json.dumps(mcp_json), encoding="utf-8")

        mod = test_engine.parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="folder-mcp")
        assert mod is not None
        assert "ext-srv" in mod.mcp_servers
        assert mod.mcp_servers["ext-srv"]["command"] == "python3"

    def test_folder_mod_external_mcp_servers_yaml(self, tmp_path, test_engine):
        """External mcp_servers.yaml inside folder mod directory."""
        mod_dir = tmp_path / "yaml-folder-mcp"
        mod_dir.mkdir(parents=True)
        manifest_data = {
            "id": "yaml-folder-mcp",
            "name": "YAML Folder MCP",
            "version": "1.0.0",
            "enabled": True,
        }
        (mod_dir / "mod.yaml").write_text(yaml.dump(manifest_data), encoding="utf-8")

        mcp_yaml = {
            "mcp_servers": {
                "yaml-srv": {
                    "command": "uvx",
                    "args": ["mcp-server-git"],
                }
            }
        }
        (mod_dir / "mcp_servers.yaml").write_text(yaml.dump(mcp_yaml), encoding="utf-8")

        mod = test_engine.parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="yaml-folder-mcp")
        assert mod is not None
        assert "yaml-srv" in mod.mcp_servers


class TestMcpVariableInterpolation:
    """Test variable interpolation: ${mod_dir}, ${MOD_DIR}, ${PLUGIN_ROOT}, ${user_config.<key>}."""

    def test_interpolation_all_surfaces(self, test_engine):
        server_cfg = {
            "command": "${mod_dir}/bin/runner",
            "args": [
                "--path", "${MOD_DIR}/data",
                "--claude", "${PLUGIN_ROOT}/tools",
                "--port", "${user_config.port}",
                "--debug", "${user_config.debug}",
            ],
            "env": {
                "SECRET_TOKEN": "${user_config.api_key}",
                "APP_DIR": "${mod_dir}",
                "UPPER_OPT": "${user_config.PORT}",
            },
            "cwd": "${mod_dir}/work",
        }

        user_cfg = {
            "port": 8080,
            "debug": True,
            "api_key": "sk-secret-12345",
        }

        res = test_engine.interpolate_mcp_config(
            server_cfg,
            mod_dir="/opt/mods/sample",
            user_config_values=user_cfg,
        )

        assert res["command"] == "/opt/mods/sample/bin/runner"
        assert res["args"] == [
            "--path", "/opt/mods/sample/data",
            "--claude", "/opt/mods/sample/tools",
            "--port", "8080",
            "--debug", "True",
        ]
        assert res["env"]["SECRET_TOKEN"] == "sk-secret-12345"
        assert res["env"]["APP_DIR"] == "/opt/mods/sample"
        assert res["env"]["UPPER_OPT"] == "8080"
        assert res["cwd"] == "/opt/mods/sample/work"

    def test_missing_config_variable_replaces_empty(self, test_engine):
        server_cfg = {
            "command": "srv",
            "args": ["--key", "${user_config.nonexistent}"],
        }
        res = test_engine.interpolate_mcp_config(server_cfg, user_config_values={})
        assert res["args"] == ["--key", ""]


class TestModConfigManagerSecretsResolution:
    """Test resolution with real ModConfigManager sensitive secrets."""

    def test_secrets_interpolation_in_mcp_servers(self, tmp_path, test_engine):
        cfg_file = tmp_path / "config.yaml"
        sec_file = tmp_path / "secrets.json"
        cfg_file.write_text(yaml.dump({"mods": {"secure-mcp": {"port": 7000}}}), encoding="utf-8")

        sec_storage = test_engine.SecretsStorage(sec_file)
        sec_storage.set_mod_secret("secure-mcp", "token", "super-secret-pass")

        mod = test_engine.Mod(
            id="secure-mcp",
            name="Secure MCP",
            targets=[],
            path=tmp_path / "secure-mcp.yaml",
            mod_dir=tmp_path / "secure-mcp-dir",
            user_config={
                "port": test_engine.ConfigField(name="port", type="int", default=8000),
                "token": test_engine.ConfigField(name="token", type="string", sensitive=True, required=True),
            },
            mcp_servers={
                "auth-srv": {
                    "command": "python3",
                    "args": ["--port", "${user_config.port}"],
                    "env": {"AUTH_BEARER": "Bearer ${user_config.token}"},
                }
            },
        )

        cfg_mgr = test_engine.ModConfigManager(cfg_file, sec_file)
        resolved = test_engine.resolve_mod_mcp_servers(mod, cfg_mgr)

        assert "auth-srv" in resolved
        assert resolved["auth-srv"]["args"] == ["--port", "7000"]
        assert resolved["auth-srv"]["env"]["AUTH_BEARER"] == "Bearer super-secret-pass"

    def test_canonical_config_interpolation_in_mcp_servers(self, tmp_path, test_engine):
        cfg_file = tmp_path / "config.yaml"
        sec_file = tmp_path / "secrets.json"
        cfg_file.write_text(yaml.dump({"mods": {"canonical-mcp": {"port": 9000}}}), encoding="utf-8")

        sec_storage = test_engine.SecretsStorage(sec_file)
        sec_storage.set_mod_secret("canonical-mcp", "token", "canonical-secret")

        mod = test_engine.Mod(
            id="canonical-mcp",
            name="Canonical MCP",
            targets=[],
            path=tmp_path / "canonical-mcp.yaml",
            mod_dir=tmp_path / "canonical-mcp-dir",
            config={
                "port": test_engine.ConfigField(name="port", type="int", default=8000),
                "token": test_engine.ConfigField(name="token", type="string", sensitive=True, required=True),
            },
            mcp_servers={
                "auth-srv": {
                    "command": "python3",
                    "args": ["--port", "${config.port}"],
                    "env": {"AUTH_BEARER": "Bearer ${config.token}"},
                }
            },
        )

        cfg_mgr = test_engine.ModConfigManager(cfg_file, sec_file)
        resolved = test_engine.resolve_mod_mcp_servers(mod, cfg_mgr)

        assert "auth-srv" in resolved
        assert resolved["auth-srv"]["args"] == ["--port", "9000"]
        assert resolved["auth-srv"]["env"]["AUTH_BEARER"] == "Bearer canonical-secret"


class TestGetEnabledModMcpServers:
    """Test discovery of all active mod MCP servers."""

    def test_enabled_and_disabled_filtering(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)

        # Enabled mod
        mod1 = custom_dir / "mod-a.yaml"
        mod1.write_text(yaml.dump({
            "id": "mod-a",
            "name": "Mod A",
            "enabled": True,
            "mcp_servers": {
                "srv-a": {"command": "python3", "args": ["a.py"]}
            }
        }), encoding="utf-8")

        # Disabled mod
        mod2 = custom_dir / "mod-b.yaml"
        mod2.write_text(yaml.dump({
            "id": "mod-b",
            "name": "Mod B",
            "enabled": False,
            "mcp_servers": {
                "srv-b": {"command": "python3", "args": ["b.py"]}
            }
        }), encoding="utf-8")

        servers = test_engine.get_enabled_mod_mcp_servers(mods_dir=mods_dir)
        assert "srv-a" in servers
        assert "srv-b" not in servers

    def test_dynamic_enable_disable_cache(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)

        mod_file = custom_dir / "toggle.yaml"
        mod_file.write_text(yaml.dump({
            "id": "toggle",
            "name": "Toggle",
            "enabled": True,
            "mcp_servers": {
                "toggle-srv": {"command": "echo", "args": ["hi"]}
            }
        }), encoding="utf-8")

        # 1. Enabled
        servers1 = test_engine.get_enabled_mod_mcp_servers(mods_dir=mods_dir)
        assert "toggle-srv" in servers1

        # 2. Disable mod
        mod_file.write_text(yaml.dump({
            "id": "toggle",
            "name": "Toggle",
            "enabled": False,
            "mcp_servers": {
                "toggle-srv": {"command": "echo", "args": ["hi"]}
            }
        }), encoding="utf-8")

        # Invalidate cache
        test_engine._mod_mcp_cache_clear()
        servers2 = test_engine.get_enabled_mod_mcp_servers(mods_dir=mods_dir)
        assert "toggle-srv" not in servers2


class TestHermesRuntimeBridge:
    """Test transparent configuration bridge in tools.mcp_tool_config."""

    def test_load_mcp_config_includes_active_mod_servers(self, hermes_mcp_env):
        from tools.mcp_tool_config import _load_mcp_config

        mods_dir = hermes_mcp_env / "mods"
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)
        mod_file = custom_dir / "active-srv.yaml"
        mod_file.write_text(yaml.dump({
            "id": "active-srv-mod",
            "name": "Active Server Mod",
            "enabled": True,
            "mcp_servers": {
                "mod-declared-server": {
                    "command": "/usr/bin/python3",
                    "args": ["-m", "test"],
                    "enabled": True,
                }
            }
        }), encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mcp_env)}):
            loaded = _load_mcp_config()
            assert "mod-declared-server" in loaded
            assert loaded["mod-declared-server"]["command"] == "/usr/bin/python3"

    def test_native_config_overrides_mod_server(self, hermes_mcp_env):
        """Native config.yaml always takes precedence on name collision."""
        from tools.mcp_tool_config import _load_mcp_config

        # Set up a native server in config.yaml
        cfg_file = hermes_mcp_env / "config.yaml"
        cfg_file.write_text(yaml.dump({
            "mcp_servers": {
                "sample-srv": {
                    "command": "/usr/bin/native-sample",
                    "enabled": True,
                }
            }
        }), encoding="utf-8")

        # Set up a mod with colliding server name
        mods_dir = hermes_mcp_env / "mods"
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)
        mod_file = custom_dir / "colliding-mod.yaml"
        mod_file.write_text(yaml.dump({
            "id": "colliding-mod",
            "name": "Colliding Mod",
            "enabled": True,
            "mcp_servers": {
                "sample-srv": {
                    "command": "/usr/local/bin/mod-sample",
                    "enabled": True,
                }
            }
        }), encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mcp_env)}):
            loaded = _load_mcp_config()
            assert "sample-srv" in loaded
            # Native entry must win
            assert loaded["sample-srv"]["command"] == "/usr/bin/native-sample"

    def test_disabled_mod_server_excluded_from_load_mcp_config(self, hermes_mcp_env):
        """When mod is disabled, its server is not in _load_mcp_config()."""
        from tools.mcp_tool_config import _load_mcp_config

        mods_dir = hermes_mcp_env / "mods"
        custom_dir = mods_dir / "custom"
        custom_dir.mkdir(parents=True, exist_ok=True)
        mod_file = custom_dir / "disabled-srv.yaml"
        mod_file.write_text(yaml.dump({
            "id": "disabled-srv-mod",
            "name": "Disabled Server Mod",
            "enabled": False,
            "mcp_servers": {
                "disabled-server": {
                    "command": "/usr/bin/python3",
                    "args": ["-m", "disabled"],
                    "enabled": True,
                }
            }
        }), encoding="utf-8")

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mcp_env)}):
            loaded = _load_mcp_config()
            assert "disabled-server" not in loaded
