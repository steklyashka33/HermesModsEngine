"""Regression tests for context metrics retention and state.db usage integration in slash commands."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key
from agent.i18n import t


def _make_source(thread_id=None, profile=None):
    return SessionSource(platform=Platform.TELEGRAM, chat_id="chat-retention-1", user_id="user-1",
                         chat_type="dm", thread_id=thread_id, profile=profile)


def _make_entry(source, sid="retention-sess-1", last_prompt_tokens=0):
    return SessionEntry(
        session_key=build_session_key(source),
        session_id=sid,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=source.platform,
        chat_type=source.chat_type,
        origin=source,
        last_prompt_tokens=last_prompt_tokens,
    )


def _make_runner(entry, history=None):
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner._session_key_for_source = lambda s: build_session_key(s)
    runner._resident_agent_for = MagicMock(return_value=None)
    runner._running_agents = {}
    runner._cached_agent_for = MagicMock(return_value=None)
    runner.adapters = {}
    runner._queue_depth = MagicMock(return_value=0)
    runner._status_session_db_facts = AsyncMock(return_value=("Test Session", {"model": "test-model"}, 15000, {}))
    runner._rehydrate_session_model_override = MagicMock()
    runner._session_model_override = MagicMock(return_value={})
    runner._resolve_route_context = AsyncMock(return_value=SimpleNamespace(
        context_length=128000, context_source="provider", model="test-model"
    ))
    runner._context_breakdown_block = MagicMock(return_value=[])
    runner._context_breakdown_lines = MagicMock(return_value=[])
    runner._session_db = MagicMock()
    runner._session_db.get_session = AsyncMock(return_value={"model": "test-model"})
    runner._session_db.get_recent_session_model_route = AsyncMock(return_value={})
    runner._persisted_billing_route = AsyncMock(return_value=(None, None))

    mock_store = MagicMock()
    runner.session_store = mock_store
    runner._async_session_store = SimpleNamespace(
        _store=mock_store,
        get_or_create_session=AsyncMock(return_value=entry),
        load_transcript=AsyncMock(return_value=history or []),
        update_session=AsyncMock(),
        _save=AsyncMock(),
        persist_rotated_compression_child=AsyncMock(return_value=True),
    )
    return runner


def _make_event(source, args=""):
    event = MagicMock(spec=MessageEvent)
    event.source = source
    event.get_command_args = MagicMock(return_value=args)
    return event


# -----------------------------------------------------------------------------
# 1. /usage with state.db data when agent is evicted
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usage_command_renders_db_stats_when_agent_is_none():
    source = _make_source()
    entry = _make_entry(source, "sess-usage-db")
    runner = _make_runner(entry)

    event = _make_event(source)
    db_usage = {
        "calls": 7,
        "input": 50000,
        "output": 10000,
        "reasoning": 0,
        "cache": 15000,
        "total": 60000,
        "compressions": 2,
    }

    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value=db_usage):
        res = await runner._handle_usage_command(event)

    # Must contain full session usage statistics
    assert "50,000" in res  # input tokens
    assert "10,000" in res  # output tokens
    assert "75,000" in res  # total tokens
    # cache pct: 15000 / (50000 + 15000) = 23%
    assert "23%" in res
    assert "7" in res      # API calls
    assert "2" in res      # compressions

    # Must NOT contain the stub message
    stub = t("gateway.usage.detailed_after_first")
    assert stub not in res
    assert t("gateway.usage.breakdown_header") not in res


@pytest.mark.asyncio
async def test_usage_command_stub_only_when_no_calls():
    source = _make_source()
    entry = _make_entry(source, "sess-usage-new")
    history = [
        {"role": "user", "content": "Initial user inquiry without any model response yet"},
    ]
    runner = _make_runner(entry, history=history)
    event = _make_event(source)

    db_usage = {
        "calls": 0, "input": 0, "output": 0, "reasoning": 0, "cache": 0, "total": 0, "compressions": 0
    }

    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value=db_usage):
        res = await runner._handle_usage_command(event)

    # Truly new session with no assistant calls must show the stub
    stub = t("gateway.usage.detailed_after_first")
    assert stub in res


def test_usage_agent_stats_lines_direct_without_agent():
    from gateway.slash_commands_status import _usage_agent_stats_lines

    db_usage = {
        "calls": 12,
        "input": 80000,
        "output": 20000,
        "reasoning": 0,
        "cache": 20000,
        "total": 100000,
        "compressions": 3,
    }

    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value=db_usage):
        lines = _usage_agent_stats_lines(
            agent=None,
            session_id="sess-direct-test",
            model_name="claude-3-7-sonnet",
            context_used=12000,
            context_total=200000,
        )

    text = "\n".join(lines)
    assert "80,000" in text
    assert "20,000" in text
    assert "120,000" in text
    assert "20%" in text  # 20k / (80k + 20k)
    assert "12" in text
    assert "3" in text   # compressions
    assert "claude-3-7-sonnet" in text


# -----------------------------------------------------------------------------
# 2. /context fallback to transcript estimate and state.db stats
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_figures_fallback_to_transcript_estimate():
    source = _make_source()
    entry = _make_entry(source, "sess-ctx-estimate", last_prompt_tokens=0)
    history = [
        {"role": "user", "content": "What is the status of the deployment pipeline?" * 20},
        {"role": "assistant", "content": "The deployment completed successfully." * 20},
    ]
    runner = _make_runner(entry, history=history)

    used, ctx_len, model_name = await runner._resolve_context_figures(
        agent=None, ctx=None, session_entry=entry, source=source
    )
    assert used > 0
    assert ctx_len == 128000
    assert model_name == "test-model"


@pytest.mark.asyncio
async def test_context_command_renders_db_compressor_stats_without_agent():
    source = _make_source()
    entry = _make_entry(source, "sess-ctx-db", last_prompt_tokens=4500)
    history = [
        {"role": "user", "content": "Task message"},
        {"role": "assistant", "content": "Task completed"},
    ]
    runner = _make_runner(entry, history=history)
    event = _make_event(source)

    db_usage = {
        "calls": 15,
        "input": 90000,
        "output": 15000,
        "reasoning": 0,
        "cache": 10000,
        "total": 105000,
        "compressions": 2,
    }

    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value=db_usage):
        res = await runner._handle_context_command(event)

    # Must contain context occupancy gauge
    assert "4,500" in res
    assert "128,000" in res

    # Must contain compressions and throughput from state.db
    assert "115,000" in res # total: 90k inp + 10k cache + 15k out
    assert "2" in res      # compressions
    assert "90,000" in res # input
    assert "15,000" in res # output
    assert "15" in res     # calls
    assert "Сжатий за всё время: 2" in res
    assert "Сжатий за этот сеанс" not in res
    assert "Автосжатие при:" in res

    # Between turns without live agent, detail notice is placed at bottom
    stub = t("gateway.context.detail_after_first")
    assert stub in res
    assert t("gateway.usage.breakdown_header") not in res


# -----------------------------------------------------------------------------
# 3. /status estimates context from transcript when context_used == 0
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_command_estimates_context_from_transcript_when_zero():
    source = _make_source()
    entry = _make_entry(source, "sess-status-zero", last_prompt_tokens=0)
    history = [
        {"role": "user", "content": "System architecture overview " * 50},
        {"role": "assistant", "content": "Architecture summary and components " * 50},
    ]
    runner = _make_runner(entry, history=history)
    event = _make_event(source)

    with patch("gateway.slash_commands_status._status_model_route",
               return_value=("test-model", "test-provider", 0, 128000, {})):
        res = await runner._handle_status_command(event)

    # Context estimate must be rendered with ~ marker
    assert "~" in res
    assert "128,000" in res


# -----------------------------------------------------------------------------
# 4. Manual /compress persists after_tokens in session_entry and session_store
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_compress_persists_after_tokens():
    from gateway.slash_commands_session import GatewaySessionCommandsMixin

    source = _make_source()
    entry = _make_entry(source, "sess-manual-comp", last_prompt_tokens=10000)
    runner = _make_runner(entry)
    runner._sync_telegram_topic_binding = MagicMock()

    tmp_agent = SimpleNamespace(
        session_id="sess-manual-comp-child",
        context_compressor=SimpleNamespace(last_prompt_tokens=3200),
    )
    compressed = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
    ]

    await GatewaySessionCommandsMixin._persist_manual_compression(
        runner, tmp_agent, entry, source, compressed, after_tokens=3200
    )

    assert entry.session_id == "sess-manual-comp-child"
    assert entry.last_prompt_tokens == 3200
    runner.async_session_store.update_session.assert_called_with(
        entry.session_key, last_prompt_tokens=3200
    )


# -----------------------------------------------------------------------------
# 5. Hygiene adopt transcript persists _new_tokens in session_entry and session_store
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hygiene_adopt_transcript_persists_new_tokens():
    from gateway.run_turn_hygiene import GatewayTurnHygieneMixin

    source = _make_source()
    entry = _make_entry(source, "sess-hyg-orig", last_prompt_tokens=15000)
    runner = _make_runner(entry)
    runner._rebind_turn_lease = MagicMock()
    runner._sync_telegram_topic_binding = MagicMock()

    hyg_agent = SimpleNamespace(
        session_id="sess-hyg-child",
        _last_compaction_in_place=False,
    )
    attempt = SimpleNamespace(
        agent=hyg_agent,
        history=[],
    )
    compressed = [
        {"role": "user", "content": "[CONTEXT COMPACTION] summarized context for continuation"},
    ]
    history = [
        {"role": "user", "content": "Detailed past instruction and conversation turns " * 50},
        {"role": "assistant", "content": "Detailed execution result and code output " * 50},
    ]
    plan = SimpleNamespace(
        msg_count=2,
        approx_tokens=15000,
        warn_token_threshold=100000,
    )

    rotated, in_place, new_count, new_tokens = await GatewayTurnHygieneMixin._hmwa_hygiene_adopt_transcript(
        runner,
        attempt,
        compressed,
        history,
        plan,
        session_entry=entry,
        source=source,
        _quick_key="qk-1",
        run_generation=1,
    )

    assert rotated is True
    assert entry.session_id == "sess-hyg-child"
    assert new_tokens > 0
    assert entry.last_prompt_tokens == new_tokens
    runner.async_session_store.update_session.assert_called_with(
        entry.session_key, last_prompt_tokens=new_tokens
    )


# -----------------------------------------------------------------------------
# 6. Honest ~ marker (tilde strictly on estimates, not on exact provider usage)
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_command_renders_tilde_when_estimated_from_transcript():
    source = _make_source()
    entry = _make_entry(source, "sess-ctx-tilde", last_prompt_tokens=0)
    history = [
        {"role": "user", "content": "Question about distributed caching architecture " * 30},
        {"role": "assistant", "content": "Answer with redis cluster topology and sharding " * 30},
    ]
    runner = _make_runner(entry, history=history)
    event = _make_event(source)

    res = await runner._handle_context_command(event)
    # Estimated context must include ~ marker in gauge and headroom
    assert "~" in res
    assert "128,000" in res


@pytest.mark.asyncio
async def test_usage_command_renders_tilde_when_estimated_from_transcript():
    source = _make_source()
    entry = _make_entry(source, "sess-usage-tilde", last_prompt_tokens=0)
    history = [
        {"role": "user", "content": "Explain raft consensus algorithm " * 20},
        {"role": "assistant", "content": "Leader election and log replication " * 20},
    ]
    runner = _make_runner(entry, history=history)
    event = _make_event(source)

    db_usage = {
        "calls": 5, "input": 12000, "output": 3000, "reasoning": 0, "cache": 0, "total": 15000, "compressions": 0
    }
    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value=db_usage):
        res = await runner._handle_usage_command(event)

    # When last_prompt_tokens was 0, context in /usage must be marked with ~
    assert "~" in res
    assert "128,000" in res


@pytest.mark.asyncio
async def test_context_and_usage_no_tilde_when_exact_provider_usage():
    source = _make_source()
    entry = _make_entry(source, "sess-exact", last_prompt_tokens=8500)
    runner = _make_runner(entry)
    event = _make_event(source)

    db_usage = {
        "calls": 10, "input": 40000, "output": 8000, "reasoning": 0, "cache": 0, "total": 48000, "compressions": 0
    }
    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value=db_usage):
        res_ctx = await runner._handle_context_command(event)
        res_usage = await runner._handle_usage_command(event)

    # Exact figures from provider must NOT have ~
    assert "8,500 / 128,000" in res_ctx
    assert "~8,500" not in res_ctx
    assert "8,500 / 128,000" in res_usage
    assert "~8,500" not in res_usage


@pytest.mark.asyncio
async def test_safe_against_none_and_empty_edge_cases():
    source = _make_source()
    entry = _make_entry(source, "sess-edge-cases", last_prompt_tokens=0)
    # Empty transcript
    runner = _make_runner(entry, history=[])
    event = _make_event(source)

    # u_db is empty or None
    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value={}):
        res_usage = await runner._handle_usage_command(event)
        res_ctx = await runner._handle_context_command(event)

    # Safe execution without exceptions
    assert isinstance(res_usage, str)
    assert isinstance(res_ctx, str)


# -----------------------------------------------------------------------------
# 7. Compressions ordering and session filtering
# -----------------------------------------------------------------------------

def test_context_compressions_ordering_and_filtering():
    from gateway.slash_commands_status import _context_compressor_lines
    from unittest.mock import MagicMock, patch

    # Case A: session_compressions == 0, total == 5
    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value={"calls": 0, "input": 0, "output": 0, "reasoning": 0, "cache": 0, "total": 0, "compressions": 5}):
        lines = _context_compressor_lines(agent=None, ctx=None, used=1000, session_id="s1", context_length=128000)
    full = "\n".join(lines)
    assert "Сжатий за всё время: 5" in full
    assert "Сжатий за этот сеанс" not in full
    assert "Автосжатие при:" in full

    # Case B: session_compressions == 2, total == 5
    ctx = MagicMock()
    ctx.compression_count = 2
    ctx.threshold_tokens = 100000
    ctx.threshold_percent = 0.8
    ctx._last_compression_savings_pct = 45.0
    with patch("gateway.slash_commands_status._active_window_usage_from_db", return_value={"calls": 0, "input": 0, "output": 0, "reasoning": 0, "cache": 0, "total": 0, "compressions": 5}):
        lines = _context_compressor_lines(agent=None, ctx=ctx, used=1000, session_id="s2", context_length=128000)
    full = "\n".join(lines)
    assert "Сжатий за всё время: 5" in full
    assert "Сжатий за этот сеанс: 2" in full
    assert "Последнее сжатие освободило: 45% контекста" in full

    # Ordering: "Сжатий за всё время" must precede "Сжатий за этот сеанс"
    idx_total = full.find("Сжатий за всё время: 5")
    idx_sess = full.find("Сжатий за этот сеанс: 2")
    idx_sav = full.find("Последнее сжатие освободило: 45% контекста")
    assert idx_total < idx_sess < idx_sav


def test_usage_registered_in_plain_commands_for_immediate_dispatch():
    from gateway.run_busy import GatewayBusySessionMixin
    assert "usage" in GatewayBusySessionMixin._PLAIN_COMMANDS
    assert "usage" not in GatewayBusySessionMixin._IDLE_COMMANDS
