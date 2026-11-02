# Compression timing and log signatures

## Timing paths

Hermes has separate compression paths:

| Path | When it runs | User-visible meaning |
|---|---|---|
| Turn preflight | Before an API call when the current request estimate reaches the effective threshold | Can happen inside an active tool/task loop |
| Gateway session hygiene | Before a new gateway turn when persisted history is oversized | Compresses before the next Telegram message is processed |
| Idle compaction | Before the first reply after `idle_compact_after_seconds` inactivity | Uses normal batch compression, not a softer algorithm |
| Micro-compaction | After a successful turn when `micro_compact=true` | Gradually folds old exchanges into a rolling summary |
| Manual `/compress` | When the user invokes it | Predictable boundary-controlled compression |

## Key evidence strings

- `Preflight compression: ~N tokens >= M threshold` — in-turn preflight trigger.
- `Session hygiene: N messages, ~T tokens — auto-compressing` — gateway pre-turn trigger.
- `Idle compaction: ... idle >= ...` — idle trigger.
- `context compression started/done` — actual compressor execution.
- `Context compression made no progress ... continuing without compression` — compressor summary path timed out or stalled; it does not prove the main task stopped.
- `commit_status=aborted` / `commit_fence_cancelled` — compaction was cancelled before durable mutation.

## Boundary policy

Automatic compression cannot be restricted strictly to before/after tasks while also guaranteeing that arbitrarily long active tasks continue: a tool loop can exceed the request budget before the final answer. For strict boundary control, disable automatic compression and use `/compress` manually, accepting overflow risk. For a best-effort policy, keep automatic compression enabled as a safety net, disable idle compaction, and optionally enable post-turn micro-compaction with a moderate cadence.

## Configuration notes

- `threshold_tokens` is an absolute cap; effective trigger is the lower of the ratio-derived threshold and the cap.
- `target_ratio` controls the post-compression target, not a hard session-size limit.
- `protect_last_n` protects recent messages but does not prevent the compressor from running.
- `abort_on_summary_failure=true` preserves the original history if summarization fails, at the cost of leaving an oversized context.
- `in_place=true` preserves the durable session ID and soft-archives prior rows.
- `micro_compact` is post-success-turn cleanup, not idle compaction; it may break the provider prompt-cache prefix.
- Always read the effective model context length and actual token counts from logs before changing thresholds.
