"""Tests for Hermes Mod Engine Static AST & Manifest Validator."""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest
import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_default_mods = HERMES_HOME / "mods"
if not _default_mods.exists() and (Path(__file__).resolve().parent.parent / ".engine").is_dir():
    _default_mods = Path(__file__).resolve().parent.parent
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(_default_mods)))
ENGINE_DIR = MODS_DIR / ".engine"
if str(MODS_DIR) not in sys.path:
    sys.path.insert(0, str(MODS_DIR))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from runner import ModLoader, ModRunner, HERMES_ROOT
from engine.models import ConfigField, Mod, ModTarget, MonitorSpec
from engine.validator import ModValidator, ModValidationResult, ValidationIssue, check_ast_snippet


@pytest.fixture
def validator():
    return ModValidator(repo_root=HERMES_ROOT, mods_dir=MODS_DIR)


def test_validate_all_existing_mods(validator):
    """Verify all live mods pass static AST and manifest validation."""
    mods = ModLoader.load_all(MODS_DIR)
    assert len(mods) >= 31, f"Expected at least 31 mods, found {len(mods)}"

    results = validator.validate_all(mods)
    assert len(results) == len(mods)

    failed = [r for r in results if not r.valid]
    if failed:
        err_details = []
        for f in failed:
            for issue in f.errors:
                err_details.append(f"{f.mod_id}: [{issue.category}] {issue.message} ({issue.file}:{issue.line})")
        pytest.fail(f"{len(failed)} mods failed validation:\n" + "\n".join(err_details))

    for r in results:
        assert r.valid is True
        assert len(r.errors) == 0


def test_validate_single_mod(validator):
    """Validate a specific mod (slash-commands-ux)."""
    mods = ModLoader.load_all(MODS_DIR)
    mod = next(m for m in mods if m.id == "slash-commands-ux")
    res = validator.validate_mod(mod)
    assert res.valid is True
    assert res.mod_id == "slash-commands-ux"
    assert len(res.errors) == 0


def test_ast_parse_syntax_error_detection(tmp_path):
    """Ensure ast.parse() catches syntax errors in new_block with exact line and pointer."""
    # Create a dummy repo file
    repo_file = tmp_path / "sample.py"
    repo_file.write_text("def run():\n    pass\n", encoding="utf-8")

    mod = Mod(
        id="test-broken-syntax",
        name="Broken Syntax Mod",
        path=tmp_path / "mod.yaml",
        targets=[
            ModTarget(
                file="sample.py",
                old_block="    pass",
                new_block="    def inner(:\n        return 1",
            )
        ],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = validator.validate_mod(mod)

    assert res.valid is False
    ast_errors = [i for i in res.errors if i.category == "ast"]
    assert len(ast_errors) >= 1
    err = ast_errors[0]
    assert "SyntaxError" in err.message
    assert err.line is not None
    assert err.source_snippet is not None
    assert "^" in err.source_snippet
    assert err.hint is not None


def test_ast_parse_indentation_error_detection(tmp_path):
    """Ensure ast.parse() catches IndentationError in new_block."""
    repo_file = tmp_path / "sample.py"
    repo_file.write_text("def run():\n    pass\n", encoding="utf-8")

    mod = Mod(
        id="test-broken-indent",
        name="Broken Indent Mod",
        path=tmp_path / "mod.yaml",
        targets=[
            ModTarget(
                file="sample.py",
                old_block="    pass",
                new_block="  if True:\n      x = 1\n    return x",
            )
        ],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = validator.validate_mod(mod)

    assert res.valid is False
    ast_errors = [i for i in res.errors if i.category == "ast"]
    assert len(ast_errors) >= 1
    err = ast_errors[0]
    assert "IndentationError" in err.message
    assert "indentation" in err.hint.lower()


def test_ast_parse_unclosed_parentheses(tmp_path):
    """Ensure unclosed parentheses or brackets in new_block are detected."""
    repo_file = tmp_path / "sample.py"
    repo_file.write_text("def run():\n    pass\n", encoding="utf-8")

    mod = Mod(
        id="test-unclosed-paren",
        name="Unclosed Paren Mod",
        path=tmp_path / "mod.yaml",
        targets=[
            ModTarget(
                file="sample.py",
                old_block="    pass",
                new_block="    items = (\n        1, 2, 3\n    return items",
            )
        ],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = validator.validate_mod(mod)

    assert res.valid is False
    ast_errors = [i for i in res.errors if i.category == "ast"]
    assert len(ast_errors) >= 1
    err = ast_errors[0]
    assert "was never closed" in err.message or "SyntaxError" in err.message


def test_target_file_existence_check(tmp_path):
    """Ensure target files that don't exist relative to repo root are flagged as errors."""
    mod = Mod(
        id="test-missing-target",
        name="Missing Target Mod",
        path=tmp_path / "mod.yaml",
        targets=[
            ModTarget(
                file="nonexistent/subsystem/tool.py",
                old_block="foo",
                new_block="bar",
            )
        ],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = validator.validate_mod(mod)

    assert res.valid is False
    file_errors = [i for i in res.errors if i.category == "target_file"]
    assert len(file_errors) >= 1
    assert "does not exist" in file_errors[0].message


def test_config_schema_validation(tmp_path):
    """Validate config schema rules: unknown types, invalid defaults, min > max."""
    mod = Mod(
        id="test-cfg-schema",
        name="Config Schema Mod",
        path=tmp_path / "mod.yaml",
        targets=[],
        config={
            "field_bad_type": ConfigField(name="field_bad_type", type="invalid_type"),
            "field_bad_default_int": ConfigField(name="field_bad_default_int", type="int", default="not_an_int"),
            "field_bad_min_max": ConfigField(name="field_bad_min_max", type="int", min=100, max=10),
            "field_bad_default_bool": ConfigField(name="field_bad_default_bool", type="bool", default="maybe"),
            "field_bad_options": ConfigField(name="field_bad_options", type="int", options=["a", "b"]),
        },
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = validator.validate_mod(mod)

    assert res.valid is False
    cfg_errors = [i for i in res.errors if i.category in ("config", "user_config")]
    assert len(cfg_errors) >= 4

    messages = " ".join(e.message for e in cfg_errors)
    assert "unsupported type 'invalid_type'" in messages
    assert "field_bad_default_int" in messages
    assert "min (100) greater than max (10)" in messages
    assert "field_bad_default_bool" in messages


test_user_config_schema_validation = test_config_schema_validation


def test_config_runtime_values_validation(tmp_path):
    """Validate configured values from config.yaml against schema types."""
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        yaml.safe_dump({
            "mods": {
                "test-runtime-cfg": {
                    "count": "not_an_int",
                    "ratio": True,  # boolean passed to float
                    "flag": "unknown_bool",
                }
            }
        }),
        encoding="utf-8"
    )
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text("{}", encoding="utf-8")

    mod = Mod(
        id="test-runtime-cfg",
        name="Runtime Config Mod",
        path=tmp_path / "mod.yaml",
        targets=[],
        config={
            "count": ConfigField(name="count", type="int", default=10),
            "ratio": ConfigField(name="ratio", type="float", default=0.5),
            "flag": ConfigField(name="flag", type="bool", default=False),
        },
    )

    validator = ModValidator(
        repo_root=tmp_path,
        mods_dir=tmp_path,
        config_file=cfg_file,
        secrets_file=secrets_file,
    )
    res = validator.validate_mod(mod)

    assert res.valid is False
    cfg_errors = [i for i in res.errors if i.category in ("config", "user_config")]
    assert len(cfg_errors) >= 3
    messages = " ".join(e.message for e in cfg_errors)
    assert "count" in messages
    assert "ratio" in messages
    assert "flag" in messages


test_user_config_runtime_values_validation = test_config_runtime_values_validation


def test_yaml_syntax_error_detection(tmp_path):
    """Validate detection of malformed YAML files with line and column reporting."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("name: Test\nconfig: [unclosed\n", encoding="utf-8")

    mod = Mod(
        id="test-bad-yaml",
        name="Bad YAML Mod",
        path=bad_yaml,
        targets=[],
    )

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    res = validator.validate_mod(mod)

    assert res.valid is False
    yaml_errors = [i for i in res.errors if i.category == "yaml"]
    assert len(yaml_errors) >= 1
    err = yaml_errors[0]
    assert err.line is not None
    assert err.source_snippet is not None
    assert err.hint is not None


def test_duplicate_mod_id_detection(tmp_path):
    """Validate detection of duplicate mod IDs across distinct files."""
    mod1_file = tmp_path / "first.yaml"
    mod1_file.write_text(yaml.safe_dump({"id": "duplicate-id", "name": "First"}), encoding="utf-8")
    mod2_file = tmp_path / "second.yaml"
    mod2_file.write_text(yaml.safe_dump({"id": "duplicate-id", "name": "Second"}), encoding="utf-8")

    mod1 = Mod(id="duplicate-id", name="First", path=mod1_file, targets=[])
    mod2 = Mod(id="duplicate-id", name="Second", path=mod2_file, targets=[])

    validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
    issues = validator.check_mod_id_uniqueness([mod1, mod2])

    dup_issues = [i for i in issues if i.category == "mod_id" and "Duplicate" in i.message]
    assert len(dup_issues) >= 1
    assert "duplicate-id" in dup_issues[0].message


def test_preflight_validation_in_apply(tmp_path):
    """Ensure runner.py apply halts and does NOT touch files when pre-flight validation fails."""
    repo_file = tmp_path / "target.py"
    original_code = "def existing():\n    return True\n"
    repo_file.write_text(original_code, encoding="utf-8")

    # Target contains an intentional AST syntax error
    mod = Mod(
        id="test-preflight-fail",
        name="Preflight Fail Mod",
        path=tmp_path / "mod.yaml",
        targets=[
            ModTarget(
                file="target.py",
                old_block="    return True",
                new_block="    return (unclosed",
            )
        ],
    )

    runner = ModRunner(repo_root=tmp_path)
    res = runner.apply_mod(mod, apply_changes=True)

    assert res["status"] == "failed_preflight_validation"
    assert res["verified"] is False
    assert "Pre-flight validation failed" in res["error"]
    assert len(res["validation_errors"]) >= 1

    # Crucial check: verify disk file was NEVER touched!
    assert repo_file.read_text(encoding="utf-8") == original_code


def test_runner_cli_validate_command():
    """Verify `python3 runner.py validate` CLI outputs cleanly with exit code 0."""
    res = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "validate"],
        capture_output=True,
        text=True,
        cwd=str(MODS_DIR),
    )
    assert res.returncode == 0
    assert "Static Validation Report" in res.stdout
    assert "Total mods scanned:" in res.stdout
    assert "All mod checks passed successfully." in res.stdout


def test_runner_cli_validate_single_mod():
    """Verify `python3 runner.py validate <mod_id>` CLI works for a specific mod."""
    res = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "validate", "command-control"],
        capture_output=True,
        text=True,
        cwd=str(MODS_DIR),
    )
    assert res.returncode == 0
    assert "command-control" in res.stdout
    assert "PASSED" in res.stdout


def test_runner_cli_validate_json():
    """Verify `python3 runner.py validate --json` outputs valid structured JSON."""
    res = subprocess.run(
        [sys.executable, str(MODS_DIR / "runner.py"), "validate", "--json"],
        capture_output=True,
        text=True,
        cwd=str(MODS_DIR),
    )
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["valid"] is True
    assert data["total_mods"] >= 31
    assert data["passed_mods"] == data["total_mods"]
    assert data["failed_mods"] == 0
    assert len(data["results"]) == data["total_mods"]
