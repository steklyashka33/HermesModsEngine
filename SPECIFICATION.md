# HermesModsEngine Specification (v1.5.0 Standard)

> **Status:** Active Standard  
> **Version:** 1.5.0  
> **Applies to:** Hermes Agent core, micro-engine (`.engine/`), and all registered mods (`system/` and `custom/`).

---

## 1. Abstract

The **HermesModsEngine** specification defines the declarative hot-patching, configuration isolation, localization overlay, and runtime lifecycle standards for extensions built on top of **Hermes Agent**.

The primary objective is **survivability**: allowing deep modifications to Hermes Agent behavior, CLI/Telegram interfaces, subagent orchestration, and tooling without maintaining fragile forks of upstream repositories.

---

## 2. Directory Layout & Mod Taxonomy

The mod ecosystem is organized into two primary tiers:

```text
~/.hermes/mods/
├── runner.py                  # Unified CLI orchestrator and entrypoint
├── .engine/                   # Core micro-engine modules
│   ├── models.py              # Schema models, DAG representation, dataclasses
│   ├── loader.py              # Recursive discovery, manifest parsers, generic reducer
│   ├── validator.py           # Static AST syntax parser and schema checks
│   ├── config_manager.py      # Flat config and 0600 POSIX secrets isolation
│   ├── monitor_manager.py     # Background daemon supervisor with TokenBucket limiter
│   ├── snapshot_manager.py    # Pre-apply delta snapshots and restore engine
│   ├── watchdog.py            # Gateway stability observer (30s window)
│   ├── circuit_breaker.py     # Incident counter, failure isolation, and trip states
│   ├── tester.py              # Pytest and inline test runner with cache
│   ├── inspector.py           # Diff and doctor diagnostics
│   └── exporter.py            # Clean pack/export and safe import sandbox
├── system/                    # Tier 0: Core System Mods (infrastructure bridges)
│   ├── mod-command-registry/
│   ├── mod-declarative-i18n/
│   ├── mod-declarative-mcp/
│   ├── mod-folder-skills/
│   ├── mod-manager/
│   └── mod-update-integration/
├── custom/                    # Tier 1: User & Feature Mods
│   ├── <single-file-mod>.yaml
│   └── <folder-mod>/
│       ├── mod.yaml           # Metadata manifest
│       ├── targets/           # Decomposed target replacement patches
│       ├── config.yaml        # Options and sensitive keys schema
│       ├── commands.yaml      # Slash commands declarations
│       ├── tests/             # Pytest test cases
│       ├── skills/            # Procedural agent skills
│       ├── scripts/           # Executable workers and helper scripts
│       └── i18n/              # 8-language localization files
├── .commands_cache.json       # Live slash commands registry cache
├── .locales_cache.json        # Compiled Mod Overlay localization cache
└── .mcp_cache.json            # Declarative MCP servers cache
```

### 2.1 Single-File Mod Layout
Single-file mods reside in `custom/<mod_id>.yaml`. They are suited for compact patches (1–3 targets) without external scripts, skills, or monitors.
- Allowed sections: `id`, `name`, `version`, `author`, `enabled`, `dependencies`, `targets`, `config`, `commands`, `tests`, `i18n`.
- Forbidden: `scripts/`, `skills/` (folder mod required for assets).

### 2.2 Folder Mod Strict Layout
Folder mods reside in `custom/<mod_id>/`. Strict Layout enforces decomposition:
- `mod.yaml`: Contains **only** scalar metadata (`id`, `name`, `description`, `version`, `author`, `enabled`, `dependencies`).
- `targets/*.yaml` or `targets.yaml`: Target replacement patches. Monolithic files >50 KB are strictly rejected by the validator.
- `config.yaml`: Configuration schema and options.
- `commands.yaml`: Slash command registrations.
- `i18n/*.yaml`: Collocated translations (English default + BCP 47 locales).
- `tests/test_*.py`: Native pytest test files.
- `skills/<name>/SKILL.md`: Optional procedural skills automatically registered in Hermes skill tiering.
- `scripts/*.py`: Optional helper daemons or processors.

---

## 3. Manifest Schemas & Data Models

### 3.1 Metadata Manifest (`mod.yaml`)

```yaml
id: string             # Required. Pattern: ^[a-z0-9_-]+$
name: string           # Required. Human-readable display name (English)
description: string    # Required. English summary of mod capabilities
version: string        # Required. Semver (e.g. 1.4.0)
author: string         # Required. Author name or organization
enabled: boolean       # Required. Default: true
dependencies: list     # Optional. List of mod_id dependencies.
                       # Custom mods may only depend on custom mods.
```

### 3.2 Target Patch Schema (`targets/*.yaml`)

```yaml
targets:
  - file: string          # Required. Relative path from Hermes Agent core root
    description: string   # Optional. Summary of target rationale
    old_block: string     # Required. Exact upstream code block + 2-4 context lines
    new_block: string     # Required. Replacement code block
    verify_cmd: string    # Optional. Shell command executed post-apply
```

#### Invariants:
1. **Uniqueness:** `old_block` must match exactly 1 time in the target file.
2. **Context lines:** Must contain 2–4 lines of immutable surrounding code to guarantee unique matching across minor upstream revisions.
3. **AST Validation:** `new_block` is parsed via Python `ast.parse()` prior to disk modification. Syntax errors abort the transaction.
4. **No Core Locale Targets:** Patching core `locales/*.yaml` directly via `targets:` is strictly prohibited.

### 3.3 Configuration Schema (`config.yaml`)

```yaml
config:
  <key_name>:
    type: string | int | float | bool
    default: any
    description: string
    min: number           # Optional numeric lower bound
    max: number           # Optional numeric upper bound
    options: list         # Optional enum choices
    sensitive: boolean    # Optional. Default: false.
                          # If true, stored strictly in ~/.hermes/secrets.yaml (0600)
    required: boolean     # Optional. Default: false
```

#### Secrets Security Policy:
- Sensitive parameters (`sensitive: true`) are never written to `config.yaml`.
- The engine enforces POSIX file permission `0600` on `~/.hermes/secrets.yaml`.
- During CLI printing (`runner.py config`), sensitive values are automatically masked as `||********||`.

### 3.4 Slash Commands Schema (`commands.yaml`)

```yaml
commands:
  - name: string          # Required. Command name without leading slash (^[a-z0-9_-]+$)
    description: string   # Required. English summary for /help
    args_hint: string     # Optional. Arguments hint (e.g. "[--dry-run] [N]")
    category: string      # Optional. Grouping category
    mode: replace | extend # Default: replace. 'replace' overrides core command.
    telegram_menu: bool   # Default: false. If true, registers in BotCommand menu.
    aliases: list         # Optional. Alternative aliases
    python: string        # Optional. Inline Python snippet executed in agent context
    handler: string       # Optional. Relative script path (e.g. "scripts/handler.py")
```

---

## 4. Cache-Driven Architecture & Zero Downtime

Hermes Agent communicates with the mod ecosystem through atomic, flat JSON caches:

```text
┌──────────────────────┐         FileLock Write         ┌──────────────────────┐
│                      │ ─────────────────────────────> │ .commands_cache.json │
│   HermesModsEngine   │ ─────────────────────────────> │ .locales_cache.json  │
│     (runner.py)      │ ─────────────────────────────> │ .mcp_cache.json      │
│                      │                                └──────────────────────┘
└──────────────────────┘                                           │
                                                                   │ mtime polling
                                                                   ▼
                                                        ┌──────────────────────┐
                                                        │  Hermes Agent Core   │
                                                        │  (Zero Restart, 0s)  │
                                                        └──────────────────────┘
```

1. **Isolation Invariant:** No Python imports of engine modules within core code.
2. **Atomic Cache Updates:** Caches are written using temporary swap files with `fcntl.flock` to prevent partial reads by concurrent workers.
3. **Instant Cache Reload:** The core inspects filesystem `st_mtime` on turn invocation. When mtime increases, internal dictionaries are purged and reloaded instantly without killing active connections.

---

## 5. Mod Overlay Localization Hierarchy

Localization strings are resolved across 4 distinct layers:

| Priority | Layer | Path | Description |
|:---:|:---|:---|:---|
| **1 (Highest)** | **`mod overlay`** | `.locales_cache.json` | Compiled translations from active mods. Overrides any core or plugin key. |
| **2** | **`packs`** | Dynamic Plugins | Registered via `PluginContext.register_locale()`. |
| **3** | **`overlay`** | `~/.hermes/locales/<lang>.yaml` | User profile-level manual overrides. |
| **4 (Base)** | **`bundled`** | `/usr/local/lib/hermes-agent/locales/<lang>.yaml` | Shipped upstream vanilla Hermes strings. |

- **Language Scope:** Validated against BCP 47 (`^[a-z]{2,3}(-[a-z0-9]{2,8})*$`).
- **Base 8-Language Matrix:** English (`en`), Spanish (`es`), French (`fr`), Chinese (`zh`), Arabic (`ar`), Russian (`ru`), German (`de`), Portuguese (`pt`).
- **Scope of Mod Localization:** Mod metadata, slash command descriptions, config titles/hints, core message overrides (`gateway.*`, `cli.*`), and custom mod strings.

---

## 6. Dependency Graph & Kahn's Topological Sort (DAG)

1. **Cycle Prevention:** Before applying mods, the engine constructs a Directed Acyclic Graph. Tarjan's / DFS cycle detection guarantees zero cyclic dependencies.
2. **Tier-Aware Ordering:** System mods (`system/`) always receive tier key `0`, ensuring they are sorted and applied before custom mods (tier key `1`).
3. **Transitive Closure:** Applying a specific mod automatically discovers and verifies all upstream dependencies.

---

## 7. Engine Resiliency Systems

### 7.1 Pre-Apply Delta Snapshots (`snapshot_manager.py`)
- Prior to modifying any file on disk, the engine computes SHA-256 hashes of all target files and archives their exact bytes into `~/.hermes/mods/.snapshots/<id>/`.
- In the event of a verification failure, exception, or interrupt, the transaction is rolled back with 100% byte-for-byte fidelity.

### 7.2 Gateway Watchdog Supervision (`watchdog.py`)
- When mods are applied with `--watchdog`, the runner observes `hermes-gateway.service` for 30 seconds.
- If the gateway fails, restarts, or crashes during this window, the engine immediately restores the pre-apply snapshot.

### 7.3 Circuit Breaker Pattern (`circuit_breaker.py`)
- Tracks sequential runtime failures per mod in `.state.json`.
- When failures exceed the threshold (default: 3), the circuit breaker transitions from `CLOSED` to `OPEN`.
- In `OPEN` state, the mod's targets are automatically unpatched from core files, isolating the failure while keeping Hermes Agent online.
