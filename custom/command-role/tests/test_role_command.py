"""Tests for /role command: uncompressible session instructions, role injection and persistence."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source(thread_id=None, profile=None):
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-42",
        user_id="user-42",
        chat_type="dm",
        thread_id=thread_id,
        profile=profile,
    )


def _entry(source, sid):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id=sid,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=source.platform,
        chat_type=source.chat_type,
        origin=source,
    )


def test_role_command_registry_and_aliases():
    """Verify CommandDef registration, idle command list, and control classification."""
    from hermes_cli.commands import COMMAND_REGISTRY, resolve_command
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.session_commands import _CONTROL_COMMANDS, command_verdict

    cmd = resolve_command("role")
    assert cmd is not None, "/role must resolve in registry"
    assert cmd.name == "role"
    assert cmd.aliases == ("instruct",), "Only 'instruct' alias must be allowed for /role"

    # Alias /instruct resolves to /role CommandDef
    alias_cmd = resolve_command("instruct")
    assert alias_cmd is not None
    assert alias_cmd.name == "role"

    # Other aliases are strictly not allowed
    for forbidden in ("persona", "guidance", "роль"):
        resolved = resolve_command(forbidden)
        if resolved is not None:
            assert resolved.name != "role", f"Alias '{forbidden}' must not resolve to role"

    # Check idle commands in gateway
    assert "role" in GatewayBusySessionMixin._IDLE_COMMANDS

    # Check session commands classification
    assert "role" in _CONTROL_COMMANDS
    assert command_verdict("role", "") == "control"


@pytest.mark.asyncio
async def test_role_command_set_add_clear_status():
    """Verify /role status, set, add, and clear operations."""
    from gateway.slash_commands_session import GatewaySessionCommandsMixin

    class DummyRunner(GatewaySessionCommandsMixin):
        def __init__(self, store, source):
            self.session_store = store
            self._source = source
            self._cached_agent = SimpleNamespace(_session_role="")

        def _session_key_for_source(self, src):
            return build_session_key(src)

        def _cached_agent_for(self, key):
            return self._cached_agent

    source = _source()
    session_key = build_session_key(source)

    metadata = {}
    store = Mock()
    store.get_session_metadata = Mock(side_effect=lambda k, field, default="": metadata.get((k, field), default))
    def _set_meta(k, field, val):
        metadata[(k, field)] = val
        return True
    store.set_session_metadata = Mock(side_effect=_set_meta)

    runner = DummyRunner(store, source)

    # 1. Status when no role set
    res = await runner._handle_role_command(MessageEvent(text="/role", source=source))
    assert "роль не задана" in res
    assert "/role set <текст>" in res

    res_stat = await runner._handle_role_command(MessageEvent(text="/role status", source=source))
    assert "роль не задана" in res_stat

    # 2. Set role via `/role set <text>`
    res_set = await runner._handle_role_command(
        MessageEvent(text="/role set Ты опытный архитектор на Python", source=source)
    )
    assert res_set == "✅ **Роль текущей сессии установлена.**"
    assert "Ты опытный архитектор на Python" not in res_set
    assert store.get_session_metadata(session_key, "session_role") == "Ты опытный архитектор на Python"
    assert runner._cached_agent._session_role == "Ты опытный архитектор на Python"

    # 3. Status when role is set
    res_curr = await runner._handle_role_command(MessageEvent(text="/role", source=source))
    assert "Установка и роль текущей сессии" in res_curr
    assert "**> Ты опытный архитектор на Python||" in res_curr

    # 4. Add rule via `/role add <text>`
    res_add = await runner._handle_role_command(
        MessageEvent(text="/role add Всегда пиши строгие type hints", source=source)
    )
    assert "📝 **Правило добавлено к роли сессии:**" in res_add
    assert "➕" not in res_add
    expected_role = "Ты опытный архитектор на Python\nВсегда пиши строгие type hints"
    expected_quoted = "**> Ты опытный архитектор на Python\n> Всегда пиши строгие type hints||"
    assert expected_quoted in res_add
    assert store.get_session_metadata(session_key, "session_role") == expected_role
    assert runner._cached_agent._session_role == expected_role

    # 5. Direct prompt without `set` keyword
    res_direct = await runner._handle_role_command(
        MessageEvent(text="/role Ты эксперт по базам данных", source=source)
    )
    assert res_direct == "✅ **Роль текущей сессии установлена.**"
    assert "Ты эксперт по базам данных" not in res_direct
    assert store.get_session_metadata(session_key, "session_role") == "Ты эксперт по базам данных"

    # 6. Clear role via `/role clear`
    res_clear = await runner._handle_role_command(MessageEvent(text="/role clear", source=source))
    assert "очищена" in res_clear
    assert store.get_session_metadata(session_key, "session_role") == ""
    assert runner._cached_agent._session_role == ""

    # Status after clear
    res_after = await runner._handle_role_command(MessageEvent(text="/role", source=source))
    assert "роль не задана" in res_after

    # 7. Validation for empty set/add
    res_empty_set = await runner._handle_role_command(MessageEvent(text="/role set", source=source))
    assert "Укажите текст роли" in res_empty_set

    res_empty_add = await runner._handle_role_command(MessageEvent(text="/role add", source=source))
    assert "Укажите текст для добавления" in res_empty_add

    # 8. Multiline role hidden blockquote in status
    await runner._handle_role_command(
        MessageEvent(text="/role set Строка 1\nСтрока 2\n\nСтрока 3", source=source)
    )
    res_multi = await runner._handle_role_command(MessageEvent(text="/role status", source=source))
    expected_multi_quote = "**> Строка 1\n> Строка 2\n>\n> Строка 3||"
    assert expected_multi_quote in res_multi


def test_role_prompt_injected_in_combined_ephemeral_prompt():
    """Verify that role is dynamically injected into TurnRunner's combined ephemeral prompt."""
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    source = _source()
    session_key = build_session_key(source)

    # Mock runner and session store
    runner = Mock()
    runner._get_system_prompt_for_channel = Mock(return_value="")
    store = Mock()
    runner.session_store = store

    ctx = TurnContext(
        source=source,
        session_key=session_key,
        session_id="session-test-sid",
        context_prompt="Base platform context",
        channel_prompt="",
        user_config={},
        run_generation=1,
    )

    tr = object.__new__(TurnRunner)
    tr._runner = runner
    tr._ctx = ctx

    # Case 1: no role set
    store.get_session_metadata = Mock(return_value=None)
    prompt1 = tr._combined_ephemeral_prompt()
    assert "Base platform context" in prompt1
    assert "# УСТАНОВКА И РОЛЬ ТЕКУЩЕЙ СЕССИИ" not in prompt1

    # Case 2: role set
    store.get_session_metadata = Mock(return_value="Соблюдай строгий zero-trust аудит")
    prompt2 = tr._combined_ephemeral_prompt()
    assert "Base platform context" in prompt2
    assert "# УСТАНОВКА И РОЛЬ ТЕКУЩЕЙ СЕССИИ:" in prompt2
    assert "Соблюдай строгий zero-trust аудит" in prompt2


def test_role_preserves_across_handoff(tmp_path, monkeypatch):
    """Verify that session_role is preserved across /handoff and session reset."""
    import hermes_state
    from gateway.session import SessionStore

    monkeypatch.setattr(hermes_state, "SessionDB", Mock(side_effect=RuntimeError("SQLite disabled in test")))
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._loaded = True

    source = _source()
    entry = store.get_or_create_session(source)
    key = entry.session_key

    # Set role in metadata
    store.set_session_metadata(key, "session_role", "Ведущий ML инженер")
    assert store.get_session_metadata(key, "session_role") == "Ведущий ML инженер"

    # Reset session (as in /handoff)
    new_entry = store.reset_session(key)
    assert new_entry is not None
    assert new_entry.session_id != entry.session_id
    # assert new_entry.metadata.get("session_role") == "Ведущий ML инженер"
    assert store.get_session_metadata(key, "session_role") == "Ведущий ML инженер"


def test_tui_live_role_output():
    """Verify desktop/TUI live slash handler for /role."""
    from tui_gateway.methods_slash import _format_live_role_output, _LIVE_SLASH_OUTPUT

    assert "role" in _LIVE_SLASH_OUTPUT
    assert _LIVE_SLASH_OUTPUT["role"][1] == _format_live_role_output

    session = {}
    # Status empty
    out = _format_live_role_output("sid1", session, "")
    assert "роль не задана" in out

    # Set
    out = _format_live_role_output("sid1", session, "set Test Role")
    assert out == "✅ **Роль текущей сессии установлена.**"
    assert "Test Role" not in out
    assert session.get("session_role") == "Test Role"

    # Status populated
    out = _format_live_role_output("sid1", session, "")
    assert "**> Test Role||" in out

    # Add
    out = _format_live_role_output("sid1", session, "add Extra Line")
    assert "📝 **Правило добавлено к роли сессии:**" in out
    assert "➕" not in out
    assert "**> Test Role\n> Extra Line||" in out
    assert session.get("session_role") == "Test Role\nExtra Line"

    # Status populated with multiline
    out_multi = _format_live_role_output("sid1", session, "")
    assert "**> Test Role\n> Extra Line||" in out_multi

    # Clear
    out = _format_live_role_output("sid1", session, "clear")
    assert "очищена" in out
    assert session.get("session_role") == ""
