import os
from pathlib import Path
import sys

_hermes_root = Path(os.environ.get("HERMES_ROOT", "/usr/local/lib/hermes-agent"))
if str(_hermes_root) not in sys.path and _hermes_root.is_dir():
    sys.path.insert(0, str(_hermes_root))

_mods_dir = Path(__file__).resolve().parents[3]
if not (_mods_dir / ".engine").is_dir():
    _mods_dir = Path(os.environ.get("HERMES_MODS_DIR", Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "mods"))

if str(_mods_dir) not in sys.path and _mods_dir.is_dir():
    sys.path.insert(0, str(_mods_dir))

if "gateway.mod_commands" not in sys.modules:
    import importlib.util
    mod_cmd_path = _mods_dir / "system" / "mod-command-registry" / "mod_commands.py"
    if mod_cmd_path.exists():
        spec = importlib.util.spec_from_file_location("gateway.mod_commands", str(mod_cmd_path))
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["gateway.mod_commands"] = mod
            spec.loader.exec_module(mod)
