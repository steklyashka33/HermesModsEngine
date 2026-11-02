"""Tests for Hermes Mod Engine Dependency DAG, Topological Sorting, Cycle Detection, and Runner integration."""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
import yaml

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_default_mods = HERMES_HOME / "mods"
if not _default_mods.exists() and (Path(__file__).resolve().parent.parent / ".engine").is_dir():
    _default_mods = Path(__file__).resolve().parent.parent
MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", str(_default_mods)))
ENGINE_DIR = MODS_DIR / ".engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))
if str(MODS_DIR) not in sys.path:
    sys.path.insert(0, str(MODS_DIR))

import importlib.util
if "engine" not in sys.modules and (ENGINE_DIR / "__init__.py").exists():
    _spec = importlib.util.spec_from_file_location("engine", str(ENGINE_DIR / "__init__.py"))
    if _spec and _spec.loader:
        _pkg = importlib.util.module_from_spec(_spec)
        sys.modules["engine"] = _pkg
        _spec.loader.exec_module(_pkg)

from engine.models import Mod, ModTarget, CycleDetectionError
from engine.loader import parse_mod_manifest, ModDependencyGraph
from runner import ModLoader, ModRunner


def make_dummy_mod(mod_id: str, deps: list[str] = None, enabled: bool = True, author: str = "User", path: Path = None) -> Mod:
    return Mod(
        id=mod_id,
        name=f"Mod {mod_id}",
        targets=[],
        path=path or Path(f"/tmp/{mod_id}.yaml"),
        dependencies=deps or [],
        enabled=enabled,
        author=author,
    )


class TestModDependencyGraphUnit:
    """Unit tests for ModDependencyGraph (DAG, cycle detection, topological sort)."""

    def test_topological_sort_linear(self):
        # C depends on B, B depends on A
        mod_a = make_dummy_mod("mod-a")
        mod_b = make_dummy_mod("mod-b", deps=["mod-a"])
        mod_c = make_dummy_mod("mod-c", deps=["mod-b"])

        graph = ModDependencyGraph([mod_c, mod_a, mod_b])
        sorted_mods = graph.topological_sort()
        order = [m.id for m in sorted_mods]
        assert order == ["mod-a", "mod-b", "mod-c"]

    def test_topological_sort_diamond(self):
        # D depends on B and C; B and C depend on A
        mod_a = make_dummy_mod("mod-a")
        mod_b = make_dummy_mod("mod-b", deps=["mod-a"])
        mod_c = make_dummy_mod("mod-c", deps=["mod-a"])
        mod_d = make_dummy_mod("mod-d", deps=["mod-b", "mod-c"])

        graph = ModDependencyGraph([mod_d, mod_c, mod_b, mod_a])
        sorted_mods = graph.topological_sort()
        order = [m.id for m in sorted_mods]

        assert order.index("mod-a") < order.index("mod-b")
        assert order.index("mod-a") < order.index("mod-c")
        assert order.index("mod-b") < order.index("mod-d")
        assert order.index("mod-c") < order.index("mod-d")

    def test_cycle_detection_direct(self):
        # A depends on B, B depends on A
        mod_a = make_dummy_mod("mod-a", deps=["mod-b"])
        mod_b = make_dummy_mod("mod-b", deps=["mod-a"])

        graph = ModDependencyGraph([mod_a, mod_b])
        with pytest.raises(CycleDetectionError) as exc_info:
            graph.detect_cycles()
        err = exc_info.value
        assert "mod-a" in err.cycle
        assert "mod-b" in err.cycle

    def test_cycle_detection_self(self):
        # A depends on A
        mod_a = make_dummy_mod("mod-a", deps=["mod-a"])
        graph = ModDependencyGraph([mod_a])
        with pytest.raises(CycleDetectionError) as exc_info:
            graph.detect_cycles()
        assert exc_info.value.cycle == ["mod-a", "mod-a"]

    def test_cycle_detection_transitive(self):
        # A -> B -> C -> A
        mod_a = make_dummy_mod("mod-a", deps=["mod-b"])
        mod_b = make_dummy_mod("mod-b", deps=["mod-c"])
        mod_c = make_dummy_mod("mod-c", deps=["mod-a"])

        graph = ModDependencyGraph([mod_a, mod_b, mod_c])
        with pytest.raises(CycleDetectionError) as exc_info:
            graph.topological_sort()
        err = exc_info.value
        assert len(err.cycle) >= 3

    def test_dependency_closure(self):
        # C -> B -> A, D has no deps
        mod_a = make_dummy_mod("mod-a")
        mod_b = make_dummy_mod("mod-b", deps=["mod-a"])
        mod_c = make_dummy_mod("mod-c", deps=["mod-b"])
        mod_d = make_dummy_mod("mod-d")

        graph = ModDependencyGraph([mod_a, mod_b, mod_c, mod_d])
        closure_c = graph.get_dependency_closure([mod_c])
        assert [m.id for m in closure_c] == ["mod-a", "mod-b", "mod-c"]

        closure_b = graph.get_dependency_closure([mod_b])
        assert [m.id for m in closure_b] == ["mod-a", "mod-b"]

    def test_missing_dependencies(self):
        mod_a = make_dummy_mod("mod-a", deps=["non-existent-pkg@1.0"])
        graph = ModDependencyGraph([mod_a])
        missing = graph.get_missing_dependencies(mod_a)
        assert missing == ["non-existent-pkg@1.0"]

    def test_disabled_dependencies(self):
        mod_base = make_dummy_mod("mod-base", enabled=False)
        mod_app = make_dummy_mod("mod-app", deps=["mod-base"], enabled=True)

        graph = ModDependencyGraph([mod_base, mod_app])
        disabled = graph.get_disabled_dependencies(mod_app)
        assert len(disabled) == 1
        assert disabled[0].id == "mod-base"

    def test_reverse_dependents(self):
        mod_base = make_dummy_mod("mod-base", enabled=True)
        mod_dep1 = make_dummy_mod("mod-dep1", deps=["mod-base"], enabled=True)
        mod_dep2 = make_dummy_mod("mod-dep2", deps=["mod-base"], enabled=False)
        mod_other = make_dummy_mod("mod-other", deps=[], enabled=True)

        graph = ModDependencyGraph([mod_base, mod_dep1, mod_dep2, mod_other])
        all_dependents = graph.get_dependents(mod_base, enabled_only=False)
        assert [m.id for m in all_dependents] == ["mod-dep1", "mod-dep2"]

        active_dependents = graph.get_dependents(mod_base, enabled_only=True)
        assert [m.id for m in active_dependents] == ["mod-dep1"]

    def test_spec_normalization(self):
        mod_base = make_dummy_mod("mod-base")
        mod_app = make_dummy_mod("mod-app", deps=["mod-base@2.1.0", "MOD-BASE>=1.0"])

        graph = ModDependencyGraph([mod_base, mod_app])
        deps = graph.get_dependencies(mod_app)
        assert len(deps) == 1
        assert deps[0].id == "mod-base"
        assert graph.get_missing_dependencies(mod_app) == []


class TestModRunnerDependencyIntegration:
    """Integration tests for runner.py CLI with dependencies (enable, disable, apply)."""

    @pytest.fixture
    def temp_mods_dir(self, tmp_path):
        mods_dir = tmp_path / "mods"
        mods_dir.mkdir()

        # Create base mod
        base_manifest = {
            "id": "base-mod",
            "name": "Base Mod",
            "version": "1.0.0",
            "enabled": True,
            "targets": [],
            "dependencies": [],
        }
        with open(mods_dir / "base-mod.yaml", "w", encoding="utf-8") as f:
            yaml.dump(base_manifest, f)

        # Create dependent mod
        dep_manifest = {
            "id": "dependent-mod",
            "name": "Dependent Mod",
            "version": "1.0.0",
            "enabled": True,
            "targets": [],
            "dependencies": ["base-mod"],
        }
        with open(mods_dir / "dependent-mod.yaml", "w", encoding="utf-8") as f:
            yaml.dump(dep_manifest, f)

        return mods_dir

    def test_runner_disable_warns_when_dependents_active(self, temp_mods_dir):
        # Disabling base-mod when dependent-mod is enabled should warn to stderr
        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "disable", "base-mod", "--mods-dir", str(temp_mods_dir)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "Предупреждение: от мода 'base-mod' зависят другие активные моды: dependent-mod" in res.stderr

        # Check base-mod was disabled in .state.json while YAML remains untouched
        state_data = json.loads((temp_mods_dir / ".state.json").read_text(encoding="utf-8"))
        assert state_data["overrides"]["base-mod"]["enabled"] is False
        mods = ModLoader.load_all(temp_mods_dir)
        assert next(m for m in mods if m.id == "base-mod").enabled is False

    def test_runner_enable_auto_enables_disabled_dependencies(self, temp_mods_dir):
        # First disable base-mod and dependent-mod
        for name in ("base-mod", "dependent-mod"):
            p = temp_mods_dir / f"{name}.yaml"
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
            data["enabled"] = False
            p.write_text(yaml.dump(data), encoding="utf-8")

        # Now enable dependent-mod; should auto-enable base-mod
        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "enable", "dependent-mod", "--mods-dir", str(temp_mods_dir)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "Автоматически включена зависимость: 'base-mod'" in res.stdout
        assert "также включены зависимости: base-mod" in res.stdout

        # Verify in .state.json and loaded mods
        state_data = json.loads((temp_mods_dir / ".state.json").read_text(encoding="utf-8"))
        assert state_data["overrides"]["base-mod"]["enabled"] is True
        assert state_data["overrides"]["dependent-mod"]["enabled"] is True
        mods = ModLoader.load_all(temp_mods_dir)
        assert next(m for m in mods if m.id == "base-mod").enabled is True
        assert next(m for m in mods if m.id == "dependent-mod").enabled is True

    def test_runner_enable_warns_on_missing_dependency(self, temp_mods_dir):
        # Create a mod with a missing dependency
        broken = {
            "id": "broken-mod",
            "name": "Broken Mod",
            "version": "1.0.0",
            "enabled": False,
            "targets": [],
            "dependencies": ["missing-pkg"],
        }
        with open(temp_mods_dir / "broken-mod.yaml", "w", encoding="utf-8") as f:
            yaml.dump(broken, f)

        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "enable", "broken-mod", "--mods-dir", str(temp_mods_dir)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        assert "Предупреждение: мод 'broken-mod' требует отсутствующие зависимости: missing-pkg" in res.stderr

    def test_runner_apply_topological_order(self, temp_mods_dir):
        # Verify apply order via verify action (dry run)
        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "verify", "--mods-dir", str(temp_mods_dir)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 0
        import json
        data = json.loads(res.stdout)
        ids = [item["id"] for item in data]
        assert ids.index("base-mod") < ids.index("dependent-mod")

    def test_runner_detects_cycle_on_apply(self, temp_mods_dir):
        # Introduce cycle: base-mod depends on dependent-mod
        p = temp_mods_dir / "base-mod.yaml"
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        data["dependencies"] = ["dependent-mod"]
        p.write_text(yaml.dump(data), encoding="utf-8")

        cmd = [sys.executable, str(MODS_DIR / "runner.py"), "apply", "--mods-dir", str(temp_mods_dir)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        assert res.returncode == 1
        assert "обнаружен цикл зависимостей" in res.stderr
