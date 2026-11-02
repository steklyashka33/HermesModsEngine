## Summary

<!-- Briefly explain what changes were made, why, and what problem is solved. -->

## Type of Change

- [ ] 🐛 Bug fix (non-breaking change which fixes an issue)
- [ ] 🚀 New feature (non-breaking change which adds functionality)
- [ ] 📦 New declarative mod / Mod update
- [ ] 🛠️ Engine core improvement (.engine/)
- [ ] 📚 Documentation update
- [ ] 🧪 Testing & CI automation

## Verification & Architecture Checklist

- [ ] **Cache-Driven Isolation:** Core Hermes Agent codebase interacts with mods strictly via JSON caches (`.commands_cache.json`, `.locales_cache.json`, `.mcp_cache.json`). No `import engine` in targets.
- [ ] **Zero Downtime Policy:** No system restarts or reloads required (`systemctl restart hermes-gateway` is strictly forbidden).
- [ ] **Collocated i18n (8 Languages):** All new or changed user-facing strings are localized across all 8 official languages (`en, ru, es, fr, de, zh, ar, pt`) without hardcoded string literals in python handlers.
- [ ] **Strict Layout:** Folder mods follow strict decomposition (`targets/*.yaml` <50KB, `commands.yaml`, `config.yaml`, `i18n/*.yaml`).
- [ ] **Secrets & Security:** No secrets or credentials are hardcoded. Sensitive parameters are routed to `secrets.yaml` (0600 permissions).
- [ ] **Static Validation:** `python3 runner.py validate --strict-layout` passes with 0 errors.
- [ ] **Tests:** `python3 runner.py test` passes with 0 failures. Unit tests added or updated.
