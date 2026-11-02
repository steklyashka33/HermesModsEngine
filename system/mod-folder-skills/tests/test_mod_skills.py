"""Tests for folder mods carrying custom skills (mod_dir/skills/)."""

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Read engine sources once at module import time (before HomeIOGuard activates during test execution)
_mods_dir = Path(__file__).resolve().parents[3]
if not (_mods_dir / ".engine").is_dir():
    _mods_dir = Path(os.environ.get("HERMES_MODS_DIR", Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "mods"))
_engine_dir = _mods_dir / ".engine"

_ENGINE_INIT = (_engine_dir / "__init__.py").read_text(encoding="utf-8")
_MODELS_PY = (_engine_dir / "models.py").read_text(encoding="utf-8")
_LOADER_PY = (_engine_dir / "loader.py").read_text(encoding="utf-8")
_CONFIG_MGR_PY = (_engine_dir / "config_manager.py").read_text(encoding="utf-8")
_MONITOR_MGR_PY = (_engine_dir / "monitor_manager.py").read_text(encoding="utf-8")
_I18N_PY = (_engine_dir / "i18n.py").read_text(encoding="utf-8") if (_engine_dir / "i18n.py").exists() else ""

from agent.skill_utils import (
    TIER_CREATE_DIR,
    TIER_EXTERNAL,
    TIER_LOCAL,
    TIER_MODS,
    TIER_PROJECT,
    _mod_skills_dirs_cache_clear,
    get_all_skills_dirs,
    get_mod_skills_dirs,
    get_skill_search_roots,
    is_external_skill_path,
    normalize_skill_lookup_name,
)
from tools.skills_tool import (
    _SKILLS_CACHE,
    _get_category_from_path,
    skill_view,
    skills_list,
)


@pytest.fixture
def hermes_mod_env(tmp_path):
    """Create a temporary HERMES_HOME with skills/ and mods/."""
    home = tmp_path / ".hermes"
    home.mkdir()
    skills_dir = home / "skills"
    skills_dir.mkdir()
    mods_dir = home / "mods"
    mods_dir.mkdir()

    # Create a local default skill
    local_skill = skills_dir / "devops" / "local-helper"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text(
        "---\nname: local-helper\ncategory: devops\ndescription: Local helper skill\n---\n# Local Helper\n",
        encoding="utf-8",
    )

    # Clear caches
    _mod_skills_dirs_cache_clear()
    _SKILLS_CACHE.clear()

    return home


def create_folder_mod(mods_dir: Path, mod_id: str, enabled: bool = True, skills: dict = None) -> Path:
    """Helper to create a folder mod with optional skills."""
    mod_dir = mods_dir / mod_id
    mod_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": mod_id,
        "name": f"Mod {mod_id}",
        "version": "1.0.0",
        "enabled": enabled,
        "description": f"Test mod {mod_id}",
        "targets": [],
    }
    with open(mod_dir / "mod.yaml", "w", encoding="utf-8") as f:
        yaml.dump(manifest, f)

    if skills:
        skills_base = mod_dir / "skills"
        for skill_rel, content in skills.items():
            skill_file = skills_base / skill_rel
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(content, encoding="utf-8")

    return mod_dir


class TestModEngineSkills:
    """Tests for the mod engine (.engine/loader.py and .engine/models.py)."""

    @pytest.fixture
    def test_engine(self, tmp_path):
        pkg_dir = tmp_path / "test_engine_pkg"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text(_ENGINE_INIT, encoding="utf-8")
        (pkg_dir / "models.py").write_text(_MODELS_PY, encoding="utf-8")
        (pkg_dir / "loader.py").write_text(_LOADER_PY, encoding="utf-8")
        (pkg_dir / "config_manager.py").write_text(_CONFIG_MGR_PY, encoding="utf-8")
        (pkg_dir / "monitor_manager.py").write_text(_MONITOR_MGR_PY, encoding="utf-8")
        if _I18N_PY:
            (pkg_dir / "i18n.py").write_text(_I18N_PY, encoding="utf-8")
        import importlib.util
        spec = importlib.util.spec_from_file_location("test_engine_pkg", str(pkg_dir / "__init__.py"), submodule_search_locations=[str(pkg_dir)])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["test_engine_pkg"] = pkg
        spec.loader.exec_module(pkg)
        return pkg

    def test_folder_mod_skills_dir_detected(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        mod_dir = create_folder_mod(
            mods_dir,
            "sample-mod",
            enabled=True,
            skills={
                "sample-skill/SKILL.md": "---\nname: sample-skill\ndescription: A sample skill\n---\n# Sample\n"
            },
        )
        mod = test_engine.parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="sample-mod")
        assert mod is not None
        assert mod.skills_dir == mod_dir / "skills"
        assert mod.has_skills is True
        skills_list_files = mod.list_skills()
        assert len(skills_list_files) == 1
        assert skills_list_files[0] == mod_dir / "skills" / "sample-skill" / "SKILL.md"

    def test_folder_mod_no_skills_dir(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        mod_dir = create_folder_mod(mods_dir, "empty-mod", enabled=True, skills=None)
        mod = test_engine.parse_mod_manifest(mod_dir / "mod.yaml", mod_dir=mod_dir, fallback_id="empty-mod")
        assert mod is not None
        assert mod.skills_dir is None
        assert mod.has_skills is False
        assert mod.list_skills() == []

    def test_single_file_mod_no_skills_dir(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        mod_file = mods_dir / "single.yaml"
        mod_file.write_text("id: single\nname: Single\nenabled: true\ntargets: []\n", encoding="utf-8")
        mod = test_engine.parse_mod_manifest(mod_file, mod_dir=mods_dir, fallback_id="single")
        assert mod is not None
        assert mod.skills_dir is None
        assert mod.has_skills is False

    def test_get_enabled_mod_skills_dirs(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        custom_dir = mods_dir / "custom"
        create_folder_mod(
            custom_dir,
            "mod-active",
            enabled=True,
            skills={"skill-a/SKILL.md": "---\nname: skill-a\ndescription: Skill A\n---\n"},
        )
        create_folder_mod(
            custom_dir,
            "mod-disabled",
            enabled=False,
            skills={"skill-b/SKILL.md": "---\nname: skill-b\ndescription: Skill B\n---\n"},
        )
        create_folder_mod(custom_dir, "mod-no-skills", enabled=True)

        res = test_engine.get_enabled_mod_skills_dirs(mods_dir)
        assert len(res) == 1
        assert res[0] == custom_dir / "mod-active" / "skills"

    def test_mod_dependencies_parsing_and_dag(self, tmp_path, test_engine):
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir(parents=True, exist_ok=True)
        base_dir = create_folder_mod(mods_dir, "base-mod", enabled=True)
        dep_file = mods_dir / "dep-mod.yaml"
        dep_file.write_text("id: dep-mod\nname: Dep Mod\nenabled: true\ndependencies:\n  - base-mod\ntargets: []\n", encoding="utf-8")

        base_mod = test_engine.parse_mod_manifest(base_dir / "mod.yaml", mod_dir=base_dir, fallback_id="base-mod")
        dep_mod = test_engine.parse_mod_manifest(dep_file, mod_dir=None, fallback_id="dep-mod")

        assert base_mod.dependencies == []
        assert dep_mod.dependencies == ["base-mod"]

        graph = test_engine.ModDependencyGraph([dep_mod, base_mod])
        sorted_mods = graph.topological_sort()
        assert [m.id for m in sorted_mods] == ["base-mod", "dep-mod"]

    def test_mod_cycle_detection_error(self, tmp_path, test_engine):
        mod_a = test_engine.Mod(id="a", name="A", targets=[], path=tmp_path / "a.yaml", dependencies=["b"])
        mod_b = test_engine.Mod(id="b", name="B", targets=[], path=tmp_path / "b.yaml", dependencies=["a"])
        graph = test_engine.ModDependencyGraph([mod_a, mod_b])
        with pytest.raises(test_engine.CycleDetectionError) as exc_info:
            graph.detect_cycles()
        assert "a" in exc_info.value.cycle
        assert "b" in exc_info.value.cycle


class TestHermesSkillUtilsModIntegration:
    """Tests for agent/skill_utils.py mod skills integration."""

    def test_get_mod_skills_dirs_active(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "voice-mod",
            enabled=True,
            skills={"voice-tool/SKILL.md": "---\nname: voice-tool\ndescription: Voice Tool\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            dirs = get_mod_skills_dirs()
            assert len(dirs) == 1
            assert dirs[0] == mods_dir / "voice-mod" / "skills"

    def test_get_mod_skills_dirs_disabled_skipped(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "voice-mod",
            enabled=False,
            skills={"voice-tool/SKILL.md": "---\nname: voice-tool\ndescription: Voice Tool\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            dirs = get_mod_skills_dirs()
            assert dirs == []

    def test_get_mod_skills_dirs_engine_failure_fallback(self, hermes_mod_env):
        """Even if engine import fails or raises, fallback direct scan discovers enabled mod skills."""
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "fallback-mod",
            enabled=True,
            skills={"fb-skill/SKILL.md": "---\nname: fb-skill\ndescription: Fallback skill\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            with patch.dict("sys.modules", {"engine": None}):
                dirs = get_mod_skills_dirs()
                assert len(dirs) == 1
                assert dirs[0] == mods_dir / "fallback-mod" / "skills"

    def test_search_roots_tier_precedence(self, hermes_mod_env):
        """TIER precedence order must be: PROJECT < LOCAL < CREATE_DIR < MODS < EXTERNAL."""
        assert TIER_PROJECT < TIER_LOCAL < TIER_CREATE_DIR < TIER_MODS < TIER_EXTERNAL

        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "tier-mod",
            enabled=True,
            skills={"tier-skill/SKILL.md": "---\nname: tier-skill\ndescription: Tier\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            roots = get_skill_search_roots(hermes_mod_env / "skills")
            tiers = [t for t, _ in roots]
            assert TIER_LOCAL in tiers
            assert TIER_MODS in tiers
            local_idx = tiers.index(TIER_LOCAL)
            mod_idx = tiers.index(TIER_MODS)
            assert local_idx < mod_idx

    def test_get_all_skills_dirs_includes_mods(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "all-dirs-mod",
            enabled=True,
            skills={"skill/SKILL.md": "---\nname: skill\ndescription: Test\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            all_dirs = get_all_skills_dirs()
            assert mods_dir / "all-dirs-mod" / "skills" in all_dirs

    def test_is_external_skill_path_protects_mod_skills(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        mod_dir = create_folder_mod(
            mods_dir,
            "protected-mod",
            enabled=True,
            skills={"safe/SKILL.md": "---\nname: safe\ndescription: Safe\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            skill_md = mod_dir / "skills" / "safe" / "SKILL.md"
            assert is_external_skill_path(skill_md) is True

    def test_normalize_skill_lookup_name_mod_skill(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        mod_dir = create_folder_mod(
            mods_dir,
            "norm-mod",
            enabled=True,
            skills={"nested/test-skill/SKILL.md": "---\nname: test-skill\ndescription: Test\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            skill_path = mod_dir / "skills" / "nested" / "test-skill"
            normalized = normalize_skill_lookup_name(str(skill_path))
            assert normalized == "nested/test-skill"


class TestSkillsToolModIntegration:
    """Tests for tools/skills_tool.py with mod skills."""

    def test_skills_list_includes_mod_skills(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "speech-mod",
            enabled=True,
            skills={
                "devops/audio-transcribe/SKILL.md": (
                    "---\nname: audio-transcribe\ncategory: devops\ndescription: Transcribe speech audio\n---\n# Audio\n"
                )
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            raw = skills_list()
            data = json.loads(raw)
            assert data["success"] is True
            names = [s["name"] for s in data["skills"]]
            assert "audio-transcribe" in names
            assert "local-helper" in names

    def test_skills_list_excludes_disabled_mod_skills(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "disabled-mod",
            enabled=False,
            skills={
                "secret-skill/SKILL.md": "---\nname: secret-skill\ndescription: Secret\n---\n# Secret\n"
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            raw = skills_list()
            data = json.loads(raw)
            names = [s["name"] for s in data["skills"]]
            assert "secret-skill" not in names

    def test_skill_view_mod_skill(self, hermes_mod_env):
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "view-mod",
            enabled=True,
            skills={
                "tools/super-tool/SKILL.md": (
                    "---\nname: super-tool\ncategory: tools\ndescription: Super tool capability\n---\n# Super Tool\nRuns superpowers.\n"
                )
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            raw = skill_view("super-tool")
            data = json.loads(raw)
            assert data["success"] is True
            assert data["name"] == "super-tool"
            assert "Runs superpowers" in data["content"]

    def test_local_skill_shadows_mod_skill(self, hermes_mod_env):
        """A local profile skill (~/.hermes/skills/) must override a mod skill with identical name."""
        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "override-mod",
            enabled=True,
            skills={
                "devops/local-helper/SKILL.md": (
                    "---\nname: local-helper\ncategory: devops\ndescription: Mod version of local helper\n---\n# Mod Version\n"
                )
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            raw = skill_view("local-helper")
            data = json.loads(raw)
            assert data["success"] is True
            # Local version should win!
            assert "Local Helper" in data["content"]
            assert "Mod Version" not in data["content"]


class TestPromptBuilderModIntegration:
    """Tests for agent/prompt_builder.py system prompt generation."""

    def test_prompt_builder_includes_mod_skills(self, hermes_mod_env):
        from agent.prompt_builder import build_skills_system_prompt

        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "prompt-mod",
            enabled=True,
            skills={
                "media/video-cutter/SKILL.md": (
                    "---\nname: video-cutter\ncategory: media\ndescription: Cut and edit video clips\n---\n# Video Cutter\n"
                )
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            prompt = build_skills_system_prompt(skills_dir_override=str(hermes_mod_env / "skills"))
            assert "video-cutter" in prompt
            assert "local-helper" in prompt

    def test_prompt_builder_excludes_disabled_mod(self, hermes_mod_env):
        from agent.prompt_builder import build_skills_system_prompt

        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "disabled-prompt-mod",
            enabled=False,
            skills={
                "media/hidden-cutter/SKILL.md": (
                    "---\nname: hidden-cutter\ncategory: media\ndescription: Hidden cutter\n---\n# Hidden\n"
                )
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            prompt = build_skills_system_prompt(skills_dir_override=str(hermes_mod_env / "skills"))
            assert "hidden-cutter" not in prompt
            assert "local-helper" in prompt

    def test_default_skills_unaffected_by_mod_failure(self, hermes_mod_env):
        """Even if a mod skill has corrupt frontmatter, default skills still render."""
        from agent.prompt_builder import build_skills_system_prompt

        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "broken-mod",
            enabled=True,
            skills={
                "broken-skill/SKILL.md": "---\nINVALID YAML ::: :::: \n---\n# Broken\n"
            },
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            _SKILLS_CACHE.clear()
            prompt = build_skills_system_prompt(skills_dir_override=str(hermes_mod_env / "skills"))
            assert "local-helper" in prompt


class TestModSkillsSecurityAndReload:
    """Security, cache invalidation, and reload_skills() integration tests."""

    def test_path_traversal_symlinked_skills_dir_rejected(self, hermes_mod_env, tmp_path):
        """A mod whose skills directory points outside the mod folder must be rejected."""
        mods_dir = hermes_mod_env / "mods"
        outside_target = tmp_path / "outside_skills"
        outside_target.mkdir(parents=True, exist_ok=True)
        (outside_target / "leak-skill").mkdir(parents=True, exist_ok=True)
        (outside_target / "leak-skill" / "SKILL.md").write_text(
            "---\nname: leak-skill\ndescription: Leaked skill\n---\n# Leak\n", encoding="utf-8"
        )
        mod_dir = mods_dir / "traversal-mod"
        mod_dir.mkdir(parents=True, exist_ok=True)
        (mod_dir / "mod.yaml").write_text("id: traversal-mod\nname: Traversal\nenabled: true\n", encoding="utf-8")
        (mod_dir / "skills").symlink_to(outside_target)

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            dirs = get_mod_skills_dirs()
            assert dirs == []

    def test_non_mod_folder_with_skills_ignored(self, hermes_mod_env):
        """A directory inside mods/ lacking a mod manifest must not be loaded as mod skills."""
        mods_dir = hermes_mod_env / "mods"
        random_dir = mods_dir / "not-a-mod"
        random_dir.mkdir(parents=True, exist_ok=True)
        skills_sub = random_dir / "skills" / "some-skill"
        skills_sub.mkdir(parents=True, exist_ok=True)
        (skills_sub / "SKILL.md").write_text(
            "---\nname: some-skill\ndescription: Some skill\n---\n", encoding="utf-8"
        )

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            dirs = get_mod_skills_dirs()
            assert dirs == []

    def test_cache_invalidation_on_enable_disable(self, hermes_mod_env):
        """Editing mod.yaml (enable/disable) invalidates get_mod_skills_dirs cache automatically."""
        mods_dir = hermes_mod_env / "mods"
        mod_dir = create_folder_mod(
            mods_dir,
            "cache-mod",
            enabled=True,
            skills={"dyn-skill/SKILL.md": "---\nname: dyn-skill\ndescription: Dyn\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            _mod_skills_dirs_cache_clear()
            # 1. Enabled initially
            dirs1 = get_mod_skills_dirs()
            assert len(dirs1) == 1

            # 2. Disable mod on disk directly without clearing in-memory cache
            manifest_file = mod_dir / "mod.yaml"
            manifest_data = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
            manifest_data["enabled"] = False
            manifest_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

            # Must return empty now because manifest mtime changed!
            dirs2 = get_mod_skills_dirs()
            assert len(dirs2) == 0

            # 3. Re-enable mod on disk
            manifest_data["enabled"] = True
            manifest_file.write_text(yaml.dump(manifest_data), encoding="utf-8")
            dirs3 = get_mod_skills_dirs()
            assert len(dirs3) == 1

    def test_clear_skills_cache_clears_mod_skills(self, hermes_mod_env):
        """clear_skills_cache() must also invalidate _MOD_SKILLS_CACHE."""
        from agent.skill_utils import _MOD_SKILLS_CACHE
        from tools.skills_tool import clear_skills_cache

        mods_dir = hermes_mod_env / "mods"
        create_folder_mod(
            mods_dir,
            "mod-clr",
            enabled=True,
            skills={"c-skill/SKILL.md": "---\nname: c-skill\ndescription: C\n---\n"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            get_mod_skills_dirs()
            assert len(_MOD_SKILLS_CACHE) > 0

            clear_skills_cache()
            assert len(_MOD_SKILLS_CACHE) == 0

    def test_reload_skills_command_diff(self, hermes_mod_env):
        """reload_skills() diff_command_snapshots reflects added and removed mod skill commands."""
        from agent.skill_commands import reload_skills

        mods_dir = hermes_mod_env / "mods"
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_mod_env)}):
            # Baseline reload
            base_diff = reload_skills()
            assert "added" in base_diff
            assert "removed" in base_diff
            assert "commands" in base_diff

            # Create mod with a new skill
            mod_dir = create_folder_mod(
                mods_dir,
                "slash-mod",
                enabled=True,
                skills={
                    "custom-action/SKILL.md": (
                        "---\nname: custom-action\ndescription: Custom action command\n---\n# Action\n"
                    )
                },
            )

            # Reload skills -> should be in added
            diff_add = reload_skills()
            added_names = [a["name"] for a in diff_add.get("added", [])]
            assert "custom-action" in added_names

            # Disable the mod
            manifest_file = mod_dir / "mod.yaml"
            manifest_data = yaml.safe_load(manifest_file.read_text(encoding="utf-8"))
            manifest_data["enabled"] = False
            manifest_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

            # Reload skills -> should be in removed
            diff_rem = reload_skills()
            removed_names = [r["name"] for r in diff_rem.get("removed", [])]
            assert "custom-action" in removed_names
