# Hermes Mod Manifest Specification

This document defines the schema, fields, and conventions for declarative Hermes Agent mod manifests.

---

## 1. Overview & Manifest Types

The Hermes Mod Engine supports two primary mod layouts:

1. **Single-File Mod (`<mod-id>.yaml`):**
   - A single YAML file containing metadata, targets, configuration schema, commands, and monitors.
   - Recommended for lightweight patches (1–5 targets).
   - Located in `system/<mod-id>.yaml` or `custom/<mod-id>.yaml` (or flat in `~/.hermes/mods/`).

2. **Folder Mod (`<mod-id>/`):**
   - A directory containing a primary manifest (`mod.yaml`) and optional modular subdirectories:
     - `targets/` — feature-oriented target files (`targets/*.yaml`).
     - `skills/` — agent skills (`skills/<skill-name>/SKILL.md`) automatically discovered when enabled.
     - `scripts/` — executable Python/shell handlers and monitor scripts.
     - `monitors/` — standalone monitor definitions (`monitors/*.yaml`).
     - `commands/` — standalone command definitions (`commands/*.yaml`).
   - Recommended for full-featured extensions, multi-component modules, and background daemons.

---

## 2. Root Manifest Attributes

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `id` | `string` | **Yes** | file stem | Unique identifier (`^[a-z0-9_-]+$`). |
| `name` | `string` | No | `id` | Human-readable title displayed in CLI reports. |
| `version` | `string` | No | `"1.0.0"` | SemVer version string (e.g. `"1.2.0"`). Bumped on every modification. |
| `author` | `string` | No | `"User"` | Author attribution (`"Engine"` for core system mods). |
| `description` | `string` | No | `""` | Functional summary of the mod. |
| `enabled` | `bool` | No | `true` | Toggle activation. Disabled mods are not applied and unload their skills. |
| `dependencies` | `list[str]` | No | `[]` | List of mod IDs required by this mod (e.g. `[mod-folder-skills]`). |
| `verify_cmd` | `string` | No | `null` | Command run after applying patches to verify functionality (e.g. `pytest tests/...`). |
| `min_engine_version` | `string` | No | `null` | Minimum required engine version (e.g. `"1.2.0"`). |
| `min_hermes_version` | `string` | No | `null` | Minimum required Hermes Agent version (e.g. `"0.20.0"`). |

---

## 3. Targets Specification (`targets`)

Each target defines an exact string substitution within the Hermes Agent source tree (`/usr/local/lib/hermes-agent`):

```yaml
targets:
  - file: "hermes_cli/commands.py"
    description: "Brief explanation of this modification"
    old_block: |
      def existing_function():
          pass
    new_block: |
      def existing_function():
          # Modified implementation
          return True
    disabled: false
    disabled_reason: ""
```

### Target Properties
- `file` (*string*, required): Relative path from the Hermes repository root.
- `old_block` (*string*, required): Exact substring to match in the target file. Must match uniquely (count == 1).
- `new_block` (*string*, required): Replacement substring.
- `description` (*string*, optional): Human-readable note for audit logs.
- `disabled` (*bool*, optional): If `true`, the target is skipped during application without failing verification.
- `disabled_reason` (*string*, optional): Explanation for why the target is temporarily bypassed.

---

## 4. Configuration Schema (`config`)

Mods can declare customizable parameters. Non-sensitive settings are stored in `config.yaml`, while sensitive credentials are stored in `secrets.yaml` (mode `0600`) (legacy `user_config` is fully supported as an alias):

```yaml
config:
  poll_interval:
    type: int
    description: "Interval in seconds"
    default: 60
    min: 5
    max: 3600
  api_token:
    type: string
    description: "Service API token"
    sensitive: true
    required: true
  log_level:
    type: string
    default: "INFO"
    options: ["DEBUG", "INFO", "WARN", "ERROR"]
```

### Supported Data Types
- `string` / `text`: Free-form string.
- `int` / `integer`: Integer with optional `min` and `max` constraints.
- `float` / `number`: Floating-point value.
- `bool` / `boolean`: Coerced from `"true"`, `"false"`, `1`, `0`, `"yes"`, `"no"`.
- `file`: Path to an existing regular file.
- `dir`: Path to an existing directory.
- `list` / `array`: List of values, or comma-separated string.

---

## 5. Custom Slash Commands (`commands`)

Declarative slash commands extend the Hermes gateway command dispatch:

```yaml
commands:
  - name: my-command
    description: "One-line description under 80 characters"
    args_hint: "[query] [--flag]"
    handler_script: "scripts/handler.py" # Or 'python:' / 'shell:' inline
    entrypoint: "handle"                 # Function name in handler_script
    category: "Custom Mods"
    busy_policy: "reject"                # reject | dispatch
    aliases:
      - mc
      - mycmd
```

### Handler Options
1. `handler_script`: Path to a Python file relative to the mod directory. The script must define a callable matching `entrypoint(args: str) -> str`.
2. `python`: Inline Python code snippet.
3. `shell`: Inline shell command string.

---

## 6. Background Monitors (`monitors`)

Background daemon processes supervised by the mod runner:

```yaml
monitors:
  - id: status_checker
    name: "Status Checker"
    command: "python3 ${MOD_DIR}/scripts/checker.py"
    description: "Periodic heartbeat verification"
    when: "always"       # always | on-demand
    delivery: "log"      # chat | agent | log
    interval: 30         # Heartbeat / execution interval in seconds
    restart: "on-failure" # no | on-failure | always
    max_restarts: 5
```

### Variable Substitutions
- `${MOD_DIR}`: Absolute path to the mod's directory.
- `${MOD_DATA}`: Dedicated mod state directory (`~/.hermes/cache/mods/<mod_id>`).
- `${config.<key>}` / `${user_config.<key>}`: Evaluated configuration parameter.

---

## 7. Declarative MCP Servers (`mcp_servers`)

MCP server definitions for tool integration:

```yaml
mcp_servers:
  sqlite-browser:
    command: "python3"
    args:
      - "${mod_dir}/server.py"
      - "--db"
      - "${config.db_path}"
    env:
      AUTH_TOKEN: "${config.api_token}"
```

Variables `${mod_dir}`, `${MOD_DIR}`, and `${config.<key>}` (along with `${user_config.<key>}`) are recursively interpolated.
