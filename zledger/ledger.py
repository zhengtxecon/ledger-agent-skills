from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows-only fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX-only fallback
    msvcrt = None

REPO_ROOT = Path(__file__).resolve().parents[1]
from .config import LEDGER_FILE
LEDGER_PATH = str(LEDGER_FILE)
LEDGER_HOME = LEDGER_FILE.parent
LEDGER_DIR = LEDGER_HOME / "ledger"
DOCS_ACCOUNTS_PATH = LEDGER_HOME / "docs" / "accounts.md"
LEDGER_LOCK_PATH = LEDGER_HOME / ".zledger-write.lock"
LEDGER_LOCK_TIMEOUT_SECONDS = 30.0
LEDGER_LOCK_POLL_SECONDS = 0.1

# Display metadata is private, optional, and loaded from the user's data directory.
import json
from .config import DATA_HOME
_metadata_path = LEDGER_HOME / "accounts.local.json"
ACCOUNTS = json.loads(_metadata_path.read_text(encoding="utf-8")) if _metadata_path.exists() else {}
CATEGORIES = [("Assets", "Assets:", None), ("Liabilities", "Liabilities:", None)]
ARCHIVE_CATEGORIES = CATEGORIES
_OPEN_ACCOUNT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}\s+open\s+([A-Z][A-Za-z0-9:-]+)(?:\s+(.*?))?\s*$")
_LOOKUP_SPLIT_PATTERN = re.compile(r"[^0-9A-Za-z\u0080-\uffff]+")
_INCLUDE_PATTERN = re.compile(r'^\s*include\s+"([^"]+)"\s*$')
_MARKDOWN_CODE_PATTERN = re.compile(r"`([^`]+)`")
ADJUSTMENTS_ACCOUNT = "Equity:Adjustments"
_FX_TOLERANCE = Decimal("0.02")
_ALIAS_SPLIT_PATTERN = re.compile(r"[，,]")
_TXID_LINE_PATTERN = re.compile(r'^\s+txid:\s*"([^"]+)"\s*$')
_TXID_PREFIX_PATTERN = re.compile(r"^\s+txid:\s*")
_TXID_VALUE_PATTERN = re.compile(r"^t(\d+)$")
_TXID_MIN_WIDTH = 6


class AccountResolutionError(ValueError):
    """Raised when a write-safe account lookup cannot be resolved."""


class UnknownAccountError(AccountResolutionError):
    def __init__(self, query: str, suggestions: list[str] | None = None):
        message = f"Unknown account '{query}'."
        if suggestions:
            message += f" Try one of: {', '.join(suggestions)}"
        super().__init__(message)


class AmbiguousAccountError(AccountResolutionError):
    def __init__(self, query: str, matches: list[str]):
        super().__init__(f"Ambiguous account '{query}'. Matches: {', '.join(matches)}")


def _normalize_lookup(text: str) -> str:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    normalized = _LOOKUP_SPLIT_PATTERN.sub(" ", spaced.casefold()).strip()
    return " ".join(normalized.split())


def _lookup_keys(text: str) -> tuple[str, ...]:
    normalized = _normalize_lookup(text)
    if not normalized:
        return tuple()
    compact = normalized.replace(" ", "")
    if compact == normalized:
        return (normalized,)
    return (normalized, compact)


def _filter_roots(accounts: list[str], roots: tuple[str, ...] | None) -> list[str]:
    if not roots:
        return accounts
    prefixes = tuple(f"{root}:" for root in roots)
    return [account for account in accounts if account.startswith(prefixes)]


@lru_cache(maxsize=1)
def _main_includes() -> tuple[str, ...]:
    if not LEDGER_FILE.exists():
        return tuple()

    includes = []
    seen = set()
    for line in LEDGER_FILE.read_text(encoding="utf-8").splitlines():
        match = _INCLUDE_PATTERN.match(line)
        if not match:
            continue
        include_path = Path(match.group(1)).as_posix()
        if include_path in seen:
            continue
        seen.add(include_path)
        includes.append(include_path)
    return tuple(includes)


def _parse_open_currencies(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return tuple()
    candidates = []
    for part in raw.split(","):
        token = part.split(";", 1)[0].strip()
        if token:
            candidates.append(token)
    return tuple(candidates)


@lru_cache(maxsize=1)
def _open_account_metadata() -> dict[str, tuple[str, ...]]:
    accounts_path = LEDGER_DIR / "accounts.beancount"
    if not accounts_path.exists():
        return {}

    metadata: dict[str, tuple[str, ...]] = {}
    for line in accounts_path.read_text(encoding="utf-8").splitlines():
        match = _OPEN_ACCOUNT_PATTERN.match(line.strip())
        if not match:
            continue
        account, raw_currencies = match.groups()
        metadata.setdefault(account, _parse_open_currencies(raw_currencies))
    return metadata


@lru_cache(maxsize=1)
def _ledger_accounts() -> tuple[str, ...]:
    return tuple(_open_account_metadata())


def _account_alias_candidates(account: str) -> set[str]:
    parts = account.split(":")
    candidates = {
        _normalize_lookup(account),
        _normalize_lookup(":".join(parts[1:])),
    }
    if len(parts) >= 3:
        candidates.add(_normalize_lookup(":".join(parts[2:])))
    if parts:
        candidates.add(_normalize_lookup(parts[-1]))

    info = ACCOUNTS.get(account)
    if info:
        name = str(info.get("name", "")).strip()
        if name:
            candidates.add(_normalize_lookup(name))
            stripped = re.sub(r"\s*\([^)]*\)\s*", " ", name)
            candidates.add(_normalize_lookup(stripped))

    expanded = set()
    for candidate in candidates:
        expanded.update(_lookup_keys(candidate))
    return {candidate for candidate in expanded if candidate}


@lru_cache(maxsize=1)
def _documented_aliases() -> dict[str, tuple[str, ...]]:
    if not DOCS_ACCOUNTS_PATH.exists():
        return {}

    aliases: dict[str, set[str]] = {}
    known_accounts = set(_ledger_accounts())
    for line in DOCS_ACCOUNTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _MARKDOWN_CODE_PATTERN.findall(line)
        if len(cells) < 2:
            continue

        alias_cell, target = cells[0], cells[1]
        if target not in known_accounts:
            continue

        for raw_alias in _ALIAS_SPLIT_PATTERN.split(alias_cell):
            for key in _lookup_keys(raw_alias):
                aliases.setdefault(key, set()).add(target)
    return {
        alias: tuple(sorted(matches))
        for alias, matches in aliases.items()
    }


@lru_cache(maxsize=1)
def _documented_alias_strings() -> dict[str, tuple[str, ...]]:
    if not DOCS_ACCOUNTS_PATH.exists():
        return {}

    aliases: dict[str, set[str]] = {}
    known_accounts = set(_ledger_accounts())
    for line in DOCS_ACCOUNTS_PATH.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = _MARKDOWN_CODE_PATTERN.findall(line)
        if len(cells) < 2:
            continue

        alias_cell, target = cells[0], cells[1]
        if target not in known_accounts:
            continue

        for raw_alias in _ALIAS_SPLIT_PATTERN.split(alias_cell):
            alias = _normalize_lookup(raw_alias)
            if alias:
                aliases.setdefault(target, set()).add(alias)

    return {
        account: tuple(sorted(values))
        for account, values in aliases.items()
    }


def _filter_archived(accounts: list[str], include_archived: bool) -> list[str]:
    if include_archived:
        return accounts
    return [account for account in accounts if not ACCOUNTS.get(account, {}).get("archived", False)]


def _archived_only(accounts: list[str]) -> list[str]:
    return [account for account in accounts if ACCOUNTS.get(account, {}).get("archived", False)]


@lru_cache(maxsize=1)
def _account_lookup() -> dict[str, tuple[str, ...]]:
    lookup: dict[str, set[str]] = {}
    for account in _ledger_accounts():
        for candidate in _account_alias_candidates(account):
            lookup.setdefault(candidate, set()).add(account)
    for alias, accounts in _documented_aliases().items():
        lookup.setdefault(alias, set()).update(accounts)
    return {
        candidate: tuple(sorted(matches))
        for candidate, matches in lookup.items()
    }


def _compress_aliases(aliases: set[str]) -> tuple[str, ...]:
    preferred: dict[str, str] = {}
    for alias in aliases:
        if not alias:
            continue
        compact = alias.replace(" ", "")
        current = preferred.get(compact)
        if current is None:
            preferred[compact] = alias
            continue
        if " " in alias and " " not in current:
            preferred[compact] = alias
            continue
        if len(alias) < len(current):
            preferred[compact] = alias
    return tuple(sorted(preferred.values(), key=lambda item: (len(item), item)))


def _is_displayable_alias(alias: str) -> bool:
    for char in alias:
        if char.isascii():
            if char.isalnum() or char.isspace() or char in ":-_/&":
                continue
            return False
        if "\u3400" <= char <= "\u9fff":
            continue
        return False
    return True


def _display_aliases_for_account(account: str) -> tuple[str, ...]:
    parts = account.split(":")
    aliases: set[str] = set(_documented_alias_strings().get(account, ()))

    aliases.add(_normalize_lookup(parts[-1]))
    aliases.add(_normalize_lookup(":".join(parts[1:])))
    if len(parts) >= 3:
        aliases.add(_normalize_lookup(":".join(parts[2:])))

    info = ACCOUNTS.get(account)
    if info:
        name = str(info.get("name", "")).strip()
        if name:
            aliases.add(_normalize_lookup(name))
            stripped = re.sub(r"\s*\([^)]*\)\s*", " ", name)
            stripped = _normalize_lookup(stripped)
            if stripped:
                aliases.add(stripped)

    displayable = {alias for alias in aliases if _is_displayable_alias(alias)}
    return _compress_aliases(displayable or aliases)


def _catalog_matches(row: dict[str, Any], needle: str | None) -> bool:
    if not needle:
        return True
    search_terms = [
        str(row.get("id", "")),
        str(row.get("name", "")),
        *[str(alias) for alias in row.get("aliases", ())],
    ]
    normalized = needle.casefold()
    return any(normalized in term.casefold() for term in search_terms)


def _filter_lookup_matches(
    matches: list[str],
    *,
    include_archived: bool,
    archived_only: bool,
) -> list[str]:
    if archived_only:
        return _archived_only(matches)
    return _filter_archived(matches, include_archived)


def _lookup_suggestions(
    query: str,
    roots: tuple[str, ...] | None,
    include_archived: bool,
    archived_only: bool = False,
) -> list[str]:
    keys = _lookup_keys(query)
    normalized = keys[0] if keys else ""
    suggestions: list[str] = []
    seen = set()
    for alias in get_close_matches(normalized, list(_account_lookup()), n=6, cutoff=0.55):
        matches = _filter_roots(list(_account_lookup()[alias]), roots)
        matches = _filter_lookup_matches(matches, include_archived=include_archived, archived_only=archived_only)
        for account in matches:
            if account in seen:
                continue
            seen.add(account)
            suggestions.append(account)
    return suggestions


def resolve_account_name(
    query: str,
    roots: tuple[str, ...] | None = None,
    *,
    include_archived: bool = True,
    archived_only: bool = False,
) -> str:
    keys = _lookup_keys(query)
    if not keys:
        raise UnknownAccountError(query)

    matches_set = set()
    for key in keys:
        matches_set.update(_account_lookup().get(key, ()))
    matches = _filter_roots(sorted(matches_set), roots)
    matches = _filter_lookup_matches(matches, include_archived=include_archived, archived_only=archived_only)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousAccountError(query, matches)

    raise UnknownAccountError(query, _lookup_suggestions(query, roots, include_archived, archived_only))


def resolve_balance_account(query: str) -> str:
    return resolve_account_name(query, roots=("Assets", "Liabilities"), include_archived=False)


def resolve_category_account(query: str, roots: tuple[str, ...]) -> str:
    return resolve_account_name(query, roots=roots)


@dataclass(frozen=True)
class TransactionPosting:
    account: str
    amount: Decimal
    currency: str
    total_price_amount: Decimal | None = None
    total_price_currency: str | None = None


@dataclass(frozen=True)
class TransactionDraft:
    entry_date: date
    payee: str | None
    memo: str | None
    postings: tuple[TransactionPosting, ...]
    target_file: Path
    txid: str | None = None

    def render(self) -> str:
        header = f"{self.entry_date.isoformat()} *"
        if self.payee:
            header = f'{header} "{_escape_beancount_string(self.payee)}"'
        if self.memo:
            header = f'{header} "{_escape_beancount_string(self.memo)}"'

        account_width = max(len(posting.account) for posting in self.postings) + 2
        lines = [header]
        if self.txid:
            lines.append(f'  txid: "{self.txid}"')
        for posting in self.postings:
            amount = _format_amount(posting.amount)
            line = f"  {posting.account:<{account_width}}{amount} {posting.currency}"
            if posting.total_price_amount is not None and posting.total_price_currency is not None:
                total_price = _format_amount(posting.total_price_amount)
                line = f"{line} @@ {total_price} {posting.total_price_currency}"
            lines.append(line)
        return "\n".join(lines)


@dataclass(frozen=True)
class TransactionLocator:
    txid: str | None
    txid_issue: str | None
    txid_issue_detail: str | None
    txid_line: int | None
    source_file: Path
    relative_file: str
    start_line: int


@dataclass(frozen=True)
class TransactionSource(TransactionLocator):
    block: str


@dataclass(frozen=True)
class _FileEntryContext:
    source_file: Path
    relative_file: str
    lines: tuple[str, ...]
    locations: tuple[tuple[int, Any], ...]


def _try_lock_file(lock_handle) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        if msvcrt is not None:
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
    except OSError:
        return False
    raise RuntimeError("No supported file-lock implementation is available in this Python environment.")


def _unlock_file(lock_handle) -> None:
    if fcntl is not None:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        return


@contextmanager
def _ledger_write_lock():
    LEDGER_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_LOCK_PATH, "a+b") as lock_handle:
        deadline = time.monotonic() + LEDGER_LOCK_TIMEOUT_SECONDS
        while True:
            if _try_lock_file(lock_handle):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Timed out waiting for the ledger write lock. Another zledger write is still in progress."
                )
            time.sleep(LEDGER_LOCK_POLL_SECONDS)

        try:
            lock_handle.seek(0)
            lock_handle.truncate()
            lock_handle.write(f"{os.getpid()}\n".encode("utf-8"))
            lock_handle.flush()
            yield
        finally:
            try:
                _unlock_file(lock_handle)
            finally:
                try:
                    lock_handle.seek(0)
                    lock_handle.truncate()
                    lock_handle.flush()
                except OSError:
                    pass


def _escape_beancount_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _format_amount(amount: Decimal) -> str:
    quantized = amount.quantize(Decimal("0.01"))
    return f"{quantized:.2f}"


def _account_currency(account: str) -> str:
    currencies = _open_account_metadata().get(account, ())
    if len(currencies) != 1:
        raise ValueError(f"Account must declare exactly one currency for this operation: {account}")
    return currencies[0]


def _account_balance_as_of(account: str, entry_date: date, currency: str) -> Decimal:
    entries, _, _ = load_entries()
    balance = Decimal("0")
    for entry in entries:
        if getattr(entry, "date", None) is None or entry.date > entry_date:
            continue
        if not hasattr(entry, "postings"):
            continue
        for posting in entry.postings:
            if posting.account != account or not posting.units or posting.units.currency != currency:
                continue
            balance += posting.units.number
    return balance


def _relative_include_path(path: Path) -> str:
    return path.resolve().relative_to(LEDGER_HOME).as_posix()


def _entry_file(entry_date: date) -> Path:
    path = LEDGER_DIR / f"{entry_date.year}.beancount"
    if not path.exists():
        raise FileNotFoundError(
            f"No yearly ledger file exists for {entry_date.year}: {path}. Create it and update main.beancount first."
        )
    include_path = _relative_include_path(path)
    if include_path not in _main_includes():
        raise RuntimeError(
            f"Yearly ledger file exists but is not included by {LEDGER_FILE}: {include_path}. "
            "Update the main ledger includes before recording transactions."
        )
    return path


def _append_text_with_spacing(existing: str, block: str) -> str:
    if not existing:
        return f"{block.rstrip()}\n"

    suffix = existing
    if not suffix.endswith("\n"):
        suffix += "\n"
    if not suffix.endswith("\n\n"):
        suffix += "\n"
    return f"{suffix}{block.rstrip()}\n"


def _is_blank_line(line: str) -> bool:
    return not line.strip()


def _parse_txid_value(value: str) -> int | None:
    match = _TXID_VALUE_PATTERN.fullmatch(value)
    if not match:
        return None
    return int(match.group(1))


def _format_txid(number: int) -> str:
    width = max(_TXID_MIN_WIDTH, len(str(number)))
    return f"t{number:0{width}d}"


def _extract_txid_state_from_lines(
    lines: list[str] | tuple[str, ...],
    start_line: int,
    next_line: int,
) -> tuple[str | None, str | None, str | None, int | None]:
    txid_lines: list[tuple[int, str]] = []
    for offset, raw_line in enumerate(lines[start_line - 1:next_line - 1], start=0):
        if _TXID_PREFIX_PATTERN.match(raw_line):
            txid_lines.append((start_line + offset, raw_line))

    if not txid_lines:
        return None, "missing", None, None

    if len(txid_lines) > 1:
        first_line, _ = txid_lines[0]
        return None, "multiple", str(len(txid_lines)), first_line

    line_number, raw_line = txid_lines[0]
    match = _TXID_LINE_PATTERN.fullmatch(raw_line)
    if not match:
        return None, "malformed", raw_line.strip(), line_number

    candidate = match.group(1)
    if _parse_txid_value(candidate) is None:
        return None, "invalid", candidate, line_number

    return candidate, None, None, line_number


def _extract_txid_state(block: str, start_line: int) -> tuple[str | None, str | None, str | None, int | None]:
    block_lines = block.splitlines()
    return _extract_txid_state_from_lines(block_lines, start_line, start_line + len(block_lines))


def _source_sort_key(source: TransactionLocator) -> tuple[int, str, int]:
    include_order = {include_path: index for index, include_path in enumerate(_main_includes())}
    return (
        include_order.get(source.relative_file, len(include_order)),
        source.relative_file,
        source.start_line,
    )


def _format_txid_error(source: TransactionLocator) -> str:
    location_line = source.txid_line or source.start_line
    location = f"{source.relative_file}:{location_line}"
    issue = source.txid_issue
    if issue == "missing":
        return f"Missing txid in {location}. Run `zledger txid backfill`."
    if issue == "multiple":
        count = source.txid_issue_detail or "multiple"
        return f"Multiple txid lines in {location} ({count}). Keep exactly one `txid` metadata line."
    if issue == "malformed":
        return f"Malformed txid metadata in {location}. Expected a quoted txid generated by zledger."
    if issue == "invalid":
        detail = source.txid_issue_detail or ""
        return f"Invalid txid '{detail}' in {location}. Expected `t` followed by digits."
    raise ValueError(f"Unknown txid issue for {location}: {issue}")


def _validate_transaction_locators(
    locators: list[TransactionLocator],
    *,
    allow_missing: bool,
) -> dict[str, TransactionLocator]:
    errors: list[str] = []
    by_txid: dict[str, TransactionLocator] = {}

    for locator in sorted(locators, key=_source_sort_key):
        if locator.txid_issue is not None:
            if allow_missing and locator.txid_issue == "missing":
                continue
            errors.append(_format_txid_error(locator))
            continue

        assert locator.txid is not None
        duplicate = by_txid.get(locator.txid)
        if duplicate is not None:
            errors.append(
                f"Duplicate txid '{locator.txid}' in "
                f"{duplicate.relative_file}:{duplicate.start_line} and {locator.relative_file}:{locator.start_line}."
            )
            continue
        by_txid[locator.txid] = locator

    if errors:
        snippets = " | ".join(errors[:3])
        if len(errors) > 3:
            snippets += f" | ... ({len(errors)} total)"
        raise ValueError(snippets)

    return by_txid


def _validate_transaction_sources(
    sources: list[TransactionSource],
    *,
    allow_missing: bool,
) -> dict[str, TransactionSource]:
    return _validate_transaction_locators(sources, allow_missing=allow_missing)


@lru_cache(maxsize=1)
def _open_entry_type():
    _, Open = _require_beancount()
    return Open


def _file_entry_contexts(entries: list[Any]) -> tuple[_FileEntryContext, ...]:
    entry_locations: dict[str, list[tuple[int, Any]]] = {}
    for entry in entries:
        meta = getattr(entry, "meta", {}) or {}
        filename = meta.get("filename")
        lineno = meta.get("lineno")
        if not filename or not lineno:
            continue
        entry_locations.setdefault(str(filename), []).append((int(lineno), entry))

    contexts: list[_FileEntryContext] = []
    for filename, locations in entry_locations.items():
        source_file = Path(filename)
        if not source_file.exists():
            continue
        lines = tuple(source_file.read_text(encoding="utf-8").splitlines())
        locations.sort(key=lambda item: item[0])
        contexts.append(
            _FileEntryContext(
                source_file=source_file,
                relative_file=_display_relative_path(source_file),
                lines=lines,
                locations=tuple(locations),
            )
        )
    return tuple(contexts)


def _build_transaction_locators(entries: list[Any]) -> tuple[dict[int, TransactionLocator], list[TransactionLocator]]:
    by_entry: dict[int, TransactionLocator] = {}
    locators: list[TransactionLocator] = []

    for context in _file_entry_contexts(entries):
        for index, (start_line, entry) in enumerate(context.locations):
            if not _is_transaction_entry(entry):
                continue

            next_line = (
                context.locations[index + 1][0]
                if index + 1 < len(context.locations)
                else len(context.lines) + 1
            )
            txid, txid_issue, txid_issue_detail, txid_line = _extract_txid_state_from_lines(
                context.lines,
                start_line,
                next_line,
            )
            locator = TransactionLocator(
                txid=txid,
                txid_issue=txid_issue,
                txid_issue_detail=txid_issue_detail,
                txid_line=txid_line,
                source_file=context.source_file,
                relative_file=context.relative_file,
                start_line=start_line,
            )
            by_entry[id(entry)] = locator
            locators.append(locator)

    return by_entry, locators


def _next_txid_number(entries: list[Any]) -> int:
    _, locators = _build_transaction_locators(entries)
    by_txid = _validate_transaction_locators(locators, allow_missing=False)
    if not by_txid:
        return 1
    return max(_parse_txid_value(txid) or 0 for txid in by_txid) + 1


def _assign_next_txid(draft: TransactionDraft) -> TransactionDraft:
    if draft.txid is not None:
        return draft
    entries, _, _ = load_entries()
    return replace(draft, txid=_format_txid(_next_txid_number(entries)))


def _insert_txid_line(existing: str, start_line: int, txid: str) -> str:
    newline = "\r\n" if "\r\n" in existing else "\n"
    lines = existing.splitlines(keepends=True)
    insert_at = start_line
    lines.insert(insert_at, f'  txid: "{txid}"{newline}')
    return "".join(lines)


def _remove_source_block(existing: str, source: TransactionSource) -> str:
    lines = existing.splitlines(keepends=True)
    block_lines = source.block.splitlines(keepends=True)
    start = source.start_line - 1
    end = start + len(block_lines)

    if start < 0 or end > len(lines) or lines[start:end] != block_lines:
        raise RuntimeError(
            f"Current file contents no longer match txid {source.txid or '<missing>'}. Re-run `zledger transaction` and pick a current txid."
        )

    remaining = lines[:start] + lines[end:]

    while remaining and _is_blank_line(remaining[0]):
        remaining.pop(0)
    while remaining and _is_blank_line(remaining[-1]):
        remaining.pop()

    index = max(0, start - 1)
    while index + 1 < len(remaining):
        if _is_blank_line(remaining[index]) and _is_blank_line(remaining[index + 1]):
            del remaining[index + 1]
            continue
        index += 1

    text = "".join(remaining)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def validate_ledger() -> None:
    if not Path(LEDGER_PATH).exists():
        raise FileNotFoundError(f"Ledger file not found: {LEDGER_PATH}")
    loader, _ = _require_beancount()
    _, errors, _ = loader.load_file(LEDGER_PATH)
    if not errors:
        return

    snippets = []
    for error in errors[:3]:
        snippets.append(str(error).splitlines()[0].strip())
    summary = " | ".join(snippets)
    raise RuntimeError(f"Ledger validation failed with {len(errors)} error(s): {summary}")


def append_transaction(draft: TransactionDraft) -> TransactionDraft:
    with _ledger_write_lock():
        draft = _assign_next_txid(draft)
        target = draft.target_file
        original_bytes = target.read_bytes()
        original = original_bytes.decode("utf-8")
        updated = _append_text_with_spacing(original, draft.render())
        target.write_text(updated, encoding="utf-8", newline="\n")
        try:
            validate_ledger()
            refreshed_entries, _, _ = load_entries()
            _, refreshed_locators = _build_transaction_locators(refreshed_entries)
            _validate_transaction_locators(refreshed_locators, allow_missing=False)
        except Exception:
            target.write_bytes(original_bytes)
            raise
        return draft


def delete_transaction(txid: str) -> TransactionSource:
    with _ledger_write_lock():
        source = get_transaction_source(txid)
        original_bytes = source.source_file.read_bytes()
        original = original_bytes.decode("utf-8")
        updated = _remove_source_block(original, source)
        source.source_file.write_text(updated, encoding="utf-8", newline="\n")
        try:
            validate_ledger()
            refreshed_entries, _, _ = load_entries()
            _, refreshed_locators = _build_transaction_locators(refreshed_entries)
            _validate_transaction_locators(refreshed_locators, allow_missing=False)
        except Exception:
            source.source_file.write_bytes(original_bytes)
            raise
        return source


def build_expense_draft(
    entry_date: date,
    amount: Decimal,
    account_query: str,
    category_query: str,
    payee: str,
    memo: str | None = None,
) -> TransactionDraft:
    account = resolve_balance_account(account_query)
    category = resolve_category_account(category_query, ("Expenses",))
    currency = _account_currency(account)
    return TransactionDraft(
        entry_date=entry_date,
        payee=payee,
        memo=memo,
        postings=(
            TransactionPosting(category, amount, currency),
            TransactionPosting(account, -amount, currency),
        ),
        target_file=_entry_file(entry_date),
    )


def build_income_draft(
    entry_date: date,
    amount: Decimal,
    account_query: str,
    category_query: str,
    payee: str,
    memo: str | None = None,
) -> TransactionDraft:
    account = resolve_balance_account(account_query)
    category = resolve_category_account(category_query, ("Income",))
    currency = _account_currency(account)
    return TransactionDraft(
        entry_date=entry_date,
        payee=payee,
        memo=memo,
        postings=(
            TransactionPosting(account, amount, currency),
            TransactionPosting(category, -amount, currency),
        ),
        target_file=_entry_file(entry_date),
    )


def build_transfer_draft(
    entry_date: date,
    amount: Decimal,
    from_query: str,
    to_query: str,
    received_amount: Decimal | None = None,
    memo: str | None = None,
) -> TransactionDraft:
    from_account = resolve_balance_account(from_query)
    to_account = resolve_balance_account(to_query)
    if from_account == to_account:
        raise ValueError("`--from` and `--to` must resolve to different accounts.")

    from_currency = _account_currency(from_account)
    to_currency = _account_currency(to_account)
    if from_currency == to_currency:
        if received_amount is not None:
            raise ValueError(
                "`--received` is only for cross-currency transfers. Omit it when both accounts use the same currency."
            )
        return TransactionDraft(
            entry_date=entry_date,
            payee=None,
            memo=memo,
            postings=(
                TransactionPosting(to_account, amount, to_currency),
                TransactionPosting(from_account, -amount, from_currency),
            ),
            target_file=_entry_file(entry_date),
        )

    if received_amount is None:
        raise ValueError(
            "Cross-currency transfer detected. Pass `--received` with the destination amount."
        )

    return TransactionDraft(
        entry_date=entry_date,
        payee=None,
        memo=memo,
        postings=(
            TransactionPosting(from_account, -amount, from_currency),
            TransactionPosting(
                to_account,
                received_amount,
                to_currency,
                total_price_amount=amount,
                total_price_currency=from_currency,
            ),
        ),
        target_file=_entry_file(entry_date),
    )


def build_adjust_draft(
    entry_date: date,
    account_query: str,
    memo: str,
    *,
    delta: Decimal | None = None,
    actual_balance: Decimal | None = None,
) -> TransactionDraft:
    if (delta is None) == (actual_balance is None):
        raise ValueError("Provide exactly one of `--delta` or `--actual-balance`.")

    account = resolve_balance_account(account_query)
    currency = _account_currency(account)

    if actual_balance is not None:
        current_balance = _account_balance_as_of(account, entry_date, currency)
        delta = actual_balance - current_balance

    assert delta is not None
    if delta == 0:
        raise ValueError("Adjustment would be zero; the ledger already matches the requested balance.")

    return TransactionDraft(
        entry_date=entry_date,
        payee=None,
        memo=memo,
        postings=(
            TransactionPosting(account, delta, currency),
            TransactionPosting(ADJUSTMENTS_ACCOUNT, -delta, currency),
        ),
        target_file=_entry_file(entry_date),
    )


def _require_beancount():
    try:
        from beancount import loader
        from beancount.core.data import Open
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "beancount is not available in this Python environment. Activate the beancount environment "
            "before running ledger commands."
        ) from exc
    return loader, Open


def load_entries():
    if not Path(LEDGER_PATH).exists():
        raise FileNotFoundError(f"Ledger file not found: {LEDGER_PATH}")
    loader, _ = _require_beancount()
    return loader.load_file(LEDGER_PATH)


def get_account_info(account: str) -> dict[str, Any]:
    currencies = _open_account_metadata().get(account, ())
    info = {"name": account.split(":")[-1], "icon": "💰", "currency": currencies[0] if len(currencies) == 1 else ""}
    info.update(ACCOUNTS.get(account, {}))
    return info


def _account_kind(account: str) -> str:
    root = account.split(":", 1)[0]
    return {
        "Assets": "asset",
        "Liabilities": "liability",
        "Expenses": "expense",
        "Income": "income",
        "Equity": "equity",
    }.get(root, root.casefold())


def _account_currency_display(account: str) -> str:
    currencies = tuple(str(item) for item in _open_account_metadata().get(account, ()) if item)
    if currencies:
        return ",".join(currencies)
    info = ACCOUNTS.get(account)
    if info and info.get("currency"):
        return str(info["currency"])
    return ""


def _catalog_name(account: str) -> str:
    if account in ACCOUNTS:
        return str(get_account_info(account)["name"])
    return account.split(":")[-1]


def _catalog_row(account: str) -> dict[str, Any]:
    return {
        "id": account,
        "name": _catalog_name(account),
        "kind": _account_kind(account),
        "currency": _account_currency_display(account),
        "archived": bool(ACCOUNTS.get(account, {}).get("archived", False)),
        "aliases": _display_aliases_for_account(account),
    }


def list_balance_accounts(
    match: str | None = None,
    *,
    archived_only: bool = False,
) -> list[dict[str, Any]]:
    accounts = _filter_roots(list(_ledger_accounts()), ("Assets", "Liabilities"))
    accounts = _archived_only(accounts) if archived_only else _filter_archived(accounts, include_archived=False)
    rows = [_catalog_row(account) for account in sorted(accounts)]
    return [row for row in rows if _catalog_matches(row, match)]


def show_balance_account(
    query: str,
    *,
    archived_only: bool = False,
) -> dict[str, Any]:
    account = resolve_account_name(
        query,
        roots=("Assets", "Liabilities"),
        include_archived=False,
        archived_only=archived_only,
    )
    return _catalog_row(account)


def list_category_accounts(
    *,
    kind: str | None = None,
    match: str | None = None,
) -> list[dict[str, Any]]:
    if kind == "expense":
        roots = ("Expenses",)
    elif kind == "income":
        roots = ("Income",)
    else:
        roots = ("Expenses", "Income")
    accounts = _filter_roots(list(_ledger_accounts()), roots)
    rows = [_catalog_row(account) for account in sorted(accounts)]
    return [row for row in rows if _catalog_matches(row, match)]


def show_category_account(
    query: str,
    *,
    kind: str,
) -> dict[str, Any]:
    roots = ("Expenses",) if kind == "expense" else ("Income",)
    account = resolve_account_name(query, roots=roots)
    return _catalog_row(account)


def get_account_balances(archived_only: bool = False) -> dict[str, dict[str, float]]:
    entries, _, _ = load_entries()
    Open = _open_entry_type()

    all_accounts = set()
    balances: dict[str, dict[str, float]] = {}
    for entry in entries:
        if isinstance(entry, Open):
            if entry.account.startswith("Assets:") or entry.account.startswith("Liabilities:"):
                all_accounts.add(entry.account)
        if hasattr(entry, "postings"):
            for posting in entry.postings:
                if posting.account not in balances:
                    balances[posting.account] = {}
                if posting.units:
                    currency = posting.units.currency
                    amount = float(posting.units.number)
                    balances[posting.account][currency] = balances[posting.account].get(currency, 0) + amount

    for account in all_accounts:
        if account not in balances:
            balances[account] = {currency: 0.0 for currency in _open_account_metadata().get(account, ())}

    if archived_only:
        return {k: v for k, v in balances.items() if ACCOUNTS.get(k, {}).get("archived", False)}
    return {k: v for k, v in balances.items() if not ACCOUNTS.get(k, {}).get("archived", False)}


def summarize_balances(archived_only: bool = False) -> dict[str, float]:
    totals: dict[str, float] = {}
    balances = get_account_balances(archived_only)
    for account, amounts in balances.items():
        if not (account.startswith("Assets:") or account.startswith("Liabilities:")):
            continue
        for currency, amount in amounts.items():
            totals[currency] = totals.get(currency, 0.0) + amount
    return {currency: round(amount, 2) for currency, amount in sorted(totals.items())}


def categorize_accounts(balances: dict[str, dict[str, float]], categories: list[tuple[Any, ...]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for cat_entry in categories:
        if len(cat_entry) == 4:
            cat_name, prefix, includes, excludes = cat_entry
        else:
            cat_name, prefix, includes = cat_entry
            excludes = []
        accounts = []
        for account, amounts in balances.items():
            if not account.startswith(prefix):
                continue
            if includes is not None and not any(account.endswith(inc) or account.endswith(":" + inc) or (":" + inc + ":") in account for inc in includes):
                continue
            if excludes and any(account.endswith(exc) or account.endswith(":" + exc) or (":" + exc + ":") in account for exc in excludes):
                continue
            info = get_account_info(account)
            for currency, amount in amounts.items():
                acc_data = {"id": account, "name": info["name"], "icon": info["icon"], "currency": currency, "balance": round(amount, 2)}
                if "limit" in info:
                    acc_data["limit"] = info["limit"]
                accounts.append(acc_data)
        if accounts:
            result[cat_name] = accounts
    return result


def flatten_balances(balances: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
    rows = []
    for account, amounts in balances.items():
        info = get_account_info(account)
        for currency, amount in amounts.items():
            row = {"id": account, "name": info["name"], "currency": currency, "balance": round(amount, 2)}
            if "limit" in info:
                row["limit"] = info["limit"]
            rows.append(row)
    return sorted(rows, key=lambda row: (row["currency"], row["id"]))


def account_matches(account: str, needle: str | None) -> bool:
    if not needle:
        return True
    info = get_account_info(account)
    haystacks = [account, str(info.get("name", ""))]
    needle = needle.casefold()
    return any(needle in haystack.casefold() for haystack in haystacks)


def _entry_matches_date(entry: Any, on_date: date | None) -> bool:
    return on_date is None or getattr(entry, "date", None) == on_date


def _is_transaction_entry(entry: Any) -> bool:
    return hasattr(entry, "postings") and hasattr(entry, "narration")


def transaction_matches_memo(row: dict[str, Any], needle: str | None) -> bool:
    if not needle:
        return True
    haystacks = [str(row.get("payee", "")), str(row.get("narration", ""))]
    needle = needle.casefold()
    return any(needle in haystack.casefold() for haystack in haystacks)


def _describe_counterpart(account: str) -> str:
    if account.startswith("Assets:") or account.startswith("Liabilities:"):
        return str(get_account_info(account)["name"])
    return account.split(":")[-1]


def _pick_counterpart(postings: list[Any], current_account: str) -> str:
    for posting in postings:
        if posting.account == current_account or posting.account == "Equity:Opening-Balances":
            continue
        return _describe_counterpart(posting.account)
    return ""


def _display_relative_path(path: Path | str) -> str:
    resolved = Path(path)
    try:
        return resolved.relative_to(LEDGER_HOME).as_posix()
    except ValueError:
        return resolved.as_posix()


def _build_transaction_sources(entries: list[Any]) -> tuple[dict[int, TransactionSource], list[TransactionSource]]:
    by_entry: dict[int, TransactionSource] = {}
    sources: list[TransactionSource] = []

    for context in _file_entry_contexts(entries):
        for index, (start_line, entry) in enumerate(context.locations):
            if not _is_transaction_entry(entry):
                continue

            next_line = (
                context.locations[index + 1][0]
                if index + 1 < len(context.locations)
                else len(context.lines) + 1
            )
            block = "\n".join(context.lines[start_line - 1:next_line - 1]).rstrip()
            if block:
                block += "\n"

            txid, txid_issue, txid_issue_detail, txid_line = _extract_txid_state_from_lines(
                context.lines,
                start_line,
                next_line,
            )
            source = TransactionSource(
                txid=txid,
                txid_issue=txid_issue,
                txid_issue_detail=txid_issue_detail,
                txid_line=txid_line,
                source_file=context.source_file,
                relative_file=context.relative_file,
                start_line=start_line,
                block=block,
            )
            by_entry[id(entry)] = source
            sources.append(source)

    return by_entry, sources


def _transaction_sources_by_txid(*, allow_missing: bool) -> tuple[dict[int, TransactionSource], dict[str, TransactionSource]]:
    entries, _, _ = load_entries()
    by_entry, sources = _build_transaction_sources(entries)
    by_txid = _validate_transaction_sources(sources, allow_missing=allow_missing)
    return by_entry, by_txid


def get_transaction_source(txid: str) -> TransactionSource:
    _, by_txid = _transaction_sources_by_txid(allow_missing=False)
    source = by_txid.get(txid)
    if source is None:
        raise ValueError(f"Unknown txid '{txid}'. Re-run `zledger transaction` and pick a current txid.")
    return source


def backfill_txids() -> dict[str, Any]:
    with _ledger_write_lock():
        entries, _, _ = load_entries()
        _, locators = _build_transaction_locators(entries)
        by_txid = _validate_transaction_locators(locators, allow_missing=True)

        next_number = 1 if not by_txid else max(_parse_txid_value(txid) or 0 for txid in by_txid) + 1
        missing_sources = [
            locator
            for locator in sorted(locators, key=_source_sort_key)
            if locator.txid_issue == "missing"
        ]
        if not missing_sources:
            return {"updated": 0, "files": {}, "next_txid": _format_txid(next_number)}

        assignments: list[tuple[TransactionLocator, str]] = []
        for source in missing_sources:
            txid = _format_txid(next_number)
            assignments.append((source, txid))
            next_number += 1

        by_file: dict[Path, list[tuple[int, str]]] = {}
        for source, txid in assignments:
            by_file.setdefault(source.source_file, []).append((source.start_line, txid))

        originals = {
            path: path.read_text(encoding="utf-8")
            for path in by_file
        }

        try:
            for path, file_assignments in by_file.items():
                updated = originals[path]
                for start_line, txid in sorted(file_assignments, key=lambda item: item[0], reverse=True):
                    updated = _insert_txid_line(updated, start_line, txid)
                path.write_text(updated, encoding="utf-8", newline="\n")

            validate_ledger()
            refreshed_entries, _, _ = load_entries()
            _, refreshed_locators = _build_transaction_locators(refreshed_entries)
            _validate_transaction_locators(refreshed_locators, allow_missing=False)
        except Exception:
            for path, original in originals.items():
                path.write_text(original, encoding="utf-8", newline="\n")
            raise

        file_counts = {
            _display_relative_path(path): len(file_assignments)
            for path, file_assignments in sorted(by_file.items(), key=lambda item: _display_relative_path(item[0]))
        }
        return {
            "updated": len(assignments),
            "files": file_counts,
            "next_txid": _format_txid(next_number),
        }


def _simple_transfer_postings(entry: Any) -> tuple[Any, Any] | None:
    if len(getattr(entry, "postings", ())) != 2:
        return None

    balance_postings = [
        posting for posting in entry.postings
        if posting.units and (posting.account.startswith("Assets:") or posting.account.startswith("Liabilities:"))
    ]
    if len(balance_postings) != 2:
        return None

    incoming = next((posting for posting in balance_postings if posting.units.number > 0), None)
    outgoing = next((posting for posting in balance_postings if posting.units.number < 0), None)
    if incoming is None or outgoing is None:
        return None

    if incoming.units.currency == outgoing.units.currency:
        if incoming.units.number + outgoing.units.number != 0:
            return None
        return incoming, outgoing

    for priced, other in ((incoming, outgoing), (outgoing, incoming)):
        price = getattr(priced, "price", None)
        if price is None or price.currency != other.units.currency:
            continue
        total = abs(priced.units.number * price.number)
        if abs(total - abs(other.units.number)) <= _FX_TOLERANCE:
            return incoming, outgoing

    return None


def _simple_adjust_postings(entry: Any) -> tuple[Any, Any] | None:
    if len(getattr(entry, "postings", ())) != 2:
        return None

    balance_postings = [
        posting for posting in entry.postings
        if posting.units and (posting.account.startswith("Assets:") or posting.account.startswith("Liabilities:"))
    ]
    adjustment_postings = [
        posting for posting in entry.postings
        if posting.units and posting.account == ADJUSTMENTS_ACCOUNT
    ]
    if len(balance_postings) != 1 or len(adjustment_postings) != 1:
        return None

    balance_posting = balance_postings[0]
    adjustment_posting = adjustment_postings[0]
    if balance_posting.units.currency != adjustment_posting.units.currency:
        return None
    if balance_posting.units.number + adjustment_posting.units.number != 0:
        return None
    return balance_posting, adjustment_posting


def list_transactions(limit: int = 20, on_date: date | None = None, memo: str | None = None) -> list[dict[str, Any]]:
    entries, _, _ = load_entries()
    locators_by_entry, locators = _build_transaction_locators(entries)
    _validate_transaction_locators(locators, allow_missing=False)
    transactions: list[dict[str, Any]] = []

    for entry in reversed(entries):
        if not _is_transaction_entry(entry):
            continue
        if not _entry_matches_date(entry, on_date):
            continue

        locator = locators_by_entry.get(id(entry))
        handled = False
        receivable_postings = [
            posting for posting in entry.postings
            if posting.units and posting.account.startswith("Assets:Receivables:")
        ]
        if receivable_postings:
            receivable_total = sum(float(posting.units.number) for posting in receivable_postings)
            is_advance = receivable_total > 0
            cash_postings = [
                posting for posting in entry.postings
                if posting.units
                and (posting.account.startswith("Assets:") or posting.account.startswith("Liabilities:"))
                and not posting.account.startswith("Assets:Receivables:")
            ]
            chosen = None
            for posting in cash_postings:
                amount = float(posting.units.number)
                if (is_advance and amount < 0) or ((not is_advance) and amount > 0):
                    chosen = posting
                    break
            if chosen is None and cash_postings:
                chosen = cash_postings[0]

            amount = abs(float(chosen.units.number)) if chosen and chosen.units else abs(receivable_total)
            currency = chosen.units.currency if chosen and chosen.units else receivable_postings[0].units.currency
            account = chosen.account if chosen else ""
            account_name = str(get_account_info(account)["name"]) if account else ""
            row = {
                "date": str(entry.date),
                "payee": entry.payee or "",
                "narration": entry.narration or "",
                "kind": "expense" if is_advance else "income",
                "category": "Reimbursement",
                "amount": round(amount, 2),
                "currency": currency,
                "account": account,
                "account_name": account_name,
                "txid": locator.txid if locator else "",
            }
            if not transaction_matches_memo(row, memo):
                continue
            transactions.append(row)
            handled = True
            if len(transactions) >= limit:
                break
            continue

        for posting in entry.postings:
            if not (posting.account.startswith("Expenses:") or posting.account.startswith("Income:")):
                continue
            account_posting = next(
                (
                    candidate for candidate in entry.postings
                    if candidate.units and (candidate.account.startswith("Assets:") or candidate.account.startswith("Liabilities:"))
                ),
                None,
            )
            account = account_posting.account if account_posting else ""
            account_name = str(get_account_info(account)["name"]) if account else ""
            row = {
                "date": str(entry.date),
                "payee": entry.payee or "",
                "narration": entry.narration or "",
                "kind": "expense" if posting.account.startswith("Expenses:") else "income",
                "category": posting.account.split(":")[-1],
                "amount": round(abs(float(posting.units.number)) if posting.units else 0.0, 2),
                "currency": posting.units.currency if posting.units else "GBP",
                "account": account,
                "account_name": account_name,
                "txid": locator.txid if locator else "",
            }
            if not transaction_matches_memo(row, memo):
                continue
            transactions.append(row)
            handled = True
            break
        if handled:
            if len(transactions) >= limit:
                break
            continue

        transfer = _simple_transfer_postings(entry)
        if transfer is not None:
            incoming, outgoing = transfer
            account_name = str(get_account_info(incoming.account)["name"])
            row = {
                "date": str(entry.date),
                "payee": entry.payee or "",
                "narration": entry.narration or "",
                "kind": "transfer",
                "category": "Transfer",
                "amount": round(abs(float(incoming.units.number)), 2),
                "currency": incoming.units.currency,
                "account": incoming.account,
                "account_name": account_name,
                "counterpart": _describe_counterpart(outgoing.account),
                "txid": locator.txid if locator else "",
            }
            if not transaction_matches_memo(row, memo):
                continue
            transactions.append(row)
            if len(transactions) >= limit:
                break
            continue

        adjust = _simple_adjust_postings(entry)
        if adjust is not None:
            balance_posting, adjustment_posting = adjust
            account_name = str(get_account_info(balance_posting.account)["name"])
            row = {
                "date": str(entry.date),
                "payee": entry.payee or "",
                "narration": entry.narration or "",
                "kind": "adjust",
                "category": "Adjustment",
                "amount": round(abs(float(balance_posting.units.number)), 2),
                "currency": balance_posting.units.currency,
                "account": balance_posting.account,
                "account_name": account_name,
                "counterpart": _describe_counterpart(adjustment_posting.account),
                "txid": locator.txid if locator else "",
            }
            if not transaction_matches_memo(row, memo):
                continue
            transactions.append(row)
        if len(transactions) >= limit:
            break

    return transactions


def list_account_transactions(
    account_filter: str,
    limit: int = 20,
    on_date: date | None = None,
    memo: str | None = None,
) -> list[dict[str, Any]]:
    entries, _, _ = load_entries()
    locators_by_entry, locators = _build_transaction_locators(entries)
    _validate_transaction_locators(locators, allow_missing=False)
    transactions: list[dict[str, Any]] = []

    for entry in reversed(entries):
        if not _is_transaction_entry(entry):
            continue
        if not _entry_matches_date(entry, on_date):
            continue

        locator = locators_by_entry.get(id(entry))
        matched_postings = [
            posting for posting in entry.postings
            if posting.units and account_matches(posting.account, account_filter)
        ]
        if not matched_postings:
            continue

        for posting in matched_postings:
            info = get_account_info(posting.account)
            row = {
                "date": str(entry.date),
                "payee": entry.payee or "",
                "narration": entry.narration or "",
                "account": posting.account,
                "account_name": str(info["name"]),
                "amount": round(float(posting.units.number), 2),
                "currency": posting.units.currency,
                "counterpart": _pick_counterpart(list(entry.postings), posting.account),
                "txid": locator.txid if locator else "",
            }
            if not transaction_matches_memo(row, memo):
                continue
            transactions.append(row)
            if len(transactions) >= limit:
                return transactions

    return transactions
