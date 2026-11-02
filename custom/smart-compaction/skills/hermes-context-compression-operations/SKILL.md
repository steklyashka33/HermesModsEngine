---
name: hermes-context-compression-operations
description: Use when tuning Hermes context compression.
category: devops
---

# Hermes Context Compression Operations

Use this skill when a Hermes conversation compresses unexpectedly, appears to stall while compacting, reaches a large context during a long Telegram task, or the user wants compression only at task boundaries.

## Telegram-first communication

Answer in Russian and keep the result concise. For each proposed change use:

`Параметр — сейчас: X → совет: Y`

Then explain the user-visible effect in one plain sentence. Separate Telegram-specific settings from Hermes-wide settings. Do not list settings that are merely being left unchanged. If the user says the explanation is unclear, explain each setting in plain language before recommending a change and avoid adding unrelated options.

## Diagnose before changing

1. Read the active `compression:` and `mods.smart-compaction:` configuration in `~/.hermes/config.yaml`.
2. Determine the effective model context length; do not assume it from the configured model name.
3. Inspect logs for the exact path:
   - `Preflight compression` = compression before an API call, potentially inside an active tool loop.
   - `Session hygiene` / `auto-compressing` = gateway compression before a new turn for an oversized persisted transcript.
   - `Idle compaction` = optional compression before the first reply after an idle gap (`idle_seconds: 3600`).
   - `context compression started/done` = actual compressor execution.
   - timeout/cancel/no-progress lines = compressor/provider problem, not proof that the main agent stopped.
4. Check the auxiliary compression provider/model and its timeout separately from the main model.
5. Report evidence with actual token counts, thresholds, model, and duration; never invent them.

## Smart Compaction (Dual-Trigger) Architecture

The `smart-compaction` mod enhances native Hermes compression with a two-level safety system:

1. **Live-Pruning of Bulky Tool Results (Level 1):**
   - Triggered at `live_threshold_ratio: 0.70` (70% of `threshold_tokens`).
   - Replaces oversized tool outputs (`min_prune_chars: 4000`) with concise summaries, reclaiming up to `min_reclaim_tokens: 20000` tokens without rewriting conversation messages.
   - Prevents mid-task context overflow while preserving all user and assistant turns.
2. **Cold Idle Compaction (Level 2):**
   - Triggers after prolonged inactivity (`idle_seconds: 3600`, default 1 hour).
   - Compacts old conversation history between user sessions so resuming work starts with fresh headroom.
3. **Ironclad Tail Protection:**
   - `compression.protect_last_n: 30` protects the most recent 30 messages verbatim against any compaction or pruning.

## Configuration semantics

- `compression.enabled`: master switch for automatic compression. Disabling it prevents all automatic safety compaction but risks context overflow during long tasks.
- `compression.threshold`: ratio trigger against the usable context window; effective floors or model-specific rules may apply.
- `compression.threshold_tokens`: absolute cap. Compression starts at the lower of the ratio-derived threshold and this cap; it does not mean “compress only between tasks.”
- `compression.target_ratio`: target amount of recent context retained after a batch compaction; not a total session-size limit.
- `compression.protect_last_n`: minimum recent messages kept verbatim (configured to 30); protects the current tail but cannot guarantee that an unbounded active task will never trigger compression.
- `compression.protect_first_n`: early conversation messages kept verbatim.
- `compression.hygiene_hard_message_limit`: gateway message-count safety trigger before a new turn; separate from token thresholds.
- `compression.hygiene_timeout_seconds` and `hygiene_total_ceiling_seconds`: inactivity and absolute wait bounds for gateway hygiene compression.
- `compression.context_timeout_seconds` and `context_total_ceiling_seconds`: bounds for in-agent compression; they do not change when compression is triggered.
- `compression.abort_on_summary_failure`: when true, failed summarization preserves the original context instead of using a fallback summary; safer for fidelity but can leave the session oversized.
- `compression.in_place`: keeps compaction in the same durable session ID and soft-archives pre-compaction rows; prefer true for Telegram continuity.
- `compression.idle_compact_after_seconds`: optional time-based pre-reply compaction after inactivity.
- `compression.micro_compact`: opt-in post-success-turn rolling summary. It runs in the turn finalizer after a successful response, not during idle, but adds auxiliary calls and can break prompt-cache prefixes.
- `compression.micro_compact_every_n_turns`: cadence for micro-compaction; larger values reduce cache breaks and auxiliary calls.
- `/compress`: manual boundary-aware compression. It is the only predictable user-controlled timing.

## Message-retention semantics

Explain the distinction between the **durable transcript** and the **active model context** before answering questions about lost messages:

- Hermes stores full session history in `~/.hermes/state.db`, including user/assistant/tool messages and tool calls/results. Compression is not a privacy delete.
- The model does not receive the whole stored transcript on every turn. It receives the active compacted window plus the current system prompt and turn-specific injections.
- Automatic compression summarizes the middle of the transcript. It protects the latest actionable user message and anchors the latest user-visible assistant reply in the tail so the current task is not replaced by a vague summary.
- This is not the same as guaranteeing that every recent tool result remains byte-for-byte unchanged: bulky/duplicate old tool results can be replaced by one-line summaries, and tail protection is budget-aware.
- `/compress here [N]` is the deterministic form: only the head is summarized; the most recent `N` exchanges (default 2) are reattached verbatim. Recommend it when the user explicitly needs exact wording preserved.
- With the default `compression.in_place: true`, the active transcript is compacted under the same session ID while pre-compaction rows are soft-archived and remain searchable/recoverable. With rotation mode, a continuation child session is created instead; the old parent still retains its history.
- A current user turn can be re-anchored after compaction because compression rebuilds message-list positions. Do not claim that the current message is always untouched in every custom/context-engine path; state the normal Hermes guarantee and mention that only the protected active tail is sent to the model.

When answering in Telegram, use a short Russian explanation with this order: (1) direct answer about the latest user message, (2) active context vs stored history, (3) `/compress here N` as the exact-preservation control. Do not dump CLI commands or unrelated compression settings unless asked.

See `references/message-retention-semantics.md` for the source-backed implementation details and caveats.

## User requirement: only before/after tasks

Make the trade-off explicit. With automatic compression enabled, Hermes cannot guarantee “never during an active task”: a long tool loop may exceed the effective request budget before the task completes, so turn preflight can compress to avoid an API overflow.

For a best-effort boundary-oriented policy:

- keep automatic compression enabled as a safety net;
- keep `smart-compaction` active so tool outputs are pruned live before full conversation compression;
- set `protect_last_n: 30` to guarantee the recent discussion is untouched;
- remove an overly low absolute cap after checking the real context window;
- enable `micro_compact` with a moderate cadence for successful-turn cleanup;
- leave idle compaction disabled unless the user wants compaction on resuming an old thread;
- keep `in_place: true`;
- use `/compress` manually before known long tasks and after them.

For a strict guarantee, set `compression.enabled: false` and run `/compress` manually before/after tasks. State clearly that this can fail when a task exceeds the model context window; there is no configuration that both guarantees no mid-task compression and guarantees arbitrary-length tasks continue.

## Safety and operations

Use `hermes config set` or `python3 runner.py config smart-compaction --set <k>=<v>` for changes and read back the exact stored values. Do not restart or reload Hermes Gateway, OmniRoute, or the server without explicit user consent. Configuration changes should be reported separately from runtime activation; do not imply that a restart occurred.

When diagnosing suspected memory growth, do not assume context compression or active subagents are the root cause. First inspect existing samplers/monitors and keep one canonical external per-turn monitor. It should record gateway RSS, anonymous RSS, swap, cgroup memory/events, PSI, threads, FDs, and turn duration before/after each turn. If a duplicate monitor was created, stop only the less informative duplicate, retain its logs, and verify the surviving timer/service and its output. Report exact files, unit/timer, interval, and verification output. For a repeated command, validate existing artifacts before modifying anything.

## References

- `references/compression-timing-and-log-signatures.md` — evidence-based timing paths, log signatures, and boundary-policy trade-offs.
