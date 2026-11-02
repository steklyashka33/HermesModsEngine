"""Hermes Mod Engine Exporter & Importer.

Provides clean, safe export, packaging, distribution, and importing of mods
without leaking local caches, databases, logs, git artifacts, or secrets.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from .models import Mod
from .validator import ModValidator, ModValidationResult, ValidationIssue
from .version import ENGINE_VERSION, MIN_SUPPORTED_HERMES_VERSION

# Exact directories to exclude entirely
EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".github",
    ".pytest_cache",
    ".pytest-cache",
    ".ruff_cache",
    ".archive",
    "venv",
    ".venv",
    "node_modules",
}

# Exact filenames to exclude entirely (all lowercase)
EXCLUDED_FILE_NAMES = {
    ".test_cache.json",
    "test_cache.json",
    ".test_cache.json.lock",
    "..test_cache.json.lock",
    ".commands_cache.json",
    "..commands_cache.json.lock",
    ".state.json",
    "secrets.yaml",
    "secrets.yml",
    "secrets.json",
    ".secrets.yaml",
    ".secrets.yml",
    ".secrets.json",
    ".secrets",
    ".ds_store",
    "thumbs.db",
    ".gitkeep",
    ".gitignore",
    ".gitattributes",
}

# File extensions to exclude entirely
EXCLUDED_EXTENSIONS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".lock",
    ".log",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".tmp",
    ".bak",
    ".swp",
    ".swo",
    ".orig",
}

# Regex patterns for files/names to exclude
EXCLUDED_PATTERNS = [
    re.compile(r"^.*secrets.*\.ya?ml$", re.IGNORECASE),
    re.compile(r"^.*secrets.*\.json$", re.IGNORECASE),
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),
    re.compile(r"^.*\.env$", re.IGNORECASE),
    re.compile(r"^.*\.(pem|key|cert|crt|p12|pfx)$", re.IGNORECASE),
    re.compile(r"^(id_rsa|id_ed25519|id_ecdsa)(\..+)?$", re.IGNORECASE),
    re.compile(r"^.*\.(log\.\d+|log)$", re.IGNORECASE),
]

# Sensitive content patterns for deep inspection (used with --no-secrets or audit)
SENSITIVE_CONTENT_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA|DSA|EC|OPENSSH|PGP)?\s*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"(?:sk-[a-zA-Z0-9]{20,})"),
    re.compile(r"(?:ghp_[a-zA-Z0-9]{20,})"),
]

SYSTEM_MOD_IDS = {
    "mod-command-registry",
    "mod-declarative-mcp",
    "mod-folder-skills",
    "mod-update-integration",
}


def is_system_mod(mod: Mod, mods_dir: Optional[Path] = None) -> bool:
    """Check if mod belongs to the system/ directory or is authored by Engine (Tier 0)."""
    if getattr(mod, "id", None) in SYSTEM_MOD_IDS:
        return True
    if (getattr(mod, "author", "") or "").strip().lower() == "engine":
        return True
    if mods_dir is not None and getattr(mod, "path", None):
        try:
            system_dir = (Path(mods_dir) / "system").resolve()
            if Path(mod.path).resolve().is_relative_to(system_dir):
                return True
        except (OSError, ValueError):
            pass
    if getattr(mod, "path", None):
        norm_path = str(mod.path).replace("\\", "/")
        if "/system/" in norm_path or norm_path.startswith("system/"):
            return True
    return False


def should_exclude_path(rel_path: Union[Path, str]) -> Tuple[bool, str]:
    """Check if a relative path should be excluded from export/archive.

    Returns (is_excluded, reason_string).
    """
    p = Path(rel_path)

    # 1. Directory names in path
    for part in p.parts[:-1]:
        part_lower = part.lower()
        if part_lower in EXCLUDED_DIR_NAMES or part_lower.startswith((".git", "__pycache")):
            return True, f"excluded directory '{part}'"

    # Also check if target is a directory itself
    if p.name.lower() in EXCLUDED_DIR_NAMES or p.name.lower().startswith((".git", "__pycache")):
        return True, f"excluded directory '{p.name}'"

    filename = p.name
    filename_lower = filename.lower()

    # 2. Exact filename matches
    if filename_lower in EXCLUDED_FILE_NAMES:
        return True, f"excluded file '{filename}'"

    # 3. File extensions
    if p.suffix.lower() in EXCLUDED_EXTENSIONS:
        return True, f"excluded extension '{p.suffix}'"

    # 4. Regex patterns
    for pat in EXCLUDED_PATTERNS:
        if pat.match(filename):
            return True, f"matched pattern '{pat.pattern}'"

    return False, ""


def check_file_for_secrets(file_path: Path) -> List[str]:
    """Inspect file content for sensitive private keys or tokens."""
    issues = []
    try:
        if not file_path.is_file() or file_path.stat().st_size > 1024 * 1024:
            return []
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []

        for pat in SENSITIVE_CONTENT_PATTERNS:
            matches = pat.findall(content)
            if matches:
                issues.append(f"Content matches sensitive pattern {pat.pattern} ({len(matches)} matches)")
    except Exception:
        pass
    return issues


@dataclass
class ExportResult:
    """Result of exporting mods to an archive."""

    success: bool
    archive_path: Optional[Path] = None
    archive_size: int = 0
    export_type: str = "single"  # single | bundle
    mods_count: int = 0
    mods: List[Dict[str, Any]] = field(default_factory=list)
    files_included: List[str] = field(default_factory=list)
    files_excluded: List[Dict[str, str]] = field(default_factory=list)
    validation_passed: bool = True
    validation_errors: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def files_excluded_count(self) -> int:
        return len(self.files_excluded)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "archive_path": str(self.archive_path) if self.archive_path else None,
            "archive_size": self.archive_size,
            "export_type": self.export_type,
            "mods_count": self.mods_count,
            "mods": self.mods,
            "files_included": self.files_included,
            "files_excluded_count": len(self.files_excluded),
            "files_excluded": self.files_excluded,
            "validation_passed": self.validation_passed,
            "validation_errors": self.validation_errors,
            "error": self.error,
        }


@dataclass
class ImportResult:
    """Result of importing mods from an archive."""

    success: bool
    archive_path: Path
    extracted_files: List[str] = field(default_factory=list)
    skipped_files: List[Dict[str, str]] = field(default_factory=list)
    imported_mods: List[str] = field(default_factory=list)
    validation: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "archive_path": str(self.archive_path),
            "extracted_count": len(self.extracted_files),
            "extracted_files": self.extracted_files,
            "skipped_count": len(self.skipped_files),
            "skipped_files": self.skipped_files,
            "imported_mods": self.imported_mods,
            "validation": self.validation,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class ModExporter:
    """Exports and packages Hermes mods into clean, portable tarballs."""

    def __init__(
        self,
        mods_dir: Union[str, Path],
        repo_root: Optional[Union[str, Path]] = None,
        validator: Optional[ModValidator] = None,
    ):
        self.mods_dir = Path(mods_dir)
        try:
            from .loader import detect_hermes_root
            detected = detect_hermes_root()
        except Exception:
            detected = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
        self.repo_root = Path(repo_root) if repo_root else detected
        self.validator = validator or ModValidator(repo_root=self.repo_root, mods_dir=self.mods_dir)

    def export(
        self,
        mods: List[Mod],
        out_path: Optional[Union[str, Path]] = None,
        target_mod_id: Optional[str] = None,
        include_custom: bool = False,
        no_secrets: bool = False,
        validate_manifests: bool = True,
    ) -> ExportResult:
        """Export single mod or collection of mods into a distribution archive."""
        # 1. Determine which mods to export
        if target_mod_id:
            t = target_mod_id.strip().lower()
            mod = next((m for m in mods if t in (m.id.lower(), m.name.lower(), m.path.stem.lower())), None)
            if not mod:
                return ExportResult(
                    success=False,
                    export_type="single",
                    error=f"Мод '{target_mod_id}' не найден",
                )
            selected_mods = [mod]
            export_type = "single"
        else:
            if include_custom:
                selected_mods = list(mods)
            else:
                selected_mods = [m for m in mods if is_system_mod(m, self.mods_dir)]
                if not selected_mods and mods:
                    selected_mods = list(mods)
            export_type = "bundle"

        if not selected_mods:
            return ExportResult(
                success=False,
                export_type=export_type,
                error="Нет модов для экспорта",
            )

        # 2. Manifest validation before packaging
        if validate_manifests:
            all_valid = True
            val_errors = []
            for m in selected_mods:
                res = self.validator.validate_mod(m)
                if not res.valid:
                    all_valid = False
                    val_errors.append(res.to_dict())

            if not all_valid:
                return ExportResult(
                    success=False,
                    export_type=export_type,
                    mods_count=len(selected_mods),
                    validation_passed=False,
                    validation_errors=val_errors,
                    error=f"Валидация манифестов не пройдена для {len(val_errors)} мод(ов). Экспорт отменен.",
                )

        # 3. Collect files and filter exclusions
        files_to_pack: List[Tuple[Path, str]] = []
        files_excluded: List[Dict[str, str]] = []
        mods_info: List[Dict[str, Any]] = []

        for mod in selected_mods:
            is_sys = is_system_mod(mod, self.mods_dir)
            category = "system" if is_sys else "custom"

            if mod.is_folder and mod.mod_dir and mod.mod_dir.is_dir():
                source_dir = mod.mod_dir
                try:
                    rel_base = source_dir.resolve().relative_to(self.mods_dir.resolve())
                except (ValueError, OSError):
                    rel_base = Path(category) / mod.id

                mod_files_count = 0
                for item in sorted(source_dir.rglob("*")):
                    if not item.is_file():
                        continue
                    item_rel = rel_base / item.relative_to(source_dir)
                    excluded, reason = should_exclude_path(item_rel)
                    if excluded:
                        files_excluded.append({"path": str(item_rel), "reason": reason})
                        continue

                    if no_secrets:
                        sec_findings = check_file_for_secrets(item)
                        if sec_findings:
                            return ExportResult(
                                success=False,
                                export_type=export_type,
                                error=f"Флаг --no-secrets обнаружил подозрительные секреты в {item_rel}: {'; '.join(sec_findings)}",
                            )

                    files_to_pack.append((item, str(item_rel).replace("\\", "/")))
                    mod_files_count += 1

                mods_info.append({
                    "id": mod.id,
                    "name": mod.name,
                    "version": mod.version,
                    "category": category,
                    "is_folder": True,
                    "entry_path": str(rel_base).replace("\\", "/"),
                    "files_count": mod_files_count,
                    "dependencies": mod.dependencies,
                })

            else:
                # Single-file mod
                source_file = mod.path
                try:
                    item_rel = source_file.resolve().relative_to(self.mods_dir.resolve())
                except (ValueError, OSError):
                    item_rel = Path(category) / source_file.name

                excluded, reason = should_exclude_path(item_rel)
                if excluded:
                    files_excluded.append({"path": str(item_rel), "reason": reason})
                    continue

                if no_secrets:
                    sec_findings = check_file_for_secrets(source_file)
                    if sec_findings:
                        return ExportResult(
                            success=False,
                            export_type=export_type,
                            error=f"Флаг --no-secrets обнаружил подозрительные секреты в {item_rel}: {'; '.join(sec_findings)}",
                        )

                files_to_pack.append((source_file, str(item_rel).replace("\\", "/")))
                mods_info.append({
                    "id": mod.id,
                    "name": mod.name,
                    "version": mod.version,
                    "category": category,
                    "is_folder": False,
                    "entry_path": str(item_rel).replace("\\", "/"),
                    "files_count": 1,
                    "dependencies": mod.dependencies,
                })

        # 4. Resolve output archive path
        if out_path:
            target_archive = Path(out_path)
        else:
            if export_type == "single":
                target_archive = Path(f"{selected_mods[0].id}.tar.gz")
            else:
                target_archive = Path("hermes-mods.tar.gz")

        target_archive.parent.mkdir(parents=True, exist_ok=True)

        # 5. Pack archive
        out_name = target_archive.name.lower()
        if out_name.endswith((".tar.bz2", ".tbz2")):
            tar_mode = "w:bz2"
        elif out_name.endswith((".tar.xz", ".txz")):
            tar_mode = "w:xz"
        elif out_name.endswith(".tar"):
            tar_mode = "w"
        else:
            tar_mode = "w:gz"

        metadata = {
            "format_version": "1.0",
            "generator": "hermes-mod-runner",
            "engine_version": ENGINE_VERSION,
            "min_hermes_version": MIN_SUPPORTED_HERMES_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "export_type": export_type,
            "mods_count": len(mods_info),
            "mods": mods_info,
            "files": [p[1] for p in files_to_pack],
        }

        with tarfile.open(target_archive, tar_mode) as tar:
            # 5a. Metadata manifest at archive root
            meta_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
            ti = tarfile.TarInfo(name="mod_export.json")
            ti.size = len(meta_bytes)
            ti.mtime = int(time.time())
            ti.mode = 0o644
            ti.uid = 0
            ti.gid = 0
            ti.uname = "hermes"
            ti.gname = "hermes"
            tar.addfile(ti, io.BytesIO(meta_bytes))

            # 5b. Normalized archive members
            for src_path, arcname in sorted(files_to_pack, key=lambda x: x[1]):
                clean_name = arcname.lstrip("/").replace("\\", "/")
                ti = tar.gettarinfo(str(src_path), arcname=clean_name)
                ti.uid = 0
                ti.gid = 0
                ti.uname = "hermes"
                ti.gname = "hermes"
                if src_path.stat().st_mode & 0o111:
                    ti.mode = 0o755
                else:
                    ti.mode = 0o644

                with open(src_path, "rb") as f:
                    tar.addfile(ti, f)

        archive_size = target_archive.stat().st_size

        return ExportResult(
            success=True,
            archive_path=target_archive,
            archive_size=archive_size,
            export_type=export_type,
            mods_count=len(mods_info),
            mods=mods_info,
            files_included=[p[1] for p in files_to_pack],
            files_excluded=files_excluded,
            validation_passed=True,
        )


class ModImporter:
    """Safely extracts and validates exported mod archives into mods directory."""

    def __init__(
        self,
        mods_dir: Union[str, Path],
        repo_root: Optional[Union[str, Path]] = None,
        validator: Optional[ModValidator] = None,
    ):
        self.mods_dir = Path(mods_dir)
        try:
            from .loader import detect_hermes_root
            detected = detect_hermes_root()
        except Exception:
            detected = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
        self.repo_root = Path(repo_root) if repo_root else detected
        self.validator = validator or ModValidator(repo_root=self.repo_root, mods_dir=self.mods_dir)

    def import_archive(
        self,
        archive_path: Union[str, Path],
        force: bool = False,
    ) -> ImportResult:
        """Unpack archive and validate all imported mods."""
        arc_path = Path(archive_path)
        if not arc_path.is_file():
            return ImportResult(
                success=False,
                archive_path=arc_path,
                errors=[f"Файл архива не найден: {arc_path}"],
            )

        try:
            tar = tarfile.open(arc_path, "r:*")
        except Exception as exc:
            return ImportResult(
                success=False,
                archive_path=arc_path,
                errors=[f"Не удалось открыть архив '{arc_path}': {exc}"],
            )

        with tar:
            # 1. Security audit: TarBomb & Path Traversal protection
            for member in tar.getmembers():
                clean_name = member.name.replace("\\", "/")
                if clean_name.startswith("/") or ".." in Path(clean_name).parts:
                    return ImportResult(
                        success=False,
                        archive_path=arc_path,
                        errors=[f"Небезопасный путь в архиве (TarBomb protection): {member.name}"],
                    )

            # 2. Extract metadata if available
            meta = None
            try:
                meta_file = tar.extractfile("mod_export.json")
                if meta_file:
                    meta = json.loads(meta_file.read().decode("utf-8"))
            except Exception:
                pass

            # 3. Safe extraction
            extracted_files: List[str] = []
            skipped_files: List[Dict[str, str]] = []

            for member in tar.getmembers():
                norm_name = member.name.lstrip("/").replace("\\", "/")
                if not norm_name or norm_name == "mod_export.json":
                    continue

                excluded, reason = should_exclude_path(norm_name)
                if excluded:
                    skipped_files.append({"path": norm_name, "reason": reason})
                    continue

                parts = Path(norm_name).parts
                if parts[0] in ("custom", "system"):
                    dest = self.mods_dir / Path(*parts)
                elif len(parts) == 1 and parts[0].endswith((".yaml", ".yml")):
                    dest = self.mods_dir / "custom" / parts[0]
                else:
                    dest = self.mods_dir / "custom" / Path(*parts)

                if member.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue

                dest.parent.mkdir(parents=True, exist_ok=True)
                f_in = tar.extractfile(member)
                if f_in:
                    with open(dest, "wb") as f_out:
                        shutil.copyfileobj(f_in, f_out)
                    if member.mode & 0o111:
                        os.chmod(dest, 0o755)
                    else:
                        os.chmod(dest, 0o644)
                    try:
                        extracted_files.append(str(dest.relative_to(self.mods_dir)))
                    except ValueError:
                        extracted_files.append(str(dest))

        # 4. Post-import validation
        from .loader import ModLoader

        current_mods = ModLoader.load_all(self.mods_dir)

        imported_ids: Set[str] = set()
        if meta and isinstance(meta.get("mods"), list):
            for m_info in meta["mods"]:
                if isinstance(m_info, dict) and "id" in m_info:
                    imported_ids.add(m_info["id"])
        else:
            for ef in extracted_files:
                parts = Path(ef).parts
                if len(parts) >= 2 and parts[0] in ("custom", "system"):
                    if parts[1].endswith((".yaml", ".yml")):
                        imported_ids.add(Path(parts[1]).stem)
                    else:
                        imported_ids.add(parts[1])

        imported_mods_list = [m for m in current_mods if m.id in imported_ids]
        val_issues = []
        val_passed = True

        for im in imported_mods_list:
            res = self.validator.validate_mod(im)
            if not res.valid:
                val_passed = False
                val_issues.append(res.to_dict())

        try:
            from .loader import update_commands_cache

            update_commands_cache(current_mods, self.mods_dir)
        except Exception:
            pass

        return ImportResult(
            success=val_passed or force,
            archive_path=arc_path,
            extracted_files=extracted_files,
            skipped_files=skipped_files,
            imported_mods=sorted(list(imported_ids)),
            validation={
                "valid": val_passed,
                "total_checked": len(imported_mods_list),
                "failed_count": len(val_issues),
                "issues": val_issues,
            },
        )
