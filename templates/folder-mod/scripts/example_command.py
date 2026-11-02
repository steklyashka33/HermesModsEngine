#!/usr/bin/env python3
"""Example slash command handler script for Hermes Folder Mod."""
import sys


def handle(args: str = "") -> str:
    """Entrypoint called by ModCommandRegistry when /folder-demo is dispatched."""
    clean_args = args.strip()
    if clean_args:
        return f"✅ Example mod command received arguments: {clean_args}"
    return "✅ Example mod command executed successfully (no arguments provided)."


if __name__ == "__main__":
    # Can also be executed directly from shell
    raw_args = " ".join(sys.argv[1:])
    print(handle(raw_args))
