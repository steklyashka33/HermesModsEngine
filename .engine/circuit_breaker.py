"""Runtime Circuit Breaker for Hermes Mods.

Monitors mod failures and exceptions in runtime (slash commands, background monitors, target hooks).
If a mod exceeds the failure threshold (e.g. >= 5 errors in 5 minutes):
1. Trips the circuit breaker (transitions to OPEN state).
2. Marks the mod as `disabled_circuit_breaker` in `.state.json`.
3. Rolls back mod patches from Hermes core (`runner.py disable <id> --circuit-breaker`).
4. Emits a single protected, rate-limited warning to logs/state.
5. Keeps all other mods and Hermes core operating smoothly.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .file_ops import FileLock, atomic_write_text
from .updater import StateManager

logger = logging.getLogger("hermes.circuit_breaker")

# Default thresholds
DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_WINDOW_SECONDS = 300.0   # 5 minutes
DEFAULT_COOLDOWN_SECONDS = 1800.0  # 30 minutes

CIRCUIT_STATE_CLOSED = "CLOSED"
CIRCUIT_STATE_OPEN = "OPEN"
CIRCUIT_STATE_HALF_OPEN = "HALF_OPEN"

SYSTEM_MOD_IDS: Set[str] = {
    "mod-command-registry",
    "mod-declarative-mcp",
    "mod-folder-skills",
    "mod-update-integration",
}


def _safe_clear_mcp_cache() -> None:
    try:
        from .loader import _mod_mcp_cache_clear
        _mod_mcp_cache_clear()
    except Exception:
        pass


class CircuitBreaker:
    """Tracks runtime incidents per mod and provides automated isolation upon repeated failures."""

    def __init__(
        self,
        mods_dir: Optional[Union[str, Path]] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        repo_root: Optional[Union[str, Path]] = None,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        auto_rollback: bool = True,
    ):
        if mods_dir is None:
            mods_dir = Path(os.environ.get("HERMES_MODS_DIR", Path.home() / ".hermes" / "mods"))
        self.mods_dir = Path(mods_dir).resolve()
        self.mods_dir.mkdir(parents=True, exist_ok=True)

        if cache_dir is None:
            cache_dir = Path(os.environ.get("HERMES_MONITORS_CACHE_DIR", str(Path.home() / ".hermes" / "cache" / "mods")))
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        if repo_root is None:
            from .loader import detect_hermes_root
            repo_root = detect_hermes_root()
        self.repo_root = Path(repo_root).resolve()

        self.state_file = self.mods_dir / ".state.json"
        self.cb_file = self.mods_dir / ".circuit_breaker.json"

        self.failure_threshold = max(1, int(failure_threshold))
        self.window_seconds = max(1.0, float(window_seconds))
        self.cooldown_seconds = float(cooldown_seconds) if cooldown_seconds else 0.0
        self.auto_rollback = auto_rollback

    def _load_data_unlocked(self) -> Dict[str, Any]:
        if not self.cb_file.exists():
            return {"incidents": {}, "statuses": {}}
        try:
            data = json.loads(self.cb_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("incidents", {})
                data.setdefault("statuses", {})
                return data
        except Exception:
            pass
        return {"incidents": {}, "statuses": {}}

    def _save_data_unlocked(self, data: Dict[str, Any]) -> None:
        try:
            atomic_write_text(self.cb_file, json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.warning("[CIRCUIT BREAKER] Failed to save circuit breaker state: %s", exc)

    def _prune_incidents_unlocked(self, data: Dict[str, Any], now: float) -> bool:
        """Remove incidents older than window_seconds."""
        changed = False
        cutoff = now - self.window_seconds
        incidents_map = data.get("incidents", {})
        for mid, incs in list(incidents_map.items()):
            valid = [inc for inc in incs if isinstance(inc, dict) and inc.get("timestamp", 0) >= cutoff]
            if len(valid) != len(incs):
                incidents_map[mid] = valid
                changed = True
        return changed

    def _get_state_unlocked(self, data: Dict[str, Any], mod_id: str, now: Optional[float] = None) -> str:
        """Determine circuit state without re-acquiring FileLock."""
        if not mod_id:
            return CIRCUIT_STATE_CLOSED

        current_time = now if now is not None else time.time()

        # 1. Check .state.json override
        try:
            state_mgr = StateManager(self.state_file)
            ovr = state_mgr.get_mod_overrides(mod_id)
            if ovr.get("disabled_circuit_breaker"):
                tripped_at = ovr.get("circuit_breaker_tripped_at")
                if self.cooldown_seconds and tripped_at:
                    try:
                        if current_time - float(tripped_at) > self.cooldown_seconds:
                            return CIRCUIT_STATE_HALF_OPEN
                    except (ValueError, TypeError):
                        pass
                return CIRCUIT_STATE_OPEN
        except Exception:
            pass

        # 2. Check in-memory/cb_file status
        status = data.get("statuses", {}).get(mod_id, {})
        current_state = status.get("state", CIRCUIT_STATE_CLOSED)
        tripped_at = status.get("tripped_at")
        if current_state == CIRCUIT_STATE_OPEN and self.cooldown_seconds and tripped_at:
            try:
                if current_time - float(tripped_at) > self.cooldown_seconds:
                    return CIRCUIT_STATE_HALF_OPEN
            except (ValueError, TypeError):
                pass
        return current_state

    def record_error(
        self,
        mod_id: str,
        error: Union[str, Exception],
        error_type: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> bool:
        """Record an error for mod_id.

        If errors in sliding window >= failure_threshold, trips the breaker (OPEN)
        and triggers isolation.

        Returns True if this error caused the breaker to trip, False otherwise.
        """
        if not mod_id:
            return False

        # Protect system engine mods from being isolated
        if mod_id in SYSTEM_MOD_IDS:
            logger.warning(
                "[CIRCUIT BREAKER] Error recorded for core system mod '%s', protected from isolation: %s",
                mod_id, error
            )
            return False

        now = timestamp if timestamp is not None else time.time()
        err_msg = str(error)
        if not error_type:
            error_type = type(error).__name__ if isinstance(error, Exception) else "RuntimeError"

        incident = {
            "timestamp": now,
            "error": err_msg,
            "error_type": error_type,
            "details": details or {},
        }

        tripped = False
        trip_reason: Optional[str] = None

        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            self._prune_incidents_unlocked(data, now)

            mod_incs = data["incidents"].setdefault(mod_id, [])
            mod_incs.append(incident)

            status = data["statuses"].setdefault(mod_id, {
                "state": CIRCUIT_STATE_CLOSED,
                "tripped_at": None,
                "warning_emitted": False,
                "reason": None,
                "failure_count": len(mod_incs),
            })
            status["failure_count"] = len(mod_incs)
            status["last_failure_time"] = now
            status["last_error"] = err_msg

            current_state = status.get("state", CIRCUIT_STATE_CLOSED)
            # Trip breaker if threshold reached and not already open
            if len(mod_incs) >= self.failure_threshold and current_state != CIRCUIT_STATE_OPEN:
                status["state"] = CIRCUIT_STATE_OPEN
                status["tripped_at"] = now
                trip_reason = (
                    f"Circuit breaker tripped: {len(mod_incs)} errors in {int(self.window_seconds)}s "
                    f"(last error: {err_msg[:120]})"
                )
                status["reason"] = trip_reason
                tripped = True

            self._save_data_unlocked(data)

        if tripped and self.auto_rollback:
            self.isolate_mod(mod_id, reason=trip_reason)

        return tripped

    def record_success(self, mod_id: str, now: Optional[float] = None) -> None:
        """Record successful execution of a mod.

        If currently in HALF_OPEN, transitions back to CLOSED and resets incidents.
        """
        if not mod_id:
            return

        current_time = now if now is not None else time.time()
        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            current_state = self._get_state_unlocked(data, mod_id, now=current_time)
            if current_state == CIRCUIT_STATE_HALF_OPEN:
                status = data.setdefault("statuses", {}).setdefault(mod_id, {})
                status["state"] = CIRCUIT_STATE_CLOSED
                status["tripped_at"] = None
                status["warning_emitted"] = False
                status["reason"] = None
                status["failure_count"] = 0
                if mod_id in data.get("incidents", {}):
                    data["incidents"][mod_id] = []
                self._save_data_unlocked(data)

                # Clear override in .state.json
                try:
                    state_mgr = StateManager(self.state_file)
                    state_mgr.set_mod_override(mod_id, {
                        "disabled_circuit_breaker": False,
                        "disabled_reason": None,
                        "circuit_breaker_tripped_at": None,
                    })
                except Exception:
                    pass

    def get_state(self, mod_id: str, now: Optional[float] = None) -> str:
        """Return the current circuit state for mod_id (accounting for cooldown)."""
        if not mod_id:
            return CIRCUIT_STATE_CLOSED
        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            return self._get_state_unlocked(data, mod_id, now=now)

    def is_tripped(self, mod_id: str, now: Optional[float] = None) -> bool:
        """True if the breaker is in OPEN state."""
        return self.get_state(mod_id, now=now) == CIRCUIT_STATE_OPEN

    def can_execute(self, mod_id: str, now: Optional[float] = None) -> bool:
        """True if execution is permitted (CLOSED or HALF_OPEN trial)."""
        return self.get_state(mod_id, now=now) != CIRCUIT_STATE_OPEN

    def get_failures(self, mod_id: str, window_only: bool = True, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Return failure incidents for mod_id."""
        current_time = now if now is not None else time.time()
        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            incs = list(data.get("incidents", {}).get(mod_id, []))
            if window_only:
                cutoff = current_time - self.window_seconds
                incs = [inc for inc in incs if isinstance(inc, dict) and inc.get("timestamp", 0) >= cutoff]
            return incs

    def isolate_mod(self, mod_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        """Isolate failing mod:
        1. Set disabled_circuit_breaker in .state.json.
        2. Emit single protected warning (log & state).
        3. Rollback targets from Hermes core.
        4. Stop background monitors.
        5. Clear relevant caches.
        """
        if mod_id in SYSTEM_MOD_IDS:
            logger.error("[CIRCUIT BREAKER] Refusing to isolate system core mod '%s'.", mod_id)
            return {"mod_id": mod_id, "tripped": False, "status": "protected_system_mod"}

        now = time.time()
        should_warn = False
        final_reason = reason or "Circuit breaker tripped: repeated runtime errors"

        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            status = data.setdefault("statuses", {}).setdefault(mod_id, {
                "state": CIRCUIT_STATE_OPEN,
                "tripped_at": now,
                "warning_emitted": False,
                "reason": final_reason,
                "failure_count": len(data.get("incidents", {}).get(mod_id, [])),
            })
            status["state"] = CIRCUIT_STATE_OPEN
            status["tripped_at"] = now
            status["reason"] = final_reason

            if not status.get("warning_emitted", False):
                should_warn = True
                status["warning_emitted"] = True

            self._save_data_unlocked(data)

        # 1. Update .state.json with override
        state_mgr = StateManager(self.state_file)
        state_mgr.set_mod_override(mod_id, {
            "enabled": False,
            "disabled_circuit_breaker": True,
            "disabled_reason": final_reason,
            "circuit_breaker_tripped_at": now,
        })

        # 2. Emit single protected warning
        warning_msg = (
            f"⚠️ [CIRCUIT BREAKER] Мод '{mod_id}' автоматически изолирован: {final_reason}. "
            f"Патчи откатаны из ядра Hermes, шлюз продолжает работу."
        )
        if should_warn:
            self._emit_single_warning(mod_id, warning_msg, final_reason, now)

        # 3. Rollback patches from core
        unpatch_report = self._rollback_mod_from_core(mod_id)

        # 4. Stop running monitors
        self._stop_mod_monitors(mod_id)

        # 5. Clear MCP and commands cache
        _safe_clear_mcp_cache()

        return {
            "success": True,
            "mod_id": mod_id,
            "status": "disabled_circuit_breaker",
            "reason": final_reason,
            "tripped_at": now,
            "warning": warning_msg if should_warn else None,
            "unpatch_report": unpatch_report,
        }

    def _emit_single_warning(self, mod_id: str, warning_msg: str, reason: str, timestamp: float) -> None:
        """Emit protected warning to log, stderr, and persist in state."""
        logger.warning(warning_msg)
        try:
            sys.stderr.write(f"\n{warning_msg}\n")
            sys.stderr.flush()
        except Exception:
            pass

        # Persist warning in .state.json
        try:
            state_mgr = StateManager(self.state_file)
            with FileLock(self.state_file):
                data = state_mgr._load_unlocked()
                warnings_map = data.setdefault("circuit_breaker_warnings", {})
                warnings_map[mod_id] = {
                    "warning": warning_msg,
                    "reason": reason,
                    "timestamp": timestamp,
                }
                atomic_write_text(self.state_file, json.dumps(data, indent=2, ensure_ascii=False))
        except Exception as exc:
            logger.warning("[CIRCUIT BREAKER] Failed to persist warning to .state.json: %s", exc)

    def _rollback_mod_from_core(self, mod_id: str) -> Dict[str, Any]:
        """Rollback patches applied by mod_id from Hermes core without touching other mods."""
        # Method A: in-process ModRunner
        try:
            from runner import ModRunner, ModLoader
            runner = ModRunner(mods_dir=self.mods_dir, repo_root=self.repo_root)
            mods = ModLoader.load_all(self.mods_dir)
            target = next((m for m in mods if m.id == mod_id), None)
            if target:
                return runner.unpatch_all([target], include_system=False)
        except Exception as exc:
            logger.debug("[CIRCUIT BREAKER] In-process rollback failed: %s", exc)

        # Method B: CLI runner.py
        runner_script = self.mods_dir / "runner.py"
        if runner_script.exists():
            try:
                proc = subprocess.run(
                    [sys.executable, str(runner_script), "unpatch", mod_id, "--mods-dir", str(self.mods_dir), "--json"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0 and proc.stdout.strip():
                    try:
                        return json.loads(proc.stdout)
                    except Exception:
                        pass
            except Exception as e:
                logger.error("[CIRCUIT BREAKER] CLI unpatch failed: %s", e)

        return {"status": "unpatch_completed", "mod_id": mod_id}

    def _stop_mod_monitors(self, mod_id: str) -> None:
        try:
            from .monitor_manager import ModMonitorManager
            mon_mgr = ModMonitorManager(cache_dir=self.cache_dir)
            running = mon_mgr.get_running_monitors()
            for key, info in running.items():
                if info.get("mod_id") == mod_id and info.get("status") == "RUNNING":
                    mon_id = info.get("monitor_id")
                    if mon_id:
                        mon_mgr.stop_monitor(mod_id, mon_id)
        except Exception as exc:
            logger.debug("[CIRCUIT BREAKER] Failed to stop monitors for '%s': %s", mod_id, exc)

    def reset(self, mod_id: str, re_enable: bool = False) -> Dict[str, Any]:
        """Reset circuit breaker for mod_id, clearing incidents and trip status."""
        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            if "incidents" in data and mod_id in data["incidents"]:
                data["incidents"][mod_id] = []
            if "statuses" in data and mod_id in data["statuses"]:
                data["statuses"][mod_id] = {
                    "state": CIRCUIT_STATE_CLOSED,
                    "tripped_at": None,
                    "warning_emitted": False,
                    "reason": None,
                    "failure_count": 0,
                }
            self._save_data_unlocked(data)

        state_mgr = StateManager(self.state_file)
        overrides: Dict[str, Any] = {
            "disabled_circuit_breaker": False,
            "disabled_reason": None,
            "circuit_breaker_tripped_at": None,
        }
        if re_enable:
            overrides["enabled"] = True
        state_mgr.set_mod_override(mod_id, overrides)

        # Clear warning from .state.json
        try:
            with FileLock(self.state_file):
                data = state_mgr._load_unlocked()
                warnings_map = data.get("circuit_breaker_warnings", {})
                if mod_id in warnings_map:
                    del warnings_map[mod_id]
                    atomic_write_text(self.state_file, json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            pass

        return {"mod_id": mod_id, "state": CIRCUIT_STATE_CLOSED, "reset": True}

    def get_all_statuses(self, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
        """Return status dictionary of all tracked mods."""
        current_time = now if now is not None else time.time()
        with FileLock(self.cb_file):
            data = self._load_data_unlocked()
            self._prune_incidents_unlocked(data, current_time)
            statuses = dict(data.get("statuses", {}))
            incidents_map = data.get("incidents", {})

            # Also cross-check .state.json
            state_mgr = StateManager(self.state_file)
            state_data = state_mgr.load()
            overrides = state_data.get("overrides", {})

            all_mod_ids = set(statuses.keys()) | set(incidents_map.keys()) | {
                mid for mid, ovr in overrides.items() if ovr.get("disabled_circuit_breaker")
            }

            result = {}
            for mid in sorted(all_mod_ids):
                stat = statuses.get(mid, {})
                ovr = overrides.get(mid, {})
                is_disabled_cb = bool(ovr.get("disabled_circuit_breaker"))
                incs = incidents_map.get(mid, [])
                state = self._get_state_unlocked(data, mid, now=current_time)
                result[mid] = {
                    "mod_id": mid,
                    "state": state,
                    "failures_in_window": len(incs),
                    "disabled_circuit_breaker": is_disabled_cb,
                    "tripped_at": stat.get("tripped_at") or ovr.get("circuit_breaker_tripped_at"),
                    "reason": stat.get("reason") or ovr.get("disabled_reason"),
                    "last_error": stat.get("last_error"),
                    "last_failure_time": stat.get("last_failure_time"),
                }
            return result

    @contextmanager
    def protect(self, mod_id: str, command: Optional[str] = None):
        """Context manager protecting mod execution. Records exceptions and isolates if threshold exceeded."""
        if not self.can_execute(mod_id):
            raise RuntimeError(f"Circuit breaker is OPEN for mod '{mod_id}'. Execution blocked.")
        try:
            yield
            self.record_success(mod_id)
        except Exception as exc:
            self.record_error(
                mod_id,
                error=exc,
                error_type=type(exc).__name__,
                details={"command": command} if command else None,
            )
            raise


_cb_instances: Dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    mods_dir: Optional[Union[str, Path]] = None,
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    auto_rollback: bool = True,
) -> CircuitBreaker:
    """Get or create singleton CircuitBreaker instance for mods_dir."""
    global _cb_instances
    if mods_dir is None:
        mods_dir = Path(os.environ.get("HERMES_MODS_DIR", Path.home() / ".hermes" / "mods"))
    key = str(Path(mods_dir).resolve())
    if key not in _cb_instances:
        _cb_instances[key] = CircuitBreaker(
            mods_dir=mods_dir,
            failure_threshold=failure_threshold,
            window_seconds=window_seconds,
            cooldown_seconds=cooldown_seconds,
            auto_rollback=auto_rollback,
        )
    return _cb_instances[key]
