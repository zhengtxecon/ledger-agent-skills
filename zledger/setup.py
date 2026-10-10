"""Initialize empty private data and add user-supplied account definitions."""
import argparse
import re
from datetime import date
from pathlib import Path

from .config import DATA_HOME, is_tool_directory


def require_private_path(path):
    path = path.expanduser().resolve()
    if is_tool_directory(path):
        raise ValueError("Private data must be outside the tool/source directory.")
    return path


def run_init(args):
    target = require_private_path(Path(args.directory) if args.directory else DATA_HOME)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Initialization requires an empty directory; existing data was preserved.")
    (target / "ledger").mkdir(parents=True, exist_ok=True)
    (target / "docs").mkdir()
    year = date.today().year
    (target / "main.beancount").write_text(
        f'include "ledger/accounts.beancount"\ninclude "ledger/{year}.beancount"\n', encoding="utf-8", newline="\n")
    # A technical balancing account, not a user financial account or transaction.
    (target / "ledger/accounts.beancount").write_text(
        '1970-01-01 open Equity:Adjustments\n', encoding="utf-8", newline="\n")
    (target / f"ledger/{year}.beancount").write_text("", encoding="utf-8", newline="\n")
    (target / "docs/accounts.md").write_text("| Aliases | Account |\n|---|---|\n", encoding="utf-8", newline="\n")
    print(f"Initialized empty private data: {target}")
    print("Set FINANCES_REPO to this directory when using a custom location.")
    return 0


def run_account_add(args):
    from . import ledger
    require_private_path(ledger.LEDGER_HOME)
    if not re.fullmatch(r"(?:Assets|Liabilities|Expenses|Income|Equity)(?::[A-Z][A-Za-z0-9-]*)+", args.name):
        raise ValueError("Use a valid full Beancount account name.")
    if not re.fullmatch(r"[A-Z][A-Z0-9]*", args.currency):
        raise ValueError("Use an uppercase currency code.")
    if any(any(c in alias for c in "\r\n|`") or not alias.strip() for alias in args.alias):
        raise ValueError("Aliases must be nonempty single-line text without Markdown delimiters.")
    opened = date.fromisoformat(args.date) if args.date else date.today()
    accounts = ledger.LEDGER_DIR / "accounts.beancount"
    aliases = ledger.DOCS_ACCOUNTS_PATH
    with ledger._ledger_write_lock():
        entries, errors, _ = ledger.load_entries()
        if errors:
            raise ValueError("Fix existing ledger validation errors before adding an account.")
        if any(getattr(entry, "account", None) == args.name and isinstance(entry, ledger._open_entry_type()) for entry in entries):
            raise ValueError("Account already exists; nothing changed.")
        before_bytes = accounts.read_bytes()
        before = before_bytes.decode("utf-8")
        aliases_existed = aliases.exists()
        aliases_bytes = aliases.read_bytes() if aliases_existed else None
        aliases_before = aliases_bytes.decode("utf-8") if aliases_existed else "| Aliases | Account |\n|---|---|\n"
        try:
            accounts.write_text(before.rstrip() + f"\n{opened.isoformat()} open {args.name} {args.currency}\n", encoding="utf-8", newline="\n")
            ledger.validate_ledger()
            if args.alias:
                aliases.parent.mkdir(parents=True, exist_ok=True)
                aliases.write_text(aliases_before.rstrip() + f"\n| `{'，'.join(args.alias)}` | `{args.name}` |\n", encoding="utf-8", newline="\n")
        except Exception:
            accounts.write_bytes(before_bytes)
            if aliases_existed:
                aliases.write_bytes(aliases_bytes)
            elif aliases.exists():
                aliases.unlink()
            raise
    print(f"Added account: {args.name} ({args.currency})")
    return 0


def run_year_add(args):
    from . import ledger
    require_private_path(ledger.LEDGER_HOME)
    if not 1 <= args.year <= 9999:
        raise ValueError("Year must be between 1 and 9999.")
    target = ledger.LEDGER_DIR / f"{args.year}.beancount"
    with ledger._ledger_write_lock():
        if target.exists():
            raise ValueError("Year file already exists; nothing changed.")
        before_bytes = ledger.LEDGER_FILE.read_bytes()
        before = before_bytes.decode("utf-8")
        try:
            target.write_text("", encoding="utf-8", newline="\n")
            ledger.LEDGER_FILE.write_text(before.rstrip() + f'\ninclude "ledger/{args.year}.beancount"\n', encoding="utf-8", newline="\n")
            ledger.validate_ledger()
        except Exception:
            ledger.LEDGER_FILE.write_bytes(before_bytes)
            target.unlink(missing_ok=True)
            raise
    print(f"Added year: {args.year}")
    return 0


def add_setup_commands(subparsers):
    init = subparsers.add_parser("init", help="Create empty private data, without user accounts or transactions")
    init.add_argument("--directory", help="Empty private directory; defaults to the configured platform data directory")
    init.set_defaults(func=run_init)
    year = subparsers.add_parser("year", help="Add a new empty year file and include")
    year.add_argument("--add", dest="year", type=int, required=True)
    year.set_defaults(func=run_year_add)


def add_account_command(subparsers):
    add = subparsers.add_parser("add", help="Add a user-supplied account definition to private data")
    add.add_argument("--name", required=True, help="Full Beancount account name")
    add.add_argument("--currency", required=True)
    add.add_argument("--date", help="Opening date; defaults to today")
    add.add_argument("--alias", action="append", default=[], help="Optional exact alias; repeat for multiple aliases")
    add.set_defaults(func=run_account_add)
