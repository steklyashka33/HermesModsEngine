#!/usr/bin/env python3
"""CLI wrapper for Hermes Gateway Watchdog."""
import importlib.util
import sys
from pathlib import Path

# Добавляем корневую директорию модов и .engine в sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
MODS_DIR = SCRIPT_DIR.parent
ENGINE_DIR = MODS_DIR / ".engine"

if str(MODS_DIR) not in sys.path:
    sys.path.insert(0, str(MODS_DIR))
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

# Register .engine as 'engine' package
if "engine" not in sys.modules and (ENGINE_DIR / "__init__.py").exists():
    spec = importlib.util.spec_from_file_location("engine", str(ENGINE_DIR / "__init__.py"))
    if spec and spec.loader:
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["engine"] = pkg
        spec.loader.exec_module(pkg)

from engine.watchdog import main

if __name__ == "__main__":
    sys.exit(main())
