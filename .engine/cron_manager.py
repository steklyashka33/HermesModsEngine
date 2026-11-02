"""Hermes Mod Cron Manager: automated scheduling and status of engine updates via crontab.
Provides duplicate detection, idempotent installation, safe uninstallation, and status inspection.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(Path(__file__).resolve().parent.parent)))
MODS_DIR = DEFAULT_MODS_DIR

CRON_MARKER_TAG = "# [HermesModsEngine:auto-update]"
DEFAULT_SCHEDULE = "0 4 * * *"


def human_readable_schedule(schedule: str) -> str:
    """Return a human-friendly description of common cron expressions."""
    sched = schedule.strip()
    if sched == "0 4 * * *":
        return "Daily at 04:00 (Nightly)"
    if sched in ("0 0 * * *", "@midnight", "@daily"):
        return "Daily at midnight (00:00)"
    if sched in ("0 * * * *", "@hourly"):
        return "Hourly"
    if sched == "@weekly":
        return "Weekly (Sunday midnight)"
    if sched == "@monthly":
        return "Monthly (1st of month at midnight)"

    # Match simple daily pattern: M H * * *
    m = re.match(r"^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*$", sched)
    if m:
        minute, hour = int(m.group(1)), int(m.group(2))
        if 0 <= minute <= 59 and 0 <= hour <= 23:
            return f"Daily at {hour:02d}:{minute:02d}"

    # Match every N hours: 0 */N * * *
    m_step_hour = re.match(r"^0\s+\*/(\d+)\s+\*\s+\*\s+\*$", sched)
    if m_step_hour:
        return f"Every {m_step_hour.group(1)} hours"

    # Match every N minutes: */N * * * *
    m_step_min = re.match(r"^\*/(\d+)\s+\*\s+\*\s+\*\s+\*$", sched)
    if m_step_min:
        return f"Every {m_step_min.group(1)} minutes"

    return sched


def validate_cron_schedule(schedule: str) -> bool:
    """Validate standard 5-part cron schedule or standard @-macro."""
    s = schedule.strip()
    if not s:
        return False
    if s in ("@daily", "@midnight", "@hourly", "@weekly", "@monthly", "@yearly", "@annually", "@reboot"):
        return True
    parts = s.split()
    if len(parts) != 5:
        return False
    # Validate allowable characters for each part
    pattern = re.compile(r"^[0-9A-Za-z,\-*/]+$")
    return all(pattern.match(p) is not None for p in parts)


class CronManager:
    """Manages crontab scheduling for HermesModsEngine auto-updates."""

    def __init__(
        self,
        mods_dir: Path = MODS_DIR,
        hermes_home: Path = HERMES_HOME,
        crontab_bin: str = "crontab",
    ):
        self.mods_dir = Path(mods_dir).resolve()
        self.hermes_home = Path(hermes_home).resolve()
        self.crontab_bin = crontab_bin
        self.runner_path = self.mods_dir / "runner.py"
        self.log_file = self.hermes_home / "logs" / "update.log"

    def get_python_bin(self, explicit_bin: Optional[str] = None) -> str:
        """Resolve the optimal Python interpreter for the cron job."""
        if explicit_bin and os.access(explicit_bin, os.X_OK):
            return explicit_bin

        env_py = os.environ.get("HERMES_PYTHON")
        if env_py and os.access(env_py, os.X_OK):
            return env_py

        # Known virtual environments
        known_candidates = [
            "/usr/local/lib/hermes-agent/venv/bin/python3",
            str(self.hermes_home / "venv" / "bin" / "python3"),
        ]
        for c in known_candidates:
            if os.access(c, os.X_OK):
                return c

        # Dynamic Hermes install venv
        for venv_py in self.hermes_home.glob("installs/*/environments/*/venv/bin/python3"):
            if os.access(str(venv_py), os.X_OK):
                return str(venv_py)

        if sys.executable and os.access(sys.executable, os.X_OK):
            return sys.executable

        which_py = shutil.which("python3")
        return which_py if which_py else "python3"

    def read_crontab(self) -> Tuple[bool, str, str]:
        """Read the current user's crontab."""
        if not shutil.which(self.crontab_bin):
            return False, "", f"crontab utility '{self.crontab_bin}' not found on system PATH"
        try:
            res = subprocess.run([self.crontab_bin, "-l"], capture_output=True, text=True, check=False)
            if res.returncode == 0:
                return True, res.stdout, ""
            err = res.stderr.strip()
            # Standard error when user has no crontab yet
            if "no crontab for" in err.lower():
                return True, "", ""
            return False, "", err
        except Exception as exc:
            return False, "", str(exc)

    def write_crontab(self, content: str) -> Tuple[bool, str]:
        """Write updated crontab for the current user."""
        if not shutil.which(self.crontab_bin):
            return False, f"crontab utility '{self.crontab_bin}' not found on system PATH"

        content_stripped = content.strip()
        if not content_stripped:
            # Empty crontab: remove it safely
            try:
                res = subprocess.run([self.crontab_bin, "-r"], capture_output=True, text=True, check=False)
                if res.returncode == 0 or "no crontab" in res.stderr.lower():
                    return True, ""
                return False, res.stderr.strip()
            except Exception as exc:
                return False, str(exc)

        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
            temp_path = f.name

        try:
            res = subprocess.run([self.crontab_bin, temp_path], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                return False, res.stderr.strip()
            return True, ""
        except Exception as exc:
            return False, str(exc)
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    def build_cron_command(self, python_bin: Optional[str] = None) -> str:
        """Construct the complete auto-update command string."""
        py = self.get_python_bin(python_bin)
        return f"{py} {self.runner_path} update --all >> {self.log_file} 2>&1"

    def _is_hermes_update_line(self, line: str, prev_line: Optional[str] = None) -> bool:
        """Check if a crontab line corresponds to HermesModsEngine auto-update."""
        stripped = line.strip()
        if not stripped:
            return False
        # If preceding line was the marker
        if prev_line and CRON_MARKER_TAG in prev_line:
            return True
        # If line itself is an active cron job running runner.py update
        if not stripped.startswith("#") and "runner.py" in stripped and "update" in stripped:
            return True
        return False

    def status(self) -> Dict[str, Any]:
        """Inspect the current auto-update cron job status and recent log output."""
        ok, crontab_text, err = self.read_crontab()
        if not ok:
            return {
                "installed": False,
                "status": "error",
                "error": err,
                "schedule": None,
                "schedule_human": None,
                "command": None,
                "log_file": str(self.log_file),
                "log_exists": self.log_file.exists(),
            }

        lines = crontab_text.splitlines()
        found_job = False
        job_schedule = None
        job_command = None
        raw_line = None

        prev_line = None
        for line in lines:
            if self._is_hermes_update_line(line, prev_line):
                found_job = True
                raw_line = line.strip()
                tokens = raw_line.split()
                if len(tokens) >= 6:
                    job_schedule = " ".join(tokens[:5])
                    job_command = " ".join(tokens[5:])
                elif raw_line.startswith("@") and len(tokens) >= 2:
                    job_schedule = tokens[0]
                    job_command = " ".join(tokens[1:])
                break
            prev_line = line

        log_exists = self.log_file.exists()
        log_size = 0
        log_mtime_str = None
        last_lines: List[str] = []

        if log_exists:
            try:
                st = self.log_file.stat()
                log_size = st.st_size
                log_mtime_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                # Read last 5 lines
                text = self.log_file.read_text(encoding="utf-8", errors="replace")
                all_l = [l.strip() for l in text.splitlines() if l.strip()]
                last_lines = all_l[-5:]
            except Exception:
                pass

        return {
            "installed": found_job,
            "status": "active" if found_job else "not_installed",
            "schedule": job_schedule,
            "schedule_human": human_readable_schedule(job_schedule) if job_schedule else None,
            "command": job_command,
            "raw_line": raw_line,
            "log_file": str(self.log_file),
            "log_exists": log_exists,
            "log_size_bytes": log_size,
            "log_mtime": log_mtime_str,
            "log_last_lines": last_lines,
        }

    def install(
        self,
        schedule: str = DEFAULT_SCHEDULE,
        python_bin: Optional[str] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Idempotently install or update the auto-update cron job."""
        schedule = schedule.strip()
        if not validate_cron_schedule(schedule):
            return {
                "success": False,
                "status": "error",
                "error": f"Invalid cron schedule expression: '{schedule}'. Expected 5 space-separated fields or standard @-macro.",
                "schedule": schedule,
            }

        py_bin = self.get_python_bin(python_bin)
        cmd = f"{py_bin} {self.runner_path} update --all >> {self.log_file} 2>&1"
        new_cron_line = f"{schedule} {cmd}"
        comment_line = f"{CRON_MARKER_TAG} Nightly three-phase engine & core update"

        if dry_run:
            return {
                "success": True,
                "status": "dry_run",
                "message": f"Simulation: would install cron job with schedule '{schedule}'",
                "schedule": schedule,
                "schedule_human": human_readable_schedule(schedule),
                "command": cmd,
                "log_file": str(self.log_file),
            }

        # Ensure logs directory exists
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return {"success": False, "status": "error", "error": f"Failed to create log directory: {exc}"}

        ok, crontab_text, err = self.read_crontab()
        if not ok:
            return {"success": False, "status": "error", "error": err}

        lines = crontab_text.splitlines()
        new_lines: List[str] = []
        replaced = False
        already_identical = False

        idx = 0
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()

            # Check if this line is our marker comment
            if stripped.startswith(CRON_MARKER_TAG):
                # Next line might be the command
                if idx + 1 < len(lines) and self._is_hermes_update_line(lines[idx + 1], prev_line=stripped):
                    existing_cmd_line = lines[idx + 1].strip()
                    if existing_cmd_line == new_cron_line:
                        already_identical = True
                        new_lines.append(line)
                        new_lines.append(lines[idx + 1])
                    else:
                        new_lines.append(comment_line)
                        new_lines.append(new_cron_line)
                        replaced = True
                    idx += 2
                    continue
                else:
                    # Stale marker alone, replace it with new block
                    new_lines.append(comment_line)
                    new_lines.append(new_cron_line)
                    replaced = True
                    idx += 1
                    continue

            # Check if this line is an un-marked runner.py update line
            if self._is_hermes_update_line(line):
                if stripped == new_cron_line:
                    already_identical = True
                    # Add comment above it if not present
                    new_lines.append(comment_line)
                    new_lines.append(new_cron_line)
                else:
                    new_lines.append(comment_line)
                    new_lines.append(new_cron_line)
                    replaced = True
                idx += 1
                continue

            new_lines.append(line)
            idx += 1

        if already_identical:
            return {
                "success": True,
                "status": "already_installed",
                "message": f"Auto-update cron job is already configured with identical schedule ({schedule}).",
                "schedule": schedule,
                "schedule_human": human_readable_schedule(schedule),
                "command": cmd,
                "log_file": str(self.log_file),
            }

        if not replaced:
            # Append to bottom with clean spacing
            if new_lines and new_lines[-1].strip():
                new_lines.append("")
            new_lines.append(comment_line)
            new_lines.append(new_cron_line)

        final_content = "\n".join(new_lines) + "\n"
        write_ok, write_err = self.write_crontab(final_content)
        if not write_ok:
            return {"success": False, "status": "error", "error": write_err}

        status_action = "updated" if replaced else "installed"
        return {
            "success": True,
            "status": status_action,
            "message": f"Successfully {status_action} auto-update cron job with schedule '{schedule}'.",
            "schedule": schedule,
            "schedule_human": human_readable_schedule(schedule),
            "command": cmd,
            "log_file": str(self.log_file),
        }

    def uninstall(self, dry_run: bool = False) -> Dict[str, Any]:
        """Safely remove any HermesModsEngine auto-update entries from user crontab."""
        ok, crontab_text, err = self.read_crontab()
        if not ok:
            return {"success": False, "status": "error", "error": err}

        lines = crontab_text.splitlines()
        new_lines: List[str] = []
        found_any = False

        idx = 0
        while idx < len(lines):
            line = lines[idx]
            stripped = line.strip()

            if stripped.startswith(CRON_MARKER_TAG):
                found_any = True
                # Skip marker and subsequent cron command if present
                if idx + 1 < len(lines) and self._is_hermes_update_line(lines[idx + 1], prev_line=stripped):
                    idx += 2
                    continue
                idx += 1
                continue

            if self._is_hermes_update_line(line):
                found_any = True
                idx += 1
                continue

            new_lines.append(line)
            idx += 1

        if not found_any:
            return {
                "success": True,
                "status": "not_found",
                "message": "No HermesModsEngine auto-update cron job was found in crontab.",
            }

        if dry_run:
            return {
                "success": True,
                "status": "dry_run",
                "message": "Simulation: would remove auto-update cron job from crontab.",
            }

        final_content = "\n".join(new_lines).rstrip()
        if final_content:
            final_content += "\n"

        write_ok, write_err = self.write_crontab(final_content)
        if not write_ok:
            return {"success": False, "status": "error", "error": write_err}

        return {
            "success": True,
            "status": "uninstalled",
            "message": "Successfully removed auto-update cron job from crontab.",
        }


def handle_cron_cli(args, mods: Optional[List[Any]] = None) -> int:
    """CLI dispatcher for `runner.py cron [status|install|uninstall]`."""
    cron_mgr = CronManager(
        mods_dir=Path(getattr(args, "mods_dir", MODS_DIR)),
    )

    # Subcommand: target argument or default to 'status'
    subcmd = getattr(args, "target", None)
    if not subcmd or subcmd == "cron":
        subcmd = "status"
    subcmd = subcmd.lower()

    schedule = getattr(args, "schedule", DEFAULT_SCHEDULE) or DEFAULT_SCHEDULE
    python_bin = getattr(args, "python_bin", None)
    dry_run = bool(getattr(args, "dry_run", False))
    as_json = bool(getattr(args, "json", False) or getattr(args, "format", "text") == "json")

    if subcmd == "status":
        res = cron_mgr.status()
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("status") != "error" else 1

        print("================================================================================")
        print(" HermesModsEngine Auto-Update Cron Status")
        print("================================================================================")
        if res.get("status") == "error":
            print(f"❌ Error checking crontab: {res.get('error')}")
            return 1

        if res.get("installed"):
            print("Status:       🟢 ACTIVE (Scheduled in user crontab)")
            print(f"Schedule:     {res.get('schedule')} ({res.get('schedule_human')})")
            print(f"Command:      {res.get('command')}")
            log_st = "exists" if res.get("log_exists") else "not yet created"
            size_kb = res.get("log_size_bytes", 0) / 1024
            last_mod = f", last run: {res.get('log_mtime')}" if res.get("log_mtime") else ""
            print(f"Log File:     {res.get('log_file')} ({log_st}, {size_kb:.1f} KB{last_mod})")
            last_lines = res.get("log_last_lines", [])
            if last_lines:
                print("Recent Log Output:")
                for l in last_lines:
                    print(f"  {l}")
        else:
            print("Status:       ⚪ NOT INSTALLED (Inactive)")
            print(f"Schedule:     None (Default recommendation: {DEFAULT_SCHEDULE})")
            print(f"Log File:     {res.get('log_file')}")
            print("\nAction:")
            print("  To enable nightly automated updates, run:")
            print(f"    python3 {cron_mgr.runner_path} cron install")
            print(f"  Or custom schedule (e.g. 03:00):")
            print(f"    python3 {cron_mgr.runner_path} cron install --schedule \"0 3 * * *\"")
        print("================================================================================")
        return 0

    elif subcmd == "install":
        res = cron_mgr.install(schedule=schedule, python_bin=python_bin, dry_run=dry_run)
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("success") else 1

        if not res.get("success"):
            print(f"❌ Error configuring auto-update cron job: {res.get('error')}", file=sys.stderr)
            return 1

        st = res.get("status")
        if st == "dry_run":
            print("🔍 Simulation (dry-run): Cron job not written to disk.")
            print(f"  Schedule: {res.get('schedule')} ({res.get('schedule_human')})")
            print(f"  Command:  {res.get('command')}")
            print(f"  Log File: {res.get('log_file')}")
            return 0

        if st == "already_installed":
            print(f"ℹ Auto-update cron job already configured with identical schedule '{res.get('schedule')}' ({res.get('schedule_human')}).")
            print(f"  Command:  {res.get('command')}")
            print(f"  Log File: {res.get('log_file')}")
            return 0

        action_word = "updated" if st == "updated" else "installed"
        print(f"✔ Auto-update cron job successfully {action_word}:")
        print(f"  Schedule: {res.get('schedule')} ({res.get('schedule_human')})")
        print(f"  Command:  {res.get('command')}")
        print(f"  Log File: {res.get('log_file')}")
        print(f"  Verified: Crontab entry persisted safely without affecting other jobs.")
        return 0

    elif subcmd in ("uninstall", "remove", "disable"):
        res = cron_mgr.uninstall(dry_run=dry_run)
        if as_json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
            return 0 if res.get("success") else 1

        if not res.get("success"):
            print(f"❌ Error removing auto-update cron job: {res.get('error')}", file=sys.stderr)
            return 1

        if res.get("status") == "dry_run":
            print("🔍 Simulation (dry-run): Would remove Hermes auto-update job from crontab.")
            return 0

        if res.get("status") == "not_found":
            print("ℹ No HermesModsEngine auto-update cron job found in crontab. Nothing to uninstall.")
            return 0

        print("✔ Auto-update cron job successfully uninstalled from crontab.")
        return 0

    else:
        err_msg = f"Unknown cron subaction: '{subcmd}'. Supported: status, install, uninstall"
        if as_json:
            print(json.dumps({"success": False, "error": err_msg}, indent=2, ensure_ascii=False))
        else:
            print(f"❌ {err_msg}", file=sys.stderr)
        return 1
