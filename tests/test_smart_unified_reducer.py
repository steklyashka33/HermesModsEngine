"""Comprehensive tests for Smart Unified Section Reducer and native i18n support in Hermes Mods Engine v1.4.0.

Tests:
1. Unified Data Contract validation (ModValidator):
   - targets, commands, config, i18n/locales, tests, mcp_servers, monitors validated under unified rules.
   - AST syntax check for inline python commands.
   - AST syntax check for inline test snippets.
   - Command name, handler existence, and policy checks.
   - i18n dictionary structure, language code format, and nested dictionary checks.
   - MCP servers command and dictionary structure checks.
2. Unified pipeline assembly in loader.py:
   - Sections can be inline, <sec>.yaml, or <sec>/*.yaml.
   - Native support for i18n/ directory and i18n: section on equal footing with locales/.
   - Priority resolution when both locales/ and i18n/ exist (i18n takes precedence).
   - Single-file mods support i18n: and locales: with full auto-propagation.
   - Dynamic custom section addition without duplicate registration.
3. Smart path auto-resolution:
   - Folder mod relative script paths (script: scripts/foo.py) auto-resolve relative to mod.path / mod_dir.
   - Single-file mods reject local relative script files in validator and execute inline python safely.
"""
from __future__ import annotations

from pathlib import Path
import pytest
import yaml

from engine.models import Mod, ModTarget, ConfigField, ModCommand, MonitorSpec
from engine.loader import (
    reduce_mod_sections,
    parse_mod_manifest,
    parse_mod_command,
    parse_inline_tests,
    update_commands_cache,
    update_locales_cache,
    compile_mod_locales,
)
from engine.validator import ModValidator, ValidationIssue


class TestSmartUnifiedSectionReducerPipeline:
    """Test the unified section reducer pipeline in loader.py across single-file and folder mods."""

    def test_single_file_mod_with_i18n_section(self, tmp_path: Path):
        """Single-file mod declaring modern 'i18n:' section is parsed into canonical i18n and backward-compatible locales."""
        manifest = tmp_path / "single-i18n.yaml"
        manifest.write_text(yaml.dump({
            "id": "single-i18n",
            "name": "Single i18n Mod",
            "version": "1.0.0",
            "description": "Mod with native i18n section",
            "i18n": {
                "ru": {
                    "name": "Мод с i18n",
                    "description": "Описание на русском",
                    "commands": {
                        "echo-cmd": {
                            "description": "Вывести эхо"
                        }
                    },
                    "config": {
                        "timeout": {
                            "title": "Таймаут",
                            "description": "Время ожидания"
                        }
                    }
                },
                "es": {
                    "name": "Mod con i18n",
                    "description": "Descripción en español"
                }
            },
            "commands": [
                {
                    "name": "echo-cmd",
                    "description": "Echo message",
                    "python": "print(args)"
                }
            ],
            "config": {
                "timeout": {
                    "type": "int",
                    "default": 30,
                    "description": "Timeout seconds"
                }
            }
        }), encoding="utf-8")

        mod = parse_mod_manifest(manifest, mod_dir=None, fallback_id="single-i18n")
        assert mod is not None
        assert not mod.is_folder

        # Unified data contract check: both i18n and locales are populated
        assert "ru" in mod.i18n
        assert "es" in mod.i18n
        assert "ru" in mod.locales
        assert "es" in mod.locales

        # Titles and descriptions with fallback
        assert mod.get_title("en") == "Single i18n Mod"
        assert mod.get_title("ru") == "Мод с i18n"
        assert mod.get_title("es") == "Mod con i18n"
        assert mod.get_description("ru") == "Описание на русском"
        assert mod.get_description("es") == "Descripción en español"

        # Auto-propagation to commands and config
        assert len(mod.commands) == 1
        cmd = mod.commands[0]
        assert cmd.name == "echo-cmd"
        assert cmd.get_description("en") == "Echo message"
        assert cmd.get_description("ru") == "Вывести эхо"

        assert "timeout" in mod.config
        cf = mod.config["timeout"]
        assert cf.get_title("ru") == "Таймаут"
        assert cf.get_description("ru") == "Время ожидания"

    def test_single_file_mod_with_legacy_locales_section(self, tmp_path: Path):
        """Single-file mod declaring legacy 'locales:' section is seamlessly normalized into canonical i18n."""
        manifest = tmp_path / "single-locales.yaml"
        manifest.write_text(yaml.dump({
            "id": "single-locales",
            "name": "Single Locales Mod",
            "locales": {
                "ru": {
                    "name": "Мод с locales",
                    "description": "Русское описание"
                }
            }
        }), encoding="utf-8")

        mod = parse_mod_manifest(manifest, mod_dir=None, fallback_id="single-locales")
        assert mod is not None
        assert "ru" in mod.i18n
        assert "ru" in mod.locales
        assert mod.get_title("ru") == "Мод с locales"
        assert mod.get_description("ru") == "Русское описание"

    def test_single_file_mod_with_both_i18n_and_locales_merges_with_i18n_priority(self, tmp_path: Path):
        """When single-file mod declares both locales and i18n, i18n takes precedence on conflicts."""
        manifest = tmp_path / "single-both.yaml"
        manifest.write_text(yaml.dump({
            "id": "single-both",
            "name": "Single Both Mod",
            "locales": {
                "ru": {
                    "name": "Старое имя",
                    "description": "Общее описание"
                },
                "fr": {
                    "name": "Nom Français"
                }
            },
            "i18n": {
                "ru": {
                    "name": "Новое имя i18n"
                },
                "es": {
                    "name": "Nombre Español"
                }
            }
        }), encoding="utf-8")

        mod = parse_mod_manifest(manifest, mod_dir=None, fallback_id="single-both")
        assert mod is not None
        # ru.name overwritten by i18n, ru.description preserved from locales
        assert mod.get_title("ru") == "Новое имя i18n"
        assert mod.get_description("ru") == "Общее описание"
        assert mod.get_title("fr") == "Nom Français"
        assert mod.get_title("es") == "Nombre Español"

    def test_folder_mod_i18n_directory_discovery_and_surfaces(self, tmp_path: Path):
        """Folder mod with i18n/ directory loads core and surface-specific translations."""
        mod_dir = tmp_path / "folder-i18n"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text(yaml.dump({
            "id": "folder-i18n",
            "name": "Folder i18n Mod",
            "version": "1.4.0",
        }), encoding="utf-8")

        i18n_dir = mod_dir / "i18n"
        i18n_dir.mkdir()
        (i18n_dir / "ru.yaml").write_text(yaml.dump({
            "name": "Папочный i18n",
            "description": "Описание папочного мода"
        }), encoding="utf-8")
        (i18n_dir / "ru.tui.yaml").write_text(yaml.dump({
            "status_label": "Статус в TUI"
        }), encoding="utf-8")
        (i18n_dir / "es.yaml").write_text(yaml.dump({
            "name": "Carpeta i18n",
            "description": "Descripción de carpeta"
        }), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="folder-i18n")
        assert mod is not None
        assert mod.is_folder is True
        assert mod.get_title("ru") == "Папочный i18n"
        assert mod.get_title("es") == "Carpeta i18n"
        assert "ru" in mod.i18n
        assert "es" in mod.i18n
        assert "tui" in mod.i18n_surfaces
        assert mod.i18n_surfaces["tui"]["ru"]["status_label"] == "Статус в TUI"

    def test_folder_mod_with_both_locales_and_i18n_dirs_prioritizes_i18n(self, tmp_path: Path):
        """When folder mod contains both locales/ and i18n/, i18n/ takes precedence on conflicting keys."""
        mod_dir = tmp_path / "folder-precedence"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text("id: folder-precedence\nname: Precedence\n", encoding="utf-8")

        locales_dir = mod_dir / "locales"
        locales_dir.mkdir()
        (locales_dir / "ru.yaml").write_text(yaml.dump({
            "name": "Имя из locales",
            "fallback_note": "Заметка из locales",
        }), encoding="utf-8")

        i18n_dir = mod_dir / "i18n"
        i18n_dir.mkdir()
        (i18n_dir / "ru.yaml").write_text(yaml.dump({
            "name": "Имя из i18n (победитель)",
        }), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="folder-precedence")
        assert mod is not None
        assert mod.get_title("ru") == "Имя из i18n (победитель)"
        assert mod.i18n["ru"]["fallback_note"] == "Заметка из locales"

    def test_dynamic_custom_section_addition_without_duplicate_registration(self, tmp_path: Path):
        """Any new section (e.g. 'audit', 'workflows') is automatically reduced into mod.sections without declaring it twice."""
        mod_dir = tmp_path / "extensible-mod"
        mod_dir.mkdir()
        (mod_dir / "mod.yaml").write_text(yaml.dump({
            "id": "extensible-mod",
            "name": "Extensible Mod",
            "version": "1.0.0",
            "inline_workflows": [{"step": "build", "cmd": "make"}],
        }), encoding="utf-8")

        # 1. Standalone custom section file (dict section)
        (mod_dir / "audit.yaml").write_text(yaml.dump({
            "compliance_level": "strict",
            "reviewer": "SecurityAgent",
            "max_risk_score": 0.2
        }), encoding="utf-8")

        # 2. Directory custom section (list section)
        pipeline_dir = mod_dir / "pipeline"
        pipeline_dir.mkdir()
        (pipeline_dir / "01_lint.yaml").write_text(yaml.dump([
            {"task": "lint", "runner": "ruff"}
        ]), encoding="utf-8")
        (pipeline_dir / "02_test.yaml").write_text(yaml.dump([
            {"task": "test", "runner": "pytest"}
        ]), encoding="utf-8")

        # Reduce sections
        reduced = reduce_mod_sections(mod_dir)
        assert "audit" in reduced
        assert reduced["audit"]["compliance_level"] == "strict"
        assert reduced["audit"]["reviewer"] == "SecurityAgent"

        assert "pipeline" in reduced
        assert len(reduced["pipeline"]) == 2
        assert reduced["pipeline"][0]["task"] == "lint"
        assert reduced["pipeline"][1]["task"] == "test"

        assert "inline_workflows" in reduced
        assert reduced["inline_workflows"][0]["step"] == "build"

        # Check full Mod object has mod.sections populated
        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="extensible-mod")
        assert mod is not None
        assert mod.sections["audit"]["compliance_level"] == "strict"
        assert len(mod.sections["pipeline"]) == 2

    def test_arbitrary_section_x_reduced_identically_across_all_forms(self, tmp_path: Path):
        """Verify any section X works across all forms: inline, X.yaml, and X/*.yaml identically."""
        # 1. Single-file mod: inline X
        single_manifest = tmp_path / "single-x.yaml"
        single_manifest.write_text(yaml.dump({
            "id": "single-x",
            "name": "Single X Mod",
            "feature_flags": {
                "enable_fast_path": True,
                "max_connections": 100
            }
        }), encoding="utf-8")
        mod_single = parse_mod_manifest(single_manifest, mod_dir=None, fallback_id="single-x")
        assert mod_single is not None
        assert mod_single.sections["feature_flags"]["enable_fast_path"] is True
        assert mod_single.sections["feature_flags"]["max_connections"] == 100

        # 2. Folder mod: standalone feature_flags.yaml
        folder_file = tmp_path / "folder-x-file"
        folder_file.mkdir()
        (folder_file / "mod.yaml").write_text("id: folder-x-file\nname: Folder X File\n", encoding="utf-8")
        (folder_file / "feature_flags.yaml").write_text(yaml.dump({
            "enable_fast_path": True,
            "max_connections": 100
        }), encoding="utf-8")
        mod_folder_file = parse_mod_manifest(folder_file / "mod.yaml", mod_dir=folder_file, fallback_id="folder-x-file")
        assert mod_folder_file is not None
        assert mod_folder_file.sections["feature_flags"]["enable_fast_path"] is True
        assert mod_folder_file.sections["feature_flags"]["max_connections"] == 100

        # 3. Folder mod: directory feature_flags/*.yaml
        folder_dir = tmp_path / "folder-x-dir"
        folder_dir.mkdir()
        (folder_dir / "mod.yaml").write_text("id: folder-x-dir\nname: Folder X Dir\n", encoding="utf-8")
        ff_dir = folder_dir / "feature_flags"
        ff_dir.mkdir()
        (ff_dir / "network.yaml").write_text(yaml.dump({"enable_fast_path": True}), encoding="utf-8")
        (ff_dir / "limits.yaml").write_text(yaml.dump({"max_connections": 100}), encoding="utf-8")
        mod_folder_dir = parse_mod_manifest(folder_dir / "mod.yaml", mod_dir=folder_dir, fallback_id="folder-x-dir")
        assert mod_folder_dir is not None
        assert mod_folder_dir.sections["feature_flags"]["enable_fast_path"] is True
        assert mod_folder_dir.sections["feature_flags"]["max_connections"] == 100


class TestSmartPathAutoResolution:
    """Test smart path resolution for scripts in folder mods vs inline safety in single-file mods."""

    def test_folder_mod_relative_script_resolution(self, tmp_path: Path):
        """In folder mods, script: scripts/foo.py resolves relative to mod_dir / mod.path."""
        mod_dir = tmp_path / "script-mod"
        mod_dir.mkdir()
        scripts_dir = mod_dir / "scripts"
        scripts_dir.mkdir()
        script_file = scripts_dir / "action.py"
        script_file.write_text("def handle(ctx): return 'ok'\\n", encoding="utf-8")

        (mod_dir / "mod.yaml").write_text(yaml.dump({
            "id": "script-mod",
            "name": "Script Mod",
            "commands": [
                {
                    "name": "run-action",
                    "description": "Execute action script",
                    "script": "scripts/action.py"
                }
            ]
        }), encoding="utf-8")

        mod = parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="script-mod")
        assert mod is not None
        assert len(mod.commands) == 1
        cmd = mod.commands[0]
        assert cmd.name == "run-action"
        assert cmd.handler_script == "scripts/action.py"

        # Auto-resolved path relative to mod directory
        resolved = cmd.resolve_handler_path()
        assert resolved is not None
        assert resolved.is_absolute()
        assert resolved == script_file.resolve()
        assert resolved.exists()

        # cmd.resolved_handler_path property
        assert cmd.resolved_handler_path == script_file.resolve()

        # mod.resolve_path helper
        assert mod.resolve_path("scripts/action.py") == script_file.resolve()

        # Serialized dictionary contains handler_file pointing to resolved path
        cmd_dict = cmd.to_dict()
        assert cmd_dict["handler_file"] == str(script_file.resolve())

    def test_single_file_mod_rejects_relative_script_in_validator(self, tmp_path: Path):
        """Single-file mod with relative script path fails validation (requires folder mod)."""
        manifest = tmp_path / "bad-single-script.yaml"
        manifest.write_text(yaml.dump({
            "id": "bad-single-script",
            "name": "Bad Single Script",
            "commands": [
                {
                    "name": "tool",
                    "description": "Tool command",
                    "script": "scripts/missing.py"
                }
            ]
        }), encoding="utf-8")

        mod = parse_mod_manifest(manifest, mod_dir=None, fallback_id="bad-single-script")
        assert mod is not None

        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        res = validator.validate_mod(mod)
        assert res.valid is False
        assert any("cannot provide local script files" in e.message for e in res.errors)

    def test_single_file_mod_inline_python_passes_and_checks_syntax(self, tmp_path: Path):
        """Single-file mod with valid inline python passes; invalid syntax fails AST check."""
        # 1. Valid inline python
        good_manifest = tmp_path / "good-inline.yaml"
        good_manifest.write_text(yaml.dump({
            "id": "good-inline",
            "name": "Good Inline Mod",
            "commands": [
                {
                    "name": "calc",
                    "description": "Calculate something",
                    "python": "result = 2 + 2"
                }
            ]
        }), encoding="utf-8")

        mod_good = parse_mod_manifest(good_manifest, mod_dir=None, fallback_id="good-inline")
        assert mod_good is not None
        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        res_good = validator.validate_mod(mod_good)
        assert res_good.valid is True

        # 2. Invalid inline python syntax
        bad_manifest = tmp_path / "bad-inline.yaml"
        bad_manifest.write_text(yaml.dump({
            "id": "bad-inline",
            "name": "Bad Inline Mod",
            "commands": [
                {
                    "name": "broken-cmd",
                    "description": "Broken command",
                    "python": "def foo(:\\n    pass"
                }
            ]
        }), encoding="utf-8")

        mod_bad = parse_mod_manifest(bad_manifest, mod_dir=None, fallback_id="bad-inline")
        assert mod_bad is not None
        res_bad = validator.validate_mod(mod_bad)
        assert res_bad.valid is False
        ast_errors = [e for e in res_bad.errors if e.category == "ast"]
        assert len(ast_errors) >= 1
        assert "syntax error" in ast_errors[0].message.lower()


class TestUnifiedDataContractValidation:
    """Test unified semantic Data Contract validation across all sections in ModValidator."""

    def test_command_validation_rules(self, tmp_path: Path):
        """Validator enforces valid command names, handlers, and busy_policy."""
        mod = Mod(
            id="cmd-val-test",
            name="Command Validation Test",
            path=tmp_path / "mod.yaml",
            targets=[],
            commands=[
                # Invalid name
                ModCommand(name="INVALID/NAME!"),
                # Missing handler
                ModCommand(name="no-handler"),
            ]
        )
        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        issues = validator.check_commands(mod)
        errs = [i for i in issues if i.severity == "error"]
        assert any("invalid name" in e.message.lower() for e in errs)
        assert any("no handler specified" in e.message.lower() for e in errs)

    def test_i18n_validation_rules(self, tmp_path: Path):
        """Validator checks i18n structure and reports invalid entries."""
        # 1. Non-dict entry for language
        mod_bad = Mod(
            id="i18n-bad-val",
            name="i18n Bad Val",
            path=tmp_path / "mod.yaml",
            targets=[],
            i18n={
                "ru": "not a dictionary"  # invalid
            }
        )
        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        issues = validator.check_i18n(mod_bad)
        assert any("must be a dictionary" in i.message for i in issues if i.severity == "error")

        # 2. Valid i18n
        mod_good = Mod(
            id="i18n-good-val",
            name="i18n Good Val",
            path=tmp_path / "mod.yaml",
            targets=[],
            i18n={
                "ru": {"name": "Хорошо", "commands": {"help": {"description": "Помощь"}}},
                "es": {"name": "Bueno"}
            }
        )
        issues_good = validator.check_i18n(mod_good)
        assert len([i for i in issues_good if i.severity == "error"]) == 0

    def test_inline_test_ast_syntax_validation(self, tmp_path: Path):
        """Validator runs AST check on inline test blocks and catches syntax errors."""
        mod_broken_test = Mod(
            id="broken-test-mod",
            name="Broken Test Mod",
            path=tmp_path / "mod.yaml",
            targets=[],
            inline_tests=[
                {"name": "test_broken", "code": "assert (1 =="}
            ]
        )
        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        issues = validator.check_tests(mod_broken_test)
        errs = [i for i in issues if i.severity == "error" and i.category == "ast"]
        assert len(errs) >= 1
        assert "syntax error" in errs[0].message.lower()

    def test_mcp_servers_validation_rules(self, tmp_path: Path):
        """Validator checks MCP servers declarations."""
        mod_bad_mcp = Mod(
            id="mcp-test-mod",
            name="MCP Test Mod",
            path=tmp_path / "mod.yaml",
            targets=[],
            mcp_servers={
                "srv-missing-cmd": {"args": ["--port", "8080"]}  # missing 'command'
            }
        )
        validator = ModValidator(repo_root=tmp_path, mods_dir=tmp_path)
        issues = validator.check_mcp_servers(mod_bad_mcp)
        errs = [i for i in issues if i.severity == "error"]
        assert len(errs) >= 1
        assert "missing required 'command'" in errs[0].message.lower()

    def test_compile_mod_locales_generates_complete_catalog_with_i18n(self, tmp_path: Path):
        """compile_mod_locales generates flattened catalogs with mods.<id>.* and slash.* keys."""
        mod = Mod(
            id="catalog-mod",
            name="Catalog Mod",
            description="Catalog Mod English",
            path=tmp_path / "mod.yaml",
            targets=[],
            i18n={
                "ru": {
                    "name": "Каталог Мод",
                    "description": "Описание каталога",
                    "commands": {
                        "cat-cmd": {"description": "Команда каталога"}
                    }
                }
            },
            commands=[
                ModCommand(name="cat-cmd", description="Catalog command", python="pass")
            ]
        )
        catalogs = compile_mod_locales([mod])
        assert "ru" in catalogs
        core_ru = catalogs["ru"]["core"]
        assert core_ru["mods.catalog-mod.name"] == "Каталог Мод"
        assert core_ru["mods.catalog-mod.description"] == "Описание каталога"
        assert core_ru["slash.cat-cmd.description"] == "Команда каталога"
