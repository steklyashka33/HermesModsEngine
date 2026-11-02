"""Hermes Core Snapshot Manager.

Управление дельта-снапшотами ядра Hermes Agent (/usr/local/lib/hermes-agent)
перед наложением модов, ручное создание, листинг и атомарный откат.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
from .loader import detect_hermes_root
HERMES_ROOT = detect_hermes_root()
DEFAULT_SNAPSHOTS_DIR = HERMES_HOME / "snapshots"

try:
    from .file_ops import FileLock, atomic_write_text
except ImportError:
    class FileLock:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def atomic_write_text(p, content, encoding="utf-8"):
        target = Path(p)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=encoding)


def compute_file_sha256(path: Path) -> str:
    """Вычисление SHA256 хэша файла."""
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SnapshotFileRecord:
    rel_path: str
    source_path: str
    sha256: str
    size: int
    exists_before: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SnapshotFileRecord:
        return cls(
            rel_path=data.get("rel_path", ""),
            source_path=data.get("source_path", ""),
            sha256=data.get("sha256", ""),
            size=int(data.get("size", 0)),
            exists_before=bool(data.get("exists_before", True)),
        )


@dataclass
class SnapshotManifest:
    id: str
    timestamp: float
    created_at: str
    trigger: str  # "pre_apply" | "manual" | "cli" | "watchdog"
    description: str
    hermes_version: str
    applied_mods: List[str] = field(default_factory=list)
    files: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    status: str = "valid"  # "valid" | "restored" | "corrupted"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "timestamp": self.timestamp,
            "created_at": self.created_at,
            "trigger": self.trigger,
            "description": self.description,
            "hermes_version": self.hermes_version,
            "applied_mods": list(self.applied_mods),
            "files": dict(self.files),
            "status": self.status,
            "file_count": len(self.files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SnapshotManifest:
        return cls(
            id=data.get("id", ""),
            timestamp=float(data.get("timestamp", 0.0)),
            created_at=data.get("created_at", ""),
            trigger=data.get("trigger", "manual"),
            description=data.get("description", ""),
            hermes_version=data.get("hermes_version", "unknown"),
            applied_mods=list(data.get("applied_mods", [])),
            files=dict(data.get("files", {})),
            status=data.get("status", "valid"),
        )


@dataclass
class RestoreResult:
    success: bool
    snapshot_id: str
    restored_files: List[str] = field(default_factory=list)
    deleted_files: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "snapshot_id": self.snapshot_id,
            "restored_files": list(self.restored_files),
            "deleted_files": list(self.deleted_files),
            "restored_count": len(self.restored_files),
            "deleted_count": len(self.deleted_files),
            "errors": list(self.errors),
        }


class SnapshotManager:
    """Менеджер снимков состояния ядра Hermes Agent.

    Обеспечивает сохранение точных дельта-снапшотов файлов ядра перед наложением модов,
    атомарный откат к последнему валидному состоянию и верификацию целостности.
    """

    def __init__(
        self,
        snapshots_dir: Optional[Union[Path, str]] = None,
        repo_root: Optional[Union[Path, str]] = None,
    ):
        self.snapshots_dir = Path(snapshots_dir) if snapshots_dir else Path(
            os.environ.get("HERMES_SNAPSHOTS_DIR", DEFAULT_SNAPSHOTS_DIR)
        )
        self.repo_root = Path(repo_root) if repo_root else Path(
            os.environ.get("HERMES_ROOT", HERMES_ROOT)
        )
        self._lock_path = self.snapshots_dir / ".snapshots.lock"

    def _ensure_dir(self) -> None:
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def _get_hermes_version(self) -> str:
        try:
            from .updater import get_hermes_version
            return get_hermes_version(self.repo_root)
        except Exception:
            return "unknown"

    def generate_snapshot_id(self, trigger: str = "snap") -> str:
        ts = time.strftime("%Y%m%d_%H%M%S")
        rand_suffix = os.urandom(3).hex()
        clean_trigger = trigger.replace("-", "_").lower()
        return f"{clean_trigger}_{ts}_{rand_suffix}"

    def create_snapshot(
        self,
        files: Optional[Sequence[Union[Path, str]]] = None,
        description: str = "Manual snapshot",
        trigger: str = "manual",
        mod_ids: Optional[List[str]] = None,
        snapshot_id: Optional[str] = None,
        mods_dir: Optional[Path] = None,
    ) -> SnapshotManifest:
        """Создает дельта-снапшот указанных (или всех затрагиваемых модами) файлов ядра."""
        self._ensure_dir()

        with FileLock(self._lock_path):
            now_ts = time.time()
            iso_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts))
            snap_id = snapshot_id or self.generate_snapshot_id(trigger)
            target_snap_dir = self.snapshots_dir / snap_id
            files_dir = target_snap_dir / "files"
            target_snap_dir.mkdir(parents=True, exist_ok=True)

            # Определение списка файлов для сохранения
            target_paths: List[Path] = []
            if files is not None:
                for f in files:
                    p = Path(f)
                    if not p.is_absolute():
                        p = (self.repo_root / p).resolve()
                    else:
                        p = p.resolve()
                    target_paths.append(p)
            elif mods_dir:
                # Если список не передан, собираем все targets из модов
                try:
                    from .loader import ModLoader
                    loaded_mods = ModLoader.load_all(mods_dir)
                    for m in loaded_mods:
                        if m.enabled and m.targets:
                            for t in m.targets:
                                if not t.disabled:
                                    target_paths.append((self.repo_root / t.file).resolve())
                except Exception:
                    pass

            # Дедупликация путей с сохранением порядка
            seen = set()
            unique_paths = []
            for p in target_paths:
                if p not in seen:
                    seen.add(p)
                    unique_paths.append(p)

            file_records: Dict[str, Dict[str, Any]] = {}

            for p in unique_paths:
                try:
                    rel_path = str(p.relative_to(self.repo_root))
                except ValueError:
                    rel_path = p.name

                if p.is_file():
                    sha = compute_file_sha256(p)
                    sz = p.stat().st_size
                    dest = files_dir / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                    rec = SnapshotFileRecord(
                        rel_path=rel_path,
                        source_path=str(p),
                        sha256=sha,
                        size=sz,
                        exists_before=True,
                    )
                else:
                    # Файл ядра еще не существовал (будет создан модом)
                    rec = SnapshotFileRecord(
                        rel_path=rel_path,
                        source_path=str(p),
                        sha256="",
                        size=0,
                        exists_before=False,
                    )
                file_records[rel_path] = rec.to_dict()

            hermes_ver = self._get_hermes_version()
            manifest = SnapshotManifest(
                id=snap_id,
                timestamp=now_ts,
                created_at=iso_time,
                trigger=trigger,
                description=description,
                hermes_version=hermes_ver,
                applied_mods=list(mod_ids or []),
                files=file_records,
                status="valid",
            )

            # Атомарная запись манифеста
            manifest_file = target_snap_dir / "manifest.json"
            atomic_write_text(
                manifest_file,
                json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
            )

            # Обновление указателя latest
            latest_file = self.snapshots_dir / "latest.json"
            atomic_write_text(
                latest_file,
                json.dumps({
                    "latest_id": snap_id,
                    "timestamp": now_ts,
                    "created_at": iso_time,
                    "trigger": trigger,
                }, indent=2),
            )

            # Автоматическая очистка старых снапшотов (храним последние 20)
            self._prune_unlocked(keep=20)

            return manifest

    def list_snapshots(self) -> List[Dict[str, Any]]:
        """Возвращает список всех существующих снапшотов от новых к старым."""
        if not self.snapshots_dir.exists():
            return []

        results: List[Dict[str, Any]] = []
        try:
            for entry in self.snapshots_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith("."):
                    manifest_path = entry / "manifest.json"
                    if manifest_path.is_file():
                        try:
                            data = json.loads(manifest_path.read_text(encoding="utf-8"))
                            data["snapshot_dir"] = str(entry)
                            results.append(data)
                        except Exception:
                            pass
        except OSError:
            return []

        # Сортировка по времени (самые свежие первыми)
        results.sort(key=lambda x: float(x.get("timestamp", 0.0)), reverse=True)
        return results

    def get_snapshot_manifest(self, snapshot_id: Optional[str] = None) -> Optional[SnapshotManifest]:
        """Получить манифест снапшота по ID или вернуть последний (latest)."""
        target_id = snapshot_id
        if not target_id or target_id in ("latest", "last"):
            latest_file = self.snapshots_dir / "latest.json"
            if latest_file.is_file():
                try:
                    meta = json.loads(latest_file.read_text(encoding="utf-8"))
                    target_id = meta.get("latest_id")
                except Exception:
                    pass

        if not target_id or target_id in ("latest", "last"):
            # Поиск самого нового по списку
            snaps = self.list_snapshots()
            if snaps:
                target_id = snaps[0].get("id")

        if not target_id:
            return None

        manifest_file = self.snapshots_dir / target_id / "manifest.json"
        if not manifest_file.is_file():
            return None

        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            return SnapshotManifest.from_dict(data)
        except Exception:
            return None

    def get_latest_snapshot(self) -> Optional[SnapshotManifest]:
        """Возвращает самый свежий снапшот ядра."""
        return self.get_snapshot_manifest("latest")

    def restore_snapshot(
        self,
        snapshot_id: Optional[str] = None,
        repo_root: Optional[Union[Path, str]] = None,
    ) -> RestoreResult:
        """Атомарный откат файлов ядра к состоянию снапшота."""
        target_root = Path(repo_root) if repo_root else self.repo_root
        self._ensure_dir()

        with FileLock(self._lock_path):
            manifest = self.get_snapshot_manifest(snapshot_id)
            if not manifest:
                target_name = snapshot_id or "latest"
                return RestoreResult(
                    success=False,
                    snapshot_id=str(target_name),
                    errors=[f"Снапшот ядра '{target_name}' не найден в {self.snapshots_dir}"],
                )

            snap_dir = self.snapshots_dir / manifest.id
            files_dir = snap_dir / "files"
            restored: List[str] = []
            deleted: List[str] = []
            errors: List[str] = []

            for rel_path, f_data in manifest.files.items():
                rec = SnapshotFileRecord.from_dict(f_data)
                target_file = target_root / rel_path

                if rec.exists_before:
                    src_backup = files_dir / rel_path
                    if not src_backup.is_file():
                        errors.append(f"Резервная копия файла отсутствует: {src_backup}")
                        continue

                    try:
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src_backup, target_file)

                        # Верификация SHA256 после восстановления
                        if rec.sha256:
                            actual_sha = compute_file_sha256(target_file)
                            if actual_sha != rec.sha256:
                                errors.append(
                                    f"Контрольная сумма восстановленного файла не совпадает: {rel_path} "
                                    f"(ожидалось: {rec.sha256[:8]}, получено: {actual_sha[:8]})"
                                )
                                continue

                        restored.append(rel_path)
                    except Exception as exc:
                        errors.append(f"Ошибка восстановления {rel_path}: {exc}")
                else:
                    # Файл не существовал до снапшота - удаляем его, если он был создан
                    if target_file.exists():
                        try:
                            if target_file.is_file():
                                target_file.unlink()
                                deleted.append(rel_path)
                            elif target_file.is_dir():
                                shutil.rmtree(target_file)
                                deleted.append(rel_path)
                        except Exception as exc:
                            errors.append(f"Ошибка удаления вновь созданного файла {rel_path}: {exc}")

            success = len(errors) == 0

            # Записываем факт отката в манифест
            if success:
                try:
                    manifest.status = "restored"
                    atomic_write_text(
                        snap_dir / "manifest.json",
                        json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False),
                    )
                except Exception:
                    pass

            return RestoreResult(
                success=success,
                snapshot_id=manifest.id,
                restored_files=restored,
                deleted_files=deleted,
                errors=errors,
            )

    def delete_snapshot(self, snapshot_id: str) -> bool:
        """Удаление указанного снапшота с диска."""
        self._ensure_dir()
        with FileLock(self._lock_path):
            target_dir = self.snapshots_dir / snapshot_id
            if not target_dir.is_dir():
                return False
            try:
                shutil.rmtree(target_dir)

                # Если был удален latest, переназначаем указатель
                latest_file = self.snapshots_dir / "latest.json"
                if latest_file.is_file():
                    try:
                        meta = json.loads(latest_file.read_text(encoding="utf-8"))
                        if meta.get("latest_id") == snapshot_id:
                            remaining = self.list_snapshots()
                            if remaining:
                                newest = remaining[0]
                                atomic_write_text(
                                    latest_file,
                                    json.dumps({
                                        "latest_id": newest["id"],
                                        "timestamp": newest["timestamp"],
                                        "created_at": newest["created_at"],
                                        "trigger": newest.get("trigger", "manual"),
                                    }, indent=2),
                                )
                            else:
                                latest_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                return True
            except OSError:
                return False

    def _prune_unlocked(self, keep: int = 20) -> int:
        snaps = self.list_snapshots()
        if len(snaps) <= keep:
            return 0
        to_remove = snaps[keep:]
        removed_count = 0
        for s in to_remove:
            snap_dir = self.snapshots_dir / s["id"]
            if snap_dir.is_dir():
                try:
                    shutil.rmtree(snap_dir)
                    removed_count += 1
                except OSError:
                    pass
        return removed_count

    def prune_snapshots(self, keep: int = 20) -> int:
        """Удаляет старые снапшоты сверх лимита `keep`."""
        self._ensure_dir()
        with FileLock(self._lock_path):
            return self._prune_unlocked(keep=keep)
