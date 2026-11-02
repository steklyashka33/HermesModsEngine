# Creating a Hermes Mod: Step-by-Step Guide

This guide walks you through building, testing, and deploying custom mods for Hermes Agent using the declarative mod engine.

---

## 1. Choose Mod Type

- **Single-File Mod (`custom/<mod-id>.yaml`):**
  Choose this for small, atomic modifications touching 1–5 files.
  Start from `templates/single-file-mod.yaml`.

- **Folder Mod (`custom/<mod-id>/`):**
  Choose this when bundling custom slash commands with scripts, agent skills (`skills/`), or multiple modular patch files (`targets/`).
  Start by copying `templates/folder-mod/`.

---

## 2. Inspect Target Source Files

Hermes Agent code lives in `/usr/local/lib/hermes-agent`.

Find the exact section of code you want to change:
```bash
git -C /usr/local/lib/hermes-agent grep "def resolve_command"
```

Rules for target blocks:
1. **Include surrounding context:** Ensure `old_block` contains 2–4 lines of unique context before and after the change so it matches exactly once in the file.
2. **Preserve indentation:** Use YAML literal block scalars (`|` or `|-`) so indentation matches the target file exactly without escaping quotes.

---

## 3. Define the Mod Manifest

Create `custom/my-feature.yaml`:

```yaml
id: my-feature
name: My Custom Feature
description: Adds custom telemetry and command dispatch hooks
version: 1.0.0
author: Community
enabled: true

dependencies:
  - mod-command-registry

config:
  log_level:
    type: string
    description: Verbosity of telemetry output
    default: "INFO"
    options: ["DEBUG", "INFO", "WARN", "ERROR"]
  secret_token:
    type: string
    description: Backend auth token
    sensitive: true
    required: false

targets:
  - file: "hermes_cli/commands.py"
    description: "Inject custom command pre-filter"
    old_block: |
      def is_gateway_known_command(name: str | None) -> bool:
          if not name:
              return False
    new_block: |
      def is_gateway_known_command(name: str | None) -> bool:
          if not name:
              return False
          # Custom mod telemetry hook
          import os
          if os.environ.get("MY_FEATURE_DEBUG") == "1":
              print(f"[DEBUG] Command check: {name}")

commands:
  - name: my-ping
    description: Responds with a health confirmation
    args_hint: ""
    python: |
      print("pong from my-feature mod")
    category: Custom Mods
```

---

## 4. Validate Static Syntax (Pre-Flight)

Before applying the mod, run the static AST and manifest validator:

```bash
python3 runner.py validate my-feature
```

The validator performs:
- **AST parsing:** Catches `SyntaxError`, `IndentationError`, unclosed strings, and parentheses in `new_block`.
- **Target file existence:** Ensures target paths exist relative to `/usr/local/lib/hermes-agent`.
- **Configuration schema checks:** Validates types, defaults, and option lists.
- **Uniqueness checks:** Prevents duplicate mod IDs and cyclic dependencies.

---

## 5. Test Dry-Run & Verification

Run `verify` to simulate applying the mod in-memory without changing any files:

```bash
python3 runner.py verify my-feature
```

Expected output:
```json
[
  {
    "id": "my-feature",
    "name": "My Custom Feature",
    "version": "1.0.0",
    "targets_count": 1,
    "config_count": 2,
    "user_config_count": 2,
    "monitors_count": 0,
    "mcp_servers_count": 0,
    "dependencies": ["mod-command-registry"],
    "status": "not_applied",
    "verified": false
  }
]
```

---

## 6. Apply the Mod

Apply the mod transactionally:

```bash
python3 runner.py apply my-feature
```

The engine:
1. Re-validates the AST syntax.
2. Creates isolated in-memory backups.
3. Applies patches atomically.
4. Executes `verify_cmd` if defined.
5. If verification fails, automatically rolls back all touched files to their backup state!

Run `verify` again to confirm idempotency:
```bash
python3 runner.py verify my-feature
```
The status must now report `already_applied` with `"verified": true`.

---

## 7. Manage Configuration & Secrets

Set non-sensitive config options in `config.yaml`:
```bash
python3 runner.py config my-feature --set log_level=DEBUG
```

Store sensitive credentials securely in `secrets.yaml` (mode 0600):
```bash
python3 runner.py config my-feature --set-secret secret_token=tok_live_12345
```

Inspect configured values (secrets are automatically masked):
```bash
python3 runner.py config my-feature
```

---

## 8. Best Practices Checklist

- [ ] Mod ID uses lowercase alphanumeric characters with hyphens (`my-mod`).
- [ ] Every modification increments `version: x.y.z`.
- [ ] `targets` have sufficient context to match uniquely (count == 1).
- [ ] Sensitive keys have `sensitive: true` declared in `config`.
- [ ] `python3 runner.py validate` passes with zero errors.
- [ ] Second run of `python3 runner.py apply` reports `already_applied`.
