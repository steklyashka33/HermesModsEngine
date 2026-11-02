"""Tests for non-destructive mod config and secrets default population."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Dict

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

from engine.models import Mod, ConfigField
from engine.config_manager import ModConfigManager, SecretsStorage, CONFIG_FILE, SECRETS_FILE


@pytest.fixture
def temp_config_env(tmp_path: Path):
    """Provides isolated config.yaml and secrets.yaml paths with realistic Hermes structure."""
    cfg_path = tmp_path / "config.yaml"
    sec_path = tmp_path / "secrets.yaml"

    initial_config = """# Hermes System Configuration
# Authoritative Operator Settings
agent:
  max_turns: 80 # default max turns
  system_prompt: "You are Hermes Agent"
display:
  language: ru

# Mod settings below
mods:
  legacy_mod:
    # Important user comment on legacy setting
    existing_flag: true
    zero_val: 0
    null_val: null
"""
    cfg_path.write_text(initial_config, encoding="utf-8")
    return cfg_path, sec_path


def make_dummy_mod(
    mod_id: str = "test-mod",
    name: str = "Test Mod",
    config_fields: Dict[str, ConfigField] = None,
    enabled: bool = True,
) -> Mod:
    return Mod(
        name=name,
        targets=[],
        path=Path(f"/tmp/{mod_id}.yaml"),
        id=mod_id,
        enabled=enabled,
        config=config_fields or {},
    )


def test_populate_defaults_creates_missing_keys_with_comments(temp_config_env):
    cfg_path, sec_path = temp_config_env
    orig_content = cfg_path.read_text(encoding="utf-8")

    mod = make_dummy_mod(
        mod_id="demo-mod",
        name="Demo Mod",
        config_fields={
            "poll_interval": ConfigField(
                name="poll_interval",
                type="int",
                default=60,
                description="Polling interval in seconds for background watcher",
            ),
            "feature_enabled": ConfigField(
                name="feature_enabled",
                type="bool",
                default=False,
                description="Enable experimental feature",
            ),
            "allowed_hosts": ConfigField(
                name="allowed_hosts",
                type="list",
                default=["localhost", "127.0.0.1"],
                description="List of allowed hosts for inbound webhooks",
            ),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod])

    assert res["config_updated"] is True
    assert "demo-mod" in res["added_config"]
    assert set(res["added_config"]["demo-mod"]) == {"poll_interval", "feature_enabled", "allowed_hosts"}

    new_content = cfg_path.read_text(encoding="utf-8")

    # 1. Verify pre-existing content is completely preserved
    assert "# Hermes System Configuration" in new_content
    assert "# Authoritative Operator Settings" in new_content
    assert "max_turns: 80 # default max turns" in new_content
    assert "# Important user comment on legacy setting" in new_content
    assert "existing_flag: true" in new_content

    # 2. Verify new keys and their values
    data = yaml.safe_load(new_content)
    assert data["mods"]["demo-mod"]["poll_interval"] == 60
    assert data["mods"]["demo-mod"]["feature_enabled"] is False
    assert data["mods"]["demo-mod"]["allowed_hosts"] == ["localhost", "127.0.0.1"]

    # 3. Verify comments appear above the new keys
    assert "# Polling interval in seconds for background watcher\n    poll_interval: 60" in new_content
    assert "# Enable experimental feature\n    feature_enabled: false" in new_content
    assert "# List of allowed hosts for inbound webhooks\n    allowed_hosts:" in new_content


def test_populate_defaults_never_overwrites_existing_values(temp_config_env):
    cfg_path, sec_path = temp_config_env

    # legacy_mod already has existing_flag=true, zero_val=0, null_val=null
    mod = make_dummy_mod(
        mod_id="legacy_mod",
        name="Legacy Mod",
        config_fields={
            "existing_flag": ConfigField(
                name="existing_flag",
                type="bool",
                default=False,  # default is False, but user set True
                description="Default flag description",
            ),
            "zero_val": ConfigField(
                name="zero_val",
                type="int",
                default=100,  # default is 100, user set 0
                description="Zero val description",
            ),
            "null_val": ConfigField(
                name="null_val",
                type="string",
                default="fallback_string",  # user set null
                description="Null val description",
            ),
            "new_added_param": ConfigField(
                name="new_added_param",
                type="int",
                default=42,
                description="Newly added parameter",
            ),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod])

    # Only new_added_param should be added
    assert res["added_config"].get("legacy_mod") == ["new_added_param"]

    new_content = cfg_path.read_text(encoding="utf-8")
    data = yaml.safe_load(new_content)

    # User values must NOT be overwritten!
    assert data["mods"]["legacy_mod"]["existing_flag"] is True
    assert data["mods"]["legacy_mod"]["zero_val"] == 0
    assert data["mods"]["legacy_mod"]["null_val"] is None
    assert data["mods"]["legacy_mod"]["new_added_param"] == 42

    # User's comment on existing setting must be preserved
    assert "# Important user comment on legacy setting" in new_content


def test_sensitive_fields_never_enter_config_yaml_and_go_to_secrets_yaml(temp_config_env):
    cfg_path, sec_path = temp_config_env

    # Pre-populate secrets.yaml with an existing secret
    sec_path.write_text(
        yaml.safe_dump({"mods": {"sec-mod": {"existing_token": "user_secret_token"}}}),
        encoding="utf-8",
    )
    os.chmod(sec_path, 0o600)

    mod = make_dummy_mod(
        mod_id="sec-mod",
        name="Security Mod",
        config_fields={
            "public_host": ConfigField(
                name="public_host",
                type="string",
                default="api.example.com",
                sensitive=False,
                description="Public API host address",
            ),
            "existing_token": ConfigField(
                name="existing_token",
                type="string",
                default="default_should_not_overwrite",
                sensitive=True,
                description="Existing API token",
            ),
            "new_api_key": ConfigField(
                name="new_api_key",
                type="string",
                default="sk-test-default-key",
                sensitive=True,
                description="New API key",
            ),
            "empty_secret": ConfigField(
                name="empty_secret",
                type="string",
                default=None,
                sensitive=True,
                description="Secret with no default",
            ),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod])

    # Verify secrets were added
    assert res["secrets_updated"] is True
    assert set(res["added_secrets"]["sec-mod"]) == {"new_api_key", "empty_secret"}
    # existing_token must not be reported as added
    assert "existing_token" not in res["added_secrets"]["sec-mod"]

    # 1. Verify sensitive keys NEVER entered config.yaml
    cfg_content = cfg_path.read_text(encoding="utf-8")
    assert "new_api_key" not in cfg_content
    assert "empty_secret" not in cfg_content
    assert "existing_token" not in cfg_content
    assert "sk-test-default-key" not in cfg_content
    # public_host MUST be in config.yaml
    assert "public_host: api.example.com" in cfg_content

    # 2. Verify secrets.yaml contents and permissions
    assert sec_path.exists()
    sec_mode = stat.S_IMODE(sec_path.stat().st_mode)
    assert sec_mode == 0o600, f"Expected 0600 permissions, got {oct(sec_mode)}"

    sec_data = yaml.safe_load(sec_path.read_text(encoding="utf-8"))
    assert sec_data["mods"]["sec-mod"]["existing_token"] == "user_secret_token"
    assert sec_data["mods"]["sec-mod"]["new_api_key"] == "sk-test-default-key"
    assert sec_data["mods"]["sec-mod"]["empty_secret"] == ""


def test_comments_strictly_in_english_ignore_russian_translations(temp_config_env, monkeypatch):
    cfg_path, sec_path = temp_config_env

    # Set environment language to Russian to test strict override
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")

    mod = make_dummy_mod(
        mod_id="i18n-mod",
        name="I18n Mod",
        config_fields={
            "timeout_seconds": ConfigField(
                name="timeout_seconds",
                type="int",
                default=30,
                description="Timeout in seconds for upstream gateway requests",
                description_ru="Таймаут в секундах для запросов к удалённому шлюзу",
                i18n={
                    "ru": {
                        "description": "Таймаут в секундах для запросов к удалённому шлюзу",
                    }
                },
            ),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    mgr.populate_defaults([mod])

    cfg_content = cfg_path.read_text(encoding="utf-8")
    # Must contain English description
    assert "# Timeout in seconds for upstream gateway requests" in cfg_content
    # Must NOT contain Russian description
    assert "Таймаут в секундах" not in cfg_content


def test_idempotent_multiple_runs(temp_config_env):
    cfg_path, sec_path = temp_config_env

    mod = make_dummy_mod(
        mod_id="idempotent-mod",
        name="Idempotent Mod",
        config_fields={
            "retries": ConfigField(name="retries", type="int", default=3, description="Number of retries"),
            "token": ConfigField(name="token", type="string", default="sec-val", sensitive=True),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)

    # First run: should update both
    res1 = mgr.populate_defaults([mod])
    assert res1["config_updated"] is True
    assert res1["secrets_updated"] is True

    cfg_after_1 = cfg_path.read_text(encoding="utf-8")
    sec_after_1 = sec_path.read_text(encoding="utf-8")

    # Second run: should NOT update anything
    res2 = mgr.populate_defaults([mod])
    assert res2["config_updated"] is False
    assert res2["secrets_updated"] is False
    assert res2["added_config"] == {}
    assert res2["added_secrets"] == {}

    cfg_after_2 = cfg_path.read_text(encoding="utf-8")
    sec_after_2 = sec_path.read_text(encoding="utf-8")

    assert cfg_after_1 == cfg_after_2
    assert sec_after_1 == sec_after_2


def test_preserves_other_mods_settings_when_disabled_or_removed(temp_config_env):
    cfg_path, sec_path = temp_config_env

    # Existing config already has legacy_mod. We run populate_defaults with another mod only.
    mod = make_dummy_mod(
        mod_id="active-mod",
        name="Active Mod",
        config_fields={
            "active_opt": ConfigField(name="active_opt", type="string", default="active_val", description="Active option"),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    mgr.populate_defaults([mod])

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # legacy_mod must still be present!
    assert "legacy_mod" in data["mods"]
    assert data["mods"]["legacy_mod"]["existing_flag"] is True
    assert "active-mod" in data["mods"]
    assert data["mods"]["active-mod"]["active_opt"] == "active_val"


def test_populates_disabled_mods(temp_config_env):
    cfg_path, sec_path = temp_config_env

    mod = make_dummy_mod(
        mod_id="dormant-mod",
        name="Dormant Mod",
        enabled=False,  # Disabled mod!
        config_fields={
            "dormant_setting": ConfigField(
                name="dormant_setting",
                type="string",
                default="dormant_val",
                description="Setting for dormant mod",
            ),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod])

    assert "dormant-mod" in res["added_config"]
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["mods"]["dormant-mod"]["dormant_setting"] == "dormant_val"


def test_class_method_and_instance_method_signatures(temp_config_env):
    cfg_path, sec_path = temp_config_env

    mod = make_dummy_mod(
        mod_id="classmethod-mod",
        name="Classmethod Mod",
        config_fields={
            "cm_key": ConfigField(name="cm_key", type="int", default=777, description="CM key description"),
        },
    )

    # 1. Call via classmethod directly
    res_cls = ModConfigManager.populate_defaults([mod], config_path=cfg_path)
    assert res_cls["config_updated"] is True
    assert "classmethod-mod" in res_cls["added_config"]

    # 2. Call via instance method
    mod2 = make_dummy_mod(
        mod_id="instancemethod-mod",
        name="Instance Method Mod",
        config_fields={
            "im_key": ConfigField(name="im_key", type="int", default=888, description="IM key description"),
        },
    )
    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res_inst = mgr.populate_defaults([mod2])
    assert res_inst["config_updated"] is True
    assert "instancemethod-mod" in res_inst["added_config"]

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["mods"]["classmethod-mod"]["cm_key"] == 777
    assert data["mods"]["instancemethod-mod"]["im_key"] == 888


def test_cli_config_populate_command(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    sec_path = tmp_path / "secrets.yaml"
    cfg_path.write_text("agent:\n  max_turns: 80\n", encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_CONFIG_FILE"] = str(cfg_path)
    env["HERMES_SECRETS_FILE"] = str(sec_path)
    env["PYTHONPATH"] = f"{MODS_DIR}:{ENGINE_DIR}"

    # 1. Human-readable CLI execution
    res = subprocess.run(
        ["python3", str(MODS_DIR / "runner.py"), "config", "--populate"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "Автозаполнение настроек модов выполнено" in res.stdout

    # Verify config was populated
    content = cfg_path.read_text(encoding="utf-8")
    assert "mods:" in content

    # 2. JSON output CLI execution
    res_json = subprocess.run(
        ["python3", str(MODS_DIR / "runner.py"), "config", "--populate", "--json"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_json.returncode == 0
    import json
    data = json.loads(res_json.stdout)
    assert "config_updated" in data
    assert "secrets_updated" in data
    assert "added_config" in data
    assert "added_secrets" in data


def test_populate_single_mod_object(temp_config_env):
    cfg_path, sec_path = temp_config_env
    mod = make_dummy_mod(
        mod_id="single-mod",
        name="Single Mod",
        config_fields={
            "single_key": ConfigField(name="single_key", type="string", default="single_val", description="Single key"),
        },
    )
    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    # Pass single Mod object instead of list
    res = mgr.populate_defaults(mod)
    assert res["config_updated"] is True
    assert "single-mod" in res["added_config"]
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert data["mods"]["single-mod"]["single_key"] == "single_val"


def test_multiline_descriptions_and_clean_formatting(temp_config_env):
    cfg_path, sec_path = temp_config_env
    mod = make_dummy_mod(
        mod_id="multiline-mod",
        name="Multiline Mod",
        config_fields={
            "complex_opt": ConfigField(
                name="complex_opt",
                type="string",
                default="opt_value",
                description="# Pre-existing hash in desc\nSecond line explaining details\n# Third line with hash",
            ),
        },
    )
    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    mgr.populate_defaults([mod])

    content = cfg_path.read_text(encoding="utf-8")
    # Verify no double comments like "# # "
    assert "# # " not in content
    assert "# Pre-existing hash in desc" in content
    assert "# Second line explaining details" in content
    assert "# Third line with hash" in content


def test_apply_cli_auto_populates_config(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    sec_path = tmp_path / "secrets.yaml"
    cfg_path.write_text("agent:\n  max_turns: 80\n", encoding="utf-8")

    env = os.environ.copy()
    env["HERMES_CONFIG_FILE"] = str(cfg_path)
    env["HERMES_SECRETS_FILE"] = str(sec_path)
    env["PYTHONPATH"] = f"{MODS_DIR}:{ENGINE_DIR}"
    # HERMES_MOD_APPLY=1 triggers apply hook
    env["HERMES_MOD_APPLY"] = "1"

    # Invoke apply on a mod that has no targets or verify_cmd
    res = subprocess.run(
        ["python3", str(MODS_DIR / "runner.py"), "apply"],
        env=env,
        capture_output=True,
        text=True,
    )
    # Even if some mods fail target patching in isolated env, config must have been populated
    content = cfg_path.read_text(encoding="utf-8")
    assert "mods:" in content
    data = yaml.safe_load(content)
    assert isinstance(data.get("mods"), dict)


def test_mods_without_config_never_create_empty_sections(temp_config_env):
    cfg_path, sec_path = temp_config_env

    # 1. Mod with completely empty config dictionary (like command-faster)
    mod_no_cfg = make_dummy_mod(
        mod_id="command-faster",
        name="Command Faster",
        config_fields={},
    )
    # 2. Mod with config_fields as None (like command-close)
    mod_none_cfg = Mod(
        name="Command Close",
        targets=[],
        path=Path("/tmp/command-close.yaml"),
        id="command-close",
        enabled=True,
        config=None,
    )
    # 3. Mod with sensitive-only parameters (must go to secrets.yaml, not config.yaml)
    mod_sensitive_only = make_dummy_mod(
        mod_id="secret-only-mod",
        name="Secret Only Mod",
        config_fields={
            "api_key": ConfigField(name="api_key", type="string", default="sk-12345", sensitive=True, description="Secret token"),
        },
    )
    # 4. Mod with actual non-sensitive settings
    mod_with_cfg = make_dummy_mod(
        mod_id="real-mod",
        name="Real Mod",
        config_fields={
            "real_param": ConfigField(name="real_param", type="string", default="valid_value", sensitive=False, description="Real parameter"),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod_no_cfg, mod_none_cfg, mod_sensitive_only, mod_with_cfg])

    # Only real-mod should be in added_config
    assert "real-mod" in res["added_config"]
    assert "command-faster" not in res["added_config"]
    assert "command-close" not in res["added_config"]
    assert "secret-only-mod" not in res["added_config"]

    # secret-only-mod should be in added_secrets
    assert "secret-only-mod" in res["added_secrets"]

    raw_content = cfg_path.read_text(encoding="utf-8")
    # Categorically forbidden empty blocks: mod-name: {}
    assert "command-faster" not in raw_content
    assert "command-close" not in raw_content
    assert "secret-only-mod" not in raw_content
    assert "{}" not in raw_content

    data = yaml.safe_load(raw_content)
    assert "real-mod" in data["mods"]
    assert data["mods"]["real-mod"]["real_param"] == "valid_value"
    assert "command-faster" not in data["mods"]
    assert "command-close" not in data["mods"]
    assert "secret-only-mod" not in data["mods"]


def test_populate_defaults_cleans_preexisting_empty_mod_sections(temp_config_env):
    cfg_path, sec_path = temp_config_env

    # Pre-populate config with dirty empty mod sections
    dirty_config = """# Hermes System Configuration
agent:
  max_turns: 80
mods:
  command-faster: {}
  command-close: {}
  enabled: true
  strict_folder_layout: false
  legacy_mod:
    existing_flag: true
"""
    cfg_path.write_text(dirty_config, encoding="utf-8")

    mod_no_cfg = make_dummy_mod(mod_id="command-faster", name="Command Faster", config_fields={})
    mod_legacy = make_dummy_mod(
        mod_id="legacy_mod",
        name="Legacy Mod",
        config_fields={
            "existing_flag": ConfigField(name="existing_flag", type="bool", default=False),
        },
    )

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod_no_cfg, mod_legacy])

    assert res["config_updated"] is True
    # No new config keys added because existing_flag is already present
    assert res["added_config"] == {}

    cleaned_content = cfg_path.read_text(encoding="utf-8")
    # Empty mod sections must be removed
    assert "command-faster" not in cleaned_content
    assert "command-close" not in cleaned_content
    assert "{}" not in cleaned_content

    # System flags and valid mod settings must be strictly preserved
    data = yaml.safe_load(cleaned_content)
    assert data["mods"]["enabled"] is True
    assert data["mods"]["strict_folder_layout"] is False
    assert data["mods"]["legacy_mod"]["existing_flag"] is True
    assert "command-faster" not in data["mods"]
    assert "command-close" not in data["mods"]


def test_populate_defaults_with_only_no_config_mods_leaves_config_untouched(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    sec_path = tmp_path / "secrets.yaml"
    initial_content = "agent:\n  max_turns: 80\n"
    cfg_path.write_text(initial_content, encoding="utf-8")

    mod_faster = make_dummy_mod(mod_id="command-faster", name="Command Faster", config_fields={})
    mod_close = make_dummy_mod(mod_id="command-close", name="Command Close", config_fields={})

    mgr = ModConfigManager(config_path=cfg_path, secrets_path=sec_path)
    res = mgr.populate_defaults([mod_faster, mod_close])

    assert res["config_updated"] is False
    assert res["secrets_updated"] is False
    assert res["added_config"] == {}
    assert res["added_secrets"] == {}
    assert cfg_path.read_text(encoding="utf-8") == initial_content

