# Contributing to HermesModsEngine 🤝

Thank you for your interest in contributing to **HermesModsEngine**! This project provides the enterprise extension framework for **Hermes Agent**. Whether you are authoring a new mod, refining core engine resilience, or adding localizations, your contributions are welcome.

---

## 📜 Code of Conduct

We are committed to providing a welcoming, productive, and inclusive environment. Please communicate respectfully, collaborate openly, and focus on constructive technical discussions.

---

## 🚀 Getting Started

### 1. Prerequisites
- **Python:** 3.10, 3.11, 3.12, 3.13, or 3.14
- **Git:** 2.30+
- **Hermes Agent:** Installed locally (or test in standalone engine mode)
- **Dependencies:** Standard library + `pytest`, `pyyaml` (for dev tests)

### 2. Fork and Clone
```bash
git clone https://github.com/<your-username>/hermes-mods.git ~/.hermes/mods
cd ~/.hermes/mods
```

### 3. Verify Local Setup
Run the diagnostics doctor:
```bash
python3 runner.py doctor
```
All checks must return green or valid warnings.

---

## 📐 Architectural Quality Gates

Every pull request must pass the following quality gates before review:

### Gate 1: Static AST Validation & Strict Layout
```bash
python3 runner.py validate --strict-layout
```
- No syntax errors in Python code blocks.
- Folder mods must use Strict Layout (`mod.yaml`, `targets/*.yaml`, `config.yaml`, `commands.yaml`, `i18n/`).
- Monolithic target files >50 KB are rejected.

### Gate 2: Full Test Suite
Run the 270+ automated tests:
```bash
pytest tests/ -q
```
All tests must pass with 0 failures and 0 regressions.

### Gate 3: Mod Unit Tests
If adding or modifying a mod:
```bash
python3 runner.py test <mod_id> --no-cache
```

### Gate 4: Zero Hardcoded Paths
- **Never** hardcode `/root/`, `/home/...`, or specific user folders.
- Use `Path.home()`, `HERMES_HOME`, and `HERMES_MODS_DIR`.

### Gate 5: Security of Sensitive Data
- Any field with `sensitive: true` must never be written to `config.yaml`.
- Secrets are stored exclusively in `secrets.yaml` with permissions `0600`.
- Verify clean archive export without leaked keys:
  ```bash
  python3 runner.py export <mod_id> --no-secrets --out /tmp/test-export.tar.gz
  ```

---

## 🛠️ Authoring a New Mod

To submit a new community mod:

1. **Scaffold the Mod:**
   ```bash
   python3 runner.py create <mod_id> --type folder
   ```
2. **Implement Targets:**
   Keep patches minimal and surgical. Always include 2–4 lines of unchanged context above and below the edit in `old_block`.
3. **Declare Configuration:**
   Add options in `config.yaml` with clear English descriptions, sensible defaults, and validation bounds.
4. **Collocated Localization:**
   Provide base English in manifests, and translations in `i18n/` for international users (e.g. `ru.yaml`, `es.yaml`, `de.yaml`).
5. **Write Unit Tests:**
   Add test cases in `tests/test_<mod_id>.py` using pytest or inline `tests:` dictionary.
6. **Verify Dry-Run:**
   ```bash
   python3 runner.py verify <mod_id>
   ```

---

## 📋 Git Commit & PR Guidelines

### Commit Convention
We follow the [Conventional Commits](https://www.conventionalcommits.org/) specification:

- `feat(mod-name): add speculative racing fallback`
- `fix(engine): resolve file lock race condition in cache writer`
- `docs(specification): document watchdog observation states`
- `test(compaction): add dual-trigger live compaction test cases`
- `refactor(loader): streamline section reducer parsing logic`

### Pull Request Checklist
Before opening a PR, ensure:
- [ ] Mod conforms to Strict Layout.
- [ ] `runner.py validate --strict-layout` passes 100%.
- [ ] `pytest tests/` passes completely.
- [ ] New features include automated tests.
- [ ] English is the primary language, with collocated translations in `i18n/`.
- [ ] Zero service restart commands included (zero-downtime standard).
- [ ] No personal tokens, API keys, or machine-specific paths committed.

---

## ⚖️ License

By contributing to **HermesModsEngine**, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).
