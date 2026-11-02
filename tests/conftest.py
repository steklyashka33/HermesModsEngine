"""Shared pytest fixtures and environment configuration for Hermes Mod Engine tests."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / ".engine"

# Ensure repo root and engine dir are in sys.path
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

HERMES_ROOT = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
if HERMES_ROOT.exists() and str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

# Ensure engine package is importable
if "engine" not in sys.modules and (ENGINE_DIR / "__init__.py").exists():
    spec = importlib.util.spec_from_file_location("engine", str(ENGINE_DIR / "__init__.py"))
    if spec and spec.loader:
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["engine"] = pkg
        spec.loader.exec_module(pkg)

# Ensure gateway.mod_commands is available for tests
if "gateway.mod_commands" not in sys.modules:
    mod_cmd_path = REPO_ROOT / "system" / "mod-command-registry" / "mod_commands.py"
    if mod_cmd_path.exists():
        spec = importlib.util.spec_from_file_location("gateway.mod_commands", str(mod_cmd_path))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["gateway.mod_commands"] = mod
            spec.loader.exec_module(mod)



@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def dummy_hermes_repo(tmp_path: Path) -> Path:
    """Creates a temporary mock Hermes Agent repository for testing."""
    root = tmp_path / "hermes-agent"
    root.mkdir(parents=True, exist_ok=True)

    # Create dummy target directories & files
    cli_dir = root / "hermes_cli"
    cli_dir.mkdir(parents=True, exist_ok=True)
    commands_py = cli_dir / "commands.py"
    commands_py.write_text(
        "def resolve_command(name: str):\n    return None\n",
        encoding="utf-8",
    )

    agent_dir = root / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    skill_utils_py = agent_dir / "skill_utils.py"
    skill_utils_py.write_text(
        "def get_skill_search_roots():\n    return []\n",
        encoding="utf-8",
    )

    tools_dir = root / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    mcp_tool_config_py = tools_dir / "mcp_tool_config.py"
    mcp_tool_config_py.write_text(
        "def _load_mcp_config():\n    return {}\n",
        encoding="utf-8",
    )

    pyproject_toml = root / "pyproject.toml"
    pyproject_toml.write_text(
        '[project]\nname = "hermes-agent"\nversion = "0.22.5"\n',
        encoding="utf-8",
    )

    return root
