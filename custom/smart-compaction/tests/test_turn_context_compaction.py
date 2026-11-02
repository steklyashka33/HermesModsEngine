"""Unit tests for ``agent.turn_context_compaction`` (turn-start compaction extracted
from ``build_turn_context``)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.turn_context_compaction import (
    CompactionOutcome,
    _codex_native_auto_compaction,
    run_turn_start_compaction,
)

def _agent(**kw):
    compressor = SimpleNamespace(
        protect_first_n=3, protect_last_n=3, threshold_tokens=1_000, context_length=8_000,
        summary_target_ratio=0.5,
    )
    base = dict(
        compression_enabled=False, context_compressor=compressor, session_id="s1",
        model="m", _clear_context_overflow_warn=MagicMock(),
    )
    base.update(kw)
    return SimpleNamespace(**base)

def test_codex_native_auto_compaction_gate():
    assert _codex_native_auto_compaction(
        SimpleNamespace(api_mode="codex_app_server", codex_app_server_auto_compaction="native")
    )
    assert _codex_native_auto_compaction(
        SimpleNamespace(api_mode="codex_app_server", codex_app_server_auto_compaction="OFF")
    )
    assert not _codex_native_auto_compaction(
        SimpleNamespace(api_mode="codex_app_server", codex_app_server_auto_compaction="hermes")
    )
    assert not _codex_native_auto_compaction(SimpleNamespace(api_mode="chat_completions"))

def test_disabled_compression_rearms_overflow_warn_when_under_window():
    agent = _agent()
    msgs = [{"role": "user", "content": "hi"}]
    out = run_turn_start_compaction(
        agent, messages=msgs, system_message=None, active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=0, user_message="hi",
        effective_task_id="t",
    )
    assert isinstance(out, CompactionOutcome)
    assert out.messages is msgs and out.current_turn_user_idx == 0
    assert out.compressed is False and out.blocked is False
    agent._clear_context_overflow_warn.assert_called_once()
    assert agent._turn_received_provider_response is False
    assert agent._turn_preflight_display_snapshot is None


def test_smart_compaction_turn_start_prune_triggers_at_70_percent(monkeypatch):
    """When context tokens >= 70% threshold, run_turn_start_compaction triggers live prune."""
    pruned_msgs = [{"role": "user", "content": "hi"}, {"role": "tool", "content": "[terminal] ran ls -> exit 0"}]
    orig_msgs = [{"role": "user", "content": "hi"}, {"role": "tool", "content": "long output " * 500}]

    mock_compressor = MagicMock()
    mock_compressor.threshold_tokens = 1000
    mock_compressor.context_length = 2000
    mock_compressor.protect_first_n = 1
    mock_compressor.protect_last_n = 1
    mock_compressor.should_compress.return_value = False
    mock_compressor.prune_tool_results_only.return_value = (pruned_msgs, 1)

    agent = _agent(
        compression_enabled=True,
        context_compressor=mock_compressor,
        _safe_print=MagicMock(),
    )

    monkeypatch.setattr("agent.turn_context._preflight_request_tokens", lambda *args, **kw: 750)

    out = run_turn_start_compaction(
        agent, messages=orig_msgs, system_message=None, active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=0, user_message="hi",
        effective_task_id="t",
    )

    assert out.messages is pruned_msgs
    mock_compressor.prune_tool_results_only.assert_called_once_with(orig_msgs, current_tokens=750)
    agent._safe_print.assert_called()
    assert any("smart-compaction" in str(c) for c in agent._safe_print.call_args_list)


def test_smart_compaction_idle_trigger_over_3900s(monkeypatch):
    """When idle gap > 3900s (65 min), run_turn_start_compaction triggers idle compaction."""
    import time
    compressed_msgs = [{"role": "system", "content": "summary"}, {"role": "user", "content": "new turn"}]
    orig_msgs = [{"role": "user", "content": "old task " * 100}, {"role": "user", "content": "new turn"}]

    mock_compressor = MagicMock()
    mock_compressor.threshold_tokens = 1000
    mock_compressor.context_length = 8000
    mock_compressor.summary_target_ratio = 0.2
    mock_compressor.protect_first_n = 1
    mock_compressor.protect_last_n = 1
    mock_compressor.awaiting_real_usage_after_compression = False
    mock_compressor.get_active_compression_failure_cooldown.return_value = None
    mock_compressor.last_compression_rough_tokens = 0

    agent = _agent(
        compression_enabled=True,
        context_compressor=mock_compressor,
        _last_activity_ts=time.time() - 4000,
        _emit_status=MagicMock(),
        _safe_print=MagicMock(),
        _compress_context=MagicMock(return_value=(compressed_msgs, "new_sys")),
    )

    monkeypatch.setattr("agent.turn_context._preflight_request_tokens", lambda *args, **kw: 600)
    monkeypatch.setattr(
        "agent.context_compressor.get_smart_compaction_config",
        lambda: {"enabled": True, "idle_seconds": 3900},
    )

    out = run_turn_start_compaction(
        agent, messages=orig_msgs, system_message="sys", active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=1, user_message="new turn",
        effective_task_id="t",
    )

    agent._compress_context.assert_called_once_with(
        orig_msgs, "sys", approx_tokens=600, task_id="t", trigger="idle",
    )
    assert out.messages is compressed_msgs
    agent._emit_status.assert_called_once()


def test_smart_compaction_idle_trigger_under_3900s(monkeypatch):
    """When idle gap < 3900s, run_turn_start_compaction skips idle compaction."""
    import time
    orig_msgs = [{"role": "user", "content": "old task " * 100}, {"role": "user", "content": "new turn"}]

    mock_compressor = MagicMock()
    mock_compressor.threshold_tokens = 1000
    mock_compressor.context_length = 8000
    mock_compressor.summary_target_ratio = 0.2
    mock_compressor.protect_first_n = 1
    mock_compressor.protect_last_n = 1
    mock_compressor.awaiting_real_usage_after_compression = False
    mock_compressor.get_active_compression_failure_cooldown.return_value = None
    mock_compressor.last_compression_rough_tokens = 0

    agent = _agent(
        compression_enabled=True,
        context_compressor=mock_compressor,
        _last_activity_ts=time.time() - 3800,
        _emit_status=MagicMock(),
        _safe_print=MagicMock(),
        _compress_context=MagicMock(),
    )

    monkeypatch.setattr("agent.turn_context._preflight_request_tokens", lambda *args, **kw: 600)
    monkeypatch.setattr(
        "agent.context_compressor.get_smart_compaction_config",
        lambda: {"enabled": True, "idle_seconds": 3900},
    )

    out = run_turn_start_compaction(
        agent, messages=orig_msgs, system_message="sys", active_system_prompt="sys",
        conversation_history=None, current_turn_user_idx=1, user_message="new turn",
        effective_task_id="t",
    )

    agent._compress_context.assert_not_called()
    assert out.messages is orig_msgs
