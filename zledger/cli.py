from __future__ import annotations

import asyncio
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import webbrowser
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
import signal

from .ledger import (
    ARCHIVE_CATEGORIES,
    CATEGORIES,
    TransactionDraft,
    append_transaction,
    backfill_txids,
    build_adjust_draft,
    build_expense_draft,
    build_income_draft,
    build_transfer_draft,
    categorize_accounts,
    delete_transaction,
    flatten_balances,
    get_account_balances,
    get_transaction_source,
    list_balance_accounts,
    list_category_accounts,
    list_account_transactions,
    list_transactions,
    show_balance_account,
    show_category_account,
    summarize_balances,
)


class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    pass


REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_PORT = int(os.environ.get("ZLEDGER_DASHBOARD_PORT", "8080"))
DASHBOARD_URL = f"http://127.0.0.1:{DASHBOARD_PORT}"
from .config import DATA_HOME
DASHBOARD_LOG = DATA_HOME / "state" / "dashboard.log"
DASHBOARD_PID_FILE = DATA_HOME / "state" / "dashboard.pid"
DASHBOARD_APP_DIR = (REPO_ROOT / "app").resolve()
DASHBOARD_APP_MAIN = DASHBOARD_APP_DIR / "main.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zledger",
        description=(
            "Query and operate on this repository's finance tools.\n\n"
            "Start with `zledger balance` to see grouped active accounts, or `zledger --help` for the full command list.\n"
            "Use `zledger --help` or `zledger <command> --help` for command details."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    from .setup import add_setup_commands
    add_setup_commands(subparsers)

    balance = subparsers.add_parser(
        "balance",
        help="Show account balances",
        description=(
            "Show balances from the main ledger.\n\n"
            "Default behavior:\n"
            "  - shows active (non-archived) accounts only\n"
            "  - groups output by account category\n"
            "  - prints one line per account/currency pair"
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    balance.add_argument("--archived", action="store_true", help="Search archived accounts instead of active ones")
    balance.add_argument(
        "--account",
        dest="account",
        metavar="TEXT",
        help="Filter by account id or display name text",
    )
    balance.set_defaults(func=run_balance)

    summary = subparsers.add_parser(
        "summary",
        help="Show net balances by currency",
        description=(
            "Show the net balance summary for active accounts.\n\n"
            "This matches the dashboard header summary and totals only Assets and Liabilities."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    summary.add_argument("--archived", action="store_true", help="Summarize archived accounts instead of active ones")
    summary.set_defaults(func=run_summary)

    account = subparsers.add_parser(
        "account",
        help="Browse balance-account definitions",
        description=(
            "Browse balance-account metadata used by zledger.\n\n"
            "These commands show balance-account ids, display names, currencies, archive state, "
            "and the exact aliases accepted by `zledger record`."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    account_subparsers = account.add_subparsers(dest="account_command", required=True)
    from .setup import add_account_command
    add_account_command(account_subparsers)
    account_list = account_subparsers.add_parser(
        "list",
        help="List balance accounts",
        description=(
            "List balance accounts from `ledger/accounts.beancount`.\n\n"
            "Default behavior shows active accounts only. Use `--archived` to list archived accounts instead."
        ),
        formatter_class=HelpFormatter,
    )
    account_list.add_argument("--match", metavar="TEXT", help="Case-insensitive substring filter across id, display name, and aliases")
    account_list.add_argument("--archived", action="store_true", help="List archived accounts instead of active ones")
    account_list.set_defaults(func=run_account_list)
    account_show = account_subparsers.add_parser(
        "show",
        help="Show one balance account",
        description=(
            "Show one balance account in detail.\n\n"
            "This uses the same exact-alias resolver as `zledger record`, so the query must resolve to one account."
        ),
        formatter_class=HelpFormatter,
    )
    account_show.add_argument("--account", required=True, metavar="TEXT", help="Exact account alias or full account id")
    account_show.add_argument("--archived", action="store_true", help="Resolve against archived accounts instead of active ones")
    account_show.set_defaults(func=run_account_show)

    category = subparsers.add_parser(
        "category",
        help="Browse income and expense categories",
        description=(
            "Browse `Expenses:` and `Income:` accounts used by zledger.\n\n"
            "These commands show category ids, allowed currencies, and the exact aliases accepted by `zledger record`."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    category_subparsers = category.add_subparsers(dest="category_command", required=True)
    category_list = category_subparsers.add_parser(
        "list",
        help="List income or expense categories",
        description=(
            "List category accounts from `ledger/accounts.beancount`.\n\n"
            "Use `--type` to choose between `Expenses:` and `Income:`."
        ),
        formatter_class=HelpFormatter,
    )
    category_list.add_argument("--type", required=True, choices=("expense", "income"), help="Which category tree to browse")
    category_list.add_argument("--match", metavar="TEXT", help="Case-insensitive substring filter across id, display name, and aliases")
    category_list.set_defaults(func=run_category_list)
    category_show = category_subparsers.add_parser(
        "show",
        help="Show one income or expense category",
        description=(
            "Show one category in detail.\n\n"
            "This uses the same exact-alias resolver as `zledger record`, so the query must resolve to one category."
        ),
        formatter_class=HelpFormatter,
    )
    category_show.add_argument("--type", required=True, choices=("expense", "income"), help="Which category tree to resolve against")
    category_show.add_argument("--category", required=True, metavar="TEXT", help="Exact category alias or full category id")
    category_show.set_defaults(func=run_category_show)

    transaction = subparsers.add_parser(
        "transaction",
        help="Show recent ledger transactions",
        description=(
            "Show recent ledger transactions.\n\n"
            "Default behavior:\n"
            "  - shows the most recent 20 transactions\n"
            "  - sorts newest first\n"
            "  - includes expenses, income, simple transfers (including FX transfers), and simple balance adjustments\n"
            "  - prints each transaction's stable `txid`\n"
            "  - does not restrict to one account unless `--account` is provided"
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    transaction_subparsers = transaction.add_subparsers(dest="transaction_command")
    transaction_show = transaction_subparsers.add_parser(
        "show",
        help="Show the full source entry for one txid",
        description=(
            "Show the exact source block for one transaction.\n\n"
            "Use the `txid` printed by `zledger transaction` and pass it here."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    transaction_show.add_argument("--txid", required=True, metavar="TXID", help="Stable transaction id printed by `zledger transaction`")
    transaction_show.set_defaults(func=run_transaction_show)
    transaction.add_argument("--limit", type=_positive_int, default=20, metavar="N", help="Maximum number of rows to print")
    transaction.add_argument("--date", dest="on_date", type=_parse_date, metavar="YYYY-MM-DD", help="Keep only one exact date")
    transaction.add_argument(
        "--account",
        dest="account",
        metavar="TEXT",
        help="Filter by account id or display name text",
    )
    transaction.add_argument(
        "--memo",
        dest="memo",
        metavar="TEXT",
        help="Filter by payee or narration text",
    )
    transaction.set_defaults(func=run_transaction)

    txid = subparsers.add_parser(
        "txid",
        help="Operate on stable transaction ids",
        description=(
            "Manage stable transaction ids (`txid`) for ledger transactions.\n\n"
            "Use `zledger txid backfill` once to add missing ids to historical transactions."
        ),
        formatter_class=HelpFormatter,
    )
    txid_subparsers = txid.add_subparsers(dest="txid_command", required=True)
    txid_backfill = txid_subparsers.add_parser(
        "backfill",
        help="Backfill missing txid metadata across the ledger",
        description=(
            "Insert `txid` metadata for every existing transaction missing one.\n\n"
            "Behavior:\n"
            "  - scans all transactions loaded by `main.beancount`\n"
            "  - preserves existing valid txids\n"
            "  - assigns new ids in include order, then source-file order\n"
            "  - validates the full ledger after writing\n"
            "  - rolls back touched files if validation fails"
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    txid_backfill.set_defaults(func=run_txid_backfill)

    reimbursement = subparsers.add_parser(
        "reimbursement",
        help="Query and manage reimbursement cases",
        description=(
            "Query and manage reimbursement cases stored in the local reimbursement database.\n\n"
            "These commands operate on reimbursement cases and case-to-transaction links.\n"
            "Transaction references use stable `txid` values from the ledger."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_subparsers = reimbursement.add_subparsers(dest="reimbursement_command", required=True)

    reimbursement_list = reimbursement_subparsers.add_parser(
        "list",
        help="List reimbursement cases",
        description=(
            "List reimbursement cases with current status and summary totals.\n\n"
            "Default behavior shows only unsettled cases (`open`, `partial`, `overpaid`).\n"
            "Use `--all` to include settled and cancelled cases."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_list.add_argument("--all", action="store_true", help="Include settled and cancelled cases")
    reimbursement_list.add_argument("--status", metavar="TEXT", help="Filter by case status")
    reimbursement_list.add_argument("--person", metavar="TEXT", help="Filter by person name or tag")
    reimbursement_list.add_argument("--q", metavar="TEXT", help="Free-text filter across title, person, and tag")
    reimbursement_list.set_defaults(func=run_reimbursement_list)

    reimbursement_create = reimbursement_subparsers.add_parser(
        "create",
        help="Create a reimbursement case",
        description=(
            "Create a new reimbursement case in the local reimbursement database.\n\n"
            "This creates the case record only. Ledger transactions are linked separately."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_create.add_argument("--person", required=True, metavar="TEXT", help="Person name")
    reimbursement_create.add_argument("--tag", metavar="TEXT", help="Optional short person tag")
    reimbursement_create.add_argument("--title", required=True, metavar="TEXT", help="Case title")
    reimbursement_create.add_argument("--base-currency", required=True, metavar="CUR", help="Case base currency, for example GBP or CNY")
    reimbursement_create.add_argument("--description", metavar="TEXT", help="Optional description")
    reimbursement_create.add_argument("--notes", metavar="TEXT", help="Optional notes")
    reimbursement_create.set_defaults(func=run_reimbursement_create)

    reimbursement_show = reimbursement_subparsers.add_parser(
        "show",
        help="Show one reimbursement case",
        description=(
            "Show a reimbursement case with summary totals and linked entries.\n\n"
            "Use `zledger reimbursement list` to discover the case id first."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_show.add_argument("--case", dest="case_id", required=True, type=int, metavar="ID", help="Reimbursement case id")
    reimbursement_show.set_defaults(func=run_reimbursement_show)

    reimbursement_candidates = reimbursement_subparsers.add_parser(
        "candidates",
        help="Show ledger candidates for one reimbursement case",
        description=(
            "Show candidate ledger transactions that can be linked into one reimbursement case.\n\n"
            "This mirrors the dashboard candidate search and includes advances, repayments, and conversions."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_candidates.add_argument("--case", dest="case_id", required=True, type=int, metavar="ID", help="Reimbursement case id")
    reimbursement_candidates.add_argument("--q", metavar="TEXT", help="Optional candidate search text")
    reimbursement_candidates.add_argument("--limit", type=_positive_int, default=20, metavar="N", help="Maximum number of candidates to print")
    reimbursement_candidates.set_defaults(func=run_reimbursement_candidates)

    reimbursement_update = reimbursement_subparsers.add_parser(
        "update",
        help="Update reimbursement case metadata",
        description=(
            "Update one reimbursement case.\n\n"
            "Only the provided fields are changed.\n"
            "Note: status is mostly auto-derived from the case summary; `cancelled` is the main manual override."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_update.add_argument("--case", dest="case_id", required=True, type=int, metavar="ID", help="Reimbursement case id")
    reimbursement_update.add_argument("--person", metavar="TEXT", help="New person name")
    reimbursement_update.add_argument("--tag", metavar="TEXT", help="New person tag")
    reimbursement_update.add_argument("--title", metavar="TEXT", help="New case title")
    reimbursement_update.add_argument("--description", metavar="TEXT", help="New description")
    reimbursement_update.add_argument("--notes", metavar="TEXT", help="New notes")
    reimbursement_update.add_argument("--status", choices=("open", "partial", "settled", "overpaid", "cancelled"), help="New case status")
    reimbursement_update.set_defaults(func=run_reimbursement_update)

    reimbursement_link = reimbursement_subparsers.add_parser(
        "link",
        help="Link a txid into one reimbursement case",
        description=(
            "Link one ledger transaction into a reimbursement case.\n\n"
            "Use `advance`, `repayment`, or `conversion` as the role."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_link.add_argument("--case", dest="case_id", required=True, type=int, metavar="ID", help="Reimbursement case id")
    reimbursement_link.add_argument("--txid", required=True, metavar="TXID", help="Stable ledger transaction id")
    reimbursement_link.add_argument("--role", required=True, choices=("advance", "repayment", "conversion"), help="Link role inside the case")
    reimbursement_link.set_defaults(func=run_reimbursement_link)

    reimbursement_unlink = reimbursement_subparsers.add_parser(
        "unlink",
        help="Remove one txid link from a reimbursement case",
        description=(
            "Remove one linked ledger transaction from a reimbursement case.\n\n"
            "This only removes the reimbursement mapping. It does not delete the ledger transaction."
        ),
        formatter_class=HelpFormatter,
    )
    reimbursement_unlink.add_argument("--case", dest="case_id", required=True, type=int, metavar="ID", help="Reimbursement case id")
    reimbursement_unlink.add_argument("--txid", required=True, metavar="TXID", help="Stable ledger transaction id currently linked to the case")
    reimbursement_unlink.set_defaults(func=run_reimbursement_unlink)

    dashboard = subparsers.add_parser(
        "dashboard",
        help="Start the dashboard UI",
        description=(
            "Start the FastAPI dashboard UI for this repository.\n\n"
            "Default behavior:\n"
            "  - reuses the existing dashboard startup script\n"
            "  - avoids starting a duplicate server if port 8080 is already serving\n"
            "  - prints the local URL and log file path"
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    dashboard_subparsers = dashboard.add_subparsers(dest="dashboard_command")
    dashboard.add_argument("--no-open", action="store_true", help="Do not try to open the dashboard URL in a browser")
    dashboard_status = dashboard_subparsers.add_parser(
        "status",
        help="Show dashboard status",
        description="Show whether the dashboard process is running and whether the local URL is reachable.",
        formatter_class=HelpFormatter,
    )
    dashboard_status.set_defaults(func=run_dashboard_status)
    dashboard_stop = dashboard_subparsers.add_parser(
        "stop",
        help="Stop the dashboard UI",
        description="Stop the dashboard process started for this repository.",
        formatter_class=HelpFormatter,
    )
    dashboard_stop.set_defaults(func=run_dashboard_stop)
    dashboard.set_defaults(func=run_dashboard_start)

    record = subparsers.add_parser(
        "record",
        help="Record a new ledger transaction",
        description=(
            "Create and append a new transaction in this repository's Beancount ledger.\n\n"
            "Default behavior:\n"
            "  - resolves accounts and categories using write-safe exact aliases\n"
            "  - writes directly into the yearly ledger file for the transaction date\n"
            "  - validates the full ledger after writing"
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    record_subparsers = record.add_subparsers(dest="record_command", required=True)

    record_expense = record_subparsers.add_parser(
        "expense",
        help="Record an expense",
        description=(
            'Record an expense from one balance-sheet account into one expense category.\n\nParameter meaning:\n  --account  balance account that paid\n  --category `Expenses:` account to post to\n  --payee    merchant / counterparty; first quoted string in the header\n  --memo     what the expense was for; second quoted string in the header'
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    _add_common_record_args(
        record_expense,
        label_help="Merchant or counterparty; first quoted string in the Beancount header",
        memo_help="What the expense was for; second quoted string in the Beancount header",
    )
    record_expense.add_argument("--account", required=True, metavar="TEXT", help="Balance account that paid")
    record_expense.add_argument("--category", required=True, metavar="TEXT", help="Expense category (`Expenses:` account)")
    record_expense.set_defaults(func=run_record_expense)

    record_income = record_subparsers.add_parser(
        "income",
        help="Record income",
        description=(
            'Record income into one balance-sheet account from one income category.\n\nParameter meaning:\n  --account  balance account that received the money\n  --category `Income:` account the money came from\n  --payer    person / institution paying you; first quoted string in the header\n  --memo     what the income was for; second quoted string in the header'
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    _add_common_record_args(
        record_income,
        label_flag="--payer",
        label_dest="payer",
        label_help="Person or institution paying you; first quoted string in the Beancount header",
        memo_help="What the income was for; second quoted string in the Beancount header",
    )
    record_income.add_argument("--account", required=True, metavar="TEXT", help="Balance account that received the money")
    record_income.add_argument("--category", required=True, metavar="TEXT", help="Income category (`Income:` account)")
    record_income.set_defaults(func=run_record_income)

    record_transfer = record_subparsers.add_parser(
        "transfer",
        help="Record a transfer between accounts",
        description=(
            'Record a transfer between two balance-sheet accounts.\n\nParameter meaning:\n  --from  source balance account\n  --to    destination balance account\n  --amount sent amount in the source account currency\n  --received optional received amount in the destination account currency\n  --memo  transfer description; the only quoted string in the header'
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    _add_common_record_args(
        record_transfer,
        label_flag=None,
        memo_help="Transfer description; the only quoted string in the Beancount header",
    )
    record_transfer.add_argument("--from", dest="from_account", required=True, metavar="TEXT", help="Source balance account")
    record_transfer.add_argument("--to", dest="to_account", required=True, metavar="TEXT", help="Destination balance account")
    record_transfer.add_argument("--received", type=_positive_decimal, metavar="AMOUNT", help="Destination amount for cross-currency transfers")
    record_transfer.set_defaults(func=run_record_transfer)

    record_adjust = record_subparsers.add_parser(
        "adjust",
        help="Record a balance adjustment against Equity:Adjustments",
        description=(
            'Record a simple balance adjustment for one balance-sheet account.\n\nParameter meaning:\n  --account         balance account being reconciled\n  --memo            adjustment description; the only quoted string in the header\n  --delta           signed change to post to the balance account\n  --actual-balance  desired signed account balance after the new adjustment'
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    record_adjust.add_argument("--date", dest="entry_date", type=_parse_date, default=date.today(), metavar="YYYY-MM-DD", help="Transaction date (defaults to today)")
    record_adjust.add_argument("--account", required=True, metavar="TEXT", help="Balance account being reconciled")
    delta_group = record_adjust.add_mutually_exclusive_group(required=True)
    delta_group.add_argument("--delta", type=_decimal, metavar="AMOUNT", help="Signed adjustment to post to the balance account")
    delta_group.add_argument("--actual-balance", dest="actual_balance", type=_decimal, metavar="AMOUNT", help="Desired signed balance after the adjustment")
    record_adjust.add_argument("--memo", required=True, metavar="TEXT", help="Adjustment description; the only quoted string in the Beancount header")
    record_adjust.set_defaults(func=run_record_adjust)

    record_delete = record_subparsers.add_parser(
        "delete",
        help="Delete an existing transaction by txid",
        description=(
            "Delete one existing transaction block by txid.\n\n"
            "Use `zledger transaction` to find the txid, then inspect it with "
            "`zledger transaction show --txid TXID` before deleting."
        ),
        epilog=(
            "Use --help for required fields. Use your own private account and transaction values."
        ),
        formatter_class=HelpFormatter,
    )
    record_delete.add_argument("--txid", required=True, metavar="TXID", help="Stable transaction id printed by `zledger transaction`")
    record_delete.set_defaults(func=run_record_delete)

    return parser


def _matches(account: dict[str, object], needle: str) -> bool:
    haystacks = [str(account.get("id", "")), str(account.get("name", ""))]
    needle = needle.casefold()
    return any(needle in haystack.casefold() for haystack in haystacks)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD format") from exc


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc


def _add_common_record_args(
    parser: argparse.ArgumentParser,
    *,
    label_flag: str | None = "--payee",
    label_dest: str = "payee",
    label_help: str = "Primary label for the transaction header",
    memo_help: str = "Second string in the transaction header",
) -> None:
    parser.add_argument("--date", dest="entry_date", type=_parse_date, default=date.today(), metavar="YYYY-MM-DD", help="Transaction date (defaults to today)")
    parser.add_argument("--amount", type=_positive_decimal, required=True, metavar="AMOUNT", help="Positive transaction amount")
    if label_flag:
        parser.add_argument(label_flag, dest=label_dest, required=True, metavar="TEXT", help=label_help)
    parser.add_argument("--memo", required=True, metavar="TEXT", help=memo_help)


def _filter_rows(rows: Iterable[dict[str, object]], needle: str | None) -> list[dict[str, object]]:
    if not needle:
        return list(rows)
    return [row for row in rows if _matches(row, needle)]


def _filter_grouped(grouped: dict[str, list[dict[str, object]]], needle: str | None) -> dict[str, list[dict[str, object]]]:
    if not needle:
        return grouped
    filtered = {}
    for category, accounts in grouped.items():
        matched = [account for account in accounts if _matches(account, needle)]
        if matched:
            filtered[category] = matched
    return filtered


def _print_flat(rows: list[dict[str, object]]) -> None:
    for row in rows:
        print(f"{row['name']:<28} {row['balance']:>12.2f} {row['currency']}  {row['id']}")


def _print_grouped(grouped: dict[str, list[dict[str, object]]]) -> None:
    for category, accounts in grouped.items():
        print(category)
        for account in sorted(accounts, key=lambda item: (str(item["currency"]), str(item["id"]))):
            print(f"  {account['name']:<26} {account['balance']:>12.2f} {account['currency']}  {account['id']}")
        print()


def _print_summary(summary: dict[str, float]) -> None:
    if not summary:
        print("No balances found.")
        return

    for currency, amount in summary.items():
        print(f"{currency:<6} {amount:>12.2f}")


def _format_aliases(aliases: Iterable[object]) -> str:
    values = [str(alias) for alias in aliases if str(alias)]
    return ", ".join(values) if values else "-"


def _print_account_catalog(rows: list[dict[str, object]], *, archived_only: bool) -> None:
    if not rows:
        scope = "archived" if archived_only else "active"
        print(f"No {scope} balance accounts found.")
        return

    for row in rows:
        status = "archived" if bool(row.get("archived")) else "active"
        print(f"{row['id']}")
        print(f"  name:     {row['name']}")
        print(f"  kind:     {row['kind']}")
        print(f"  currency: {row.get('currency') or '-'}")
        print(f"  status:   {status}")
        print(f"  aliases:  {_format_aliases(row.get('aliases', ()))}")
        print()


def _print_account_detail(row: dict[str, object]) -> None:
    status = "archived" if bool(row.get("archived")) else "active"
    print(f"Account: {row['id']}")
    print(f"Name: {row['name']}")
    print(f"Kind: {row['kind']}")
    print(f"Currency: {row.get('currency') or '-'}")
    print(f"Status: {status}")
    print(f"Aliases: {_format_aliases(row.get('aliases', ()))}")


def _print_category_catalog(rows: list[dict[str, object]], *, kind: str) -> None:
    if not rows:
        print(f"No {kind} categories found.")
        return

    for row in rows:
        print(f"{row['id']}")
        print(f"  name:     {row['name']}")
        print(f"  currency: {row.get('currency') or '-'}")
        print(f"  aliases:  {_format_aliases(row.get('aliases', ()))}")
        print()


def _print_category_detail(row: dict[str, object]) -> None:
    print(f"Category: {row['id']}")
    print(f"Name: {row['name']}")
    print(f"Kind: {row['kind']}")
    print(f"Currency: {row.get('currency') or '-'}")
    print(f"Aliases: {_format_aliases(row.get('aliases', ()))}")


def _load_reimbursement_runtime():
    try:
        from app import main as reimbursement_main
    except Exception as exc:  # pragma: no cover - environment-specific import
        raise RuntimeError(
            "Reimbursement commands require the dashboard/beancount environment. "
            "Run them in the same environment used for `zledger dashboard`."
        ) from exc

    reimbursement_main.init_reimbursements_db()
    return reimbursement_main


def _run_reimbursement_call(coro):
    try:
        return asyncio.run(coro)
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        if detail:
            raise RuntimeError(str(detail)) from exc
        raise


def _print_reimbursement_items(items: list[dict[str, object]]) -> None:
    if not items:
        print("No reimbursement cases found.")
        return

    for item in items:
        summary = dict(item.get("summary") or {})
        print(
            f"#{item['id']:<4} {str(item.get('status', '')):<9} {str(item.get('base_currency', '')):<3}  "
            f"adv {float(summary.get('total_advanced_base', 0.0)):>9.2f}  "
            f"rep {float(summary.get('total_repaid_base', 0.0)):>9.2f}  "
            f"conv {float(summary.get('total_converted_base', 0.0)):>9.2f}  "
            f"rem {float(summary.get('remaining_base', 0.0)):>9.2f}  "
            f"{item.get('person_name', '')} | {item.get('title', '')}"
        )


def _print_reimbursement_rows(title: str, rows: list[dict[str, object]], base_currency: str) -> None:
    print(title)
    if not rows:
        print("  none")
        print()
        return

    for row in rows:
        txid = str(row.get("txid") or "-")
        date_value = str(row.get("date") or "-")
        amount = float(row.get("amount") or 0.0)
        currency = str(row.get("currency") or base_currency)
        base_amount = float(row.get("base_amount") or 0.0)
        account = str(row.get("account") or "-")
        memo = str(row.get("memo") or "-")
        print(
            f"  {date_value}  {txid:<8}  {amount:>10.2f} {currency:<3}  "
            f"=> {base_amount:>10.2f} {base_currency:<3}  {account}  {memo}"
        )
    print()


def _print_reimbursement_case(case: dict[str, object]) -> None:
    summary = dict(case["summary"])
    base_currency = str(case["base_currency"])

    print(f"Case #{case['id']}: {case['title']}")
    person_tag = case.get("person_tag")
    person_line = str(case["person_name"])
    if person_tag:
        person_line = f"{person_line} (#{person_tag})"
    print(f"Person: {person_line}")
    print(f"Status: {case['status']}")
    print(f"Base currency: {base_currency}")
    print()
    print(f"Advanced:            {float(summary.get('total_advanced_base', 0.0)):>10.2f} {base_currency}")
    print(f"Repaid (ledger):     {float(summary.get('total_repaid_base', 0.0)):>10.2f} {base_currency}")
    print(f"Repaid (market):     {float(summary.get('total_repaid_market_base', 0.0)):>10.2f} {base_currency}")
    print(f"Converted:           {float(summary.get('total_converted_base', 0.0)):>10.2f} {base_currency}")
    print(f"Remaining:           {float(summary.get('remaining_base', 0.0)):>10.2f} {base_currency}")
    print(f"FX estimate:         {float(summary.get('fx_gain_base', 0.0)):>10.2f} {base_currency}")
    print(f"Linked entries:      {int(summary.get('linked_entries', 0))}")
    print(f"Conversion entries:  {int(summary.get('conversion_entries', 0))}")
    print()

    _print_reimbursement_rows("Advances", list(case.get("advances") or []), base_currency)
    _print_reimbursement_rows("Repayments", list(case.get("repayments") or []), base_currency)
    _print_reimbursement_rows("Conversions", list(case.get("conversions") or []), base_currency)


def _print_reimbursement_candidates(items: list[dict[str, object]]) -> None:
    if not items:
        print("No reimbursement candidates found.")
        return

    for item in items:
        txid = str(item.get("txid") or "-")
        role = str(item.get("inferred_role") or "-")
        kind = str(item.get("entry_kind") or "-")
        date_value = str(item.get("date") or "-")
        amount = float(item.get("amount") or 0.0)
        currency = str(item.get("currency") or "-")
        account = str(item.get("account") or "-")
        linked_case_id = item.get("linked_case_id")
        linked_text = f" linked-to:{linked_case_id}" if linked_case_id is not None else ""
        print(
            f"{txid:<8}  {date_value}  {role:<10} {kind:<10} "
            f"{amount:>10.2f} {currency:<3}  {account}{linked_text}"
        )
        print(f"  {item.get('memo') or item.get('title') or '-'}")


def _unlink_reimbursement_txid(runtime, case_id: int, txid: str) -> dict[str, object]:
    normalized = runtime._normalize_txid(txid)
    if not normalized:
        raise RuntimeError("`--txid` must be a non-empty txid.")

    conn = runtime.get_db()
    try:
        case = conn.execute("SELECT id FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
        if not case:
            raise RuntimeError(f"Reimbursement case {case_id} was not found.")

        row = conn.execute(
            "SELECT id FROM reimbursement_links WHERE case_id = ? AND txid = ?",
            (case_id, normalized),
        ).fetchone()
        if not row:
            raise RuntimeError(f"txid {normalized} is not linked to reimbursement case {case_id}.")

        conn.execute("DELETE FROM reimbursement_links WHERE id = ?", (row["id"],))
        conn.execute("UPDATE reimbursement_cases SET updated_at = ? WHERE id = ?", (runtime.now_iso(), case_id))
        conn.commit()

        _, txid_index = runtime.get_reimbursement_ledger_entries()
        return runtime.compute_case_summary(conn, case_id, txid_index)
    finally:
        conn.close()


def _dashboard_is_reachable(host: str = "127.0.0.1", port: int = DASHBOARD_PORT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _read_dashboard_pid() -> int | None:
    try:
        raw = DASHBOARD_PID_FILE.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _process_exists(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_dashboard_process(pid: int) -> bool:
    return pid > 0 and _process_exists(pid)


def _dashboard_pids() -> list[int]:
    pid_from_file = _read_dashboard_pid()
    if pid_from_file and _is_dashboard_process(pid_from_file):
        return [pid_from_file]
    return []


def _cleanup_dashboard_pid_file() -> None:
    try:
        DASHBOARD_PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _open_dashboard_url(url: str) -> None:
    try:
        webbrowser.open(url)
    except OSError:
        pass


def _terminate_dashboard_process(pid: int, force: bool = False) -> None:
    if os.name == "nt":
        command = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def _wait_for_dashboard(timeout_seconds: float = 8.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _dashboard_is_reachable():
            return True
        time.sleep(0.25)
    return _dashboard_is_reachable()


def _dashboard_status() -> tuple[list[int], bool]:
    pids = _dashboard_pids()
    reachable = _dashboard_is_reachable()
    if not pids and _read_dashboard_pid() is not None:
        _cleanup_dashboard_pid_file()
    return pids, reachable


def _transaction_summary(row: dict[str, object]) -> str:
    parts = []
    payee = str(row.get("payee", "")).strip()
    narration = str(row.get("narration", "")).strip()
    if payee:
        parts.append(payee)
    if narration and narration != payee:
        parts.append(narration)
    return " | ".join(parts)


def _print_transactions(rows: list[dict[str, object]], account_mode: bool) -> None:
    if not rows:
        print("No transactions found.")
        return

    for row in rows:
        summary = _transaction_summary(row)
        txid = str(row.get("txid", "")).strip()
        if account_mode:
            counterpart = str(row.get("counterpart", ""))
            line = (
                f"{txid:<8}  {row['date']}  {row['amount']:>+10.2f} {row['currency']:<3}  "
                f"{row['account_name']:<26} {counterpart:<18} {summary}"
            )
        else:
            account_name = str(row.get("account_name", ""))
            line = (
                f"{txid:<8}  {row['date']}  {row['kind']:<8} {row['amount']:>10.2f} {row['currency']:<3}  "
                f"{row['category']:<14} {account_name:<26} {summary}"
            )

        print(line)


def run_balance(args: argparse.Namespace) -> int:
    needle = args.account
    use_flat = bool(args.account)
    balances = get_account_balances(args.archived)
    if use_flat:
        rows = _filter_rows(flatten_balances(balances), needle)
        _print_flat(rows)
        return 0

    categories = ARCHIVE_CATEGORIES if args.archived else CATEGORIES
    grouped = _filter_grouped(categorize_accounts(balances, categories), needle)
    _print_grouped(grouped)
    return 0


def run_summary(args: argparse.Namespace) -> int:
    _print_summary(summarize_balances(args.archived))
    return 0


def run_account_list(args: argparse.Namespace) -> int:
    rows = list_balance_accounts(match=args.match, archived_only=args.archived)
    _print_account_catalog(rows, archived_only=args.archived)
    return 0


def run_account_show(args: argparse.Namespace) -> int:
    row = show_balance_account(args.account, archived_only=args.archived)
    _print_account_detail(row)
    return 0


def run_category_list(args: argparse.Namespace) -> int:
    rows = list_category_accounts(kind=args.type, match=args.match)
    _print_category_catalog(rows, kind=args.type)
    return 0


def run_category_show(args: argparse.Namespace) -> int:
    row = show_category_account(args.category, kind=args.type)
    _print_category_detail(row)
    return 0


def run_transaction(args: argparse.Namespace) -> int:
    if args.account:
        rows = list_account_transactions(args.account, limit=args.limit, on_date=args.on_date, memo=args.memo)
        _print_transactions(rows, account_mode=True)
        return 0

    rows = list_transactions(limit=args.limit, on_date=args.on_date, memo=args.memo)
    _print_transactions(rows, account_mode=False)
    return 0


def run_transaction_show(args: argparse.Namespace) -> int:
    source = get_transaction_source(args.txid)
    print(f"txid: {source.txid}")
    print(f"file: {source.relative_file}:{source.start_line}")
    print()
    print(source.block.rstrip())
    return 0


def run_txid_backfill(args: argparse.Namespace) -> int:
    result = backfill_txids()
    updated = int(result["updated"])
    files = dict(result["files"])
    if updated == 0:
        print("Backfill complete: 0 changes.")
        print(f"Next txid: {result['next_txid']}")
        return 0

    print(f"Backfilled txid for {updated} transaction(s) across {len(files)} file(s).")
    for path, count in files.items():
        print(f"- {path}: {count}")
    print(f"Next txid: {result['next_txid']}")
    return 0


def run_reimbursement_list(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    data = _run_reimbursement_call(
        runtime.list_reimbursements(status=args.status, person=args.person, q=args.q)
    )
    items = list(data.get("items") or [])
    original_count = len(items)
    if not args.all and not args.status:
        items = [item for item in items if str(item.get("status")) not in {"settled", "cancelled"}]
        if not items and original_count:
            print("No unsettled reimbursement cases found. Use `--all` to include settled and cancelled cases.")
            return 0
    _print_reimbursement_items(items)
    return 0


def run_reimbursement_create(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    payload = runtime.ReimbursementCaseCreate(
        person_name=args.person,
        person_tag=args.tag,
        title=args.title,
        description=args.description,
        base_currency=args.base_currency,
        notes=args.notes,
    )
    case = _run_reimbursement_call(runtime.create_reimbursement_case(payload))
    print(f"Created reimbursement case #{case['id']}.")
    print()
    _print_reimbursement_case(case)
    return 0


def run_reimbursement_show(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    case = _run_reimbursement_call(runtime.get_reimbursement_case(args.case_id))
    _print_reimbursement_case(case)
    return 0


def run_reimbursement_candidates(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    data = _run_reimbursement_call(
        runtime.get_reimbursement_candidates(args.case_id, q=args.q, limit=args.limit)
    )
    _print_reimbursement_candidates(list(data.get("items") or []))
    return 0


def run_reimbursement_update(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    if all(
        value is None
        for value in (args.person, args.tag, args.title, args.description, args.notes, args.status)
    ):
        raise RuntimeError("Provide at least one field to update.")

    payload = runtime.ReimbursementCaseUpdate(
        person_name=args.person,
        person_tag=args.tag,
        title=args.title,
        description=args.description,
        notes=args.notes,
        status=args.status,
    )
    case = _run_reimbursement_call(runtime.update_reimbursement_case(args.case_id, payload))
    print(f"Updated reimbursement case #{args.case_id}.")
    print()
    _print_reimbursement_case(case)
    return 0


def run_reimbursement_link(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    payload = runtime.LedgerLinkCreate(txid=args.txid, role=args.role)
    case = _run_reimbursement_call(runtime.add_reimbursement_link(args.case_id, payload))
    print(f"Linked txid {args.txid} to reimbursement case {args.case_id} as {args.role}.")
    print()
    _print_reimbursement_case(case)
    return 0


def run_reimbursement_unlink(args: argparse.Namespace) -> int:
    runtime = _load_reimbursement_runtime()
    case = _unlink_reimbursement_txid(runtime, args.case_id, args.txid)
    print(f"Unlinked txid {args.txid} from reimbursement case {args.case_id}.")
    print()
    _print_reimbursement_case(case)
    return 0


def run_dashboard_start(args: argparse.Namespace) -> int:
    pids, reachable = _dashboard_status()
    if pids and reachable:
        print(f"Dashboard already running at {DASHBOARD_URL}")
        print(f"PID: {', '.join(str(pid) for pid in pids)}")
        print(f"Log: {DASHBOARD_LOG}")
        if not args.no_open:
            _open_dashboard_url(DASHBOARD_URL)
        return 0

    if pids:
        for pid in pids:
            _terminate_dashboard_process(pid, force=True)
        _cleanup_dashboard_pid_file()

    if reachable:
        print(f"Dashboard already available at {DASHBOARD_URL}")
        print(f"Log: {DASHBOARD_LOG}")
        if not args.no_open:
            _open_dashboard_url(DASHBOARD_URL)
        return 0

    creationflags = 0
    popen_kwargs: dict[str, object] = {}
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_kwargs["start_new_session"] = True
    DASHBOARD_LOG.parent.mkdir(parents=True, exist_ok=True)
    with DASHBOARD_LOG.open("ab") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(DASHBOARD_APP_MAIN)],
            cwd=str(DASHBOARD_APP_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=creationflags,
            **popen_kwargs,
        )
    DASHBOARD_PID_FILE.write_text(str(process.pid), encoding="utf-8")

    if not _wait_for_dashboard():
        _terminate_dashboard_process(process.pid, force=True)
        _cleanup_dashboard_pid_file()
        raise RuntimeError(
            f"Dashboard did not become reachable at {DASHBOARD_URL}. Check the log at {DASHBOARD_LOG}."
        )

    print(f"Dashboard started at {DASHBOARD_URL}")
    pids, _ = _dashboard_status()
    if pids:
        print(f"PID: {', '.join(str(pid) for pid in pids)}")
    print(f"Log: {DASHBOARD_LOG}")
    if not args.no_open:
        _open_dashboard_url(DASHBOARD_URL)
    return 0


def run_dashboard_status(args: argparse.Namespace) -> int:
    pids, reachable = _dashboard_status()
    if pids:
        print(f"Dashboard running at {DASHBOARD_URL}")
        print(f"PID: {', '.join(str(pid) for pid in pids)}")
        print(f"Reachable: {'yes' if reachable else 'no'}")
        print(f"Log: {DASHBOARD_LOG}")
        return 0

    if reachable:
        print(f"Port 8080 is reachable at {DASHBOARD_URL}, but no managed dashboard process was found.")
        print(f"Log: {DASHBOARD_LOG}")
        return 0

    print("Dashboard is not running.")
    print(f"Log: {DASHBOARD_LOG}")
    return 0


def run_dashboard_stop(args: argparse.Namespace) -> int:
    pids, reachable = _dashboard_status()
    if not pids:
        if reachable:
            raise RuntimeError(
                f"Port 8080 is reachable at {DASHBOARD_URL}, but no managed dashboard process was found. Stop it manually."
            )
        print("Dashboard is not running.")
        _cleanup_dashboard_pid_file()
        return 0

    for pid in pids:
        try:
            _terminate_dashboard_process(pid)
        except OSError:
            continue

    deadline = time.time() + 5.0
    while time.time() < deadline:
        remaining = [pid for pid in pids if _is_dashboard_process(pid)]
        if not remaining:
            _cleanup_dashboard_pid_file()
            wait_deadline = time.time() + 2.0
            while time.time() < wait_deadline and _dashboard_is_reachable():
                time.sleep(0.1)
            if _dashboard_is_reachable():
                print(f"Stopped managed dashboard process at {DASHBOARD_URL}, but port 8080 is still reachable.")
            else:
                print(f"Stopped dashboard at {DASHBOARD_URL}")
            return 0
        time.sleep(0.25)

    remaining = [pid for pid in pids if _is_dashboard_process(pid)]
    for pid in remaining:
        try:
            _terminate_dashboard_process(pid, force=True)
        except OSError:
            continue

    time.sleep(0.25)
    still_running = [pid for pid in pids if _is_dashboard_process(pid)]
    if still_running:
        raise RuntimeError(f"Failed to stop dashboard process(es): {', '.join(str(pid) for pid in still_running)}")

    _cleanup_dashboard_pid_file()
    wait_deadline = time.time() + 2.0
    while time.time() < wait_deadline and _dashboard_is_reachable():
        time.sleep(0.1)
    if _dashboard_is_reachable():
        print(f"Stopped managed dashboard process at {DASHBOARD_URL}, but port 8080 is still reachable.")
    else:
        print(f"Stopped dashboard at {DASHBOARD_URL}")
    return 0


def _finalize_record(draft: TransactionDraft) -> int:
    draft = append_transaction(draft)
    print(draft.render())
    print()
    print(f"Wrote transaction to {draft.target_file}")
    return 0


def run_record_expense(args: argparse.Namespace) -> int:
    draft = build_expense_draft(
        entry_date=args.entry_date,
        amount=args.amount,
        account_query=args.account,
        category_query=args.category,
        payee=args.payee,
        memo=args.memo,
    )
    return _finalize_record(draft)


def run_record_income(args: argparse.Namespace) -> int:
    draft = build_income_draft(
        entry_date=args.entry_date,
        amount=args.amount,
        account_query=args.account,
        category_query=args.category,
        payee=args.payer,
        memo=args.memo,
    )
    return _finalize_record(draft)


def run_record_transfer(args: argparse.Namespace) -> int:
    draft = build_transfer_draft(
        entry_date=args.entry_date,
        amount=args.amount,
        from_query=args.from_account,
        to_query=args.to_account,
        received_amount=args.received,
        memo=args.memo,
    )
    return _finalize_record(draft)


def run_record_adjust(args: argparse.Namespace) -> int:
    draft = build_adjust_draft(
        entry_date=args.entry_date,
        account_query=args.account,
        memo=args.memo,
        delta=args.delta,
        actual_balance=args.actual_balance,
    )
    return _finalize_record(draft)


def run_record_delete(args: argparse.Namespace) -> int:
    source = delete_transaction(args.txid)
    print(f"Deleted transaction {source.txid}")
    print(f"Removed from {source.relative_file}:{source.start_line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
