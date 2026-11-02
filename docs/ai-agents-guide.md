# HermesModsEngine — Autonomous AI Agent Developer Guide (`docs/ai-agents-guide.md`)

> **Target Audience:** Autonomous AI coding agents (Claude Code, OpenAI Codex, Hermes Agent, Cursor Agent, Windsurf, Devin, GitHub Copilot CLI) and multi-agent orchestration frameworks operating without human intervention.

---

## ⚡ Executive Mission & The Prime Invariant

HermesModsEngine provides enterprise-grade, transactional hot-patching and extension capabilities for [Hermes Agent](https://hermes-agent.nousresearch.com). It allows agents to modify, expand, and enhance Hermes core behavior without maintaining hard forks or breaking upstream compatibility.

```text
╔══════════════════════════════════════════════════════════════════════════════════════╗
║                          CRITICAL PRIME INVARIANT: ZERO DOWNTIME                     ║
║                                                                                      ║
║  STRICTLY FORBIDDEN:                                                                 ║
║  • NEVER execute 'systemctl restart hermes-gateway' or 'systemctl reload'            ║
║  • NEVER execute 'hermes gateway restart' or kill running gateway PIDs               ║
║  • NEVER reboot services during patch application or updates                         ║
║                                                                                      ║
║  All modifications take effect live through transactional AST patching and           ║
║  JSON cache mtime invalidation without dropping active sessions or turns!            ║
╚══════════════════════════════════════════════════════════════════════════════════════╝
```

---

## 🧭 Autonomous Protocol for AI Agents

Whenever an AI agent is tasked with inspecting, installing, extending, or debugging Hermes Agent capabilities, it must follow this 4-stage lifecycle:

```text
┌─────────────────────────────────┐
│ Stage 1: Detect Engine Status   │ ──> Is ~/.hermes/mods/runner.py available?
└────────────────┬────────────────┘
                 │
                 ├── No ──> Stage 2: Install Engine in 1 Command
                 │
                 └── Yes ─> Stage 3: Author, Test & Apply Mod
                                    │
                                    └── Error? ──> Stage 4: Autonomous Self-Healing
```

---

## 🔍 Stage 1: Autonomous Engine Detection

Before performing any mod operations, verify whether HermesModsEngine is installed and healthy on the target environment.

### 1.1 Fast Shell Detection Probe
Execute the following non-blocking check:
```bash
if [ -f "$HOME/.hermes/mods/runner.py" ]; then
    python3 "$HOME/.hermes/mods/runner.py" status --format json
else
    echo '{"installed": false}'
fi
```

### 1.2 Parsing the Health Response
When installed, `runner.py status --format json` outputs structured telemetry:
```json
{
  "engine_version": "1.4.0",
  "status": "HEALTHY",
  "mods_enabled": true,
  "counts": {
    "applied": 37,
    "disabled": 0,
    "total": 37
  },
  "hermes_core": {
    "path": "/usr/local/lib/hermes-agent",
    "version": "0.22.5",
    "writable": true
  },
  "dag_clean": true,
  "circuit_breaker": {
    "active": true,
    "tripped": 0
  }
}
```

* **If `installed: false`:** Proceed to **Stage 2** (Install).
* **If `status: "HEALTHY"`:** Engine is operational. Proceed to your task.
* **If `status: "DEGRADED"`:** Run `python3 ~/.hermes/mods/runner.py doctor --no-color` to identify issues.

---

## ⚡ Stage 2: Autonomous 1-Command Installation

If the engine is missing, install it using the official one-line command:

```bash
curl -sSL https://raw.githubusercontent.com/steklyashka33/HermesModsEngine/main/install.sh | bash
```

### 2.1 Autonomous Post-Install Verification
Immediately confirm installation success and ecosystem health:
```bash
python3 ~/.hermes/mods/runner.py doctor --no-color
python3 ~/.hermes/mods/runner.py status
```

### 2.2 Configure Automated Nightly Auto-Updates (Cron)
Immediately configure the nightly three-phase auto-update scheduler:
```bash
python3 ~/.hermes/mods/runner.py cron install
python3 ~/.hermes/mods/runner.py cron status
```
This ensures the engine, system mods, and Hermes core stay updated nightly at 04:00 with zero downtime.

### 2.3 Non-Interactive Installation Flags
For custom environments, the installer supports headless flags:
```bash
# Install with automatic cron update scheduling enabled:
curl -sSL https://raw.githubusercontent.com/steklyashka33/HermesModsEngine/main/install.sh | bash -s -- --cron

# Custom target location:
bash install.sh --target /custom/path/mods

# Fetch and install without applying patches to core immediately:
bash install.sh --no-apply

# Dry run simulation (validates Python 3.10+, Git, and core paths without disk writes):
bash install.sh --dry-run
```

---

## 🛠️ Stage 3: Creating, Verifying & Applying Mods

Follow this strict 7-step sequence when authoring or modifying mods:

```text
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  1. Scaffold │ ──> │   2. Author  │ ──> │ 3. Validate  │ ──> │   4. Test    │
│ runner.py    │     │ Targets &    │     │ --strict-    │     │ runner.py    │
│ create       │     │ Manifest     │     │ layout       │     │ test         │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                      │
┌──────────────┐     ┌──────────────┐     ┌──────────────┐            │
│ 7. Telemetry │ <── │   6. Apply   │ <── │  5. Verify   │ <──────────┘
│ runner.py    │     │ runner.py    │     │ runner.py    │
│ status       │     │ apply        │     │ verify       │
└──────────────┘     └──────────────┘     └──────────────┘
```

### Step 3.1: Scaffolding a New Mod
Always use the built-in generator to ensure standard folder layout:
```bash
# Multi-target or complex mod (Strict Layout recommended):
python3 ~/.hermes/mods/runner.py create <mod_id> --type folder

# Single-file compact patch:
python3 ~/.hermes/mods/runner.py create <mod_id> --type single
```
*Naming rule:* Lowercase alphanumeric characters and hyphens only (`^[a-z0-9-]+$`).

### Step 3.2: Strict Layout Directory Structure
For folder mods (`custom/<mod_id>/`):
```text
custom/<mod_id>/
├── mod.yaml             # Metadata only (id, name, version, author, enabled, dependencies)
├── targets/             # Decomposed target YAML files (<50 KB each)
│   ├── 01-agent.yaml    # First atomic target patch
│   └── 02-gateway.yaml  # Second atomic target patch
├── config.yaml          # Public options schema and sensitive field definitions
├── commands.yaml        # Declarative slash commands registration
├── i18n/                # 8 mandatory collocated localization files
│   ├── en.yaml          # English (Base standard)
│   ├── ru.yaml          # Russian
│   ├── es.yaml          # Spanish
│   ├── fr.yaml          # French
│   ├── de.yaml          # German
│   ├── zh.yaml          # Chinese
│   ├── ar.yaml          # Arabic
│   └── pt.yaml          # Portuguese
└── tests/               # Native pytest automated unit tests
    └── test_<mod_id>.py
```

### Step 3.3: Target Replacement Syntax (`targets/*.yaml`)
Targets execute exact literal string substitutions with AST pre-flight validation:
```yaml
targets:
  - file: agent/turn_runner.py       # Relative to Hermes core root
    description: "Add timeout recovery handler to turn runner"
    old_block: |
      try:
          response = await self.client.generate(prompt)
      except Exception as exc:
          logger.error(f"Turn execution failed: {exc}")
          raise
    new_block: |
      try:
          response = await self.client.generate(prompt)
      except TimeoutError as exc:
          logger.warning(f"Turn timeout intercepted, triggering recovery: {exc}")
          response = await self.fallback_recovery(prompt)
      except Exception as exc:
          logger.error(f"Turn execution failed: {exc}")
          raise
```

#### Invariant Target Rules:
1. **2–4 Context Lines:** Always provide 2–4 unchanged surrounding lines above and below your modification in `old_block` to guarantee unique matching (exactly 1 match in file).
2. **Pure Literal Block Scalar:** Use `|` with exact indentation (2 or 4 spaces, never tabs).
3. **No Monoliths >50 KB:** Break large patches into `targets/01-*.yaml`, `targets/02-*.yaml`.

### Step 3.4: Configuration & Secrets Separation
```yaml
# custom/<mod_id>/config.yaml
config:
  max_retries:
    type: int
    default: 3
    description: "Maximum retry attempts"
  api_key:
    type: string
    sensitive: true      # Stored exclusively in ~/.hermes/secrets.yaml (0600)
    description: "External API token"
```
* **Public values:** Read from `~/.hermes/config.yaml` (`mods.<mod_id>.<key>`).
* **Sensitive values:** Stored in `~/.hermes/secrets.yaml` with POSIX permissions `0600`. Secrets are automatically masked as `||********||` in CLI and logs.

### Step 3.5: Mandatory 8-Language Localization (`i18n/*.yaml`)
* **English-by-Default:** Manifests, code comments, and log messages are written in English.
* **Collocated Translations:** Every command, configuration option, and user message must have translations in all 8 languages (`en`, `ru`, `es`, `fr`, `de`, `zh`, `ar`, `pt`).
* **Zero Core File Patches:** Never create patches targeting `locales/*.yaml` in core Hermes. Use the Mod Overlay layer in `i18n/`.

### Step 3.6: Static Validation (AST & Schema Gate)
```bash
python3 ~/.hermes/mods/runner.py validate <mod_id> --strict-layout
```
Validation verifies:
- YAML syntax and section schemas.
- Clean DAG (no cyclic dependencies).
- AST parse tree of `new_block` (rejects Python syntax errors before touching disk).
- File size bounds (<50 KB per target file).

### Step 3.7: Automated Unit Testing
```bash
python3 ~/.hermes/mods/runner.py test <mod_id> --no-cache
```

### Step 3.8: In-Memory Verification (Dry-Run)
```bash
python3 ~/.hermes/mods/runner.py verify <mod_id>
```
Confirms that all `old_block` targets match clean core code uniquely.

### Step 3.9: Transactional Apply with Watchdog Supervision
```bash
python3 ~/.hermes/mods/runner.py apply <mod_id> --watchdog
```
* `--watchdog` activates a 30-second supervision window. If the gateway crashes or fails health checks, it automatically restores the pre-apply delta snapshot.

---

## 🔄 Stage 4: Autonomous Update & Self-Healing

AI agents must handle routine updates and runtime anomalies without human intervention.

### 4.1 Updating the Engine in 1 Command
To pull latest engine updates, bugfixes, and system mods:
```bash
python3 ~/.hermes/mods/runner.py update
```
* **Update Guarantees:**
  - Performs git fetch and pre-flight in-memory simulation before applying.
  - Automatically preserves user-authored mods in `custom/` and secrets in `secrets.yaml`.
  - Re-applies all active mods transactionally with zero service restarts.
  - Regenerates `.commands_cache.json`, `.locales_cache.json`, and `.mcp_cache.json`.

* **Non-destructive Update Flags:**
  ```bash
  # Check if updates are available without pulling:
  python3 ~/.hermes/mods/runner.py update --check

  # Simulate update and patch application in memory:
  python3 ~/.hermes/mods/runner.py update --dry-run

  # Pull git repo without reapplying patches:
  python3 ~/.hermes/mods/runner.py update --no-apply
  ```

---

## 🚨 Deterministic Self-Healing Decision Tree

If an autonomous agent encounters an error, execute these automated remediations:

| Symptom / Error | Root Cause | Automated Agent Remediation Protocol |
|---|---|---|
| **`Target matched 0 times`** | Core code differs from `old_block` (upstream update or missing context) | 1. Run `python3 runner.py diff <mod_id>` to view current core state.<br>2. Inspect `/usr/local/lib/hermes-agent/<file>` to read current upstream syntax.<br>3. Update `old_block` with 2–4 lines of current surrounding context.<br>4. Re-run `python3 runner.py verify <mod_id>`. |
| **`Target matched N>1 times`** | Ambiguous context in `old_block` | 1. Add 2 additional lines above and below the change in `old_block` until match count is strictly 1.<br>2. Re-run `python3 runner.py verify <mod_id>`. |
| **`AST SyntaxError: invalid syntax`** | Malformed Python in `new_block` | 1. Check indentation (spaces vs tabs, uneven indents).<br>2. Run `python3 -m py_compile` on snippet.<br>3. Re-run `python3 runner.py validate <mod_id> --strict-layout`. |
| **`Core File Divergence` / `diverged`** | Core modified out-of-band | 1. List available snapshots: `python3 runner.py snapshot list`.<br>2. Restore clean core: `python3 runner.py snapshot restore`.<br>3. Reapply: `python3 runner.py apply --force`. |
| **Gateway crash during apply** | Bad code triggered startup failure | 1. Watchdog auto-restores previous snapshot within 30s.<br>2. Read recent error logs: `python3 runner.py logs <mod_id> -n 50`.<br>3. Isolate mod via Circuit Breaker: `python3 runner.py circuit-breaker trip <mod_id> --reason "Startup crash"`. |
| **Circuit Breaker Tripped (`OPEN`)** | Mod crashed runtime >5 times in 300s | 1. Read incidents: `python3 runner.py circuit-breaker failures <mod_id>`.<br>2. Fix code in `custom/<mod_id>/`.<br>3. Reset breaker: `python3 runner.py circuit-breaker reset <mod_id>`.<br>4. Reapply: `python3 runner.py apply <mod_id>`. |
| **User mod misbehavior (Safe Rollback)** | Non-critical mod bug | Run `python3 runner.py unpatch <mod_id>` (removes target mod while keeping Tier 0 system infrastructure online). |
| **Emergency Vanilla Reset** | Complete recovery needed | Run `python3 runner.py purge --confirm` (reverts Hermes core 100% to clean upstream vanilla state). |

---

## 🤖 Machine-Readable CLI Output Reference (`--format json`)

Autonomous agents should parse JSON output rather than text streams:

```bash
# 1. Full ecosystem state
python3 ~/.hermes/mods/runner.py status --format json

# 2. Mod catalog and statuses
python3 ~/.hermes/mods/runner.py list --format json

# 3. Detailed mod card
python3 ~/.hermes/mods/runner.py info <mod_id> --format json

# 4. Registered slash commands registry
python3 ~/.hermes/mods/runner.py commands --json

# 5. Mod logs and errors
python3 ~/.hermes/mods/runner.py logs <mod_id> --format json

# 6. Auto-update cron job status
python3 ~/.hermes/mods/runner.py cron status --format json
```

### JSON Exit Code Conventions:
* `0`: Success / Passed / Healthy.
* `1`: Validation or application failure (safe rollback executed).
* `2`: Environment or dependency issue (check `runner.py doctor`).

---

## 📚 Architectural Summary for Multi-Agent Systems

1. **Isolation Invariant:** Core Hermes never imports `.engine/`. Flat JSON caches bridge mods to core.
2. **Zero Downtime:** Never restart gateway processes (`hermes-gateway`).
3. **Collocated i18n:** English default in manifests; 8 languages in `i18n/*.yaml`.
4. **Secret Security:** Sensitive values strictly in `~/.hermes/secrets.yaml` with permissions `0600`.
5. **Tier Hierarchy:** Tier 0 (system infrastructure) applies before Tier 1 (custom mods). Custom mods never declare system dependencies in `mod.yaml`.
