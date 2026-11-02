"""Tests for subagent role and toolsets in unlimited mode.

Verifies:
1. Level 0 root agent (depth=0, _unlimited_mode=True):
   - child at depth=1 gets role='orchestrator'
   - child gets toolset 'delegation' and can delegate
   - child inherits _unlimited_mode=True
   - child prompt confirms subagent spawning is allowed and notes depth 1 / max 2
2. Level 1 orchestrator (depth=1, _unlimited_mode=True):
   - can successfully call delegate_task without hitting depth limit
   - child at depth=2 gets role='leaf'
   - child at depth=2 has 'delegation' blocked/stripped
   - child at depth=2 prompt does not contain orchestrator spawning instructions
3. Level 2 leaf (depth=2, _unlimited_mode=True):
   - calling delegate_task fails with 'Delegation depth limit reached (depth=2, max_spawn_depth=2)'
   - recursion is strictly blocked
4. Standard mode (unlimited=False, default max_spawn_depth=1):
   - child at depth=1 gets role='leaf'
   - child at depth=1 has 'delegation' blocked
5. Session metadata fallback (_is_session_unlimited()=True):
   - child at depth=1 gets role='orchestrator' even if agent._unlimited_mode is unset
6. Tree-wide concurrency limit in unlimited mode:
   - per-call limit: 11 tasks exceeds default unlimited_children=10
   - session-wide active count: active + requested > 10 is rejected
"""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest

from tools import delegate_tool
from tools.registry import registry


def _create_mock_parent(depth: int = 0, unlimited_mode: bool = False) -> MagicMock:
    parent = MagicMock()
    parent._delegate_depth = depth
    parent._unlimited_mode = unlimited_mode
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._session_db = None
    parent.enabled_toolsets = ["terminal", "file", "delegation"]
    parent.disabled_toolsets = []
    parent.request_overrides = {}
    parent.provider = "custom"
    parent.base_url = "https://fixture.invalid/v1"
    parent.api_key = "fixture-only"
    parent.api_mode = "chat_completions"
    parent.model = "fixture"
    parent.platform = "cli"
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent.session_id = "test-session-root"
    parent._delegate_parent_ref = None
    return parent


def _create_mock_child() -> MagicMock:
    child = MagicMock()
    child._credential_pool = None
    child._delegate_saved_tool_names = []
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.model = "fixture"
    child.session_id = "test-child-session"
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "api_calls": 1,
        "messages": [],
    }
    return child


def test_level_0_spawns_level_1_orchestrator_in_unlimited_mode(tmp_path, monkeypatch):
    """Level 0 root agent with _unlimited_mode=True spawns depth=1 child with role='orchestrator'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=0, unlimited_mode=True)
    child = _create_mock_child()

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    args = {"tasks": [{"goal": "Coordinate research"}]}

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
        patch("run_agent.AIAgent", return_value=child) as agent_class,
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    assert "error" not in result
    assert result.get("status") == "dispatched"
    assert agent_class.call_count == 1

    kwargs = agent_class.call_args.kwargs
    prompt = kwargs["ephemeral_system_prompt"]

    # Child must be at depth 1 with role 'orchestrator'
    assert child._delegate_depth == 1
    assert child._delegate_role == "orchestrator"

    # Child toolsets must retain delegation
    assert "delegation" in kwargs["enabled_toolsets"]
    assert "delegation" not in kwargs.get("disabled_toolsets", [])

    # Child must inherit unlimited_mode
    assert child._unlimited_mode is True

    # Child prompt must confirm subagent spawning is allowed and note depth caps
    assert "CAN spawn your own subagents" in prompt
    assert "depth 1" in prompt
    assert "max_spawn_depth=2" in prompt
    assert "Your own children MUST be leaves" in prompt


def test_level_1_spawns_level_2_leaf_in_unlimited_mode(tmp_path, monkeypatch):
    """Level 1 orchestrator with _unlimited_mode=True spawns depth=2 child with role='leaf'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=1, unlimited_mode=True)
    grandchild = _create_mock_child()

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    args = {"tasks": [{"goal": "Leaf task execution"}]}

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
        patch("run_agent.AIAgent", return_value=grandchild) as agent_class,
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    # Orchestrator runs children synchronously within turn
    grandchild.run_conversation.assert_called_once()
    assert "error" not in result

    kwargs = agent_class.call_args.kwargs
    prompt = kwargs["ephemeral_system_prompt"]

    # Grandchild must be at depth 2 with role 'leaf'
    assert grandchild._delegate_depth == 2
    assert grandchild._delegate_role == "leaf"

    # Grandchild toolsets must NOT contain delegation
    assert "delegation" not in kwargs["enabled_toolsets"]
    assert "delegation" in kwargs.get("disabled_toolsets", [])

    # Leaf prompt does not contain orchestrator delegation instructions
    assert "CAN spawn your own subagents" not in prompt
    assert "## Subagent Spawning (Orchestrator Role)" not in prompt


def test_level_2_leaf_cannot_delegate_in_unlimited_mode(tmp_path, monkeypatch):
    """Level 2 subagent attempting to call delegate_task is rejected at depth ceiling."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=2, unlimited_mode=True)
    child = _create_mock_child()

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    args = {"tasks": [{"goal": "Illegal third level"}]}

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
        patch("run_agent.AIAgent", return_value=child),
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    # Must fail immediately with depth limit error
    assert "error" in result
    assert "Delegation depth limit reached (depth=2, max_spawn_depth=2)" in result["error"]
    child.run_conversation.assert_not_called()


def test_standard_mode_spawns_leaf_by_default(tmp_path, monkeypatch):
    """When unlimited mode is OFF, depth=0 parent spawns depth=1 child with role='leaf' (default max_spawn_depth=1)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=0, unlimited_mode=False)
    child = _create_mock_child()

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    args = {"tasks": [{"goal": "Standard task"}]}

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
        patch("run_agent.AIAgent", return_value=child) as agent_class,
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    assert "error" not in result
    assert result.get("status") == "dispatched"
    assert agent_class.call_count == 1

    kwargs = agent_class.call_args.kwargs
    prompt = kwargs["ephemeral_system_prompt"]

    # In standard mode with max_spawn_depth=1, child is leaf
    assert child._delegate_depth == 1
    assert child._delegate_role == "leaf"
    assert "delegation" not in kwargs["enabled_toolsets"]
    assert "CAN spawn your own subagents" not in prompt


def test_session_metadata_unlimited_fallback(tmp_path, monkeypatch):
    """When parent_agent._unlimited_mode is False/unset, but session is unlimited via _is_session_unlimited()."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=0, unlimited_mode=False)
    child = _create_mock_child()

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    args = {"tasks": [{"goal": "Session unlimited task"}]}

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch("tools.delegate_tool._is_session_unlimited", return_value=True),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
        patch("run_agent.AIAgent", return_value=child) as agent_class,
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    assert "error" not in result
    assert result.get("status") == "dispatched"
    assert agent_class.call_count == 1

    kwargs = agent_class.call_args.kwargs
    assert child._delegate_depth == 1
    assert child._delegate_role == "orchestrator"
    assert "delegation" in kwargs["enabled_toolsets"]
    assert "CAN spawn your own subagents" in kwargs["ephemeral_system_prompt"]


def test_unlimited_concurrency_limit_per_call(tmp_path, monkeypatch):
    """In unlimited mode, per-call limit is unlimited_children (default 10)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=0, unlimited_mode=True)

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    # 11 tasks requested at once (exceeds default unlimited_children=10)
    args = {
        "tasks": [{"goal": f"Execute real delegated research item number {i} with specific scope"} for i in range(11)],
    }

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    assert "error" in result
    assert "Too many tasks: 11 provided, but max_concurrent_children is 10" in result["error"]


def test_unlimited_concurrency_limit_across_tree(tmp_path, monkeypatch):
    """Session-wide concurrency checks active children across the conversation tree against unlimited_children (10)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    parent = _create_mock_parent(depth=0, unlimited_mode=True)

    config = {"max_spawn_depth": 1, "orchestrator_enabled": True}
    # 3 tasks requested when 8 active subagents already exist across the tree (8 + 3 = 11 > 10)
    args = {
        "tasks": [
            {"goal": "Inspect database migration files and verify index creation"},
            {"goal": "Analyze query performance logs across all replica instances"},
            {"goal": "Refactor authentication middleware to use constant time comparison"},
        ],
    }

    # Simulate 8 active subagent records owned by parent
    fake_records = {
        f"sub-{i}": {"agent": MagicMock(), "owner_agent_session_id": parent.session_id}
        for i in range(8)
    }

    with (
        patch.object(delegate_tool, "_load_config", return_value=config),
        patch.object(delegate_tool, "_resolve_workspace_hint", return_value=str(tmp_path)),
        patch("tools.delegate_tool._active_subagents", fake_records),
        patch("tools.delegate_tool._owns_subagent_record", return_value=True),
    ):
        result = json.loads(registry.get_entry("delegate_task").handler(args, parent_agent=parent))

    assert "error" in result
    assert "Session subagent concurrency limit reached: 8 active + 3 requested exceeds 10 parallel subagents" in result["error"]
