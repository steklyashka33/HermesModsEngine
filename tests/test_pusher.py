"""Tests for Hermes Mod Pusher and runner.py push functionality."""
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
import pytest

from engine.pusher import ModPusher, handle_push_cli
from engine.version import ENGINE_VERSION

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_default_mods = HERMES_HOME / "mods"
if not _default_mods.exists() and (Path(__file__).resolve().parent.parent / ".engine").is_dir():
    _default_mods = Path(__file__).resolve().parent.parent
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(_default_mods)))


class TestModPusher:
    """Unit tests for ModPusher engine module."""

    def test_pusher_initialization(self):
        pusher = ModPusher(mods_dir=MODS_DIR)
        assert pusher.mods_dir == MODS_DIR
        assert pusher.is_git_repo() is True
        assert pusher.get_current_branch() in ("main", "master")

    def test_conventional_commit_generation_heuristics(self):
        pusher = ModPusher(mods_dir=MODS_DIR)

        # Single custom mod
        msg = pusher.generate_commit_message(["custom/my-cool-mod.yaml"])
        assert msg == "feat(mod-my-cool-mod): update my-cool-mod declarative mod"

        msg_folder = pusher.generate_commit_message(["custom/smart-compaction/mod.yaml", "custom/smart-compaction/targets.yaml"])
        assert "feat(mods)" in msg_folder or "feat(mod-smart-compaction)" in msg_folder

        # Single system component
        msg_sys = pusher.generate_commit_message(["system/mod-manager/commands.yaml"])
        assert msg_sys == "fix(system): update mod-manager system component"

        # Engine component
        msg_eng = pusher.generate_commit_message([".engine/pusher.py"])
        assert msg_eng == "feat(engine): update engine pusher module"

        # CLI
        msg_cli = pusher.generate_commit_message(["runner.py"])
        assert msg_cli == "feat(cli): enhance runner CLI operations"

        # CI / Workflows
        msg_ci = pusher.generate_commit_message([".github/workflows/ci.yml"])
        assert msg_ci == "ci: update GitHub workflows and community templates"

        # Documentation
        msg_doc = pusher.generate_commit_message(["docs/architecture.md"])
        assert msg_doc == "docs: update documentation and specifications"

        # Empty files
        msg_empty = pusher.generate_commit_message([])
        assert "chore(engine)" in msg_empty

    def test_post_commit_hook_installation(self, tmp_path: Path):
        # Create an isolated git repo
        repo_dir = tmp_path / "test_repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(repo_dir)], check=True, capture_output=True)

        pusher = ModPusher(mods_dir=repo_dir)
        success, hook_path_str = pusher.install_post_commit_hook()
        assert success is True
        hook_path = Path(hook_path_str)
        assert hook_path.is_file()

        # Check executable permissions
        mode = hook_path.stat().st_mode
        assert bool(mode & stat.S_IXUSR)

        content = hook_path.read_text(encoding="utf-8")
        assert "HERMES_DISABLE_AUTOPUSH" in content
        assert "git-askpass.sh" in content
        assert "git push" in content

    def test_dry_run_push(self):
        pusher = ModPusher(mods_dir=MODS_DIR)
        res = pusher.push(dry_run=True)
        assert res["status"] == "dry_run"
        assert res["dry_run"] is True
        assert res["exit_code"] == 0
        assert "remote" in res
        assert "branch" in res
        assert "files_to_stage" in res
        assert "commit_message" in res

    def test_runner_push_dry_run_cli(self):
        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "push", "--dry-run"]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(MODS_DIR))
        assert res.returncode == 0
        assert "Симуляция выгрузки на GitHub (dry-run)" in res.stdout
        assert "origin" in res.stdout

    def test_runner_push_dry_run_json_cli(self):
        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "push", "--dry-run", "--json"]
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(MODS_DIR))
        assert res.returncode == 0
        data = json.loads(res.stdout)
        assert data["status"] == "dry_run"
        assert data["dry_run"] is True
        assert data["exit_code"] == 0
        assert isinstance(data["files_to_stage"], list)
