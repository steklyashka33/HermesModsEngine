"""Hermes Gateway Watchdog & Core Snapshot Rollback.

Контроль успешного запуска шлюза Hermes Agent (hermes-gateway.service)
и автоматический откат к последнему валидному снапшоту ядра при сбое
в течение первых 30 секунд.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from .snapshot_manager import SnapshotManager, RestoreResult

DEFAULT_SERVICE = "hermes-gateway"
DEFAULT_WINDOW_SECONDS = 30.0
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_START_TIMEOUT = 15.0


@dataclass
class ServiceHealthStatus:
    is_active: bool
    is_failed: bool
    pid: int
    restarts: int
    active_state: str
    sub_state: str
    raw_properties: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WatchdogResult:
    success: bool
    service_name: str
    stable_seconds: float
    rolled_back: bool = False
    snapshot_id: Optional[str] = None
    restored_files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    exit_code: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "service_name": self.service_name,
            "stable_seconds": round(self.stable_seconds, 2),
            "rolled_back": self.rolled_back,
            "snapshot_id": self.snapshot_id,
            "restored_files": list(self.restored_files),
            "error": self.error,
            "exit_code": self.exit_code,
        }


class GatewayWatchdog:
    """Сторожевой таймер для верификации запуска шлюза и автоотката к снапшоту ядра."""

    def __init__(
        self,
        service_name: str = DEFAULT_SERVICE,
        snapshot_mgr: Optional[SnapshotManager] = None,
        systemctl_bin: str = "systemctl",
    ):
        self.service_name = service_name
        self.snapshot_mgr = snapshot_mgr or SnapshotManager()
        self.systemctl_bin = systemctl_bin

    def check_service_health(self) -> ServiceHealthStatus:
        """Проверка статуса сервиса через systemd или fallback к процессам."""
        # 1. Попытка проверки через systemctl show
        has_systemctl = shutil.which(self.systemctl_bin) is not None
        if has_systemctl:
            try:
                res = subprocess.run(
                    [
                        self.systemctl_bin,
                        "show",
                        self.service_name,
                        "--property=ActiveState,SubState,NRestarts,ExecMainPID,Result",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if res.returncode == 0 and res.stdout:
                    props: Dict[str, str] = {}
                    for line in res.stdout.splitlines():
                        if "=" in line:
                            k, v = line.split("=", 1)
                            props[k.strip()] = v.strip()

                    active_state = props.get("ActiveState", "unknown")
                    sub_state = props.get("SubState", "unknown")
                    pid_val = int(props.get("ExecMainPID", "0")) if props.get("ExecMainPID", "").isdigit() else 0
                    restarts_val = int(props.get("NRestarts", "0")) if props.get("NRestarts", "").isdigit() else 0

                    is_active = (active_state == "active" and sub_state in ("running", "active"))
                    is_failed = (active_state == "failed" or sub_state == "failed")

                    return ServiceHealthStatus(
                        is_active=is_active,
                        is_failed=is_failed,
                        pid=pid_val,
                        restarts=restarts_val,
                        active_state=active_state,
                        sub_state=sub_state,
                        raw_properties=props,
                    )
            except Exception:
                pass

        # 2. Fallback: поиск процесса шлюза в ps (если systemctl недоступен или мок-окружение)
        try:
            res = subprocess.run(
                ["pgrep", "-f", "hermes.*gateway"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                first_pid = int(res.stdout.strip().split()[0])
                return ServiceHealthStatus(
                    is_active=True,
                    is_failed=False,
                    pid=first_pid,
                    restarts=0,
                    active_state="active",
                    sub_state="running",
                    raw_properties={"ExecMainPID": str(first_pid)},
                )
        except Exception:
            pass

        return ServiceHealthStatus(
            is_active=False,
            is_failed=False,
            pid=0,
            restarts=0,
            active_state="inactive",
            sub_state="dead",
            raw_properties={},
        )

    def restart_service(self) -> bool:
        """Мягкая проверка статуса сервиса без вызова systemctl restart."""
        # Перезапуск сервисов через systemctl restart запрещен архитектурным регламентом.
        try:
            status = self.check_service_health()
            return status.is_active
        except Exception:
            return False

    def monitor_and_rollback(
        self,
        snapshot_id: Optional[str] = None,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        start_timeout: float = DEFAULT_START_TIMEOUT,
        auto_restore: bool = True,
        restart_service_on_rollback: bool = True,
        check_fn: Optional[Callable[[], bool]] = None,
    ) -> WatchdogResult:
        """Наблюдение за шлюзом в течение первых window_seconds секунд.

        Если сервис падает или рестартится, выполняется автоматический откат к snapshot_id.
        """
        start_time = time.time()
        initial_pid: Optional[int] = None
        initial_restarts: Optional[int] = None
        service_became_active = False

        # Фаза 1: Ожидание перехода в active (start_timeout)
        wait_deadline = start_time + start_timeout
        while time.time() < wait_deadline:
            status = self.check_service_health()
            if check_fn:
                try:
                    custom_ok = check_fn()
                    if not custom_ok:
                        status.is_active = False
                        status.is_failed = True
                except Exception:
                    status.is_active = False
                    status.is_failed = True

            if status.is_failed:
                break

            if status.is_active:
                service_became_active = True
                initial_pid = status.pid
                initial_restarts = status.restarts
                break

            time.sleep(min(poll_interval, 0.5))

        if not service_became_active:
            err_msg = (
                f"Шлюз '{self.service_name}' не смог подняться за {start_timeout}с "
                f"(ActiveState: {status.active_state}, SubState: {status.sub_state})"
            )
            return self._trigger_rollback(
                error_msg=err_msg,
                snapshot_id=snapshot_id,
                auto_restore=auto_restore,
                restart_on_rollback=restart_service_on_rollback,
                stable_seconds=0.0,
            )

        # Фаза 2: Контрольное окно стабильности (window_seconds)
        obs_start = time.time()
        obs_end = obs_start + window_seconds

        while time.time() < obs_end:
            time.sleep(poll_interval)
            elapsed_stable = time.time() - obs_start
            status = self.check_service_health()

            # Дополнительный внешний зонд
            if check_fn:
                try:
                    if not check_fn():
                        err_msg = f"Пользовательский healthcheck probe шлюза вернул ошибку через {elapsed_stable:.1f}с"
                        return self._trigger_rollback(
                            error_msg=err_msg,
                            snapshot_id=snapshot_id,
                            auto_restore=auto_restore,
                            restart_on_rollback=restart_service_on_rollback,
                            stable_seconds=elapsed_stable,
                        )
                except Exception as exc:
                    err_msg = f"Исключение при вызове healthcheck probe: {exc}"
                    return self._trigger_rollback(
                        error_msg=err_msg,
                        snapshot_id=snapshot_id,
                        auto_restore=auto_restore,
                        restart_on_rollback=restart_service_on_rollback,
                        stable_seconds=elapsed_stable,
                    )

            # Проверка падения
            if status.is_failed:
                err_msg = (
                    f"Шлюз '{self.service_name}' упал в статус failed через {elapsed_stable:.1f}с "
                    f"(ActiveState: {status.active_state}, SubState: {status.sub_state})"
                )
                return self._trigger_rollback(
                    error_msg=err_msg,
                    snapshot_id=snapshot_id,
                    auto_restore=auto_restore,
                    restart_on_rollback=restart_service_on_rollback,
                    stable_seconds=elapsed_stable,
                )

            # Проверка остановки
            if not status.is_active:
                err_msg = (
                    f"Шлюз '{self.service_name}' перестал быть активным через {elapsed_stable:.1f}с "
                    f"(ActiveState: {status.active_state}, SubState: {status.sub_state})"
                )
                return self._trigger_rollback(
                    error_msg=err_msg,
                    snapshot_id=snapshot_id,
                    auto_restore=auto_restore,
                    restart_on_rollback=restart_service_on_rollback,
                    stable_seconds=elapsed_stable,
                )

            # Проверка краш-лупа / смены PID
            if initial_restarts is not None and status.restarts > initial_restarts:
                err_msg = (
                    f"Обнаружен циклический перезапуск шлюза (рестартов: {status.restarts} > {initial_restarts}) "
                    f"через {elapsed_stable:.1f}с"
                )
                return self._trigger_rollback(
                    error_msg=err_msg,
                    snapshot_id=snapshot_id,
                    auto_restore=auto_restore,
                    restart_on_rollback=restart_service_on_rollback,
                    stable_seconds=elapsed_stable,
                )

            if initial_pid and status.pid and status.pid != initial_pid:
                err_msg = (
                    f"PID шлюза изменился с {initial_pid} на {status.pid} через {elapsed_stable:.1f}с "
                    "(краш процесса и перезапуск супервизором)"
                )
                return self._trigger_rollback(
                    error_msg=err_msg,
                    snapshot_id=snapshot_id,
                    auto_restore=auto_restore,
                    restart_on_rollback=restart_service_on_rollback,
                    stable_seconds=elapsed_stable,
                )

        # Успешное завершение: шлюз отработал стабильно всё окно
        stable_total = time.time() - obs_start
        return WatchdogResult(
            success=True,
            service_name=self.service_name,
            stable_seconds=stable_total,
            rolled_back=False,
            snapshot_id=snapshot_id,
            restored_files=[],
            error=None,
            exit_code=0,
        )

    def _trigger_rollback(
        self,
        error_msg: str,
        snapshot_id: Optional[str],
        auto_restore: bool,
        restart_on_rollback: bool,
        stable_seconds: float,
    ) -> WatchdogResult:
        restored_files: List[str] = []
        target_snap_id = snapshot_id

        if auto_restore:
            restore_res: RestoreResult = self.snapshot_mgr.restore_snapshot(target_snap_id)
            target_snap_id = restore_res.snapshot_id
            restored_files = restore_res.restored_files

            if restore_res.success:
                full_err = (
                    f"{error_msg}. Сработал автоматический watchdog-откат к снапшоту '{target_snap_id}' "
                    f"(восстановлено файлов: {len(restored_files)})."
                )
                if restart_on_rollback:
                    self.restart_service()
            else:
                full_err = (
                    f"{error_msg}. Ошибка при попытке автоматического отката к снапшоту: "
                    f"{', '.join(restore_res.errors)}"
                )
        else:
            full_err = f"{error_msg}. Автоматический откат отключен (auto_restore=False)."

        return WatchdogResult(
            success=False,
            service_name=self.service_name,
            stable_seconds=stable_seconds,
            rolled_back=auto_restore,
            snapshot_id=target_snap_id,
            restored_files=restored_files,
            error=full_err,
            exit_code=1,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Hermes Gateway Watchdog & Core Snapshot Rollback",
    )
    parser.add_argument(
        "--service",
        default=DEFAULT_SERVICE,
        help=f"Systemd service name to monitor (default: {DEFAULT_SERVICE})",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=DEFAULT_WINDOW_SECONDS,
        help=f"Watchdog observation window in seconds (default: {DEFAULT_WINDOW_SECONDS}s)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"Polling interval in seconds (default: {DEFAULT_POLL_INTERVAL}s)",
    )
    parser.add_argument(
        "--start-timeout",
        type=float,
        default=DEFAULT_START_TIMEOUT,
        help=f"Startup grace period waiting for active status (default: {DEFAULT_START_TIMEOUT}s)",
    )
    parser.add_argument(
        "--snapshot-id",
        default=None,
        help="Target snapshot ID for rollback (default: latest)",
    )
    parser.add_argument(
        "--no-rollback",
        action="store_true",
        help="Do not trigger automatic snapshot rollback on failure",
    )
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart service after successful snapshot rollback",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON result",
    )
    args = parser.parse_args()

    watchdog = GatewayWatchdog(service_name=args.service)
    result = watchdog.monitor_and_rollback(
        snapshot_id=args.snapshot_id,
        window_seconds=args.window,
        poll_interval=args.poll,
        start_timeout=args.start_timeout,
        auto_restore=not args.no_rollback,
        restart_service_on_rollback=not args.no_restart,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    else:
        if result.success:
            print(f"✓ Шлюз '{result.service_name}' успешно запущен и стабилен ({result.stable_seconds:.1f}s)")
        else:
            print(f"✗ Ошибка мониторинга шлюза: {result.error}", file=sys.stderr)
            if result.rolled_back and result.restored_files:
                print(f"  Восстановлено файлов ядра: {len(result.restored_files)}", file=sys.stderr)
                for rf in result.restored_files:
                    print(f"    • {rf}", file=sys.stderr)

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
