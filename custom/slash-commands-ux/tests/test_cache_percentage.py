import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace


def test_usage_agent_stats_lines_with_cache():
    from gateway.slash_commands_status import _usage_agent_stats_lines

    agent = MagicMock()
    agent.session_id = "test-session-cache-1"
    agent.model = "test-model"
    agent.session_api_calls = 3
    agent.session_input_tokens = 80_000
    agent.session_output_tokens = 20_000
    agent.session_reasoning_tokens = 0
    agent.session_cache_read_tokens = 20_000
    agent.session_cache_write_tokens = 0
    agent.session_total_tokens = 100_000
    agent.get_rate_limit_state.return_value = None
    ctx = MagicMock()
    ctx.last_prompt_tokens = 0
    ctx.compression_count = 0
    agent.context_compressor = ctx

    lines = _usage_agent_stats_lines(agent)
    text = "\n".join(lines)
    # Total includes cache: 80k inp + 20k cache + 20k out = 120,000
    # Cache pct is 20,000 / (80k + 20k) = 20%
    assert "20%" in text
    assert ("кэш" in text or "cache" in text)
    assert "120,000" in text
    assert ("80,000" in text and "20,000" in text)
    assert "3" in text


def test_usage_agent_stats_lines_real_session_proportions():
    """Verify exact proportions reported by user (3.6M in, 110k out, 130M cache read => 97% cache)."""
    from gateway.slash_commands_status import _usage_agent_stats_lines

    agent = MagicMock()
    agent.session_id = "test-session-user-bug"
    agent.model = "test-model"
    agent.session_api_calls = 524
    agent.session_input_tokens = 3_672_004
    agent.session_output_tokens = 109_599
    agent.session_reasoning_tokens = 0
    agent.session_cache_read_tokens = 130_833_920
    agent.session_cache_write_tokens = 0
    agent.session_total_tokens = 3_781_603
    agent.get_rate_limit_state.return_value = None
    ctx = MagicMock()
    ctx.last_prompt_tokens = 0
    ctx.compression_count = 0
    agent.context_compressor = ctx

    lines = _usage_agent_stats_lines(agent)
    text = "\n".join(lines)
    # Total with cache is 3,672,004 + 130,833,920 + 109,599 = 134,615,523
    assert "134,615,523" in text
    # Cache pct is 130,833,920 / (3,672,004 + 130,833,920) = 97.27% => 97%
    assert "97%" in text
    assert ("кэш" in text or "cache" in text)


def test_usage_agent_stats_lines_zero_cache():
    from gateway.slash_commands_status import _usage_agent_stats_lines

    agent = MagicMock()
    agent.session_id = "test-session-cache-0"
    agent.model = "test-model"
    agent.session_api_calls = 1
    agent.session_input_tokens = 10_000
    agent.session_output_tokens = 2_000
    agent.session_reasoning_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_total_tokens = 12_000
    agent.get_rate_limit_state.return_value = None
    ctx = MagicMock()
    ctx.last_prompt_tokens = 0
    ctx.compression_count = 0
    agent.context_compressor = ctx

    lines = _usage_agent_stats_lines(agent)
    text = "\n".join(lines)
    assert "кэш" not in text
    assert "cache" not in text
    assert "12,000" in text


def test_status_report_fields_and_lines_cache():
    from hermes_cli.status_report import build_status_fields, status_lines

    agent = SimpleNamespace(
        model="test-model",
        provider="test-provider",
        session_total_tokens=100_000,
        session_input_tokens=80_000,
        session_output_tokens=20_000,
        session_cache_read_tokens=20_000,
        session_cache_write_tokens=0,
    )
    fields = build_status_fields(
        session_id="s1",
        agent=agent,
        meta={},
    )
    # Total is inp (80k) + cache (20k) + out (20k) = 120k
    # Cache pct is 20k / (80k + 20k) = 20%
    assert fields["cache_pct"] == 20
    assert fields["tokens"] == "120,000"
    lines = status_lines(fields, "tokens")
    assert "Tokens: 120,000 (20% cache)" in lines[0]

    # Without cache
    agent_no_cache = SimpleNamespace(
        model="test-model",
        provider="test-provider",
        session_total_tokens=10_000,
        session_input_tokens=8_000,
        session_output_tokens=2_000,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
    )
    fields_nc = build_status_fields(
        session_id="s2",
        agent=agent_no_cache,
        meta={},
    )
    assert fields_nc["cache_pct"] == 0
    assert fields_nc["tokens"] == "10,000"
    lines_nc = status_lines(fields_nc, "tokens")
    assert lines_nc[0] == "Tokens: 10,000"


def test_methods_slash_live_usage_tokens_cache():
    from tui_gateway import methods_slash
    import threading

    agent = SimpleNamespace(
        model="test-model",
        session_cache_read_tokens=30_000,
        session_cache_write_tokens=0,
    )
    session = {
        "agent": agent,
        "history": [],
        "history_lock": threading.Lock(),
        "_metadata_message_count": 5,
    }

    usage_mock = {
        "model": "test-model",
        "input": 60_000,
        "output": 10_000,
        "reasoning": 0,
        "prompt": 90_000,
        "completion": 10_000,
        "total": 70_000,
        "calls": 2,
    }

    orig = methods_slash._format_live_usage_tokens.__globals__.get("_session_usage_snapshot")
    try:
        methods_slash._format_live_usage_tokens.__globals__["_session_usage_snapshot"] = lambda s: usage_mock
        output = methods_slash._format_live_usage_tokens(session)
    finally:
        if orig is not None:
            methods_slash._format_live_usage_tokens.__globals__["_session_usage_snapshot"] = orig
        else:
            methods_slash._format_live_usage_tokens.__globals__.pop("_session_usage_snapshot", None)

    # prompt = 60k + 30k = 90k, cache = 30k / 90k = 33%
    # total = 60k + 30k + 10k = 100,000
    assert "33% cache" in output
    assert "100,000" in output


def test_insights_formatting_cache():
    from agent.insights import InsightsEngine

    report = {
        "days": 7,
        "empty": False,
        "overview": {
            "total_sessions": 2,
            "total_messages": 10,
            "total_tool_calls": 5,
            "total_input_tokens": 50_000,
            "total_output_tokens": 10_000,
            "total_cache_read_tokens": 50_000,
            "total_cache_write_tokens": 0,
            "total_tokens": 60_000,
            "total_hours": 1,
            "avg_session_duration": 1800,
            "avg_messages_per_session": 5.0,
            "user_messages": 5,
            "estimated_cost": 0.05,
            "actual_cost": 0.05,
        },
        "models": [],
        "platforms": [{"platform": "cli", "sessions": 2, "messages": 10, "total_tokens": 60_000}],
        "tools": [],
    }

    engine = InsightsEngine(MagicMock())
    gw_output = engine.format_gateway(report)
    term_output = engine.format_terminal(report)

    # 50k / (50k + 50k) = 50%
    # total = 50k + 50k + 10k = 110,000
    assert "50% cache" in gw_output
    assert "50% cache" in term_output
    assert "110,000" in gw_output
    assert "110,000" in term_output
