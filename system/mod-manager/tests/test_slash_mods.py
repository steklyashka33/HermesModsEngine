"""Tests for mod-manager Telegram slash command (/mods, /mod)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
import pytest

MOD_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = MOD_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import handler


def test_manifest_files_exist():
    """Verify strict folder layout files and collocated i18n."""
    assert (MOD_DIR / "mod.yaml").is_file()
    assert (MOD_DIR / "targets.yaml").is_file()
    assert (MOD_DIR / "commands.yaml").is_file()
    assert (MOD_DIR / "i18n" / "ru.yaml").is_file()
    assert (MOD_DIR / "i18n" / "en.yaml").is_file()
    assert not (MOD_DIR / "locales").exists()
    assert (MOD_DIR / "scripts" / "handler.py").is_file()


def test_command_list_output():
    """1. /mods and /mods list: concise custom mods list + hidden system mods details with strike-through for inactive."""
    ctx = {"args": "", "mod_dir": str(MOD_DIR)}
    out = handler.handle(ctx)
    assert "Активные кастомные моды:" in out
    assert "<details><summary>Системные моды (Tier 0)</summary>" in out
    assert "</details>" in out
    assert "(активен)" not in out
    assert "(отключен)" not in out
    # Strike-through only expected if inactive mods exist
    if "<s>" in out:
        assert "</s>" in out
    assert "• " in out
    assert "Менеджер модов" in out

    # Check explicit 'list'
    ctx_list = {"args": "list", "mod_dir": str(MOD_DIR)}
    out_list = handler.handle(ctx_list)
    assert "<details><summary>Системные моды (Tier 0)</summary>" in out_list
    assert "(активен)" not in out_list
    assert "(отключен)" not in out_list
    if "<s>" in out_list:
        assert "</s>" in out_list


def test_command_info_output():
    """2. /mods info <mod_id>: detailed mod card (version, author, dependencies, targets, commands)."""
    # 2a. Info for custom mod with targets
    ctx = {"args": "info command-close", "mod_dir": str(MOD_DIR)}
    out = handler.handle(ctx)
    assert "Command Close" in out
    assert "Версия:" in out
    assert "Автор:" in out
    assert "Статус:" in out
    assert "Зависимости:" in out
    assert "Файлы / Таргеты" in out
    assert "Описание:" in out

    # 2b. Info for declarative mod with commands
    ctx_self = {"args": "info mod-manager", "mod_dir": str(MOD_DIR)}
    out_self = handler.handle(ctx_self)
    assert "Менеджер модов" in out_self
    assert "Команды" in out_self
    assert "/mods" in out_self
    assert "алиасы: /mod" in out_self

    # 2c. Direct mod_id invocation without 'info' keyword
    ctx_direct = {"args": "mod-manager", "mod_dir": str(MOD_DIR)}
    out_direct = handler.handle(ctx_direct)
    assert "Менеджер модов" in out_direct
    assert "Версия:" in out_direct

    # 2d. Missing mod_id
    ctx_missing = {"args": "info", "mod_dir": str(MOD_DIR)}
    out_missing = handler.handle(ctx_missing)
    assert "Использование: /mods info <mod_id>" in out_missing

    # 2e. Non-existent mod_id
    ctx_not_found = {"args": "info non-existent-mod-xyz", "mod_dir": str(MOD_DIR)}
    out_not_found = handler.handle(ctx_not_found)
    assert "не найден" in out_not_found


def test_command_status_output():
    """3. /mods status: ecosystem health summary (HEALTHY/DEGRADED/DISABLED, applied count, master killswitch)."""
    ctx = {"args": "status", "mod_dir": str(MOD_DIR)}
    out = handler.handle(ctx)
    assert "Экосистема модов Hermes:" in out
    assert any(s in out for s in ("HEALTHY", "DEGRADED", "DISABLED"))
    assert "Мастер-рубильник:" in out
    assert "mods.enabled: true" in out
    assert "Сводка модов:" in out
    assert "Применено и активно:" in out
    assert "Граф зависимостей:" in out
    assert "Хранилище секретов:" in out
    # Verify no pseudo-bullets in metric lines
    assert "• Всего зарегистрировано" not in out
    assert "• Применено и активно" not in out
    assert "• Отключено" not in out


def test_command_logs_output():
    """4. /mods logs <mod_id> [N]: logs under spoiler."""
    ctx = {"args": "logs command-close 10", "mod_dir": str(MOD_DIR)}
    out = handler.handle(ctx)
    assert ("Логи мода" in out and "<details><summary>" in out and "<tg-spoiler>" in out) or ("не найдено" in out)

    # Missing mod_id
    ctx_missing = {"args": "logs", "mod_dir": str(MOD_DIR)}
    out_missing = handler.handle(ctx_missing)
    assert "Использование: /mods logs <mod_id> [N]" in out_missing

    # Non-existent mod_id
    ctx_not_found = {"args": "logs non-existent-mod-xyz", "mod_dir": str(MOD_DIR)}
    out_not_found = handler.handle(ctx_not_found)
    assert "не найден" in out_not_found


def test_command_help_fallback():
    """Fallback to help menu on unrecognized subcommands."""
    ctx = {"args": "unknown_subcommand_test", "mod_dir": str(MOD_DIR)}
    out = handler.handle(ctx)
    assert "Управление и инспекция модов Hermes" in out
    assert "/mods info" in out
    assert "/mods status" in out
    assert "/mods logs" in out


def test_command_i18n_languages():
    """Verify 8-language localization resolution without hardcode."""
    for lang, expected_summary in [
        ("en", "System mods (Tier 0)"),
        ("ru", "Системные моды (Tier 0)"),
        ("es", "Mods del sistema (Tier 0)"),
        ("fr", "Mods système (Tier 0)"),
        ("de", "System-Mods (Tier 0)"),
        ("zh", "系统模组 (Tier 0)"),
        ("ar", "تعديلات النظام (Tier 0)"),
        ("pt", "Mods do sistema (Tier 0)"),
    ]:
        ctx = {"args": "list", "lang": lang, "mod_dir": str(MOD_DIR)}
        out = handler.handle(ctx)
        assert f"<details><summary>{expected_summary}</summary>" in out
        assert "(активен)" not in out
        assert "(отключен)" not in out


def test_registry_integration():
    """Verify mod-command-registry loads /mods and alias /mod from commands.yaml."""
    hermes_root = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
    if str(hermes_root) not in sys.path and hermes_root.is_dir():
        sys.path.insert(0, str(hermes_root))
    mods_dir = handler._resolve_mods_dir({"mod_dir": str(MOD_DIR)})
    handler._init_engine(mods_dir)
    from engine.loader import ModLoader, update_commands_cache
    try:
        from gateway.mod_commands import ModCommandRegistry
    except ModuleNotFoundError:
        from hermes_cli.commands import _get_mod_command_registry
        _get_mod_command_registry()
        from gateway.mod_commands import ModCommandRegistry

    mods = ModLoader.load_all(mods_dir)
    slash_mod = next((m for m in mods if m.id == "mod-manager"), None)
    assert slash_mod is not None
    assert len(slash_mod.commands) == 1
    cmd = slash_mod.commands[0]
    assert cmd.name == "mods"
    assert "mod" in cmd.aliases
    assert cmd.busy_policy == "dispatch"

    update_commands_cache(mods, mods_dir)
    reg = ModCommandRegistry(mods_dir)
    reg.refresh()

    assert reg.is_known_command("mods")
    assert reg.is_known_command("mod")
    assert reg.is_known_command("/mods")
    assert reg.is_known_command("/mod")

    cmd_meta = reg.get_command("mods")
    assert cmd_meta is not None
    assert cmd_meta["name"] == "mods"

    alias_meta = reg.get_command("mod")
    assert alias_meta is not None
    assert alias_meta["name"] == "mods"
