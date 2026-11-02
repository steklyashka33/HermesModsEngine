# Hermes Mods Engine: Architecture & Design

The Hermes Mods Engine is a declarative runtime patching and extension framework for Hermes Agent. It enables developers to customize, extend, and monitor the Hermes runtime while guaranteeing that modifications survive upstream repository updates (`hermes update`).

---

## 1. System Overview

```
                      +-----------------------------+
                      |         runner.py           |
                      |   (CLI & Orchestrator)      |
                      +--------------+--------------+
                                     |
       +-----------------------------+-----------------------------+
       |                             |                             |
+------v-------+             +-------v------+             +--------v--------+
|   .engine/   |             |   system/    |             |     custom/     |
| Micro-Core   |             | System Mods  |             | User Extensions |
+--------------+             +--------------+             +-----------------+
       |
       +---> loader.py           (Discovery, Manifest Parsing, Structured Scan)
       +---> models.py           (ModTarget, ConfigField, ModCommand, DAG)
       +---> validator.py        (Static AST Parsing, Schema Validation)
       +---> config_manager.py   (0600 Secure Secrets & Config Management)
       +---> monitor_manager.py  (Process Supervision & TokenBucket Rate Limiting)
       +---> updater.py          (Upstream Sync, Compatibility, Rollbacks)
```

---

## 2. Core Components

### 2.1 The Runner (`runner.py`)
`runner.py` is the unified entrypoint for managing the mod ecosystem. It parses command-line arguments, configures directory paths, resolves dependency closures, and delegates operations to the micro-engine.

Supported operations:
- `apply`: Applies target patches transactionally.
- `verify`: Simulates patch application in-memory and executes verification commands.
- `validate`: Runs static AST checks and schema validation.
- `list`: Displays loaded mods, authors, statuses, and capabilities.
- `enable` / `disable`: Toggles activation status with cascade dependency management.
- `config`: Reads and updates user configuration (`--set`, `--set-secret`).
- `monitors`: Supervised background daemon management.
- `mcp`: Declarative MCP server discovery and synchronization.
- `commands`: Declarative slash command inspection.
- `update`: Upstream synchronization with automatic backup and rollback.

### 2.2 Structured Discovery (`.engine/loader.py`)
The loader scans directories in priority order:
1. `system/`: Core system extensions (e.g. `mod-command-registry`, `mod-folder-skills`, `mod-declarative-mcp`).
2. `custom/`: User-defined mods (both single-file `*.yaml` and folder mods `<id>/mod.yaml`).
3. Root directory: Legacy flat layout support.
Excluded folders (`templates`, `docs`, `tests`, `skills`, `.engine`, `.git`, `.github`) are ignored automatically.

### 2.3 Dependency Graph & Cycle Detection (`ModDependencyGraph`)
Mods declare dependencies via `dependencies: [mod-a, mod-b]`. The engine builds a Directed Acyclic Graph (DAG) and applies:
- **Kahn's Topological Sort:** Ensures mods are applied in strict dependency order.
- **Tarjan / DFS Cycle Detection:** Immediately raises `CycleDetectionError` with the exact cyclic chain if a dependency loop is detected (e.g. `A -> B -> C -> A`).
- **Closure Resolution:** Resolves transitive dependencies automatically when applying a single mod.

### 2.4 Static AST & Manifest Validator (`.engine/validator.py`)
Before modifying any live file, the validator verifies:
- **AST Correctness:** Uses `ast.parse()` on every `new_block` to catch syntax errors, unclosed strings/brackets, and indentation errors before code touches disk.
- **Target File Sanity:** Checks that target paths exist relative to the Hermes root (with graceful fallback warnings in standalone CI environments).
- **Schema Validation:** Verifies types, bounds (`min`/`max`), option sets, and identifier patterns (`^[a-z0-9_-]+$`).

### 2.5 Transactional Two-Pass Patch Engine
Patching follows a strict transactional flow:
1. **In-Memory Simulation:** The engine loads all affected files into memory and attempts all string replacements. If any target matches 0 times (upstream code changed) or >1 times (ambiguous match), the entire transaction aborts.
2. **Pre-flight AST Verification:** The simulated modified file content is validated against AST syntax parsers.
3. **Atomic Backup & Write:** Original file contents are preserved in memory and disk backups before write.
4. **Post-Apply Verification:** If `verify_cmd` is specified, the engine runs the command. On failure, all files are restored from backup immediately.

### 2.6 Decoupled Secrets & Configuration (`.engine/config_manager.py`)
- Standard options are written to `config.yaml` under `mods.<mod_id>.<key>`.
- Sensitive fields (`sensitive: true`) are written to `secrets.yaml` with strict `0600` POSIX file permissions (readable and writable only by the owner).
- The CLI automatically masks sensitive values when inspecting configurations.

### 2.7 Supervised Background Daemons (`.engine/monitor_manager.py`)
Mods can declare background monitors (`monitors:`). The manager handles:
- Daemon process spawning with environment variables (`HERMES_MOD_DIR`, `HERMES_MOD_DATA`).
- Health checks and PID tracking via `.state.json`.
- **TokenBucket Rate Limiting:** Prevents output flood from spamming notification channels (burst 10 events, refill 0.5 tokens/sec).

---

## 3. Runtime Integration Bridges

### 3.1 Slash Command Registry (`mod-command-registry.yaml`)
Injects `ModCommandRegistry` into Hermes CLI command dispatch. Active mods cache their commands in `.commands_cache.json`, enabling millisecond resolution and idle-pipeline execution without touching Hermes core files on every turn.

### 3.2 Mod Folder Skills (`mod-folder-skills.yaml`)
Integrates mod-bundled skills (`mod_dir/skills/`) directly into Hermes Agent's skill search tiering.
Search precedence:
1. Project Skills (`.hermes/skills/`)
2. User Profile Skills (`~/.hermes/skills/`)
3. Create Dir Skills
4. **Mod Skills (`TIER_MODS`)**
5. External Directories

### 3.3 Declarative MCP Servers (`mod-declarative-mcp.yaml`)
Transparently exposes MCP servers defined in mod manifests to the Hermes MCP tool discovery pipeline, supporting Claude Code / portable formats with variable interpolation.
