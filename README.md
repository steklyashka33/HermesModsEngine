# HermesModsEngine 🚀

[![Hermes Agent](https://img.shields.io/badge/Hermes-Agent-blue.svg)](https://hermes-agent.nousresearch.com)
[![Engine Version](https://img.shields.io/badge/version-1.5.0-emerald.svg)](https://github.com/steklyashka33/HermesModsEngine)
[![Python Versions](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://python.org)
[![Test Suite](https://img.shields.io/badge/tests-277%20passed-brightgreen.svg)](https://github.com/steklyashka33/HermesModsEngine)
[![Zero Downtime](https://img.shields.io/badge/runtime-zero--downtime-orange.svg)](https://github.com/steklyashka33/HermesModsEngine)
[![Localization](https://img.shields.io/badge/i18n-8%20languages-purple.svg)](https://github.com/steklyashka33/HermesModsEngine)
[![Production Mods](https://img.shields.io/badge/mods-37%20active-blueviolet.svg)](https://github.com/steklyashka33/HermesModsEngine)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> **Enterprise-grade declarative hot-patching and extension engine for [Hermes Agent](https://hermes-agent.nousresearch.com).**  
> Build robust capabilities, rich Telegram interfaces, multimodal audio/video pipelines, and subagent orchestration workflows that **survive upstream updates (`hermes update`)** without maintaining fragile hard forks.

Read this in other languages: **[English](README.md)** | **[Русский](README.ru.md)**

---

## ⚡ Quick Start (1 Line)

Install or update the engine and apply all 37 production mods in a single automated step:

```bash
curl -sSL https://raw.githubusercontent.com/steklyashka33/HermesModsEngine/main/install.sh | bash
```

### 🔄 Update in 1 Command
Keep the engine, system mods, and caches in sync with the official repository with zero downtime:
```bash
python3 ~/.hermes/mods/runner.py update
```
* **Git Synchronization:** Safely pulls upstream updates and improvements.
* **Preserves Local Mods:** Local user mods in `custom/` and secrets in `~/.hermes/secrets.yaml` are strictly preserved and never overwritten.
* **Pre-Flight AST Verification:** Simulates target patches in memory before touching disk.
* **Zero Downtime:** Transactionally reapplies patches to core without restarting background services or the gateway (`hermes-gateway`).
* **Available Flags:** `--check` (inspect without pulling), `--dry-run` (simulate in memory), `--no-apply` (pull without patching core), `--force` (reset hard to origin/main).

### 🛠️ Manual Installation (2 Commands)
```bash
git clone https://github.com/steklyashka33/HermesModsEngine.git ~/.hermes/mods
python3 ~/.hermes/mods/runner.py apply
```

> **Zero-Downtime Guarantee:** The installation and patch lifecycle never restarts background services or the gateway (`hermes-gateway`). All caches update atomically on the fly.

---

## 💡 Why HermesModsEngine?

Modifying large-scale AI agent runtimes usually forces engineering teams into **The Fork Trap**:

```text
               THE FORK TRAP                              HERMES MODS ENGINE
  ┌─────────────────────────────────────┐       ┌─────────────────────────────────────┐
  │ Upstream Hermes Releases New Feats │       │ Upstream Hermes Releases New Feats │
  └──────────────────┬──────────────────┘       └──────────────────┬──────────────────┘
                     │                                             │ hermes update
                     ▼                                             ▼
  ┌─────────────────────────────────────┐       ┌─────────────────────────────────────┐
  │ Merge Conflicts & Broken Baselines  │       │ Clean Core Upstream Code Restored   │
  └──────────────────┬──────────────────┘       └──────────────────┬──────────────────┘
                     │ Days of debugging                           │ python3 runner.py apply
                     ▼                                             ▼
  ┌─────────────────────────────────────┐       ┌─────────────────────────────────────┐
  │ Abandon Upstream or Re-fork         │       │ 37 Mods Transactionally Reapplied   │
  │ (Stale, fragile, out-of-date)       │       │ (Pre-flight AST verified, 0 errors) │
  └─────────────────────────────────────┘       └─────────────────────────────────────┘
```

**HermesModsEngine** eliminates the fork trap through **declarative runtime overlays**:
- **Declarative Surgical Patches:** Edits are expressed as exact target code blocks with surrounding context.
- **Pre-Flight AST Parsing:** Patches are validated with Python's Abstract Syntax Tree parser *before* touching disk.
- **Delta Snapshots & Gateway Watchdog:** Byte-exact pre-apply snapshots guarantee instantaneous rollback if a patch fails or causes the gateway to become unstable within 30 seconds.
- **Cache-Driven Isolation:** Core Hermes Agent communicates with mods strictly through flat JSON caches, ensuring zero runtime dependencies on engine internals.

---

## 🏛️ System Architecture

### 1. Cache-Driven Runtime Bridge

```mermaid
flowchart TD
    subgraph HermesCore ["Hermes Agent Core (/usr/local/lib/hermes-agent)"]
        G[Gateway & Telegram Service]
        CLI[Hermes CLI Dispatcher]
        TurnRunner[Agent Turn Runner]
        I18nResolver[agent.i18n Resolver]
    end

    subgraph Caches ["Atomic JSON Caches (Zero-Downtime Bridge)"]
        CmdCache[".commands_cache.json\n(Slash Commands Registry)"]
        LocCache[".locales_cache.json\n(Mod Overlay: Highest Priority)"]
        McpCache[".mcp_cache.json\n(Declarative MCP Servers)"]
    end

    subgraph Engine ["HermesModsEngine (.engine/ & runner.py)"]
        Runner["runner.py CLI"]
        DAG["Topological DAG Sorter\n(Kahn's Algorithm)"]
        Snap["Snapshot Manager\n(SHA-256 Delta Snapshots)"]
        Watchdog["Gateway Watchdog\n(30s Supervision)"]
        CircuitBreaker["Circuit Breaker\n(Fault Isolation: CLOSED/OPEN)"]
    end

    subgraph ModCatalog ["37 Registered Production Mods"]
        Tier0["Tier 0: System Mods (system/)\n• mod-command-registry\n• mod-declarative-i18n\n• mod-declarative-mcp\n• mod-folder-skills\n• mod-manager (/mods)\n• mod-update-integration"]
        Tier1["Tier 1: Custom Feature Mods (custom/)\n• Smart Compaction Dual-Trigger\n• Speculative Racing STT & TTS\n• Slash Commands UX Polish\n• Subagent Scaling & Queue Isolation\n• Multimodal Video Analysis"]
    end

    Tier0 --> DAG
    Tier1 --> DAG
    DAG --> Runner
    Runner --> Snap
    Runner --> Watchdog
    Runner --> CircuitBreaker
    Runner -->|Atomic FileLock Write| CmdCache
    Runner -->|Atomic FileLock Write| LocCache
    Runner -->|Atomic FileLock Write| McpCache

    CmdCache -.->|mtime polling (0s restart)| CLI
    CmdCache -.->|mtime polling (0s restart)| G
    LocCache -.->|Highest Priority Overlay| I18nResolver
    McpCache -.->|Dynamic Bridges| TurnRunner
```

---

### 2. Ecosystem Directory Structure

```text
~/.hermes/mods/
├── .engine/                     # Core runtime micro-kernel (Zero dependencies on core)
│   ├── loader.py                # Manifest parser, Section Reducer & cache compiler
│   ├── validator.py             # AST syntax checker, dependency DAG validator
│   ├── updater.py               # One-command atomic updater (runner.py update)
│   ├── snapshot_manager.py      # SHA-256 pre-apply delta snapshot manager
│   ├── watchdog.py              # Gateway supervision (30s window) with auto-rollback
│   ├── circuit_breaker.py       # Autonomous fault isolation (CLOSED / OPEN)
│   ├── i18n.py                  # Collocated 8-language localization & Mod Overlay
│   ├── config_manager.py        # Config schema validator & secrets.yaml (0600) isolation
│   └── tester.py                # Multi-threaded parallel test runner with cache
├── runner.py                    # Unified CLI entry point for humans and AI agents
├── install.sh                   # Resilient zero-downtime installer (Linux/macOS)
├── system/                      # Tier 0 System Infrastructure Mods
│   ├── mod-command-registry/    # Dynamic slash commands execution bridge
│   ├── mod-declarative-i18n/    # Collocated localization engine (Mod Overlay)
│   ├── mod-declarative-mcp/     # Manifest-driven MCP server integration
│   ├── mod-folder-skills/       # Folder mod skill auto-discovery
│   ├── mod-manager/             # Telegram /mods & /mod management (renamed from mod-slash-mods)
│   └── mod-update-integration.yaml # Auto-reapply on upstream hermes update
├── custom/                      # Tier 1 User & Feature Mods
│   ├── smart-compaction/        # Dual-trigger proactive context compaction
│   ├── slash-commands-ux/       # Overhaul: /undo, /goal, /model, /agents, /handoff...
│   ├── speech-to-text/          # Speculative racing STT (cloud vs local Whisper)
│   ├── text-to-speech/          # Multimodal voice synthesis pipeline
│   ├── tool-video-analyze/      # Video frame analysis integration
│   └── ...                      # 30+ additional production-tested mods
├── docs/                        # Architectural documentation and specifications
│   ├── architecture.md          # Technical deep-dive
│   ├── manifest-spec.md         # Manifest YAML schema specification
│   ├── creating-a-mod.md        # Step-by-step authoring tutorial
│   └── ai-agents-guide.md       # Comprehensive AI agent developer guide
├── SPECIFICATION.md             # Complete ecosystem specification & invariants
└── tests/                       # Automated test harness (277+ tests)
```

---

### 3. Transactional Two-Pass Patch Flow

```text
 1. SCAN & REDUCE           2. DAG & AST PRE-FLIGHT      3. DELTA SNAPSHOT           4. ATOMIC COMMIT
┌──────────────────┐       ┌──────────────────────┐    ┌────────────────────┐      ┌──────────────────┐
│ Discovers mods   │       │ Builds DAG graph     │    │ Archives original  │      │ Writes modified  │
│ Reducer compiles │ ────> │ Verifies AST syntax  │ ─> │ core files with    │ ───> │ files with       │
│ targets/*.yaml   │       │ Simulates in-memory  │    │ SHA-256 hashes     │      │ FileLock         │
└──────────────────┘       └──────────────────────┘    └────────────────────┘      └──────────────────┘
                                                                                         │
                                                                                         ▼
 7. RUNTIME STABLE          6. SUCCESS & HEALTHCHECK     5. GATEWAY WATCHDOG        [ FAILURE DETECTED ]
┌──────────────────┐       ┌──────────────────────┐    ┌────────────────────┐                │
│ All 37 mods live │       │ runner.py doctor     │    │ Observes gateway   │                ▼
│ Zero downtime    │ <──── │ Reports HEALTHY      │ <─ │ status for 30s     │      ┌──────────────────┐
│ Caches reloaded  │       │ 277 tests passed     │    │ (zero false drops) │      │ Auto-Restores    │
└──────────────────┘       └──────────────────────┘    └────────────────────┘      │ Delta Snapshot   │
                                                                               │ (Instant 0s Fix) │
                                                                               └──────────────────┘
```

---

## 🌟 Key Features & Architectural Pillars

| Pillar | Architecture & Rationale |
|---|---|
| **🛡️ Zero-Downtime Hot-Patching** | Core Hermes never imports `.engine/` or `runner.py`. Communication occurs strictly through JSON caches (`.commands_cache.json`, `.locales_cache.json`, `.mcp_cache.json`). The core invalidates internal caches on file `mtime` change without restarting gateway processes. |
| **⚡ Transactional Two-Pass Engine** | Patches are simulated in memory and parsed with `ast.parse()` before touching disk. If any block fails to match uniquely (0 matches or >1 ambiguous matches), the transaction aborts with zero side effects. |
| **📸 Pre-Apply Delta Snapshots** | `snapshot_manager.py` computes SHA-256 hashes of all affected core files and saves byte-exact archives before any apply. Snapshots can be restored at any time via `python3 runner.py snapshot restore`. |
| **🐕 Gateway Watchdog Supervision** | When applied with `--watchdog`, `watchdog.py` monitors `hermes-gateway.service` for 30 seconds. If a crash or restart loop occurs, it triggers an instant automatic rollback to the pre-apply snapshot. |
| **🔌 Autonomous Circuit Breaker** | `circuit_breaker.py` isolates repeatedly failing mods. If a mod exceeds error thresholds, its state trips to `OPEN`, unpatching its targets from core files while keeping Hermes Agent online. |
| **🌐 Universal 8-Language Localization** | Comprehensive Mod Overlay layer with highest resolution priority. Supports English (`en`), Russian (`ru`), Spanish (`es`), German (`de`), French (`fr`), Chinese (`zh`), Arabic (`ar`), Portuguese (`pt`), and all valid BCP-47 codes. |
| **🧩 Generic Section Reducer** | Strict Layout prevents monolithic YAML targets >50 KB. The loader's section reducer automatically merges decomposed files from `targets/*.yaml` into an ordered sequence. |
| **🏎️ Speculative Racing Fallback** | Designed for heavy APIs (STT/TTS). Spawns cloud and local paths concurrently; the winning worker delivers the response while the losing path is **immediately cancelled** to free system resources. |
| **🔒 Strict 0600 POSIX File Security** | Sensitive parameters (`sensitive: true`) are segregated into `~/.hermes/secrets.yaml` with strict `0600` permissions. Public configuration resides in `config.yaml`. Sensitive values are masked in CLI. |
| **📦 Portable Mod Distribution** | `runner.py export` / `import` packages mods into clean, portable tarballs stripped of `.git`, caches, and secrets, protected by TarBomb and Path Traversal guards. |

---

## 📦 Production Mod Catalog (37 Active Mods)

Every mod is tested, validated, localized into 8 languages, and active in production:

### 1. Tier 0: Core System Mods (`system/`)
*Infrastructure bridges connecting Hermes Agent to the declarative mod ecosystem.*

| Mod ID | Name | Ver | Description |
|---|---|:---:|---|
| `mod-command-registry` | **Command Registry** | 1.2.0 | Native registration, atomic caching, and execution of declarative slash commands from mods. |
| `mod-declarative-i18n` | **Declarative i18n** | 1.1.0 | Collocated localization engine with highest-priority Mod Overlay layer resolution. |
| `mod-declarative-mcp` | **Declarative MCP** | 1.1.0 | Manifest-driven Model Context Protocol server integration with dynamic variable interpolation. |
| `mod-folder-skills` | **Folder Mod Skills** | 1.2.0 | Tiered procedural skills integration (`skills/<name>/SKILL.md`) into runtime skill discovery. |
| `mod-manager` | **Mod Manager** | 1.2.0 | Native Telegram chat commands `/mods` and `/mod` to inspect status, catalog, logs, and health directly from chats (renamed from `mod-slash-mods`). |
| `mod-update-integration` | **Update Integration**| 1.1.0 | Automatic transactional reapplying of mods after Hermes upstream updates (`/update` and `hermes update`). |

---

### 2. Context Management & Intelligence (`custom/`)
*Next-generation context retention, compaction, and reasoning control.*

| Mod ID | Name | Ver | Description |
|---|---|:---:|---|
| `smart-compaction` | **Smart Compaction Dual-Trigger** | 1.4.0 | Intelligent dual-level context compaction: proactive live tool output trimming + idle background compression. |
| `reasoning-tool-turn-control` | **Reasoning Turn Control** | 1.2.0 | Prevents premature clean-stop on reasoning tool turns and provides accurate reasoning token accounting. |
| `core-tools-enhancements` | **Core Tools Enhancements** | 1.3.0 | Adds literal search flag (`-F`) to `search_files`, atomic writes in `write_file`, and vision error handling. |
| `skill-manage-frontmatter-validation` | **Frontmatter Validation** | 1.0.1 | Automatic YAML frontmatter validation during skill authoring and editing. |
| `skill-prompt-desc-limit` | **Skill Description Limit** | 1.3.0 | Configurable truncation threshold for skill descriptions in the system prompt. |

---

### 3. Telegram & User Experience Polish (`custom/`)
*Superior chat ergonomics, message formatting, and quiet modes.*

| Mod ID | Name | Ver | Description |
|---|---|:---:|---|
| `slash-commands-ux` | **Slash Commands UX Polish** | 4.0.0 | Complete UX overhaul: `/undo`, `/goal`, `/model`, `/agents`, `/compress [N]`, `/handoff`, `/context`, `/usage`, `/status`, and honest token cache percentage display. |
| `command-close` | **Command Close** | 1.4.0 | Slash commands `/close` and `/topic close` to close Telegram forum topics and safely archive sessions. |
| `command-control` | **Command Control** | 1.2.0 | Dynamic Telegram bot menu whitelist/blacklist management with low-latency dispatch. |
| `command-role` | **Command Role** | 1.2.0 | Slash command `/role` (alias: `/instruct`) for non-compressible session system prompts and rules. |
| `telegram-quiet-notifications` | **Quiet Notifications** | 2.3.0 | Mutes background system noise and sets silent delivery by default for service notices. |
| `telegram-ux` | **Telegram Rich Formatting** | 2.4.0 | Delivers rich Telegram messages (clean tables, LaTeX equations, spoilers, multiline blockquotes, photo batches). |
| `profile-switcher` | **Profile Switcher** | 1.3.0 | Dynamic switching of active Hermes profiles by Telegram forum topic (`/profile <name>`). |

---

### 4. Subagent Scaling & Queue Isolation (`custom/`)
*High-throughput multi-agent orchestration and queue safety.*

| Mod ID | Name | Ver | Description |
|---|---|:---:|---|
| `command-unlimited` | **Command Unlimited** | 1.3.0 | Slash command `/unlimited [on\|off\|status]` to unlock agent and subagent iteration caps. |
| `command-faster` | **Command Faster** | 1.3.0 | Slash command `/faster` for instant acceleration of current task and active subagents without duplicate outputs. |
| `command-queue` | **Command Queue Next** | 1.2.0 | Extends `/queue`: guaranteed non-interrupting delivery of next message or voice STT into queue. |
| `gateway-busy-queue-sender-isolation` | **Queue Sender Isolation** | 1.1.1 | Isolates busy session queues and photo batch aggregation by sender ID to prevent cross-user crosstalk. |

---

### 5. Multimodal Audio & Video (`custom/`)
*High-speed speech and vision processing with speculative racing.*

| Mod ID | Name | Ver | Description |
|---|---|:---:|---|
| `speech-to-text` | **Speech to Text (Speculative Race)** | 1.3.0 | Dual-engine STT: cloud multimodal API raced against local Faster-Whisper with instant cancellation. |
| `text-to-speech` | **Text to Speech** | 1.3.0 | Natural speech synthesis via external voice endpoints with automated text pronunciation conditioning. |
| `tool-video-analyze` | **Tool Video Analyze** | 1.4.0 | Registers `video_analyze` tool in core agent toolset, routed through auxiliary video pipelines. |
| `multimodal-fallback` | **Multimodal Fallback** | 1.1.0 | Envelopes tool images for user delivery and synthesizes text descriptions for non-vision LLMs. |
| `voice-command-custom` | **Voice Command Custom** | 1.3.0 | Adds `/voice` operational modes (`on`, `tts`, `off`) and on-demand async synthesis of last reply. |
| `stt-provider-echo` | **STT Provider Tracking** | 1.3.0 | Attaches verified STT provider metadata to transcripts and handles tuple types safely. |

---

### 6. Resilience, State & Infrastructure (`custom/`)
*Enterprise stability guards and connection management.*

| Mod ID | Name | Ver | Description |
|---|---|:---:|---|
| `network-resilience` | **Network Resilience Guard** | 1.3.0 | LLM stream retries, 60s connect timeouts, and Keyless MCP loop defense. |
| `subscription-limit-notice` | **Subscription Limit Guard** | 1.3.0 | Concise rate limit notices with reset timer countdowns, suppressing blind retry loops. |
| `profile-cascading` | **Profile Cascading & Deep Merge** | 1.1.0 | Hierarchical secret scope inheritance, cache sharing, and deep merge of `config.yaml`. |
| `state-db-threaded-repair-safety` | **State DB Repair Safety** | 1.0.1 | Drains idle SQLite connection pools before executing database vacuum and repair routines. |
| `terminal-core-fixes` | **Terminal & PTY Core Fixes** | 1.1.0 | Output drainage via `poll()` to guard against file descriptor overflow (`fd >= 1024`) and spurious SIGWINCH. |
| `inventory-moa-filter` | **Inventory MoA Filter** | 1.1.0 | Hides inactive or excluded Mixture-of-Agents providers from available model listings. |
| `multiplex-fallback-provider-env-resolution`| **Multiplex Env Resolution** | 1.0.1 | Resolves fallback provider environment variables within the profile secret scope. |

---

## 📋 Complete CLI Reference (`runner.py`)

Run commands directly using `python3 ~/.hermes/mods/runner.py <command>`:

```bash
# === Git Synchronization & Updates ===
python3 runner.py update                 # Atomic Git update + simulation + transactional apply
python3 runner.py update --check         # Check remote updates without applying
python3 runner.py update --dry-run       # In-memory update simulation without disk modification
python3 runner.py update --no-apply      # Pull repository updates without patching core
python3 runner.py push                   # Zero-friction auto-commit (Conventional Commit) + push to GitHub
python3 runner.py push --dry-run         # Dry-run push simulation (view staged files and commit message)
python3 runner.py push -m "message"      # Push with explicit commit message
python3 runner.py push --install-hook    # Install post-commit hook for auto-push on any git commit

# === Lifecycle & Patching ===
python3 runner.py apply                  # Apply all enabled mods in topological DAG order
python3 runner.py apply <mod_id>         # Apply specific mod and its dependencies
python3 runner.py apply --watchdog       # Apply with 30s gateway watchdog supervision
python3 runner.py verify [mod_id]        # Verify patch matching in-memory without modifying disk
python3 runner.py unpatch [mod_id]       # Soft rollback: removes custom mods, keeps system core
python3 runner.py purge --confirm        # Full wipe down to clean upstream vanilla Hermes

# === Diagnostic Healthchecks ===
python3 runner.py status                 # Ecosystem health dashboard (HEALTHY/DEGRADED)
python3 runner.py doctor                 # Comprehensive audit (Python, core permissions, DAG, locks)
python3 runner.py diff [mod_id]          # View unified diff between core upstream and patched state
python3 runner.py logs <mod_id> -n 50    # View recent journalctl and application logs for a mod

# === Mod Catalog & Inspection ===
python3 runner.py list                   # List all registered mods with status and versions
python3 runner.py list --format json     # Machine-readable inventory for AI agents
python3 runner.py info <mod_id>          # Detailed card with targets, options, and commands
python3 runner.py info <mod_id> --lang ru # Inspect mod details in any of 8 languages
python3 runner.py commands [--json]      # Summary of all active slash commands across mods

# === Configuration & Secrets ===
python3 runner.py config <mod_id>        # View options (secrets automatically masked)
python3 runner.py config <mod_id> --set key=value          # Update public option in config.yaml
python3 runner.py config <mod_id> --set-secret key=value   # Save secret strictly in secrets.yaml (0600)
python3 runner.py config <mod_id> --populate               # Populate default values

# === Snapshots & Circuit Breaker ===
python3 runner.py snapshot list          # List available pre-apply delta snapshots
python3 runner.py snapshot restore       # Instantly restore core to previous valid snapshot
python3 runner.py circuit-breaker status # Inspect fault isolation status (CLOSED/OPEN)
python3 runner.py circuit-breaker trip <mod_id> --reason "Crash"  # Manually isolate mod
python3 runner.py circuit-breaker reset <mod_id>                  # Reset isolation and re-enable

# === Development & Distribution ===
python3 runner.py create <mod_id> --type folder  # Scaffold new mod with Strict Layout
python3 runner.py validate --strict-layout       # Validate AST syntax and layout rules
python3 runner.py test [mod_id] --no-cache       # Execute test suite
python3 runner.py export <mod_id> --out mod.tar.gz --no-secrets  # Pack portable archive
python3 runner.py import mod.tar.gz              # Safely unpack and validate external mod
```

---

## 🛠️ Creating a New Mod in 3 Steps

Use the built-in generator to scaffold a mod conforming to Strict Layout:

```bash
# 1. Scaffold mod
python3 ~/.hermes/mods/runner.py create my-feature --type folder

# 2. Validate AST and schema
python3 ~/.hermes/mods/runner.py validate my-feature --strict-layout

# 3. Apply with watchdog supervision
python3 ~/.hermes/mods/runner.py apply my-feature --watchdog
```

For complete authoring conventions, directory layouts, and templates, see **[SPECIFICATION.md](SPECIFICATION.md)**.

---

## 🤖 AI Agent Integration Guide

Autonomous AI coding agents (Claude Code, OpenAI Codex, Cursor, Hermes Agent) can author, test, and patch mods autonomously without human supervision.

Read the authoritative AI handbook: **[AI Agents Developer Guide](docs/ai-agents-guide.md)**.

Key agent rules:
- **Zero Core Imports:** Never `import engine` in core files.
- **Decomposed Targets:** Use `targets/*.yaml` to avoid monolithic files >50 KB.
- **English-by-Default:** Manifests in English; translations collocated in `i18n/*.yaml`.
- **Zero Downtime:** Never invoke `systemctl restart hermes-gateway`.
- **Autonomous Error Resolution:** Use `runner.py status --format json`, `verify`, `doctor`, and `snapshot restore` for autonomous self-healing.

---

## 🧪 Testing & Quality Assurance

The ecosystem maintains a comprehensive automated test harness with **277 passing tests**:

```bash
# Run complete test suite:
pytest ~/.hermes/mods/tests -q

# Run with fail-fast on first error:
python3 ~/.hermes/mods/runner.py test -x --no-cache
```

Test coverage includes:
- Manifest schema and dependency DAG cycle detection (Tarjan / Kahn algorithms).
- Generic section reducer and Strict Layout validators.
- Pre-apply delta snapshots, byte-for-byte SHA-256 verification, and restore routines.
- Collocated 8-language localization merging and FileLock atomicity.
- Circuit breaker state machine transitions and incident counters.
- Clean export sanitization (guaranteeing zero leaked secrets).

---

## 🤝 Contributing

We welcome community contributions, new feature mods, and localization improvements! Please review our **[Contributing Guide](CONTRIBUTING.md)** and ensure all submissions pass `python3 runner.py validate --strict-layout` and `pytest tests/`.

---

## 📜 License

HermesModsEngine is open-source software licensed under the **[MIT License](LICENSE)**.  
Built for the **[Nous Research Hermes Agent](https://hermes-agent.nousresearch.com)** ecosystem.
