# Hermes message-retention semantics

Source-backed notes for explaining compression in Telegram sessions.

## Official/documented behavior

The slash-command reference documents:

- `/compress` — summarize conversation context.
- `/compress here [N]` — summarize everything except the most recent `N` exchanges (default 2), which are kept verbatim.
- `/context [all]` — on messaging platforms, show context usage, compression threshold/headroom, compression stats, and category breakdown.

The sessions documentation states that SQLite stores full message history, including roles, content, tool calls, and tool results. It also states that active model prompts contain the selected system prompt, the current conversation window, and explicit per-turn injections—not every byte ever stored.

## Implementation details verified locally

Relevant source/tests:

- `gateway/slash_commands.py`: `/compress here [N]` splits the transcript; only the head is passed to the compressor; the tail is rejoined unchanged.
- `agent/context_compressor.py`: automatic compression protects the latest actionable user message and anchors the latest user-visible assistant reply in the tail. It also keeps a configured recent tail budget.
- `agent/conversation_compression.py`: default `compression.in_place` is true. The active transcript is compacted under the same session ID; pre-compaction rows are soft-archived rather than deleted. Rotation mode is an explicit opt-out and creates a continuation child session.
- `agent/turn_context.py`: after compression, the current user-turn index is re-anchored because the message list may have been rebuilt.
- `tests/run_agent/test_in_place_compaction.py`: verifies the compacted active set is loaded on resume while original messages remain inactive, searchable, and recoverable under the same ID.
- `tests/cli/test_compress_here.py`: verifies the recent tail is re-appended verbatim.

## Wording/caveats

Say “normally preserved verbatim in the active tail,” not “the latest message is always immutable.” Automatic compression may still prune bulky or duplicate old tool results, and external/custom context engines may have their own behavior. Distinguish:

1. what is preserved in the durable database;
2. what is currently supplied to the model;
3. what is summarized or pruned to fit the active context.

For a user who wants exact recent wording, recommend `/compress here 3` (or another suitable `N`) rather than bare `/compress`.
