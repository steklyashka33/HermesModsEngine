"""Hermes Mod Pusher: zero-friction automated push to GitHub via git-askpass credential helper.
Supports dry-run simulation, conventional commit auto-generation, and post-commit auto-push hook installation.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .version import ENGINE_VERSION

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(Path(__file__).resolve().parent.parent)))
MODS_DIR = DEFAULT_MODS_DIR
DEFAULT_REMOTE_URL = "https://github.com/steklyashka33/HermesModsEngine.git"


class ModPusher:
    """Manages zero-friction pushing and synchronization with GitHub."""

    def __init__(
        self,
        mods_dir: Path = MODS_DIR,
        git_bin: str = "git",
        default_remote_url: str = DEFAULT_REMOTE_URL,
    ):
        self.mods_dir = Path(mods_dir)
        self.git_bin = git_bin
        self.default_remote_url = default_remote_url
        self.askpass_script = self.mods_dir / "scripts" / "git-askpass.sh"

    def run_git(
        self,
        args: List[str],
        env_extra: Optional[Dict[str, str]] = None,
        timeout: int = 60,
    ) -> Tuple[int, str, str]:
        """Execute a git command within mods_dir using configured credential helper."""
        cmd = [self.git_bin, "-C", str(self.mods_dir)] + args
        try:
            env = os.environ.copy()
            if self.askpass_script.exists():
                try:
                    mode = self.askpass_script.stat().st_mode
                    if not (mode & stat.S_IXUSR):
                        self.askpass_script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                except Exception:
                    pass
                env["GIT_ASKPASS"] = str(self.askpass_script)
                env.setdefault("GIT_TERMINAL_PROMPT", "0")

            if env_extra:
                env.update(env_extra)

            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=env,
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except Exception as exc:
            return 1, "", str(exc)

    def is_git_repo(self) -> bool:
        """Verify whether mods_dir is a git repository."""
        code, out, _ = self.run_git(["rev-parse", "--is-inside-work-tree"])
        if code != 0 or out != "true":
            return False
        # Avoid false positives from parent repos (e.g. root home directory ~/.git)
        code_top, out_top, _ = self.run_git(["rev-parse", "--show-toplevel"])
        if code_top == 0 and out_top:
            top_path = Path(out_top).resolve()
            mods_res = self.mods_dir.resolve()
            if top_path != mods_res and mods_res.is_relative_to(top_path) and not (self.mods_dir / ".git").exists():
                return False
        return True

    def get_current_branch(self) -> str:
        """Return the current branch name or 'main'."""
        code, out, _ = self.run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if code == 0 and out and out != "HEAD":
            return out
        return "main"

    def ensure_remote(self, remote: str = "origin", default_url: Optional[str] = None) -> bool:
        """Ensure the specified remote exists, configuring default URL if missing."""
        if not self.is_git_repo():
            return False
        code, remotes_out, _ = self.run_git(["remote"])
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip()]
        if remote not in remotes:
            target_url = default_url or self.default_remote_url
            code_add, _, _ = self.run_git(["remote", "add", remote, target_url])
            return code_add == 0
        return True

    def ensure_credentials_helper(self) -> bool:
        """Configure local repository askpass helper so direct git CLI uses git-askpass.sh."""
        if not self.is_git_repo() or not self.askpass_script.exists():
            return False
        try:
            self.run_git(["config", "--local", "core.askpass", str(self.askpass_script)])
            self.run_git(["config", "--local", "credential.helper", ""])
            return True
        except Exception:
            return False

    def install_post_commit_hook(self) -> Tuple[bool, str]:
        """Install or update .git/hooks/post-commit hook for automated zero-friction pushes."""
        if not self.is_git_repo():
            return False, "Not a git repository"

        hooks_dir = self.mods_dir / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        hook_path = hooks_dir / "post-commit"

        hook_content = (
            "#!/usr/bin/env bash\\n"
            "# HermesModsEngine post-commit auto-push hook\\n"
            "# Automatically pushes commits to origin main without user intervention.\\n"
            "set -e\\n\\n"
            "if [ \"${HERMES_DISABLE_AUTOPUSH:-0}\" = \"1\" ]; then\\n"
            "    exit 0\\n"
            "fi\\n\\n"
            "REPO_DIR=\"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\"\\n"
            "ASKPASS=\"${REPO_DIR}/scripts/git-askpass.sh\"\\n\\n"
            "if [ -f \"$ASKPASS\" ]; then\\n"
            "    chmod +x \"$ASKPASS\" 2>/dev/null || true\\n"
            "    export GIT_ASKPASS=\"$ASKPASS\"\\n"
            "    export GIT_TERMINAL_PROMPT=\"0\"\\n"
            "fi\\n\\n"
            "BRANCH=\"$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo \"main\")\"\\n"
            "if [ \"$BRANCH\" = \"HEAD\" ] || [ -z \"$BRANCH\" ]; then\\n"
            "    exit 0\\n"
            "fi\\n\\n"
            "REMOTE=\"origin\"\\n"
            "echo \"🚀 [hermes-mods] Auto-pushing commit $(git rev-parse --short HEAD 2>/dev/null) to ${REMOTE}/${BRANCH}...\"\\n"
            "if git push -u \"$REMOTE\" \"$BRANCH\"; then\\n"
            "    echo \"✓ [hermes-mods] Auto-push succeeded.\"\\n"
            "else\\n"
            "    echo \"⚠️  [hermes-mods] Auto-push failed or offline. Run 'python3 runner.py push' manually.\" >&2\\n"
            "fi\\n"
        ).replace("\\n", "\n")

        try:
            hook_path.write_text(hook_content, encoding="utf-8")
            hook_path.chmod(0o755)
            return True, str(hook_path)
        except Exception as exc:
            return False, f"Failed to install post-commit hook: {exc}"

    def get_changed_files(self) -> List[str]:
        """Retrieve list of modified, added, deleted, or untracked files respecting .gitignore."""
        code, out, _ = self.run_git(["status", "--porcelain"])
        if code != 0 or not out:
            return []
        files = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: 'XY path' or 'XY path -> path'
            parts = line.split(maxsplit=1)
            if len(parts) >= 2:
                file_path = parts[1].strip()
                if " -> " in file_path:
                    file_path = file_path.split(" -> ")[1].strip()
                files.append(file_path.strip('"'))
        return files

    def generate_commit_message(self, changed_files: List[str]) -> str:
        """Synthesize an intelligent Conventional Commit message based on changed files."""
        if not changed_files:
            return f"chore(engine): update HermesModsEngine v{ENGINE_VERSION}"

        # Single file heuristics
        if len(changed_files) == 1:
            f = changed_files[0]
            if f.startswith("custom/"):
                sub = f[len("custom/"):]
                mod_name = sub.split("/")[0].replace(".yaml", "").replace(".yml", "")
                return f"feat(mod-{mod_name}): update {mod_name} declarative mod"
            elif f.startswith("system/"):
                sub = f[len("system/"):]
                mod_name = sub.split("/")[0].replace(".yaml", "").replace(".yml", "")
                return f"fix(system): update {mod_name} system component"
            elif f.startswith(".engine/"):
                mod_part = Path(f).stem
                return f"feat(engine): update engine {mod_part} module"
            elif f == "runner.py":
                return "feat(cli): enhance runner CLI operations"
            elif f.startswith(".github/"):
                return "ci: update GitHub workflows and community templates"
            elif f.startswith("docs/") or f.endswith(".md"):
                return "docs: update documentation and specifications"
            elif f.startswith("tests/"):
                return "test: update test suite"
            elif f == "install.sh":
                return "fix(installer): improve universal installation script"
            elif f == "pyproject.toml":
                return "chore(build): update project packaging configuration"

        # Categorize changes
        has_custom = any(f.startswith("custom/") for f in changed_files)
        has_system = any(f.startswith("system/") for f in changed_files)
        has_engine = any(f.startswith(".engine/") or f == "runner.py" for f in changed_files)
        has_github = any(f.startswith(".github/") for f in changed_files)
        has_docs = any(f.startswith("docs/") or f.endswith(".md") for f in changed_files)
        has_tests = any(f.startswith("tests/") for f in changed_files)

        categories = []
        if has_engine:
            categories.append("engine")
        if has_system:
            categories.append("system")
        if has_custom:
            categories.append("mods")
        if has_github:
            categories.append("ci")
        if has_docs:
            categories.append("docs")
        if has_tests:
            categories.append("tests")

        if len(categories) == 1:
            cat = categories[0]
            if cat == "mods":
                return "feat(mods): update declarative custom mods"
            elif cat == "system":
                return "fix(system): refine core system components"
            elif cat == "engine":
                return "feat(engine): enhance mod engine runtime modules"
            elif cat == "ci":
                return "ci(github): update workflows and templates"
            elif cat == "docs":
                return "docs: update documentation and specifications"
            elif cat == "tests":
                return "test: add and update test suites"

        cats_str = ", ".join(categories[:3]) if categories else "mods"
        return f"chore({cats_str}): synchronize changes across HermesModsEngine"

    def get_commits_ahead_and_behind(self, remote: str = "origin", branch: str = "main") -> Tuple[int, int]:
        """Count how many commits local is ahead and behind remote."""
        remote_ref = f"{remote}/{branch}"
        # Test if remote ref exists
        code_ref, _, _ = self.run_git(["rev-parse", "--verify", remote_ref])
        if code_ref != 0:
            # Remote ref not found; return (commits on HEAD, 0)
            code_head, out_head, _ = self.run_git(["rev-list", "--count", "HEAD"])
            ahead = int(out_head) if (code_head == 0 and out_head.isdigit()) else 0
            return ahead, 0

        _, ahead_str, _ = self.run_git(["rev-list", "--count", f"{remote_ref}..HEAD"])
        _, behind_str, _ = self.run_git(["rev-list", "--count", f"HEAD..{remote_ref}"])
        ahead = int(ahead_str) if ahead_str.isdigit() else 0
        behind = int(behind_str) if behind_str.isdigit() else 0
        return ahead, behind

    def push(
        self,
        message: Optional[str] = None,
        dry_run: bool = False,
        install_hook: bool = False,
        remote: str = "origin",
        branch: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """Execute the complete push and synchronization workflow."""
        if not self.is_git_repo():
            return {
                "status": "error",
                "error": f"Directory {self.mods_dir} is not a git repository.",
                "exit_code": 1,
            }

        target_branch = branch or self.get_current_branch()
        self.ensure_remote(remote)
        self.ensure_credentials_helper()

        hook_installed = False
        hook_msg = ""
        if install_hook:
            hook_installed, hook_msg = self.install_post_commit_hook()

        changed_files = self.get_changed_files()
        is_dirty = len(changed_files) > 0
        ahead, behind = self.get_commits_ahead_and_behind(remote, target_branch)

        commit_msg = message.strip() if message else self.generate_commit_message(changed_files)

        # Simulation mode
        if dry_run:
            c_code, head_commit, _ = self.run_git(["rev-parse", "--short", "HEAD"])
            return {
                "status": "dry_run",
                "dry_run": True,
                "remote": remote,
                "branch": target_branch,
                "dirty": is_dirty,
                "files_to_stage": changed_files,
                "commit_message": commit_msg,
                "ahead": ahead,
                "behind": behind,
                "head_commit": head_commit if c_code == 0 else "-",
                "hook_installed": hook_installed,
                "hook_message": hook_msg,
                "exit_code": 0,
            }

        committed = False
        new_commit_hash = ""

        # 1. Commit changes if working directory is dirty
        if is_dirty:
            code_add, _, err_add = self.run_git(["add", "-A"])
            if code_add != 0:
                return {
                    "status": "error",
                    "error": f"git add -A failed: {err_add}",
                    "exit_code": 1,
                }

            code_com, out_com, err_com = self.run_git(
                ["commit", "-m", commit_msg],
                env_extra={"HERMES_DISABLE_AUTOPUSH": "1"},
            )
            if code_com != 0:
                return {
                    "status": "error",
                    "error": f"git commit failed: {err_com or out_com}",
                    "exit_code": 1,
                }

            committed = True
            _, new_commit_hash, _ = self.run_git(["rev-parse", "HEAD"])
            ahead += 1
        else:
            _, new_commit_hash, _ = self.run_git(["rev-parse", "HEAD"])

        # 2. Push if new commits were made or branch is ahead of remote
        if committed or ahead > 0 or force:
            push_args = ["push", "-u", remote, target_branch]
            if force:
                push_args.append("--force")

            code_push, out_push, err_push = self.run_git(
                push_args,
                env_extra={"HERMES_DISABLE_AUTOPUSH": "1"},
                timeout=120,
            )
            if code_push != 0:
                return {
                    "status": "error",
                    "error": f"git push failed: {err_push or out_push}",
                    "committed": committed,
                    "commit_hash": new_commit_hash,
                    "commit_message": commit_msg,
                    "exit_code": 1,
                }

            short_hash = new_commit_hash[:7] if new_commit_hash else "-"
            return {
                "status": "completed",
                "committed": committed,
                "pushed": True,
                "commit_hash": new_commit_hash,
                "short_commit": short_hash,
                "commit_message": commit_msg,
                "remote": remote,
                "branch": target_branch,
                "files_staged": changed_files,
                "hook_installed": hook_installed,
                "exit_code": 0,
            }

        # 3. Already up to date
        short_hash = new_commit_hash[:7] if new_commit_hash else "-"
        return {
            "status": "up_to_date",
            "committed": False,
            "pushed": False,
            "commit_hash": new_commit_hash,
            "short_commit": short_hash,
            "remote": remote,
            "branch": target_branch,
            "message": f"Репозиторий уже синхронизирован с {remote}/{target_branch} (коммит: {short_hash}). Нечего выгружать.",
            "hook_installed": hook_installed,
            "exit_code": 0,
        }


def handle_push_cli(args, mods: Optional[List[Any]] = None) -> int:
    """CLI handler for `runner.py push` command."""
    pusher = ModPusher(
        mods_dir=Path(getattr(args, "mods_dir", MODS_DIR)),
    )
    remote = getattr(args, "remote", "origin") or "origin"
    branch = getattr(args, "branch", None)
    message = getattr(args, "message", None)
    dry_run = bool(getattr(args, "dry_run", False))
    install_hook = bool(getattr(args, "install_hook", False))
    force = bool(getattr(args, "force", False))

    res = pusher.push(
        message=message,
        dry_run=dry_run,
        install_hook=install_hook,
        remote=remote,
        branch=branch,
        force=force,
    )

    if getattr(args, "json", False):
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return res.get("exit_code", 0)

    # Human-readable output formatting
    if res.get("hook_installed"):
        print(f"✓ Git post-commit hook успешно установлен: {res.get('hook_message', '.git/hooks/post-commit')}")

    if res.get("status") == "dry_run":
        print("🔍 Симуляция выгрузки на GitHub (dry-run): изменения не вносятся")
        print(f"  • Ветка: {res.get('branch')}")
        print(f"  • Удаленный репозиторий: {res.get('remote')}")
        files = res.get("files_to_stage", [])
        if files:
            print(f"  • Файлов к добавлению: {len(files)}")
            for f in files[:10]:
                print(f"    - {f}")
            if len(files) > 10:
                print(f"    ... и ещё {len(files) - 10} файл(ов)")
            print(f"  • Сообщение коммита: {res.get('commit_message')}")
        else:
            print("  • Рабочая директория чиста (нет незакоммиченных изменений)")
        ahead = res.get("ahead", 0)
        if ahead > 0:
            print(f"  • Коммитов к отправке: {ahead}")
        else:
            print("  • Локальная ветка синхронизирована с удаленной")
        return 0

    if res.get("status") == "completed":
        staged = res.get("files_staged", [])
        short_h = res.get("short_commit", "-")
        if res.get("committed"):
            print(f"✓ Изменения успешно закоммичены ({short_h}) и отправлены в GitHub!")
            print(f"  • Сообщение: {res.get('commit_message')}")
            print(f"  • Затронуто файлов: {len(staged)}")
        else:
            print(f"✓ Коммиты успешно отправлены в GitHub ({res.get('remote')}/{res.get('branch')})!")
            print(f"  • Текущий коммит: {short_h}")
        return 0

    if res.get("status") == "up_to_date":
        print(f"✓ {res.get('message')}")
        return 0

    err = res.get("error", "Неизвестная ошибка выгрузки")
    print(f"❌ Ошибка выгрузки: {err}", file=sys.stderr)
    return res.get("exit_code", 1)
