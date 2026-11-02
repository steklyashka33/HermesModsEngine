"""Manager for mod background daemon monitors with stdout streaming and rate limiting."""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .models import MonitorSpec

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
MONITORS_CACHE_DIR = Path(os.environ.get("HERMES_MONITORS_CACHE_DIR", str(HERMES_HOME / "cache" / "mods")))
STATE_FILE = MONITORS_CACHE_DIR / "monitors_state.json"


class TokenBucket:
    """Rate limiter to prevent stdout notification spam."""

    def __init__(self, capacity: int = 10, refill_rate: float = 0.5):
        self.capacity = capacity
        self.refill_rate = refill_rate  # tokens per second (0.5 = 1 token per 2 sec)
        self.tokens = float(capacity)
        self.last_update = time.time()
        self.suppressed_count = 0

    def consume(self) -> bool:
        now = time.time()
        elapsed = now - self.last_update
        self.last_update = now
        self.tokens = min(float(self.capacity), self.tokens + elapsed * self.refill_rate)
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        self.suppressed_count += 1
        return False


class ModMonitorManager:
    """Controls lifecycle and tracking of mod monitors."""

    def __init__(self, cache_dir: Path = MONITORS_CACHE_DIR):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.cache_dir / "monitors_state.json"
        self._rate_limiters: Dict[str, TokenBucket] = {}

    def _load_state(self) -> Dict[str, dict]:
        if not self.state_file.exists():
            return {}
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_state(self, state: Dict[str, dict]) -> None:
        self.state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _is_pid_alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def get_monitor_key(self, mod_id: str, monitor_id: str) -> str:
        return f"{mod_id}:{monitor_id}"

    def get_running_monitors(self) -> Dict[str, dict]:
        state = self._load_state()
        running = {}
        changed = False
        for key, info in list(state.items()):
            pid = info.get("pid")
            if pid and self._is_pid_alive(pid):
                running[key] = info
            else:
                if info.get("status") == "RUNNING":
                    info["status"] = "STOPPED"
                    info["stopped_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
                    changed = True
                running[key] = info
        if changed:
            self._save_state(state)
        return running

    def resolve_monitor_command(self, spec: MonitorSpec, config_vars: Dict[str, Any]) -> Tuple[str, dict]:
        """Substitute paths and variables in command."""
        mod_dir_str = str(spec.mod_dir) if spec.mod_dir else str(HERMES_HOME / "mods")
        mod_data_dir = self.cache_dir / spec.mod_id
        mod_data_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["HERMES_MOD_DIR"] = mod_dir_str
        env["HERMES_MOD_DATA"] = str(mod_data_dir)
        env["HERMES_MOD_ID"] = spec.mod_id
        env["HERMES_MOD_VERSION"] = str(getattr(spec, "mod_version", "1.0.0"))
        env["HERMES_MONITOR_ID"] = spec.id
        env["PYTHONUNBUFFERED"] = "1"

        cmd = spec.command
        cmd = cmd.replace("${MOD_DIR}", mod_dir_str)
        cmd = cmd.replace("${mod_dir}", mod_dir_str)
        cmd = cmd.replace("${MOD_DATA}", str(mod_data_dir))
        cmd = cmd.replace("${mod_data}", str(mod_data_dir))

        for k, v in config_vars.items():
            if v is not None:
                clean_k = re.sub(r"[^A-Z0-9_]", "_", str(k).upper())
                env[f"HERMES_MOD_CONFIG_{clean_k}"] = str(v)
                env[f"HERMES_MOD_OPTION_{clean_k}"] = str(v)
                # Safe non-sensitive interpolation only
                cmd = cmd.replace(f"${{config.{k}}}", str(v)).replace(f"${{user_config.{k}}}", str(v))

        return cmd, env

    def start_monitor(self, spec: MonitorSpec, config_vars: Dict[str, Any]) -> Tuple[bool, str]:
        key = self.get_monitor_key(spec.mod_id, spec.id)
        state = self.get_running_monitors()
        curr = state.get(key)
        if curr and curr.get("status") == "RUNNING" and self._is_pid_alive(curr.get("pid", 0)):
            return True, f"Monitor '{key}' is already running (PID {curr['pid']})."

        cmd_str, proc_env = self.resolve_monitor_command(spec, config_vars)
        log_dir = self.cache_dir / spec.mod_id / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{spec.id}.log"

        cwd = spec.mod_dir if spec.mod_dir and spec.mod_dir.exists() else Path.home()
        out_f = open(log_file, "a", encoding="utf-8")

        try:
            # Spawn process in new process group
            proc = subprocess.Popen(
                cmd_str,
                shell=True,
                cwd=str(cwd),
                env=proc_env,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception as exc:
            return False, f"Failed to start monitor '{key}': {exc}"
        finally:
            try:
                out_f.close()
            except OSError:
                pass

        state[key] = {
            "mod_id": spec.mod_id,
            "monitor_id": spec.id,
            "name": spec.name or spec.id,
            "description": spec.description,
            "command": cmd_str,
            "pid": proc.pid,
            "status": "RUNNING",
            "delivery": spec.delivery,
            "log_file": str(log_file),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "restarts": curr.get("restarts", 0) if curr else 0,
        }
        self._save_state(state)
        return True, f"✓ Started monitor '{key}' (PID {proc.pid}, log: {log_file})"

    def stop_monitor(self, mod_id: str, monitor_id: str, timeout: float = 5.0) -> Tuple[bool, str]:
        """Stop a running monitor process group with strict bounded timeout (max 5-10s)."""
        key = self.get_monitor_key(mod_id, monitor_id)
        state = self.get_running_monitors()
        curr = state.get(key)
        if not curr or not curr.get("pid"):
            return False, f"Monitor '{key}' is not registered."

        pid = curr["pid"]
        if not self._is_pid_alive(pid):
            curr["status"] = "STOPPED"
            self._save_state(state)
            return True, f"Monitor '{key}' was already stopped."

        # Bound timeout between 1.0 and 10.0 seconds
        effective_timeout = max(1.0, min(float(timeout), 10.0))
        graceful_budget = effective_timeout * 0.6
        kill_budget = effective_timeout * 0.4

        try:
            # Terminate whole process group
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

        # Wait up to graceful_budget for graceful termination
        stopped = False
        start_time = time.time()
        while time.time() - start_time < graceful_budget:
            time.sleep(0.05)
            if not self._is_pid_alive(pid):
                stopped = True
                break

        if not stopped:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    os.kill(pid, signal.SIGKILL)
                except Exception:
                    pass

            kill_start = time.time()
            while time.time() - kill_start < kill_budget:
                time.sleep(0.05)
                if not self._is_pid_alive(pid):
                    stopped = True
                    break

        curr["status"] = "STOPPED"
        curr["stopped_at"] = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        self._save_state(state)
        return True, f"✓ Stopped monitor '{key}' (PID {pid})."

    def format_notification(self, mod_id: str, monitor_id: str, text: str) -> Optional[str]:
        """Format event line with rate-limiting."""
        key = self.get_monitor_key(mod_id, monitor_id)
        bucket = self._rate_limiters.setdefault(key, TokenBucket())
        if not bucket.consume():
            return None

        suppressed = ""
        if bucket.suppressed_count > 0:
            suppressed = f" [+{bucket.suppressed_count} events rate-limited]"
            bucket.suppressed_count = 0

        clean_text = text.strip()[:500]
        return f"🔔 **[Монитор {key}]** {clean_text}{suppressed}"
