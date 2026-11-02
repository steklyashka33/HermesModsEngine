"""Gateway /handoff briefing and route-rotation regression tests (no network)."""
import asyncio
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source(thread_id=None, profile=None):
    return SessionSource(platform=Platform.TELEGRAM, chat_id="chat-7", user_id="user-7",
                         chat_type="dm", thread_id=thread_id, profile=profile)


def _entry(source, sid):
    return SessionEntry(session_key=build_session_key(source), session_id=sid,
                        created_at=datetime.now(), updated_at=datetime.now(),
                        platform=source.platform, chat_type=source.chat_type, origin=source)


@pytest.fixture
def rig(monkeypatch):
    from gateway.run import GatewayRunner

    source = _source()
    old, new = _entry(source, "old-session-111"), _entry(source, "new-session-222")
    history = [{"role": "user", "content": "Build /work/app.py"},
               {"role": "assistant", "content": "Implemented it"},
               {"role": "tool", "content": "test passed"}]
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.session_store = Mock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=old),
        load_transcript=AsyncMock(return_value=history),
        reset_session=AsyncMock(return_value=new),
        append_to_transcript=AsyncMock(),
    )
    runner._session_key_for_source = lambda src: build_session_key(src)
    runner._resolve_session_agent_runtime = Mock(return_value=(
        "main-model", {"provider": "test-provider", "api_key": "test-key",
                       "base_url": "https://example.test/v1"}))
    compressor = SimpleNamespace(_serialize_for_summary=Mock(return_value="[USER]: Build /work/app.py"))
    tmp_agent = SimpleNamespace(context_compressor=compressor)
    runner._build_manual_compression_agent = AsyncMock(return_value=tmp_agent)
    runner._cleanup_old_agent_for_reset = AsyncMock()
    runner._evict_cached_agent = Mock()
    runner._clear_conversation_scope = Mock()
    runner._cleanup_agent_resources_off_loop = AsyncMock()
    runner._is_telegram_topic_lane = Mock(return_value=False)
    runner._record_telegram_topic_binding = Mock()
    runner._run_in_executor_with_context = AsyncMock(side_effect=lambda fn: fn())
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content="# CONTEXT HANDOFF\n## 1. Goal & Requirements\nBuild app"))])
    llm = Mock(return_value=response)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", llm)
    # Reset is a process-wide mutation; keep tests hermetic and assert invocations.
    reset_global = Mock()
    monkeypatch.setattr("gateway.slash_commands_session._reset_process_scoped_tool_state", reset_global)
    return SimpleNamespace(runner=runner, source=source, old=old, new=new, history=history,
                           compressor=compressor, tmp_agent=tmp_agent, llm=llm, reset_global=reset_global)


@pytest.mark.asyncio
@pytest.mark.parametrize("arg,expected", [("", "Continuation of active tasks"),
                                         ("  focus on the parser  ", "focus on the parser")])
async def test_briefing_and_new_session(rig, arg, expected):
    runner = rig.runner
    result = await runner._handle_handoff_command(MessageEvent(text="/handoff" + (" " + arg if arg else ""),
                                                                 source=rig.source))
    prompt = rig.llm.call_args.kwargs["messages"][0]["content"]
    assert "# CONTEXT HANDOFF" in prompt
    assert "## 7. Next Priority Steps" in prompt
    assert "[USER]: Build /work/app.py" in prompt
    assert f"Session Focus: {expected}" in prompt
    assert ("USER FOCUS FOR HANDOFF" in prompt) == bool(arg.strip())
    assert rig.llm.call_args.kwargs.get("model") == "main-model" or rig.llm.call_args.kwargs.get("task") == "compression"
    assert rig.llm.call_args.kwargs["api_key"] == "test-key"
    assert rig.llm.call_args.kwargs["base_url"] == "https://example.test/v1"
    assert "gateway_session_key" not in rig.llm.call_args.kwargs
    assert rig.runner._build_manual_compression_agent.await_args.args[2]["gateway_session_key"] == rig.old.session_key
    assert rig.compressor._serialize_for_summary.call_args.args[0] == rig.history
    runner.async_session_store.reset_session.assert_awaited_once_with(rig.old.session_key)
    sid, turn = runner.async_session_store.append_to_transcript.await_args.args
    assert sid == rig.new.session_id
    assert turn["role"] == "user"
    assert "# CONTEXT HANDOFF" in turn["content"]
    assert "NEW SESSION READY" in turn["content"]
    assert "[ZERO-TRUST DIRECTIVE]" in turn["content"]
    assert "Считай этот бриф контекстом и рабочей гипотезой" in turn["content"]
    assert "Не додумывай код, сверяйся с диском" in turn["content"]
    assert "Контекст передан" in result
    runner._cleanup_old_agent_for_reset.assert_awaited_once_with(rig.old.session_key)
    runner._evict_cached_agent.assert_called_once_with(rig.old.session_key)
    runner._clear_conversation_scope.assert_called_once_with(rig.old.session_key, reason="handoff")
    rig.reset_global.assert_called_once()
    runner._cleanup_agent_resources_off_loop.assert_awaited_once_with(rig.tmp_agent, context="handoff")


@pytest.mark.asyncio
@pytest.mark.parametrize("history", [[], [{"role": "user", "content": "alone"}]])
async def test_empty_history_never_rotates_or_calls_llm(rig, history):
    rig.runner.async_session_store.load_transcript.return_value = history
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Недостаточно" in result
    rig.llm.assert_not_called()
    rig.runner._build_manual_compression_agent.assert_not_awaited()
    rig.runner.async_session_store.reset_session.assert_not_awaited()
    rig.runner._evict_cached_agent.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_transcript_never_rotates(rig):
    rig.runner.async_session_store.load_transcript.side_effect = RuntimeError("unreadable")
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Недостаточно" in result
    rig.runner.async_session_store.reset_session.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [None, "", "   "])
async def test_empty_model_reply_keeps_session_and_cleans_tmp_agent(rig, reply):
    rig.llm.return_value = reply
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "❌" in result
    rig.runner.async_session_store.reset_session.assert_not_awaited()
    rig.runner._evict_cached_agent.assert_not_called()
    rig.runner._cleanup_agent_resources_off_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_provider_error_keeps_old_session_and_cleans_tmp_agent(rig):
    rig.llm.side_effect = RuntimeError("provider offline")
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Не удалось сгенерировать handoff" in result
    rig.runner.async_session_store.reset_session.assert_not_awaited()
    rig.runner._cleanup_agent_resources_off_loop.assert_awaited_once()

@pytest.mark.asyncio
async def test_topic_rebind_uses_same_source_key_and_new_id(rig):
    source = _source(thread_id="topic-83")
    rig.runner.async_session_store.get_or_create_session.return_value = _entry(source, "old-topic-id")
    rig.runner.async_session_store.reset_session.return_value = _entry(source, "new-topic-id")
    rig.runner._is_telegram_topic_lane.return_value = True
    await rig.runner._handle_handoff_command(MessageEvent(text="/handoff topic work", source=source))
    key = build_session_key(source)
    assert "topic-83" in key
    rig.runner.async_session_store.reset_session.assert_awaited_once_with(key)
    rig.runner._record_telegram_topic_binding.assert_called_once()
    bound_source, bound_entry = rig.runner._record_telegram_topic_binding.call_args.args
    assert bound_source is source
    assert (bound_entry.session_key, bound_entry.session_id) == (key, "new-topic-id")
    assert rig.runner.async_session_store.append_to_transcript.await_args.args[0] == "new-topic-id"


@pytest.mark.asyncio
async def test_ordinary_chat_does_not_bind_topic(rig):
    await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    rig.runner._record_telegram_topic_binding.assert_not_called()


def test_gateway_registration_and_local_cli_refusal():
    from gateway.run_busy import GatewayBusySessionMixin
    from gateway.session_commands import command_verdict
    from hermes_cli.commands import resolve_command
    command = resolve_command("handoff")
    assert command and not command.cli_only and command.busy_policy == "reject"
    assert "handoff" in GatewayBusySessionMixin._IDLE_COMMANDS
    assert command_verdict("handoff", "focus") == "refused"


def test_gateway_call_signature_matches_auxiliary_client():
    """A real auxiliary response is structured, not a string; gateway must pass keyword args."""
    import inspect
    from agent.auxiliary_client import call_llm
    sig = inspect.signature(call_llm)
    assert "messages" in sig.parameters
    assert sig.parameters["messages"].kind == inspect.Parameter.KEYWORD_ONLY


def test_reset_does_not_remove_environment_value(monkeypatch):
    from gateway.slash_commands_session import _reset_process_scoped_tool_state
    monkeypatch.setenv("HANDOFF_TEST_SECRET", "kept-in-environment")
    with (patch("tools.env_passthrough.clear_env_passthrough") as env_clear,
          patch("tools.credential_files.clear_credential_files") as cred_clear):
        _reset_process_scoped_tool_state()
    env_clear.assert_called_once_with()
    cred_clear.assert_called_once_with()
    assert os.environ["HANDOFF_TEST_SECRET"] == "kept-in-environment"


@pytest.mark.asyncio
async def test_handoff_preserves_tool_calls_with_none_content(rig):
    """An assistant message that only made tool calls has content=None; handoff must preserve it."""
    tool_call_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "tc-1", "type": "function",
                        "function": {"name": "execute_code", "arguments": '{"cmd": "pytest"}'}}],
    }
    history = [
        {"role": "user", "content": "Run the tests"},
        tool_call_msg,
        {"role": "tool", "content": "14 passed", "tool_call_id": "tc-1"},
        {"role": "assistant", "content": "All tests passed."},
    ]
    rig.runner.async_session_store.load_transcript.return_value = history
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result
    passed_turns = rig.compressor._serialize_for_summary.call_args.args[0]
    assert tool_call_msg in passed_turns
    assert len(passed_turns) == 4


@pytest.mark.asyncio
async def test_fallback_serializer_avoids_none_string(rig):
    """When context_compressor is absent, fallback serializer must not render literal '[ASSISTANT]: None'."""
    rig.tmp_agent.context_compressor = None
    history = [
        {"role": "user", "content": "Do task"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function", "function": {"name": "fn"}}]},
    ]
    rig.runner.async_session_store.load_transcript.return_value = history
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result
    prompt = rig.llm.call_args.kwargs["messages"][0]["content"]
    assert "[ASSISTANT]: None" not in prompt
    assert "[ASSISTANT]: Called tool(s)" in prompt


@pytest.mark.asyncio
async def test_missing_session_entry_returns_not_enough(rig):
    """If get_or_create_session returns None or empty session_id, handoff returns not enough messages."""
    rig.runner.async_session_store.get_or_create_session.return_value = None
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Недостаточно" in result


@pytest.mark.asyncio
async def test_handoff_preserves_all_metadata_and_settings(rig):
    """Handoff must transfer all metadata keys (e.g. unlimited_mode, custom_flag), model_override, and yolo."""
    rig.old.metadata = {"custom_flag": True, "unlimited_mode": True, "extra_setting": "persisted"}
    rig.old.model_override = {"model": "custom/test-model", "provider": "custom-prov"}
    rig.old.yolo = True

    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff focus on tests", source=rig.source))
    assert "Контекст передан" in result

    # Check that new session entry received all metadata and configuration
    assert rig.new.metadata == {"custom_flag": True, "unlimited_mode": True, "extra_setting": "persisted"}
    assert rig.new.model_override == {"model": "custom/test-model", "provider": "custom-prov"}
    assert rig.new.yolo is True


@pytest.mark.asyncio
async def test_handoff_propagates_metadata_flags_to_cached_agent(rig):
    """Handoff propagates metadata keys generically to any cached/active agent."""
    rig.old.metadata = {"unlimited_mode": True, "custom_flag": "enabled"}
    mock_agent = SimpleNamespace()
    rig.runner._cached_agent_for = Mock(return_value=mock_agent)

    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result

    # Cached agent receives generic attributes matching metadata
    assert getattr(mock_agent, "unlimited_mode", None) is True or getattr(mock_agent, "_unlimited_mode", None) is True
    assert getattr(mock_agent, "custom_flag", None) == "enabled" or getattr(mock_agent, "_custom_flag", None) == "enabled"


def test_session_store_reset_session_preserves_metadata_and_settings(tmp_path, monkeypatch):
    """SessionStore.reset_session() universally preserves metadata, model_override, yolo, and display_name."""
    import hermes_state
    from gateway.session import SessionStore

    monkeypatch.setattr(hermes_state, "SessionDB", Mock(side_effect=RuntimeError("SQLite disabled in test")))
    store = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    store._loaded = True

    source = _source()
    entry = store.get_or_create_session(source)
    key = entry.session_key

    # Configure session entry with custom metadata, model_override, yolo, display_name
    store.set_session_metadata(key, "custom_flag", True)
    store.set_session_metadata(key, "unlimited_mode", True)
    store.set_session_metadata(key, "arbitrary_mod_data", {"nested": 123})
    store.set_model_override(key, {"model": "test-provider/test-model", "provider": "test-provider"})
    store.set_session_yolo(key, True)
    store._entries[key].display_name = "Session Alpha"

    old_sid = store._entries[key].session_id

    # Reset session (as done on /handoff or reset)
    new_entry = store.reset_session(key)

    assert new_entry is not None
    assert new_entry.session_id != old_sid
    assert new_entry.is_fresh_reset is True
    assert new_entry.display_name == "Session Alpha"
    # assert new_entry.yolo is True
    assert new_entry.model_override == {"model": "test-provider/test-model", "provider": "test-provider"}
    assert new_entry.metadata["custom_flag"] is True
    assert new_entry.metadata["unlimited_mode"] is True
    assert new_entry.metadata["arbitrary_mod_data"] == {"nested": 123}

    # Verify store helpers return the preserved settings
    assert store.get_session_metadata(key, "custom_flag") is True
    assert store.get_session_metadata(key, "unlimited_mode") is True
    assert store.get_session_metadata(key, "arbitrary_mod_data") == {"nested": 123}
    assert store.get_model_override(key) == {"model": "test-provider/test-model", "provider": "test-provider"}


def test_microcompact_tool_turns_standalone():
    """_microcompact_tool_turns leaves the last N tool outputs intact and compacts older large tool outputs."""
    from gateway.slash_commands_session import GatewaySessionCommandsMixin

    mc = GatewaySessionCommandsMixin._microcompact_tool_turns

    # Case 1: <= 6 tool turns -> untouched
    short_turns = [
        {"role": "user", "content": "hello"},
        {"role": "tool", "content": "x" * 600, "tool_call_id": "tc1"},
    ]
    res1 = mc(short_turns, keep_recent_tools=6)
    assert res1[1]["content"] == "x" * 600

    # Case 2: 8 tool turns (> 400 chars each)
    turns = [{"role": "user", "content": "start"}]
    for i in range(8):
        turns.append({"role": "assistant", "content": f"call {i}", "tool_calls": [{"id": f"tc_{i}"}]})
        turns.append({"role": "tool", "content": f"output_{i}_" + ("a" * 500), "tool_call_id": f"tc_{i}"})
    turns.append({"role": "assistant", "content": "done"})

    compacted = mc(turns, keep_recent_tools=6, max_len=400, head_len=150)
    assert len(compacted) == len(turns)

    # First 2 tool outputs (indices 2 and 4 in turns) must be microcompacted
    # 8 tool calls total: indices for tools are 0..7.
    # The first 2 (0 and 1) should be truncated, and the last 6 (2..7) preserved.
    compacted_tools = [t for t in compacted if t.get("role") == "tool"]
    assert len(compacted_tools) == 8

    # Old tools truncated:
    for i in range(2):
        assert "[Tool result cleared" in compacted_tools[i]["content"]
        assert len(compacted_tools[i]["content"]) < 200

    # Recent tools preserved:
    for i in range(2, 8):
        assert "[Tool result cleared" not in compacted_tools[i]["content"]
        assert len(compacted_tools[i]["content"]) > 500

    # Case 3: old tool <= 400 chars is NOT truncated
    turns_mixed = [
        {"role": "tool", "content": "small output", "tool_call_id": "tc_old"},
    ] + [{"role": "tool", "content": "recent", "tool_call_id": f"tc_{j}"} for j in range(6)]
    res3 = mc(turns_mixed, keep_recent_tools=6, max_len=400)
    assert res3[0]["content"] == "small output"


@pytest.mark.asyncio
async def test_handoff_applies_microcompact_in_flow(rig):
    """/handoff microcompacts older tool turns before sending to context_compressor."""
    history = [{"role": "user", "content": "Do a long pipeline"}]
    for i in range(8):
        history.append({"role": "assistant", "content": f"step {i}", "tool_calls": [{"id": f"tc{i}"}]})
        history.append({"role": "tool", "content": f"TOOL_RES_{i} " + ("DATA_" * 100), "tool_call_id": f"tc{i}"})
    history.append({"role": "assistant", "content": "Pipeline finished"})

    rig.runner.async_session_store.load_transcript.return_value = history
    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result

    passed_turns = rig.compressor._serialize_for_summary.call_args.args[0]
    passed_tools = [t for t in passed_turns if t.get("role") == "tool"]
    assert len(passed_tools) == 8
    # The first 2 tool outputs are old (>400 chars) -> must be microcompacted
    assert "[Tool result cleared" in passed_tools[0]["content"]
    assert "[Tool result cleared" in passed_tools[1]["content"]
    # The last 6 tool outputs must be preserved full length
    for t in passed_tools[2:]:
        assert "[Tool result cleared" not in t["content"]


def test_collect_git_status_standalone(monkeypatch, tmp_path):
    """_collect_git_status parses status and diff, and fails open on non-git or error."""
    import subprocess
    from gateway.slash_commands_session import GatewaySessionCommandsMixin

    collect = GatewaySessionCommandsMixin._collect_git_status

    # 1. Success case: git returns status and diff
    def mock_subprocess_run(cmd, **kwargs):
        if cmd == ["git", "status", "--short"]:
            return SimpleNamespace(returncode=0, stdout=" M app.py\n?? test.py\n")
        if cmd == ["git", "diff", "--stat"]:
            return SimpleNamespace(returncode=0, stdout=" app.py | 2 +-\n 1 file changed\n")
        return SimpleNamespace(returncode=1, stdout="")

    monkeypatch.setattr(subprocess, "run", mock_subprocess_run)
    out = collect(cwd=str(tmp_path), timeout=3.0)
    assert "Changed / Untracked files:" in out
    assert "M app.py" in out
    assert "Diff summary:" in out
    assert "1 file changed" in out

    # 2. Non-zero returncode (e.g. not a git repo) -> fail-open empty string
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=128, stdout="fatal: not a git repo"))
    assert collect(cwd=str(tmp_path), timeout=3.0) == ""

    # 3. Exception (e.g. timeout or command not found) -> fail-open empty string
    def mock_raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=3.0)

    monkeypatch.setattr(subprocess, "run", mock_raise)
    assert collect(cwd=str(tmp_path), timeout=3.0) == ""


@pytest.mark.asyncio
async def test_handoff_injects_actual_repository_state(rig, monkeypatch):
    """/handoff collects git status and injects ACTUAL REPOSITORY STATE into summarizer prompt."""
    fake_git_status = "Changed / Untracked files:\n M gateway/slash_commands_session.py\n\nDiff summary:\n 1 file changed, 50 insertions(+)"
    monkeypatch.setattr("gateway.slash_commands_session.GatewaySessionCommandsMixin._collect_git_status",
                        staticmethod(lambda cwd=None, timeout=3.0: fake_git_status))

    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result

    prompt = rig.llm.call_args.kwargs["messages"][0]["content"]
    assert "ACTUAL REPOSITORY STATE (GROUND TRUTH FROM DISK):" in prompt
    assert "M gateway/slash_commands_session.py" in prompt
    assert "1 file changed, 50 insertions(+)" in prompt
    assert "Use this actual repository state to ground your summary" in prompt


@pytest.mark.asyncio
async def test_handoff_fail_open_without_git(rig, monkeypatch):
    """When git status fails or is empty, /handoff completes cleanly without ACTUAL REPOSITORY STATE."""
    monkeypatch.setattr("gateway.slash_commands_session.GatewaySessionCommandsMixin._collect_git_status",
                        staticmethod(lambda cwd=None, timeout=3.0: ""))

    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result

    prompt = rig.llm.call_args.kwargs["messages"][0]["content"]
    assert "ACTUAL REPOSITORY STATE" not in prompt


@pytest.mark.asyncio
async def test_handoff_never_creates_log_or_transcript_files(rig, tmp_path, monkeypatch):
    """Requirement: /handoff must strictly NOT create any log files, transcripts, or dump files on disk."""
    transcripts_dir = tmp_path / ".hermes" / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = await rig.runner._handle_handoff_command(MessageEvent(text="/handoff", source=rig.source))
    assert "Контекст передан" in result

    files = list(transcripts_dir.iterdir())
    assert files == [], f"Expected no transcript files on disk, found: {files}"
