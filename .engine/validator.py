"""Static AST and Manifest Validator for Hermes Mod Engine.

Provides deep pre-flight and on-demand validation:
- Static AST parsing via ast.parse() for Python target files (catching IndentationError,
  SyntaxError, unclosed parentheses, brackets, quotes, and invalid syntax).
- config schema and configured value type checks (bool, int, float, str, list).
- Target files existence checks relative to the Hermes repository root.
- Mod ID uniqueness and YAML syntax validation.
- Monitor specification validation.
- Clean colored CLI reporting with line numbers, code snippets, carets, and hints.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import yaml

from .models import Mod, IDENTIFIER_RE
from .config_manager import ModConfigManager, CONFIG_FILE, SECRETS_FILE

COMMAND_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

from .loader import detect_hermes_root
HERMES_ROOT = detect_hermes_root()
DEFAULT_MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(Path(__file__).resolve().parent.parent)))
MODS_DIR = DEFAULT_MODS_DIR

VALID_CONFIG_TYPES = {
    "bool", "boolean",
    "int", "integer",
    "float", "number",
    "str", "string", "text",
    "file", "dir",
    "list", "array",
}


class CategoryStr(str):
    def __eq__(self, other):
        if str(self) in ("config", "user_config") and other in ("config", "user_config"):
            return True
        return super().__eq__(other)

    def __hash__(self):
        return hash(str(self))


CONFIG_CATEGORY = CategoryStr("config")


@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    category: str  # "ast" | "config" | "target_file" | "yaml" | "mod_id" | "monitor"
    message: str
    mod_id: str
    file: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None
    source_snippet: Optional[str] = None
    hint: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
            "mod_id": self.mod_id,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "source_snippet": self.source_snippet,
            "hint": self.hint,
        }


@dataclass
class ModValidationResult:
    mod_id: str
    name: str
    valid: bool
    issues: List[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mod_id": self.mod_id,
            "name": self.name,
            "valid": self.valid,
            "errors_count": len(self.errors),
            "warnings_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }


@dataclass
class CompositeSimulationReport:
    valid: bool
    issues: List[ValidationIssue] = field(default_factory=list)
    touched_files: Dict[str, str] = field(default_factory=dict)
    file_modifiers: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def errors(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> List[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def __bool__(self) -> bool:
        return self.valid

    def __iter__(self):
        yield self.valid
        yield self.issues

    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors_count": len(self.errors),
            "warnings_count": len(self.warnings),
            "touched_files_count": len(self.touched_files),
            "issues": [i.to_dict() for i in self.issues],
        }


def check_ast_snippet(code: str) -> Tuple[bool, Optional[SyntaxError]]:
    """Try parsing code snippet standalone using multiple heuristic wrappers."""
    dedented = textwrap.dedent(code).strip()
    if not dedented:
        return True, None

    # 1. Direct dedented parse
    try:
        ast.parse(dedented)
        return True, None
    except SyntaxError as e:
        first_err = e

    # 2. Inside an async function body
    try:
        indented = textwrap.indent(dedented, "    ")
        ast.parse(f"async def _probe():\n{indented}")
        return True, None
    except Exception:
        pass

    # 3. Inside an if / try block (elif, else, except, finally)
    try:
        ast.parse(f"if True:\n    pass\n{dedented}")
        return True, None
    except Exception:
        pass

    try:
        ast.parse(f"try:\n    pass\n{dedented}")
        return True, None
    except Exception:
        pass

    # 4. Inside grouping brackets (expressions, tuple elements, dict items)
    for ob, cb in [("(", ")"), ("[", "]"), ("{", "}")]:
        try:
            ast.parse(f"{ob}\n{dedented}\n{cb}")
            return True, None
        except Exception:
            pass

    return False, first_err


def _extract_snippet_with_pointer(source_text: str, lineno: int, col_offset: Optional[int] = None, context_lines: int = 1) -> str:
    """Generate a multi-line source snippet with line numbers and a caret pointer."""
    lines = source_text.splitlines()
    if not lines or lineno < 1 or lineno > len(lines):
        return ""

    start_idx = max(0, lineno - 1 - context_lines)
    end_idx = min(len(lines), lineno + context_lines)
    max_line_num_width = len(str(end_idx))

    out_lines = []
    for idx in range(start_idx, end_idx):
        cur_line_num = idx + 1
        line_str = lines[idx]
        is_err = (cur_line_num == lineno)
        prefix = "> " if is_err else "  "
        num_str = str(cur_line_num).rjust(max_line_num_width)
        out_lines.append(f"{prefix}{num_str} | {line_str}")
        if is_err and col_offset is not None and col_offset >= 0:
            pad = " " * (len(prefix) + max_line_num_width + 3 + max(0, col_offset - 1))
            out_lines.append(f"{pad}^")

    return "\n".join(out_lines)


class ModValidator:
    """Validates Hermes declarative mods for syntax, types, targets, and manifest integrity."""

    def __init__(
        self,
        repo_root: Path = HERMES_ROOT,
        mods_dir: Path = MODS_DIR,
        config_file: Path = CONFIG_FILE,
        secrets_file: Optional[Path] = None,
        strict_folder_layout: Optional[bool] = None,
    ):
        self.repo_root = Path(repo_root)
        self.mods_dir = Path(mods_dir)
        sec_path = Path(secrets_file) if secrets_file is not None else (Path(config_file).parent / "secrets.yaml")
        self.config_mgr = ModConfigManager(config_file, sec_path)
        if strict_folder_layout is None:
            sfl = False
            if Path(config_file).is_file():
                try:
                    import yaml
                    with open(config_file, "r", encoding="utf-8") as f:
                        cdata = yaml.safe_load(f)
                    if isinstance(cdata, dict):
                        mods_section = cdata.get("mods", {})
                        if isinstance(mods_section, dict):
                            sfl = bool(mods_section.get("strict_folder_layout", False))
                except Exception:
                    sfl = False
            self.strict_folder_layout = sfl
        else:
            self.strict_folder_layout = bool(strict_folder_layout)
        self._file_cache: Dict[Path, str] = {}
        self._valid_ast_hashes: Set[int] = set()

    def check_yaml_file(self, path: Path, mod_id: str) -> List[ValidationIssue]:
        """Validate YAML syntax and structure of a file."""
        issues: List[ValidationIssue] = []
        if not path.exists():
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="yaml",
                    message=f"File not found: {path}",
                    mod_id=mod_id,
                    file=str(path),
                )
            )
            return issues

        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="yaml",
                    message=f"Cannot read file {path}: {exc}",
                    mod_id=mod_id,
                    file=str(path),
                )
            )
            return issues

        try:
            data = yaml.safe_load(content)
            if data is None:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="yaml",
                        message=f"YAML file is empty: {path.name}",
                        mod_id=mod_id,
                        file=str(path),
                    )
                )
        except yaml.YAMLError as exc:
            line = None
            col = None
            snippet = None
            if hasattr(exc, "problem_mark") and exc.problem_mark:
                line = exc.problem_mark.line + 1
                col = exc.problem_mark.column + 1
                snippet = _extract_snippet_with_pointer(content, line, col)

            issues.append(
                ValidationIssue(
                    severity="error",
                    category="yaml",
                    message=f"YAML syntax error: {exc}",
                    mod_id=mod_id,
                    file=str(path),
                    line=line,
                    column=col,
                    source_snippet=snippet,
                    hint="Check YAML indentation, colons, brackets, and quote unescaped characters.",
                )
            )

        return issues

    def check_mod_id_uniqueness(self, mods: List[Mod]) -> List[ValidationIssue]:
        """Verify uniqueness and format of mod identifiers across all loaded mods."""
        issues: List[ValidationIssue] = []
        seen: Dict[str, List[Path]] = {}

        # Scan raw files across system/ and custom/ (root of mods_dir is isolated)
        scan_dirs = []
        for sub in ("system", "custom"):
            p = self.mods_dir / sub
            if p.is_dir():
                scan_dirs.append(p)
        if not scan_dirs:
            scan_dirs = [self.mods_dir]

        for s_dir in scan_dirs:
            for entry in sorted(s_dir.iterdir()):
                if entry.name.startswith((".", "_")) or entry.name.endswith(".disabled"):
                    continue
                if s_dir == self.mods_dir and entry.name in ("system", "custom"):
                    continue

                manifest = None
                if entry.is_dir():
                    manifest = entry / "mod.yaml"
                    if not manifest.exists():
                        manifest = entry / f"{entry.name}.yaml"
                elif entry.is_file() and entry.suffix in (".yaml", ".yml"):
                    manifest = entry

                if manifest and manifest.exists():
                    try:
                        with open(manifest, "r", encoding="utf-8") as f:
                            data = yaml.safe_load(f)
                        if isinstance(data, dict):
                            m_id = str(data.get("id") or entry.stem).strip()
                            entry_list = seen.setdefault(m_id, [])
                            if manifest not in entry_list:
                                entry_list.append(manifest)
                    except Exception:
                        pass

        for mod in mods:
            if not mod.id:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mod_id",
                        message="Mod ID is empty.",
                        mod_id=mod.id or "unknown",
                        file=str(mod.path),
                        hint="Define 'id: your-mod-id' in the mod manifest.",
                    )
                )
            elif not IDENTIFIER_RE.match(mod.id):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mod_id",
                        message=f"Mod ID '{mod.id}' contains invalid characters. Must match {IDENTIFIER_RE.pattern}.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Use lowercase alphanumeric characters, dashes, and underscores.",
                    )
                )

        for m_id, paths in seen.items():
            if len(paths) > 1:
                path_strs = ", ".join(str(p) for p in paths)
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mod_id",
                        message=f"Duplicate mod ID '{m_id}' declared in multiple locations: {path_strs}",
                        mod_id=m_id,
                        file=str(paths[0]),
                        hint="Ensure each mod has a globally unique ID.",
                    )
                )

        return issues

    def check_targets_and_simulate(self, mod: Mod) -> Tuple[List[ValidationIssue], Dict[str, str]]:
        """Validate target file existence and simulate content replacements."""
        issues: List[ValidationIssue] = []
        simulated_files: Dict[str, str] = {}

        # Check all targets
        for idx, target in enumerate(mod.targets):
            if not target.file:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="target_file",
                        message=f"Target #{idx} file path is empty.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Specify a relative file path in target.file.",
                    )
                )
                continue

            if not self.repo_root.exists():
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="target_file",
                        message=f"Hermes repository root not found at {self.repo_root}; skipped target existence check for {target.file}",
                        mod_id=mod.id,
                        file=target.file,
                        hint="Verify HERMES_ROOT or mount the Hermes repository.",
                    )
                )
                continue

            target_path = self.repo_root / target.file
            if not target_path.exists():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="target_file",
                        message=f"Target file does not exist: {target.file}",
                        mod_id=mod.id,
                        file=target.file,
                        hint=f"Verify target path relative to Hermes root ({self.repo_root}).",
                    )
                )
                continue

            if not target_path.is_file():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="target_file",
                        message=f"Target path is not a regular file: {target.file}",
                        mod_id=mod.id,
                        file=target.file,
                    )
                )
                continue

            if target.disabled:
                continue

            # Load or retrieve accumulated simulated content
            if target.file not in simulated_files:
                if target_path not in self._file_cache:
                    try:
                        self._file_cache[target_path] = target_path.read_text(encoding="utf-8")
                    except Exception as exc:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                category="target_file",
                                message=f"Failed to read target file {target.file}: {exc}",
                                mod_id=mod.id,
                                file=target.file,
                            )
                        )
                        continue
                simulated_files[target.file] = self._file_cache[target_path]

            current_sim = simulated_files[target.file]

            # Check presence of old_block or new_block
            if target.new_block in current_sim:
                # Already applied or present
                pass
            elif target.old_block in current_sim:
                simulated_files[target.file] = current_sim.replace(target.old_block, target.new_block, 1)
            else:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="target_file",
                        message=f"Target #{idx} old_block not found in {target.file} (upstream may have changed).",
                        mod_id=mod.id,
                        file=target.file,
                        hint="Check if upstream code changed or if mod requires re-basing.",
                    )
                )

        return issues, simulated_files

    def check_ast_syntax(
        self,
        mod: Mod,
        simulated_files: Dict[str, str],
    ) -> List[ValidationIssue]:
        """Validate Python target replacements with ast.parse() to detect syntax and indentation errors."""
        issues: List[ValidationIssue] = []

        # 1. Check each active target individually
        for idx, target in enumerate(mod.targets):
            if target.disabled:
                continue

            if not (target.file.endswith(".py") or target.file.endswith(".pyw")):
                continue

            target_path = self.repo_root / target.file
            if not target_path.exists():
                # Target file doesn't exist; try standalone snippet AST probe
                ok, err = check_ast_snippet(target.new_block)
                if not ok and err:
                    snippet = _extract_snippet_with_pointer(target.new_block, err.lineno or 1, err.offset)
                    hint = self._suggest_syntax_hint(err)
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="ast",
                            message=f"Target #{idx} ({target.file}) new_block AST syntax error: {err.msg}",
                            mod_id=mod.id,
                            file=target.file,
                            line=err.lineno,
                            column=err.offset,
                            source_snippet=snippet,
                            hint=hint,
                        )
                    )
                continue

            # Target file exists: simulate individual replacement
            if target_path in self._file_cache:
                original_content = self._file_cache[target_path]
            else:
                try:
                    original_content = target_path.read_text(encoding="utf-8")
                    self._file_cache[target_path] = original_content
                except Exception:
                    continue

            if target.new_block in original_content:
                probe_content = original_content
                start_line = 1
            elif target.old_block in original_content:
                prefix = original_content[:original_content.find(target.old_block)]
                start_line = prefix.count("\n") + 1
                probe_content = original_content.replace(target.old_block, target.new_block, 1)
            else:
                # Neither found; attempt standalone snippet check
                ok, err = check_ast_snippet(target.new_block)
                if not ok and err:
                    snippet = _extract_snippet_with_pointer(target.new_block, err.lineno or 1, err.offset)
                    hint = self._suggest_syntax_hint(err)
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="ast",
                            message=f"Target #{idx} ({target.file}) new_block AST syntax error: {err.msg}",
                            mod_id=mod.id,
                            file=target.file,
                            line=err.lineno,
                            column=err.offset,
                            source_snippet=snippet,
                            hint=hint,
                        )
                    )
                continue

            content_hash = hash(probe_content)
            if content_hash in self._valid_ast_hashes:
                continue

            try:
                ast.parse(probe_content, filename=target.file)
                self._valid_ast_hashes.add(content_hash)
            except (SyntaxError, IndentationError) as exc:
                err_line = exc.lineno or 1
                err_col = exc.offset
                err_type = type(exc).__name__
                snippet = _extract_snippet_with_pointer(probe_content, err_line, err_col)
                hint = self._suggest_syntax_hint(exc)

                new_block_lines = target.new_block.count("\n") + 1
                end_line = start_line + new_block_lines - 1
                rel_note = ""
                if start_line <= err_line <= end_line:
                    rel_line = err_line - start_line + 1
                    rel_note = f" (line {rel_line} of new_block)"

                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="ast",
                        message=f"Target #{idx} ({target.file}) {err_type}: {exc.msg}{rel_note}",
                        mod_id=mod.id,
                        file=target.file,
                        line=err_line,
                        column=err_col,
                        source_snippet=snippet,
                        hint=hint,
                    )
                )

        # 2. Cumulative multi-target check for files touched by multiple targets
        for file_rel, full_content in simulated_files.items():
            if not (file_rel.endswith(".py") or file_rel.endswith(".pyw")):
                continue
            matching_targets = [t for t in mod.targets if t.file == file_rel and not t.disabled]
            if len(matching_targets) <= 1:
                continue

            cum_hash = hash(full_content)
            if cum_hash in self._valid_ast_hashes:
                continue

            try:
                ast.parse(full_content, filename=file_rel)
                self._valid_ast_hashes.add(cum_hash)
            except (SyntaxError, IndentationError) as exc:
                err_line = exc.lineno or 1
                err_col = exc.offset
                err_type = type(exc).__name__
                snippet = _extract_snippet_with_pointer(full_content, err_line, err_col)
                hint = self._suggest_syntax_hint(exc)
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="ast",
                        message=f"Combined targets for {file_rel} failed AST parse ({err_type}): {exc.msg}",
                        mod_id=mod.id,
                        file=file_rel,
                        line=err_line,
                        column=err_col,
                        source_snippet=snippet,
                        hint=f"Check interactions between {len(matching_targets)} targets touching {file_rel}. {hint}",
                    )
                )

        return issues

    def _suggest_syntax_hint(self, exc: SyntaxError) -> str:
        if isinstance(exc, IndentationError):
            return "IndentationError: Ensure indentation in new_block matches surrounding block depth and does not mix tabs and spaces."
        msg = str(exc.msg).lower()
        if "was never closed" in msg or "unmatched" in msg or "expected '}'" in msg or "expected ')'" in msg:
            return f"SyntaxError: {exc.msg}. Ensure all parentheses, brackets, braces, and quotes in new_block are balanced."
        if "expected an indented block" in msg:
            return "IndentationError: Body of function, if, try, or loop statement must be indented."
        if "invalid syntax" in msg:
            return "SyntaxError: Check colons, variable names, keywords, and expression syntax in new_block."
        return f"SyntaxError: {exc.msg}. Review new_block lines around the reported location."

    def check_config(self, mod: Mod) -> List[ValidationIssue]:
        """Validate config schema declarations and configured runtime values against type."""
        issues: List[ValidationIssue] = []

        # 1. Validate schema declarations
        for key, field_spec in mod.config.items():
            norm_type = str(field_spec.type).strip().lower()
            if norm_type not in VALID_CONFIG_TYPES:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category=CONFIG_CATEGORY,
                        message=f"Field '{key}' has unsupported type '{field_spec.type}'. Allowed: {sorted(VALID_CONFIG_TYPES)}",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Use one of: bool, int, float, str, file, dir, list.",
                    )
                )

            # Validate default value if declared
            if field_spec.default is not None:
                ok, _, err = field_spec.validate_value(field_spec.default)
                if not ok:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category=CONFIG_CATEGORY,
                            message=f"Field '{key}' default value {field_spec.default!r} is invalid: {err}",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint=f"Provide a default value compatible with declared type '{field_spec.type}'.",
                        )
                    )

            # Validate options list
            if field_spec.options is not None:
                if not isinstance(field_spec.options, list):
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category=CONFIG_CATEGORY,
                            message=f"Field '{key}' options must be a list, got {type(field_spec.options).__name__}.",
                            mod_id=mod.id,
                            file=str(mod.path),
                        )
                    )
                else:
                    for opt in field_spec.options:
                        ok, _, err = field_spec.validate_value(opt)
                        if not ok:
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    category=CONFIG_CATEGORY,
                                    message=f"Field '{key}' option {opt!r} does not match type '{field_spec.type}': {err}",
                                    mod_id=mod.id,
                                    file=str(mod.path),
                                )
                            )

            # Validate min / max boundaries
            if field_spec.min is not None or field_spec.max is not None:
                if norm_type not in ("int", "integer", "float", "number"):
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            category=CONFIG_CATEGORY,
                            message=f"Field '{key}' defines min/max but has non-numeric type '{field_spec.type}'.",
                            mod_id=mod.id,
                            file=str(mod.path),
                        )
                    )
                if field_spec.min is not None and field_spec.max is not None and field_spec.min > field_spec.max:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category=CONFIG_CATEGORY,
                            message=f"Field '{key}' has min ({field_spec.min}) greater than max ({field_spec.max}).",
                            mod_id=mod.id,
                            file=str(mod.path),
                        )
                    )

        # 2. Validate saved runtime config values (from config.yaml and secrets.yaml)
        configured_opts = self.config_mgr.get_mod_stored_options(mod.id, getattr(mod, "name", ""))
        configured_secrets = self.config_mgr.secrets.get_mod_secrets(mod.id)

        all_configured: Dict[str, Any] = {**configured_opts, **configured_secrets}
        for key, val in all_configured.items():
            if key not in mod.config:
                continue

            field_spec = mod.config[key]
            ok, coerced, err = field_spec.validate_value(val)
            if not ok:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category=CONFIG_CATEGORY,
                        message=f"Configured value for '{key}' is invalid: {err}",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint=f"Update config.yaml under mods.{mod.id}.{key} or run: runner.py config {mod.id} --set {key}=<valid_value>",
                    )
                )

        # Check required fields without defaults or configuration
        for key, field_spec in mod.config.items():
            if field_spec.required and field_spec.default is None and key not in all_configured:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category=CONFIG_CATEGORY,
                        message=f"Required field '{key}' has no default and is not configured.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint=f"Set with: runner.py config {mod.id} --set {key}=<value>" if not field_spec.sensitive else f"Set secret with: runner.py config {mod.id} --set-secret {key}=<value>",
                    )
                )

        return issues

    def check_user_config(self, mod: Mod) -> List[ValidationIssue]:
        """Backward-compatibility alias for check_config."""
        return self.check_config(mod)

    def check_monitors(self, mod: Mod) -> List[ValidationIssue]:
        """Validate monitor daemon specifications."""
        issues: List[ValidationIssue] = []
        for idx, mon in enumerate(mod.monitors):
            if not mon.command or not str(mon.command).strip():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="monitor",
                        message=f"Monitor #{idx} ('{mon.id}') command is empty.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
            if mon.interval is not None and mon.interval <= 0:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="monitor",
                        message=f"Monitor '{mon.id}' interval must be > 0, got {mon.interval}.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
            if mon.delivery not in ("chat", "agent", "log"):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="monitor",
                        message=f"Monitor '{mon.id}' has unusual delivery '{mon.delivery}'. Expected chat, agent, or log.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
            if mon.when not in ("always", "on-demand"):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="monitor",
                        message=f"Monitor '{mon.id}' has unusual when '{mon.when}'. Expected always or on-demand.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
        return issues

    def check_layout_and_capabilities(self, mod: Mod) -> List[ValidationIssue]:
        """Validate single-file vs folder mod layout constraints and feature capabilities."""
        issues: List[ValidationIssue] = []

        # 1. Folder mod checks
        if mod.is_folder:
            if mod.mod_dir:
                for sec_name in ("secrets.yaml", "secrets.yml", "secrets.json"):
                    if (mod.mod_dir / sec_name).exists():
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                category="security",
                                message=f"Folder mod '{mod.id}' contains forbidden '{sec_name}'. Secrets must be stored in ~/.hermes/secrets.yaml (0600) and must never be distributed inside a mod.",
                                mod_id=mod.id,
                                file=str(mod.mod_dir / sec_name),
                                hint="Remove secrets file from mod folder and configure secrets via runner.py config --set-secret.",
                            )
                        )
            if mod.mod_dir and (mod.mod_dir / "commands").is_dir():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mod_layout",
                        message=f"Folder mod '{mod.id}' contains forbidden 'commands/' directory. Commands must be placed in commands.yaml or inline in mod.yaml.",
                        mod_id=mod.id,
                        file=str(mod.mod_dir / "commands"),
                        hint="Remove commands/ directory and place commands in commands.yaml or inline in mod.yaml.",
                    )
                )
            if self.strict_folder_layout:
                if mod.has_inline_targets:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="mod_layout",
                            message=f"Folder mod '{mod.id}' defines inline targets in {mod.path.name}. With strict_folder_layout enabled, targets must be placed in targets/*.yaml or targets.yaml.",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint="Move targets to a separate targets/ directory or targets.yaml file.",
                        )
                    )
                if mod.has_inline_commands:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="mod_layout",
                            message=f"Folder mod '{mod.id}' defines inline commands in {mod.path.name}. With strict_folder_layout enabled, commands must be placed in commands.yaml.",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint="Move commands to commands.yaml file.",
                        )
                    )
                if mod.has_inline_config:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="mod_layout",
                            message=f"Folder mod '{mod.id}' defines inline config in {mod.path.name}. With strict_folder_layout enabled, config must be placed in config.yaml.",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint="Move config to config.yaml.",
                        )
                    )
                if mod.has_inline_monitors:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="mod_layout",
                            message=f"Folder mod '{mod.id}' defines inline monitors in {mod.path.name}. With strict_folder_layout enabled, monitors must be placed in monitors/*.yaml or monitors.yaml.",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint="Move monitors to monitors/ directory or monitors.yaml.",
                        )
                    )
                standard_sections = {
                    "targets", "commands", "config", "user_config", "config_schema", "monitors",
                    "id", "name", "version", "author", "description", "enabled",
                    "min_hermes_version", "min_engine_version", "verify_cmd", "skills",
                    "i18n", "locales", "dependencies", "depends_on", "tests", "test", "mcp_servers", "mcpServers"
                }
                for sec in sorted(getattr(mod, "inline_sections", set())):
                    if sec not in standard_sections:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                category="mod_layout",
                                message=f"Folder mod '{mod.id}' defines inline section '{sec}' in {mod.path.name}. With strict_folder_layout enabled, {sec} must be placed in {sec}/*.yaml or {sec}.yaml.",
                                mod_id=mod.id,
                                file=str(mod.path),
                                hint=f"Move {sec} to a separate {sec}/ directory or {sec}.yaml file.",
                            )
                        )

            # Validate i18n/ and locales/ directory if present
            for dir_name in ("i18n", "locales"):
                if mod.mod_dir and (mod.mod_dir / dir_name).exists():
                    loc_dir = mod.mod_dir / dir_name
                    if not loc_dir.is_dir():
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                category="i18n",
                                message=f"Folder mod '{mod.id}' has '{dir_name}' path that is not a directory.",
                                mod_id=mod.id,
                                file=str(loc_dir),
                                hint=f"Ensure {dir_name} is a directory containing <lang>[.<surface>].yaml files.",
                            )
                        )
                    else:
                        for lf in sorted(loc_dir.iterdir()):
                            if lf.name.startswith((".", "_")) or lf.name.endswith(".disabled"):
                                continue
                            if not lf.is_file() or lf.suffix not in (".yaml", ".yml", ".json"):
                                issues.append(
                                    ValidationIssue(
                                        severity="warning",
                                        category="i18n",
                                        message=f"Folder mod '{mod.id}' contains unrecognized locale file '{lf.name}' in {dir_name}/.",
                                        mod_id=mod.id,
                                        file=str(lf),
                                        hint="Locale files should be named <lang>.yaml (e.g. ru.yaml, en.yaml, ru.tui.yaml).",
                                    )
                                )

        # 2. Single-file mod checks
        else:
            if mod.has_skills or mod.skills_dir:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mod_capability",
                        message=f"Single-file mod '{mod.id}' cannot provide skills. Local assets (skills/, scripts/) require a folder mod layout.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint=f"Convert to a folder mod by creating directory '{mod.id}/' with '{mod.id}/mod.yaml' and '{mod.id}/skills/'.",
                    )
                )

            for cmd in mod.commands:
                if cmd.handler_script and not Path(cmd.handler_script).is_absolute():
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="mod_capability",
                            message=f"Single-file mod '{mod.id}' cannot provide local script files ('{cmd.handler_script}'). Local script handlers require a folder mod layout.",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint=f"Convert to a folder mod by creating directory '{mod.id}/' with '{mod.id}/mod.yaml' and '{mod.id}/scripts/'.",
                        )
                    )

            for mon in mod.monitors:
                cmd_str = str(mon.command)
                if "${MOD_DIR}" in cmd_str or "${mod_dir}" in cmd_str or cmd_str.startswith("./") or "scripts/" in cmd_str:
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            category="mod_capability",
                            message=f"Single-file mod '{mod.id}' references local mod directory in monitor command '{mon.command}'.",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint=f"Single-file mods do not have a dedicated directory; convert to folder mod '{mod.id}/mod.yaml' if bundling scripts.",
                        )
                    )

        return issues

    def check_commands(self, mod: Mod) -> List[ValidationIssue]:
        """Validate declarative slash commands: names, handlers, inline syntax, policies."""
        issues: List[ValidationIssue] = []
        for cmd in mod.commands:
            # 1. Command name
            if not cmd.name:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="command",
                        message=f"Mod '{mod.id}' defines a command with an empty name.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Commands must have a non-empty name matching ^[a-z0-9_-]+$.",
                    )
                )
            elif not COMMAND_NAME_RE.match(cmd.name):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="command",
                        message=f"Command '{cmd.name}' in mod '{mod.id}' has invalid name. Must match {COMMAND_NAME_RE.pattern}.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Use lowercase alphanumeric characters with hyphens or underscores (e.g. status, my-cmd).",
                    )
                )
            elif len(cmd.name) > 32:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="command",
                        message=f"Command '{cmd.name}' in mod '{mod.id}' exceeds 32 characters ({len(cmd.name)}).",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Shorten command name to 32 characters or fewer.",
                    )
                )

            # 2. Handler specification
            has_handler = bool(cmd.handler_script or cmd.python or cmd.shell)
            if not has_handler:
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="command",
                        message=f"Command '{cmd.name}' in mod '{mod.id}' has no handler specified.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Specify 'python', 'shell', or 'handler_script'/'script'.",
                    )
                )

            # 3. Python handler AST check
            if cmd.python:
                ok, err = check_ast_snippet(cmd.python)
                if not ok and err:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="ast",
                            message=f"Command '{cmd.name}' inline Python has syntax error: {err}",
                            mod_id=mod.id,
                            file=str(mod.path),
                            line=getattr(err, "lineno", None),
                            column=getattr(err, "offset", None),
                            hint="Fix Python syntax in command python: block.",
                        )
                    )

            # 4. Handler script resolution
            if cmd.handler_script and mod.is_folder and not Path(cmd.handler_script).is_absolute():
                resolved = cmd.resolve_handler_path()
                if resolved and not resolved.exists():
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            category="command",
                            message=f"Command '{cmd.name}' handler script not found: {resolved}",
                            mod_id=mod.id,
                            file=str(mod.path),
                            hint=f"Ensure script file exists at {cmd.handler_script} relative to mod directory.",
                        )
                    )

            # 5. Busy policy
            if cmd.busy_policy and cmd.busy_policy not in ("reject", "dispatch", "interrupt_then_dispatch"):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="command",
                        message=f"Command '{cmd.name}' has unexpected busy_policy '{cmd.busy_policy}'.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Allowed values: reject, dispatch, interrupt_then_dispatch.",
                    )
                )
        return issues

    def check_i18n(self, mod: Mod) -> List[ValidationIssue]:
        """Validate declarative localization catalogs across languages and surfaces."""
        issues: List[ValidationIssue] = []
        if not isinstance(mod.i18n, dict):
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="i18n",
                    message=f"Mod '{mod.id}' i18n section must be a dictionary, got {type(mod.i18n).__name__}.",
                    mod_id=mod.id,
                    file=str(mod.path),
                    hint="Declare i18n as a mapping of language codes to translation dictionaries (e.g. ru: {...}, es: {...}).",
                )
            )
            return issues

        for lang, catalog in mod.i18n.items():
            if not isinstance(catalog, dict):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="i18n",
                        message=f"Mod '{mod.id}' locale entry for '{lang}' must be a dictionary, got {type(catalog).__name__}.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint=f"Ensure {lang} contains translation key-value mappings.",
                    )
                )
                continue

            norm_lang = str(lang).strip().lower().replace("_", "-")
            if not re.match(r"^[a-z]{2,3}(-[a-z0-9]+)?$", norm_lang):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="i18n",
                        message=f"Mod '{mod.id}' defines unusual language code '{lang}'. Expected 2-letter ISO code (e.g. ru, en, es, zh).",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )

            cmds = catalog.get("commands") or catalog.get("slash")
            if cmds is not None and not isinstance(cmds, dict):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="i18n",
                        message=f"Mod '{mod.id}' i18n.{lang}.commands must be a dictionary of commands, got {type(cmds).__name__}.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
            cfg = catalog.get("config")
            if cfg is not None and not isinstance(cfg, dict):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="i18n",
                        message=f"Mod '{mod.id}' i18n.{lang}.config must be a dictionary of config fields, got {type(cfg).__name__}.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
        return issues

    def check_tests(self, mod: Mod) -> List[ValidationIssue]:
        """Validate inline and folder tests declarations and Python syntax."""
        issues: List[ValidationIssue] = []
        for t in getattr(mod, "inline_tests", []):
            t_name = t.get("name", "inline_test")
            code = t.get("code", "")
            if not code:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="test",
                        message=f"Inline test '{t_name}' in mod '{mod.id}' has empty code block.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
            else:
                ok, err = check_ast_snippet(code)
                if not ok and err:
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="ast",
                            message=f"Inline test '{t_name}' in mod '{mod.id}' has Python syntax error: {err}",
                            mod_id=mod.id,
                            file=str(mod.path),
                            line=getattr(err, "lineno", None),
                            column=getattr(err, "offset", None),
                            hint="Fix Python syntax or indentation in tests block.",
                        )
                    )

        for tf in getattr(mod, "test_files", []):
            if not tf.exists():
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="test",
                        message=f"Test file not found: {tf}",
                        mod_id=mod.id,
                        file=str(tf),
                    )
                )
            elif tf.suffix != ".py":
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="test",
                        message=f"Test file '{tf.name}' in mod '{mod.id}' is not a Python (.py) file.",
                        mod_id=mod.id,
                        file=str(tf),
                    )
                )
        return issues

    def check_mcp_servers(self, mod: Mod) -> List[ValidationIssue]:
        """Validate declarative MCP servers definitions."""
        issues: List[ValidationIssue] = []
        if not isinstance(mod.mcp_servers, dict):
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="mcp",
                    message=f"Mod '{mod.id}' mcp_servers must be a dictionary, got {type(mod.mcp_servers).__name__}.",
                    mod_id=mod.id,
                    file=str(mod.path),
                )
            )
            return issues

        for srv_name, srv_spec in mod.mcp_servers.items():
            if not isinstance(srv_spec, dict):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mcp",
                        message=f"MCP server '{srv_name}' in mod '{mod.id}' must be a dictionary.",
                        mod_id=mod.id,
                        file=str(mod.path),
                    )
                )
                continue

            if not srv_spec.get("command"):
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="mcp",
                        message=f"MCP server '{srv_name}' in mod '{mod.id}' is missing required 'command'.",
                        mod_id=mod.id,
                        file=str(mod.path),
                        hint="Specify command string or executable path for MCP server.",
                    )
                )
        return issues

    def validate_mod(self, mod: Mod) -> ModValidationResult:
        """Run all static checks on a single mod."""
        issues: List[ValidationIssue] = []

        # 1. YAML checks
        issues.extend(self.check_yaml_file(mod.path, mod.id))
        if mod.mod_dir and mod.mod_dir.is_dir():
            targets_dir = mod.mod_dir / "targets"
            if targets_dir.is_dir():
                for tf in targets_dir.glob("*.yaml"):
                    issues.extend(self.check_yaml_file(tf, mod.id))
            mon_dir = mod.mod_dir / "monitors"
            if mon_dir.is_dir():
                for mf in mon_dir.glob("*.yaml"):
                    issues.extend(self.check_yaml_file(mf, mod.id))
            for cand_cfg in ("config.yaml", "config.yml", "user_config.yaml", "user_config.yml"):
                uc_file = mod.mod_dir / cand_cfg
                if uc_file.exists():
                    issues.extend(self.check_yaml_file(uc_file, mod.id))
            for cand_cmd in ("commands.yaml", "commands.yml"):
                cmd_file = mod.mod_dir / cand_cmd
                if cmd_file.exists():
                    issues.extend(self.check_yaml_file(cmd_file, mod.id))
            for cand_loc in ("i18n.yaml", "i18n.yml", "locales.yaml", "locales.yml"):
                loc_file = mod.mod_dir / cand_loc
                if loc_file.exists():
                    issues.extend(self.check_yaml_file(loc_file, mod.id))
            for loc_sub in ("i18n", "locales"):
                loc_sub_dir = mod.mod_dir / loc_sub
                if loc_sub_dir.is_dir():
                    for lf in loc_sub_dir.glob("*.yaml"):
                        issues.extend(self.check_yaml_file(lf, mod.id))

        # 2. Mod ID format
        if not mod.id or not IDENTIFIER_RE.match(mod.id):
            issues.append(
                ValidationIssue(
                    severity="error",
                    category="mod_id",
                    message=f"Invalid mod ID '{mod.id}'. Must match {IDENTIFIER_RE.pattern}.",
                    mod_id=mod.id,
                    file=str(mod.path),
                )
            )

        # 3. Targets existence & simulation
        target_issues, simulated_files = self.check_targets_and_simulate(mod)
        issues.extend(target_issues)

        # 4. Static AST parsing
        ast_issues = self.check_ast_syntax(mod, simulated_files)
        issues.extend(ast_issues)

        # 5. config schema and value checks
        config_issues = self.check_config(mod)
        issues.extend(config_issues)

        # 6. Monitors checks
        mon_issues = self.check_monitors(mod)
        issues.extend(mon_issues)

        # 7. Commands checks
        cmd_issues = self.check_commands(mod)
        issues.extend(cmd_issues)

        # 8. i18n checks
        i18n_issues = self.check_i18n(mod)
        issues.extend(i18n_issues)

        # 9. Tests checks
        test_issues = self.check_tests(mod)
        issues.extend(test_issues)

        # 10. MCP servers checks
        mcp_issues = self.check_mcp_servers(mod)
        issues.extend(mcp_issues)

        # 11. Layout and capability checks
        layout_issues = self.check_layout_and_capabilities(mod)
        issues.extend(layout_issues)

        has_errors = any(i.severity == "error" for i in issues)
        return ModValidationResult(
            mod_id=mod.id,
            name=mod.name,
            valid=not has_errors,
            issues=issues,
        )

    def validate_all(self, mods: Optional[List[Mod]] = None) -> List[ModValidationResult]:
        """Validate all loaded mods and perform cross-mod integrity checks."""

        if mods is None:
            from runner import ModLoader
            mods = ModLoader.load_all(self.mods_dir)

        results: List[ModValidationResult] = []
        global_issues = self.check_mod_id_uniqueness(mods)
        global_by_mod: Dict[str, List[ValidationIssue]] = {}
        for issue in global_issues:
            global_by_mod.setdefault(issue.mod_id, []).append(issue)

        for mod in mods:
            res = self.validate_mod(mod)
            if mod.id in global_by_mod:
                res.issues.extend(global_by_mod[mod.id])
                if any(i.severity == "error" for i in global_by_mod[mod.id]):
                    res.valid = False
            results.append(res)

        return results

    def simulate_composite_apply(self, mods: Optional[List[Mod]] = None) -> CompositeSimulationReport:
        """Simulate in-memory application of all active mods in topological order.

        1. Creates virtual RAM file system (virtual_fs: Dict[str, str]).
        2. Applies target replacements sequentially in memory.
        3. Detects target collision:
           - if target old_block not found in virtual_fs[file], flags TargetConflictIssue
             with information about which earlier mod modified the file.
           - if target old_block is ambiguous (>1 matches), flags ambiguity error.
        4. Runs ast.parse() on the composite code of every modified Python file.
        5. Blocks disk apply if any syntax error or target collision is detected.
        """
        if mods is None:
            from runner import ModLoader
            mods = ModLoader.load_all(self.mods_dir)

        active_mods = [m for m in mods if m.enabled]
        if not active_mods:
            return CompositeSimulationReport(valid=True, issues=[])

        from .loader import ModDependencyGraph
        from .models import CycleDetectionError
        try:
            graph = ModDependencyGraph(active_mods)
            ordered_mods = graph.topological_sort(active_mods)
        except CycleDetectionError as exc:
            issue = ValidationIssue(
                severity="error",
                category="dependency",
                message=f"Dependency cycle detected in active mods: {exc}",
                mod_id="global",
            )
            return CompositeSimulationReport(valid=False, issues=[issue])

        virtual_fs: Dict[str, str] = {}
        file_modifiers: Dict[str, List[str]] = {}
        issues: List[ValidationIssue] = []

        for mod in ordered_mods:
            active_targets = [t for t in mod.targets if not t.disabled]
            if not active_targets:
                continue

            for idx, target in enumerate(active_targets):
                file_rel = target.file
                if not file_rel:
                    issues.append(ValidationIssue(
                        severity="error",
                        category="target_file",
                        message=f"Target #{idx} file path is empty",
                        mod_id=mod.id,
                        file=str(mod.path),
                    ))
                    continue

                clean_file = file_rel.strip().lstrip("/\\")
                target_path = self.repo_root / clean_file

                if clean_file not in virtual_fs:
                    if not target_path.exists():
                        issues.append(ValidationIssue(
                            severity="error",
                            category="target_file",
                            message=f"Target file does not exist: {clean_file}",
                            mod_id=mod.id,
                            file=clean_file,
                        ))
                        continue
                    try:
                        virtual_fs[clean_file] = target_path.read_text(encoding="utf-8")
                    except Exception as exc:
                        issues.append(ValidationIssue(
                            severity="error",
                            category="target_file",
                            message=f"Failed to read target file {clean_file}: {exc}",
                            mod_id=mod.id,
                            file=clean_file,
                        ))
                        continue

                curr_content = virtual_fs[clean_file]
                if target.new_block in curr_content:
                    # Already present in base code or applied earlier
                    continue

                cnt = curr_content.count(target.old_block)
                if cnt == 0:
                    prev_mods = file_modifiers.get(clean_file, [])
                    if prev_mods:
                        msg = (
                            f"Target #{idx} conflict in '{clean_file}': target old_block not found. "
                            f"File was modified by earlier mod(s): {', '.join(prev_mods)}."
                        )
                    else:
                        msg = f"Target #{idx} in '{clean_file}' old_block not found (upstream changed or block mismatch)."
                    issues.append(ValidationIssue(
                        severity="error",
                        category="target_conflict",
                        message=msg,
                        mod_id=mod.id,
                        file=clean_file,
                        hint="Check target old_block matches the current code state after prior mods are applied.",
                    ))
                elif cnt > 1:
                    issues.append(ValidationIssue(
                        severity="error",
                        category="target_conflict",
                        message=f"Target #{idx} in '{clean_file}' is ambiguous (matches {cnt} times in simulated file).",
                        mod_id=mod.id,
                        file=clean_file,
                        hint="Expand old_block context to match uniquely.",
                    ))
                else:
                    virtual_fs[clean_file] = curr_content.replace(target.old_block, target.new_block, 1)
                    file_modifiers.setdefault(clean_file, []).append(mod.id)

        # Composite AST validation on all modified Python files
        for file_rel, modified_code in virtual_fs.items():
            if file_rel in file_modifiers and (file_rel.endswith(".py") or file_rel.endswith(".pyw")):
                try:
                    ast.parse(modified_code, filename=file_rel)
                except (SyntaxError, IndentationError) as exc:
                    err_line = exc.lineno or 1
                    err_col = exc.offset
                    err_type = type(exc).__name__
                    snippet = _extract_snippet_with_pointer(modified_code, err_line, err_col)
                    modifiers = file_modifiers.get(file_rel, [])
                    mod_id = modifiers[-1] if modifiers else "unknown"
                    issues.append(ValidationIssue(
                        severity="error",
                        category="ast",
                        message=f"Composite AST error in '{file_rel}' ({err_type}): {exc.msg}. File modified by: {', '.join(modifiers)}",
                        mod_id=mod_id,
                        file=file_rel,
                        line=err_line,
                        column=err_col,
                        source_snippet=snippet,
                        hint=self._suggest_syntax_hint(exc),
                    ))

        has_errors = any(i.severity == "error" for i in issues)
        return CompositeSimulationReport(
            valid=not has_errors,
            issues=issues,
            touched_files=virtual_fs,
            file_modifiers=file_modifiers,
        )

    @staticmethod
    def format_issue(issue: ValidationIssue, use_color: bool = True) -> str:
        """Format an issue with colored labels, snippet, and actionable hint."""
        RED = "\033[1;31m" if use_color else ""
        YELLOW = "\033[1;33m" if use_color else ""
        CYAN = "\033[1;36m" if use_color else ""
        BOLD = "\033[1m" if use_color else ""
        DIM = "\033[2m" if use_color else ""
        RESET = "\033[0m" if use_color else ""

        if issue.severity == "error":
            sym = f"{RED}✗{RESET}"
            tag = f"{RED}[{issue.category.upper()} ERROR]{RESET}"
        else:
            sym = f"{YELLOW}⚠{RESET}"
            tag = f"{YELLOW}[{issue.category.upper()} WARNING]{RESET}"

        loc = ""
        if issue.file:
            loc = f" {CYAN}{issue.file}{RESET}"
            if issue.line is not None:
                loc += f":{issue.line}"
                if issue.column is not None:
                    loc += f":{issue.column}"

        lines = [f"  {sym} {tag}{loc}"]
        lines.append(f"    {BOLD}Mod:{RESET} {issue.mod_id}")
        lines.append(f"    {BOLD}Message:{RESET} {issue.message}")

        if issue.source_snippet:
            lines.append(f"    {BOLD}Code:{RESET}")
            for sline in issue.source_snippet.splitlines():
                lines.append(f"      {sline}")

        if issue.hint:
            lines.append(f"    {DIM}Hint: {issue.hint}{RESET}")

        return "\n".join(lines)

    @staticmethod
    def format_report(results: List[ModValidationResult], use_color: Optional[bool] = None) -> str:
        """Format full validation report across all mods."""
        if use_color is None:
            use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

        RED = "\033[1;31m" if use_color else ""
        GREEN = "\033[1;32m" if use_color else ""
        YELLOW = "\033[1;33m" if use_color else ""
        BOLD = "\033[1m" if use_color else ""
        DIM = "\033[2m" if use_color else ""
        RESET = "\033[0m" if use_color else ""

        lines = []
        total = len(results)
        passed = sum(1 for r in results if r.valid)
        failed = total - passed
        total_errors = sum(len(r.errors) for r in results)
        total_warnings = sum(len(r.warnings) for r in results)

        lines.append(f"{BOLD}Hermes Mod Engine: Static Validation Report{RESET}")
        lines.append(f"{DIM}Repository root: {HERMES_ROOT}{RESET}")
        lines.append("=" * 80)

        for r in results:
            if r.valid and not r.warnings:
                sym = f"{GREEN}✓{RESET}"
                status_txt = f"{GREEN}PASSED{RESET}"
                lines.append(f"{sym} {BOLD}{r.mod_id:<32}{RESET} [{status_txt}] {DIM}({r.name}){RESET}")
            elif r.valid and r.warnings:
                sym = f"{YELLOW}⚠{RESET}"
                status_txt = f"{YELLOW}PASSED (with warnings){RESET}"
                lines.append(f"{sym} {BOLD}{r.mod_id:<32}{RESET} [{status_txt}] {DIM}({r.name}){RESET}")
                for issue in r.warnings:
                    lines.append(ModValidator.format_issue(issue, use_color=use_color))
                lines.append("")
            else:
                sym = f"{RED}✗{RESET}"
                status_txt = f"{RED}FAILED ({len(r.errors)} errors){RESET}"
                lines.append(f"{sym} {BOLD}{r.mod_id:<32}{RESET} [{status_txt}] {DIM}({r.name}){RESET}")
                for issue in r.issues:
                    lines.append(ModValidator.format_issue(issue, use_color=use_color))
                lines.append("")

        lines.append("=" * 80)
        lines.append(f"{BOLD}Summary:{RESET}")
        lines.append(f"  Total mods scanned: {total}")
        lines.append(f"  Passed:             {GREEN}{passed}{RESET}")
        if failed > 0:
            lines.append(f"  Failed:             {RED}{failed}{RESET} ({total_errors} errors)")
        else:
            lines.append("  Failed:             0")
        if total_warnings > 0:
            lines.append(f"  Warnings:           {YELLOW}{total_warnings}{RESET}")

        if failed == 0:
            lines.append(f"\n{GREEN}✓ All mod checks passed successfully.{RESET}")
        else:
            lines.append(f"\n{RED}✗ Validation failed. Fix the reported errors above before applying mods.{RESET}")

        return "\n".join(lines)
