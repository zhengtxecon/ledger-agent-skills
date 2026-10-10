"""Resolve private runtime data independently of the source checkout."""
import os
from pathlib import Path

def default_data_home():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "zledger"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "zledger"

def is_tool_directory(path):
    package_root = Path(__file__).resolve().parents[1]
    if path.is_relative_to(package_root):
        return True
    return any((parent / "pyproject.toml").is_file() and (parent / "zledger/config.py").is_file()
               for parent in (path, *path.parents))

configured_ledger = os.environ.get("FINANCES_LEDGER_PATH")
if configured_ledger:
    LEDGER_FILE = Path(configured_ledger).expanduser().resolve()
else:
    LEDGER_FILE = Path(os.environ.get("FINANCES_REPO", default_data_home())).expanduser().resolve() / "main.beancount"
DATA_HOME = LEDGER_FILE.parent
if is_tool_directory(DATA_HOME):
    raise RuntimeError("Private data must be outside the tool/source directory.")
