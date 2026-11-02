# AGENTS.md — Autonomous AI Agent Onboarding & Operations Handbook

> **Target Audience:** Autonomous AI coding agents (Claude Code, Hermes Agent, Cursor, OpenAI Codex, OpenCode, Devin, Windsurf, GitHub Copilot CLI) and multi-agent orchestration frameworks operating without human intervention.

---

## ⚡ Executive Mission: "Install this mod engine"

If a human user sent you a link to this repository (`https://github.com/steklyashka33/HermesModsEngine`) with a prompt such as:
* *"Установи мне этот движок модов"*
* *"Install this mod engine for me"*
* *"Поставь моды для Hermes"*
* *"Deploy HermesModsEngine on this server"*

**Follow this deterministic 3-step execution protocol immediately without asking redundant clarifying questions.**

```text
╔══════════════════════════════════════════════════════════════════════════════════════╗
║                          CRITICAL PRIME INVARIANT: ZERO DOWNTIME                     ║
║                                                                                      ║
║  STRICTLY FORBIDDEN:                                                                 ║
║  • NEVER execute 'systemctl restart hermes-gateway' or 'systemctl reload'            ║
║  • NEVER execute 'hermes gateway restart' or kill running gateway PIDs               ║
║  • NEVER reboot services during installation, updates, or mod application            ║
║                                                                                      ║
║  All modifications take effect live through transactional AST patching and           ║
║  JSON cache mtime invalidation without dropping active sessions or turns!            ║
╚══════════════════════════════════════════════════════════════════════════════════════╝
```

---

## 🚀 The 3-Step Turnkey Onboarding Protocol

```text
┌──────────────────────────────────────┐
│ Step 1: Autonomous One-Line Install  │ ──> curl -sSL .../install.sh | bash
└──────────────────┬───────────────────┘
                   │
┌──────────────────▼───────────────────┐
│ Step 2: Validate Environment & Status│ ──> runner.py doctor && runner.py status
└──────────────────┬───────────────────┘
                   │
┌──────────────────▼───────────────────┐
│ Step 3: Configure Auto-Update (Cron) │ ──> runner.py cron install && cron status
└──────────────────┬───────────────────┘
                   │
┌──────────────────▼───────────────────┐
│ Step 4: Report Completion to User    │ ──> Use Standard Human Report Template
└──────────────────────────────────────┘
```

### Step 1: Autonomous One-Line Installation
Execute the official unattended production installer:
```bash
curl -sSL https://raw.githubusercontent.com/steklyashka33/HermesModsEngine/main/install.sh | bash
```
*(Pro-tip: If you want to configure auto-update in the same command, pass `--cron`:)*
```bash
curl -sSL https://raw.githubusercontent.com/steklyashka33/HermesModsEngine/main/install.sh | bash -s -- --cron
```

**What the installer does automatically:**
1. Validates Git CLI and detects compatible Python 3.10+ (probing system Python and Hermes Agent virtualenvs).
2. Auto-locates Hermes Agent core path (`/usr/local/lib/hermes-agent`).
3. Safely clones or updates the repository into `~/.hermes/mods` without overwriting untracked custom mods or user secrets.
4. Enforces strict POSIX `0600` file permissions on `~/.hermes/secrets.yaml`.
5. Compiles atomic runtime caches (`.commands_cache.json`, `.locales_cache.json`, `.mcp_cache.json`).
6. Applies all 37 active production mods transactionally with zero downtime.

---

### Step 2: Validate Environment & Ecosystem Status
Run pre-flight diagnostics and verify the active runtime state:
```bash
python3 ~/.hermes/mods/runner.py doctor
python3 ~/.hermes/mods/runner.py status
```

**Pass Criteria:**
- `doctor`: All checks `[OK]` (Python Environment, Git CLI, Hermes Core, Secrets Storage 0600, Engine Integrity 14 modules, Mod Manifests & Dependencies DAG clean, Commands Cache & State, Circuit Breaker).
- `status`: Overall Health `🟢 HEALTHY`, Master Killswitch `🟢 ACTIVE`, 37 applied mods (0 disabled, 0 not applied).

---

### Step 3: Configure Automated Nightly Auto-Updates (Cron)
Configure the automated three-phase update pipeline in the user's crontab:
```bash
python3 ~/.hermes/mods/runner.py cron install
python3 ~/.hermes/mods/runner.py cron status
```

**What this accomplishes:**
- Installs an idempotent cron task scheduled nightly at 04:00:
  ```cron
  # [HermesModsEngine:auto-update] Nightly three-phase engine & core update
  0 4 * * * <python> ~/.hermes/mods/runner.py update --all >> ~/.hermes/logs/update.log 2>&1
  ```
- Executes the full three-phase pipeline:
  - **Phase 1 (Engine):** Synchronizes engine infrastructure and system mods from GitHub.
  - **Phase 2 (Core):** Performs upstream Hermes Agent core updates.
  - **Phase 3 (Mods):** Re-applies all active mods transactionally with watchdog verification.
- **Zero duplicates:** Replaces existing entries cleanly if schedule changes.
- **Safe logging:** Appends execution logs to `~/.hermes/logs/update.log`.
- **Zero downtime:** Guaranteed zero gateway restarts.

---

## 📋 Standard Human Reporting Template

When finished, provide a concise, factual summary directly to the user. Use this template:

```text
🎉 Движок модов HermesModsEngine успешно установлен и настроен!

• Статус системы:        🟢 HEALTHY (37/37 активных модов)
• Диагностика (Doctor):  Все 8 подсистем прошли проверку (Python, Git, Hermes Core, DAG, 0600 Secrets)
• Автообновление (Cron): 🟢 АКТИВНО — настроено ночное трёхфазное обновление (04:00 ежедневно)
• Лог обновлений:        ~/.hermes/logs/update.log
• Слэш-команды в чате:   /mods, /mod, /status, /usage, /context, /insights
• Время простоя (SLA):   0 секунд (Zero Downtime — шлюз не перезагружался)

Полезные команды для управления:
  - Проверить статус:      python3 ~/.hermes/mods/runner.py status
  - Статус автообновлений: python3 ~/.hermes/mods/runner.py cron status
  - Обновить вручную:      python3 ~/.hermes/mods/runner.py update --all
  - Каталог слэш-команд:   python3 ~/.hermes/mods/runner.py commands
```

---

## 🛠️ AI Agent Command Reference Cheat Sheet

| Task | Command |
|---|---|
| **Ecosystem Status** | `python3 ~/.hermes/mods/runner.py status [--format json]` |
| **Diagnostics / Audit** | `python3 ~/.hermes/mods/runner.py doctor [--no-color]` |
| **Cron Status** | `python3 ~/.hermes/mods/runner.py cron status [--format json]` |
| **Install Auto-Update** | `python3 ~/.hermes/mods/runner.py cron install [--schedule "0 4 * * *"]` |
| **Uninstall Auto-Update** | `python3 ~/.hermes/mods/runner.py cron uninstall` |
| **Update Engine (1-Command)**| `python3 ~/.hermes/mods/runner.py update [--check\|--dry-run]` |
| **Full 3-Phase Update** | `python3 ~/.hermes/mods/runner.py update --all` |
| **Slash Commands UX** | `python3 ~/.hermes/mods/runner.py commands [--lang ru\|en]` |
| **Core Diff** | `python3 ~/.hermes/mods/runner.py diff [mod_id]` |
| **Run Unit Tests** | `python3 ~/.hermes/mods/runner.py test [mod_id] [-x]` |
| **Validate Layout** | `python3 ~/.hermes/mods/runner.py validate --strict-layout` |
| **Create New Mod** | `python3 ~/.hermes/mods/runner.py create <mod_id> --type folder` |
| **Push to GitHub** | `python3 ~/.hermes/mods/runner.py push -m "feat(scope): message"` |
| **Emergency Rollback** | `python3 ~/.hermes/mods/runner.py snapshot restore` |
| **Circuit Breaker** | `python3 ~/.hermes/mods/runner.py circuit-breaker status` |

---

## 🧭 Autonomous Mod Authoring Protocol

When the user asks you to create or modify a mod, strictly follow these standards:

1. **Scaffold with Strict Layout:**
   ```bash
   python3 ~/.hermes/mods/runner.py create <mod_id> --type folder
   ```
2. **Decompose Targets (`custom/<mod_id>/targets/*.yaml`):**
   - Each target file must be under 50 KB.
   - Include 2–4 unchanged surrounding context lines above and below your edit to guarantee unique match.
   - Literal block scalars `|` with exact indentation (2 or 4 spaces, never tabs).
3. **Configuration & Secrets (`config.yaml`):**
   - Public parameters: stored in `~/.hermes/config.yaml`.
   - Sensitive tokens (`sensitive: true`): stored strictly in `~/.hermes/secrets.yaml` (mode `0600`).
4. **Mandatory 8-Language Localization (`i18n/*.yaml`):**
   - Provide collocated translations in all 8 languages: `en` (base), `ru`, `es`, `fr`, `de`, `zh`, `ar`, `pt`.
   - Complete ban on string hardcoding in Python code: use `t("key", lang=...)`.
5. **Automated Pytest Suite (`tests/test_<mod_id>.py`):**
   - Create tests exercising the logic.
6. **Verification Gate:**
   ```bash
   python3 ~/.hermes/mods/runner.py validate --strict-layout
   python3 ~/.hermes/mods/runner.py test <mod_id>
   python3 ~/.hermes/mods/runner.py apply <mod_id>
   python3 ~/.hermes/mods/runner.py status
   ```
7. **Commit & Push to GitHub:**
   ```bash
   python3 ~/.hermes/mods/runner.py push -m "feat(<mod_id>): descriptive message"
   ```

---

## 🚑 Self-Healing & Troubleshooting

### Scenario A: Status reports `DEGRADED` or target failed to apply
1. Run diagnostics:
   ```bash
   python3 ~/.hermes/mods/runner.py doctor --no-color
   ```
2. Inspect core diff to see applied vs cleanly restored files:
   ```bash
   python3 ~/.hermes/mods/runner.py diff <mod_id>
   ```
3. If core was modified by external tools, restore clean snapshot:
   ```bash
   python3 ~/.hermes/mods/runner.py snapshot restore
   python3 ~/.hermes/mods/runner.py apply
   ```

### Scenario B: Circuit Breaker Tripped (OPEN)
1. Inspect failure logs:
   ```bash
   python3 ~/.hermes/mods/runner.py circuit-breaker failures <mod_id>
   ```
2. Fix the underlying bug in `custom/<mod_id>/`.
3. Reset circuit breaker:
   ```bash
   python3 ~/.hermes/mods/runner.py circuit-breaker reset <mod_id>
   python3 ~/.hermes/mods/runner.py apply <mod_id>
   ```

### Scenario C: Cron Auto-Update Troubleshooting
1. Check cron status:
   ```bash
   python3 ~/.hermes/mods/runner.py cron status
   ```
2. Inspect update log:
   ```bash
   tail -n 50 ~/.hermes/logs/update.log
   ```
3. Run manual simulation:
   ```bash
   python3 ~/.hermes/mods/runner.py update --all --dry-run
   ```
