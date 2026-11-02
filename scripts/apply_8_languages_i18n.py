#!/usr/bin/env python3
"""
Hermes Mods 8-Language Universal Localization (i18n) Engine Script
Standard 8 Languages:
  en - English (Base / Default)
  ru - Russian
  es - Spanish
  fr - French
  zh - Chinese (Simplified)
  ar - Arabic
  de - German
  pt - Portuguese

Applies clean collocated i18n across all 37 Hermes mods:
- Folder mods: i18n/<lang>.yaml (with backwards compatibility in locales/<lang>.yaml)
- Single-file mods: inline i18n: block with all 8 language dicts
"""
import os
import sys
import json
import re
import yaml
from pathlib import Path

# Add engine to sys.path
SCRIPT_DIR = Path(__file__).resolve().parent.parent
ENGINE_DIR = SCRIPT_DIR / ".engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import importlib.util
if "engine" not in sys.modules and (ENGINE_DIR / "__init__.py").exists():
    _spec = importlib.util.spec_from_file_location("engine", str(ENGINE_DIR / "__init__.py"))
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["engine"] = _pkg
    _spec.loader.exec_module(_pkg)

from engine.loader import ModLoader, update_locales_cache

MODS_DIR = Path(os.environ.get("HERMES_MODS_DIR", Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "mods"))
WORKSPACES_MODS_DIR = Path(os.environ.get("HERMES_WORKSPACES_MODS_DIR", Path.home() / "workspaces" / "hermes-mods"))
CATALOG_PATH = SCRIPT_DIR / "i18n_catalog.json"

LANGS = ["en", "ru", "es", "fr", "zh", "ar", "de", "pt"]

def main():
    if not CATALOG_PATH.is_file():
        sys.stderr.write(f"Catalog file {CATALOG_PATH} not found!\n")
        sys.exit(1)

    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    mods = ModLoader.load_all(MODS_DIR)
    print(f"Loaded {len(mods)} mods from {MODS_DIR}")
    print(f"Catalog contains {len(catalog)} mods with 8 languages")

    folder_count = 0
    single_count = 0

    for mod in sorted(mods, key=lambda m: m.id):
        mod_id = mod.id
        if mod_id not in catalog:
            print(f"Warning: mod {mod_id} not found in translation catalog!")
            continue

        mod_translations = catalog[mod_id]

        if mod.mod_dir and mod.mod_dir.is_dir():
            # Folder mod
            folder_count += 1
            i18n_dir = mod.mod_dir / "i18n"
            locales_dir = mod.mod_dir / "locales"
            i18n_dir.mkdir(parents=True, exist_ok=True)
            locales_dir.mkdir(parents=True, exist_ok=True)

            for lang in LANGS:
                lang_data = mod_translations.get(lang, {})
                yaml_content = yaml.dump(lang_data, allow_unicode=True, sort_keys=False)
                # Write to i18n/<lang>.yaml
                (i18n_dir / f"{lang}.yaml").write_text(yaml_content, encoding="utf-8")
                # Write to locales/<lang>.yaml (backwards compatibility)
                (locales_dir / f"{lang}.yaml").write_text(yaml_content, encoding="utf-8")

            print(f"✓ [FOLDER] {mod_id:36} -> updated i18n/ and locales/ (8 languages)")

        else:
            # Single-file mod
            single_count += 1
            mod_file = mod.path
            file_text = mod_file.read_text(encoding="utf-8")
            lines = file_text.splitlines()

            # Find existing locales: or i18n: section
            loc_start = None
            for idx, line in enumerate(lines):
                if line.startswith("locales:") or line.startswith("i18n:"):
                    loc_start = idx
                    break

            # Format the new i18n block
            i18n_dict = {"i18n": mod_translations}
            new_block_lines = yaml.dump(i18n_dict, allow_unicode=True, sort_keys=False).splitlines()

            if loc_start is not None:
                # Find end of section (next unindented line or EOF)
                loc_end = len(lines)
                for idx in range(loc_start + 1, len(lines)):
                    line = lines[idx]
                    if line and not line.startswith(" ") and not line.startswith("\t"):
                        loc_end = idx
                        break

                updated_lines = lines[:loc_start] + new_block_lines + lines[loc_end:]
            else:
                updated_lines = lines + [""] + new_block_lines

            updated_text = "\n".join(updated_lines).rstrip() + "\n"
            mod_file.write_text(updated_text, encoding="utf-8")
            print(f"✓ [SINGLE] {mod_id:36} -> updated inline i18n: (8 languages)")

    print(f"\nCompleted: {folder_count} folder mods, {single_count} single-file mods updated.")

    # Re-scan mods and update cache
    updated_mods = ModLoader.load_all(MODS_DIR)
    cache_path = update_locales_cache(updated_mods, MODS_DIR)
    print(f"Updated locales cache at {cache_path}")

    # Verify cache
    with open(cache_path, "r", encoding="utf-8") as f:
        cache_data = json.load(f)

    catalogs = cache_data.get("catalogs", {})
    available_langs = sorted(catalogs.keys())
    print(f"Catalogs in cache ({len(available_langs)}): {available_langs}")
    missing_langs = set(LANGS) - set(available_langs)
    if missing_langs:
        sys.stderr.write(f"ERROR: Missing languages in cache: {missing_langs}\n")
        sys.exit(1)
    else:
        print("✓ SUCCESS: All 8 standard languages are present in .locales_cache.json!")

if __name__ == "__main__":
    main()
