#!/usr/bin/env python3
"""Finance Dashboard - FastAPI"""
import hashlib
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from beancount import loader
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from zledger.ledger import (
    ACCOUNTS,
    ARCHIVE_CATEGORIES,
    CATEGORIES,
    LEDGER_PATH,
    categorize_accounts,
    get_account_balances,
    get_account_info,
    summarize_balances,
)
from pydantic import BaseModel

app = FastAPI(title="Finance Dashboard")
STATIC_DIR = Path(__file__).parent / "static"
from zledger.config import DATA_HOME
DEFAULT_DB_PATH = DATA_HOME / "state" / "finance-reimbursements.db"


def resolve_db_path() -> Path:
    configured = os.environ.get("FINANCES_REIMBURSEMENTS_DB")
    if not configured:
        return DEFAULT_DB_PATH
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = DATA_HOME / path
    resolved_root = DATA_HOME.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise RuntimeError(
            "FINANCES_REIMBURSEMENTS_DB must point inside the configured data directory."
        ) from exc
    return resolved_path


DB_PATH = resolve_db_path()
SETTLE_TOLERANCE_SAME = 0.01
SETTLE_TOLERANCE_CROSS = 1.0
FX_CACHE = {}


class ReimbursementCaseCreate(BaseModel):
    person_name: str
    person_tag: Optional[str] = None
    title: str
    description: Optional[str] = None
    base_currency: str
    notes: Optional[str] = None


class ReimbursementCaseUpdate(BaseModel):
    person_name: Optional[str] = None
    person_tag: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class AdvanceCreate(BaseModel):
    date: str
    amount: float
    currency: str
    account: Optional[str] = None
    payee_type: Optional[str] = None
    memo: Optional[str] = None
    txid: Optional[str] = None
    ledger_ref: Optional[str] = None


class RepaymentCreate(BaseModel):
    date: str
    amount: float
    currency: str
    account: Optional[str] = None
    memo: Optional[str] = None
    fx_rate_to_base: Optional[float] = None
    fx_base_amount: Optional[float] = None
    fx_source: Optional[str] = None
    txid: Optional[str] = None
    ledger_ref: Optional[str] = None


class LedgerLinkCreate(BaseModel):
    txid: Optional[str] = None
    ledger_ref: Optional[str] = None
    role: str

def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def _ensure_reimbursements_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_reimbursements_db():
    conn = get_db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS reimbursement_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_name TEXT NOT NULL,
            person_tag TEXT,
            title TEXT NOT NULL,
            description TEXT,
            base_currency TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reimbursement_advances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            account TEXT,
            payee_type TEXT,
            memo TEXT,
            ledger_ref TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES reimbursement_cases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reimbursement_repayments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            account TEXT,
            memo TEXT,
            fx_rate_to_base REAL,
            fx_base_amount REAL,
            fx_source TEXT,
            ledger_ref TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES reimbursement_cases(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS reimbursement_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            ledger_ref TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES reimbursement_cases(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_reimbursement_cases_person ON reimbursement_cases(person_name);
        CREATE INDEX IF NOT EXISTS idx_reimbursement_cases_status ON reimbursement_cases(status);
        CREATE INDEX IF NOT EXISTS idx_reimbursement_advances_case_id ON reimbursement_advances(case_id);
        CREATE INDEX IF NOT EXISTS idx_reimbursement_repayments_case_id ON reimbursement_repayments(case_id);
        CREATE INDEX IF NOT EXISTS idx_reimbursement_links_case_id ON reimbursement_links(case_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_reimbursement_links_case_ref ON reimbursement_links(case_id, ledger_ref);
        """
    )
    _ensure_reimbursements_column(conn, "reimbursement_advances", "txid", "TEXT")
    _ensure_reimbursements_column(conn, "reimbursement_repayments", "txid", "TEXT")
    _ensure_reimbursements_column(conn, "reimbursement_links", "txid", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reimbursement_advances_txid ON reimbursement_advances(txid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reimbursement_repayments_txid ON reimbursement_repayments(txid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reimbursement_links_txid ON reimbursement_links(txid) WHERE txid IS NOT NULL")
    conn.commit()
    conn.close()


def validate_status(status: str):
    valid = {"open", "partial", "settled", "overpaid", "cancelled"}
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status: {status}")


def validate_link_role(role: str):
    valid = {"advance", "repayment", "conversion"}
    if role not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid link role: {role}")


def _normalize_txid(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    text = value.strip()
    return text or None


def convert_to_base(amount: float, currency: str, base_currency: str, fx_rate_to_base: Optional[float], fx_base_amount: Optional[float]):
    if currency == base_currency:
        return float(amount)
    if fx_base_amount is not None:
        return float(fx_base_amount)
    if fx_rate_to_base is not None and fx_rate_to_base > 0:
        return float(amount) / float(fx_rate_to_base)
    raise HTTPException(status_code=400, detail="Cross-currency repayment requires fx_rate_to_base or fx_base_amount")


def build_ledger_ref(entry) -> str:
    parts = [str(entry.date), entry.payee or "", entry.narration or ""]
    for idx, posting in enumerate(entry.postings):
        if posting.units:
            parts.append(f"{idx}|{posting.account}|{posting.units.number}|{posting.units.currency}")
        else:
            parts.append(f"{idx}|{posting.account}|")
    digest = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"txn_{digest}"


def get_entry_txid(entry) -> Optional[str]:
    meta = getattr(entry, "meta", {}) or {}
    txid = meta.get("txid")
    if isinstance(txid, str):
        txid = txid.strip()
        return txid or None
    return None


def fetch_historical_fx_rate(date_str: str, base_currency: str, quote_currency: str):
    if base_currency == quote_currency:
        return {
            "rate_to_base": 1.0,
            "source": "same-currency",
            "resolved_date": date_str,
        }

    key = (date_str, base_currency, quote_currency)
    if key in FX_CACHE:
        return FX_CACHE[key]

    import json
    import urllib.request
    import urllib.parse

    query = urllib.parse.urlencode({"from": base_currency, "to": quote_currency})
    url = f"https://api.frankfurter.app/{date_str}?{query}"
    headers = {"User-Agent": "zledger-dashboard"}
    last_error = None

    for _ in range(2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
            rates = data.get("rates") or {}
            rate = rates.get(quote_currency)
            if not rate:
                raise ValueError(f"Missing FX rate for {quote_currency}")
            result = {
                "rate_to_base": float(rate),
                "source": "frankfurter",
                "resolved_date": data.get("date", date_str),
            }
            FX_CACHE[key] = result
            return result
        except Exception as exc:
            last_error = str(exc)

    result = {
        "rate_to_base": None,
        "source": f"unavailable: {last_error}" if last_error else "unavailable",
        "resolved_date": date_str,
    }
    FX_CACHE[key] = result
    return result


def _simple_fx_transfer_postings(entry):
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
        return None

    for priced, other in ((incoming, outgoing), (outgoing, incoming)):
        price = getattr(priced, "price", None)
        if price is None or price.currency != other.units.currency:
            continue
        total = abs(float(priced.units.number * price.number))
        if abs(total - abs(float(other.units.number))) <= 0.02:
            return incoming, outgoing

    return None


def build_reimbursement_ledger_entry(entry):
    if not (hasattr(entry, "postings") and hasattr(entry, "narration")):
        return None

    txid = get_entry_txid(entry)
    legacy_ref = build_ledger_ref(entry)

    receivable_postings = [
        posting for posting in entry.postings
        if posting.units and posting.account.startswith("Assets:Receivables:")
    ]
    filename = (entry.meta or {}).get("filename") if hasattr(entry, "meta") else None
    lineno = (entry.meta or {}).get("lineno") if hasattr(entry, "meta") else None
    location = f"{Path(filename).name}:{lineno}" if filename and lineno else None

    if receivable_postings:
        receivable_total = sum(float(posting.units.number) for posting in receivable_postings)
        if abs(receivable_total) < 1e-9:
            return None

        inferred_role = "advance" if receivable_total > 0 else "repayment"
        base_currency = receivable_postings[0].units.currency
        base_amount = round(abs(receivable_total), 2)

        cash_postings = [
            posting for posting in entry.postings
            if posting.units
            and (posting.account.startswith("Assets:") or posting.account.startswith("Liabilities:"))
            and not posting.account.startswith("Assets:Receivables:")
        ]

        desired_negative = inferred_role == "advance"
        chosen_posting = None
        for posting in cash_postings:
            amount = float(posting.units.number)
            if (desired_negative and amount < 0) or ((not desired_negative) and amount > 0):
                chosen_posting = posting
                break
        if chosen_posting is None and cash_postings:
            chosen_posting = cash_postings[0]

        amount = base_amount
        currency = base_currency
        account = None
        if chosen_posting and chosen_posting.units:
            amount = round(abs(float(chosen_posting.units.number)), 2)
            currency = chosen_posting.units.currency
            account = chosen_posting.account

        ledger_fx_rate_to_base = None
        for posting in receivable_postings:
            if posting.price and posting.price.currency == currency:
                ledger_fx_rate_to_base = float(posting.price.number)
                break

        market_fx = fetch_historical_fx_rate(str(entry.date), base_currency, currency)
        market_fx_rate_to_base = market_fx.get("rate_to_base")
        market_base_amount = base_amount
        if currency != base_currency and market_fx_rate_to_base:
            market_base_amount = round(amount / market_fx_rate_to_base, 2)

        receivable_accounts = [posting.account for posting in receivable_postings]
        title = entry.narration or entry.payee or "Untitled reimbursement entry"
        search_parts = [
            entry.payee or "",
            entry.narration or "",
            account or "",
            " ".join(receivable_accounts),
            txid or "",
            location or "",
        ]

        return {
            "txid": txid,
            "legacy_ledger_ref": legacy_ref,
            "date": str(entry.date),
            "payee": entry.payee or "",
            "narration": entry.narration or "",
            "title": title,
            "memo": title,
            "account": account,
            "amount": amount,
            "currency": currency,
            "base_currency": base_currency,
            "base_amount": base_amount,
            "ledger_fx_rate_to_base": ledger_fx_rate_to_base,
            "market_fx_rate_to_base": market_fx_rate_to_base,
            "market_base_amount": market_base_amount,
            "market_fx_source": market_fx.get("source"),
            "market_fx_date": market_fx.get("resolved_date"),
            "entry_kind": "receivable",
            "inferred_role": inferred_role,
            "location": location,
            "receivable_accounts": receivable_accounts,
            "compatible_base_currencies": [base_currency],
            "search_text": " ".join(part for part in search_parts if part).lower(),
        }

    transfer = _simple_fx_transfer_postings(entry)
    if transfer is None:
        return None

    incoming, outgoing = transfer
    title = entry.narration or entry.payee or "Untitled conversion"
    search_parts = [
        entry.payee or "",
        entry.narration or "",
        incoming.account,
        outgoing.account,
        txid or "",
        location or "",
    ]
    compatible_base_currencies = list(dict.fromkeys([incoming.units.currency, outgoing.units.currency]))
    return {
        "txid": txid,
        "legacy_ledger_ref": legacy_ref,
        "date": str(entry.date),
        "payee": entry.payee or "",
        "narration": entry.narration or "",
        "title": title,
        "memo": title,
        "account": f"{outgoing.account} -> {incoming.account}",
        "amount": round(abs(float(incoming.units.number)), 2),
        "currency": incoming.units.currency,
        "entry_kind": "conversion",
        "inferred_role": "conversion",
        "from_account": outgoing.account,
        "from_amount": round(abs(float(outgoing.units.number)), 2),
        "from_currency": outgoing.units.currency,
        "to_account": incoming.account,
        "to_amount": round(abs(float(incoming.units.number)), 2),
        "to_currency": incoming.units.currency,
        "location": location,
        "compatible_base_currencies": compatible_base_currencies,
        "search_text": " ".join(part for part in search_parts if part).lower(),
    }


def get_reimbursement_ledger_entries():
    entries, errors, options = loader.load_file(LEDGER_PATH)
    items = []
    txid_index = {}
    for entry in entries:
        item = build_reimbursement_ledger_entry(entry)
        if not item:
            continue
        items.append(item)
        if item.get("txid"):
            txid_index[item["txid"]] = item
    items.sort(key=lambda item: (item["date"], item.get("txid") or item["legacy_ledger_ref"]), reverse=True)
    return items, txid_index


def _lookup_linked_ledger_entry(link, txid_index: dict):
    txid = _normalize_txid(link["txid"]) if "txid" in link.keys() else None
    if txid:
        item = txid_index.get(txid)
        if item:
            return txid, item
    return txid, None


def _conversion_case_row(link, ledger_entry: dict, base_currency: str):
    if base_currency not in ledger_entry["compatible_base_currencies"]:
        raise HTTPException(
            status_code=400,
            detail=f"Case base currency {base_currency} is not compatible with conversion txid {ledger_entry.get('txid') or 'unknown'}"
        )

    if ledger_entry["to_currency"] == base_currency:
        base_amount = float(ledger_entry["to_amount"])
        base_account = ledger_entry["to_account"]
    else:
        base_amount = float(ledger_entry["from_amount"])
        base_account = ledger_entry["from_account"]

    return {
        "id": f"link-{link['id']}",
        "source": "ledger",
        "link_id": link["id"],
        "case_id": link["case_id"],
        "date": ledger_entry["date"],
        "amount": float(ledger_entry["to_amount"]),
        "currency": ledger_entry["to_currency"],
        "base_amount": round(base_amount, 2),
        "account": f"{ledger_entry['from_account']} -> {ledger_entry['to_account']}",
        "memo": ledger_entry["memo"],
        "txid": ledger_entry.get("txid"),
        "ledger_ref": ledger_entry["legacy_ledger_ref"],
        "location": ledger_entry["location"],
        "role": "conversion",
        "created_at": link["created_at"],
        "missing": False,
        "from_account": ledger_entry["from_account"],
        "from_amount": float(ledger_entry["from_amount"]),
        "from_currency": ledger_entry["from_currency"],
        "to_account": ledger_entry["to_account"],
        "to_amount": float(ledger_entry["to_amount"]),
        "to_currency": ledger_entry["to_currency"],
        "base_account": base_account,
    }


def compute_case_summary(
    conn: sqlite3.Connection,
    case_id: int,
    txid_index: Optional[dict] = None,
):
    case = conn.execute("SELECT * FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")

    advances = conn.execute("SELECT * FROM reimbursement_advances WHERE case_id = ? ORDER BY date, id", (case_id,)).fetchall()
    repayments = conn.execute("SELECT * FROM reimbursement_repayments WHERE case_id = ? ORDER BY date, id", (case_id,)).fetchall()
    links = conn.execute("SELECT * FROM reimbursement_links WHERE case_id = ? ORDER BY created_at, id", (case_id,)).fetchall()

    if txid_index is None:
        _, txid_index = get_reimbursement_ledger_entries()

    base_currency = case["base_currency"]
    total_advanced_base = 0.0
    total_repaid_base = 0.0
    total_repaid_market_base = 0.0
    total_converted_base = 0.0
    has_cross_currency = False

    advances_data = []
    for adv in advances:
        base_amount = float(adv["amount"]) if adv["currency"] == base_currency else 0.0
        if adv["currency"] != base_currency:
            has_cross_currency = True
        total_advanced_base += base_amount
        advances_data.append({
            "id": f"manual-advance-{adv['id']}",
            "source": "manual",
            "case_id": adv["case_id"],
            "date": adv["date"],
            "amount": float(adv["amount"]),
            "currency": adv["currency"],
            "base_amount": round(base_amount, 2),
            "account": adv["account"],
            "payee_type": adv["payee_type"],
            "memo": adv["memo"],
            "txid": adv["txid"],
            "ledger_ref": adv["ledger_ref"],
            "created_at": adv["created_at"],
        })

    repayments_data = []
    for rep in repayments:
        base_amount = convert_to_base(
            amount=float(rep["amount"]),
            currency=rep["currency"],
            base_currency=base_currency,
            fx_rate_to_base=rep["fx_rate_to_base"],
            fx_base_amount=rep["fx_base_amount"],
        )
        if rep["currency"] != base_currency:
            has_cross_currency = True
        total_repaid_base += base_amount
        total_repaid_market_base += base_amount
        repayments_data.append({
            "id": f"manual-repayment-{rep['id']}",
            "source": "manual",
            "case_id": rep["case_id"],
            "date": rep["date"],
            "amount": float(rep["amount"]),
            "currency": rep["currency"],
            "base_amount": round(base_amount, 2),
            "market_base_amount": round(base_amount, 2),
            "account": rep["account"],
            "memo": rep["memo"],
            "fx_rate_to_base": rep["fx_rate_to_base"],
            "fx_base_amount": rep["fx_base_amount"],
            "fx_source": rep["fx_source"],
            "txid": rep["txid"],
            "market_fx_rate_to_base": rep["fx_rate_to_base"],
            "market_fx_source": rep["fx_source"],
            "market_fx_date": rep["date"],
            "ledger_ref": rep["ledger_ref"],
            "created_at": rep["created_at"],
        })

    linked_entries = []
    conversions_data = []
    for link in links:
        resolved_txid, ledger_entry = _lookup_linked_ledger_entry(link, txid_index)
        if not ledger_entry:
            row = {
                "id": f"link-{link['id']}",
                "source": "ledger",
                "link_id": link["id"],
                "case_id": link["case_id"],
                "date": None,
                "amount": 0.0,
                "currency": base_currency,
                "base_amount": 0.0,
                "market_base_amount": 0.0,
                "account": None,
                "memo": "Missing ledger entry",
                "txid": resolved_txid,
                "ledger_ref": link["ledger_ref"],
                "location": None,
                "role": link["role"],
                "created_at": link["created_at"],
                "missing": True,
            }
            if row["role"] == "conversion":
                row.update({
                    "from_account": None,
                    "from_amount": 0.0,
                    "from_currency": None,
                    "to_account": None,
                    "to_amount": 0.0,
                    "to_currency": None,
                    "base_account": None,
                })
        else:
            if link["role"] == "conversion":
                row = _conversion_case_row(link, ledger_entry, base_currency)
            else:
                if base_currency not in ledger_entry["compatible_base_currencies"]:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Case base currency {base_currency} does not match ledger entry currencies {ledger_entry['compatible_base_currencies']}"
                    )
                if ledger_entry["currency"] != base_currency:
                    has_cross_currency = True
                row = {
                    "id": f"link-{link['id']}",
                    "source": "ledger",
                    "link_id": link["id"],
                    "case_id": link["case_id"],
                    "date": ledger_entry["date"],
                    "amount": float(ledger_entry["amount"]),
                    "currency": ledger_entry["currency"],
                    "base_amount": float(ledger_entry["base_amount"]),
                    "market_base_amount": float(ledger_entry["market_base_amount"]),
                    "account": ledger_entry["account"],
                    "memo": ledger_entry["memo"],
                    "ledger_fx_rate_to_base": ledger_entry["ledger_fx_rate_to_base"],
                    "market_fx_rate_to_base": ledger_entry["market_fx_rate_to_base"],
                    "market_fx_source": ledger_entry["market_fx_source"],
                    "market_fx_date": ledger_entry["market_fx_date"],
                    "txid": ledger_entry.get("txid"),
                    "ledger_ref": ledger_entry["legacy_ledger_ref"],
                    "location": ledger_entry["location"],
                    "role": link["role"],
                    "created_at": link["created_at"],
                    "missing": False,
                }

        linked_entries.append(row)
        if row["role"] == "advance":
            total_advanced_base += row["base_amount"]
            advances_data.append(row)
        elif row["role"] == "repayment":
            total_repaid_base += row["base_amount"]
            total_repaid_market_base += row.get("market_base_amount", row["base_amount"])
            repayments_data.append(row)
        else:
            total_converted_base += row["base_amount"]
            conversions_data.append(row)

    remaining = total_advanced_base - total_repaid_base
    market_remaining = total_advanced_base - total_repaid_market_base
    progress = (total_repaid_base / total_advanced_base) if total_advanced_base > 0 else 0.0
    market_progress = (total_repaid_market_base / total_advanced_base) if total_advanced_base > 0 else 0.0
    tolerance = SETTLE_TOLERANCE_CROSS if has_cross_currency else SETTLE_TOLERANCE_SAME
    ledger_net_gain_base = total_repaid_base - total_advanced_base
    market_net_gain_base = total_repaid_market_base - total_advanced_base
    fx_gain_base = total_repaid_market_base - total_repaid_base

    if case["status"] == "cancelled":
        derived_status = "cancelled"
    elif total_repaid_base <= 0:
        derived_status = "open"
    elif abs(remaining) <= tolerance:
        derived_status = "settled"
    elif remaining > tolerance:
        derived_status = "partial"
    else:
        derived_status = "overpaid"

    if case["status"] != "cancelled" and case["status"] != derived_status:
        conn.execute(
            "UPDATE reimbursement_cases SET status = ?, updated_at = ? WHERE id = ?",
            (derived_status, now_iso(), case_id),
        )
        conn.commit()
        case = conn.execute("SELECT * FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()

    return {
        "id": case["id"],
        "person_name": case["person_name"],
        "person_tag": case["person_tag"],
        "title": case["title"],
        "description": case["description"],
        "base_currency": base_currency,
        "status": case["status"],
        "notes": case["notes"],
        "created_at": case["created_at"],
        "updated_at": case["updated_at"],
        "summary": {
            "total_advanced_base": round(total_advanced_base, 2),
            "total_repaid_base": round(total_repaid_base, 2),
            "total_repaid_market_base": round(total_repaid_market_base, 2),
            "total_converted_base": round(total_converted_base, 2),
            "remaining_base": round(remaining, 2),
            "market_remaining_base": round(market_remaining, 2),
            "progress": round(progress, 4),
            "market_progress": round(market_progress, 4),
            "tolerance": tolerance,
            "ledger_net_gain_base": round(ledger_net_gain_base, 2),
            "market_net_gain_base": round(market_net_gain_base, 2),
            "fx_gain_base": round(fx_gain_base, 2),
            "fx_gain_mode": "estimated",
            "net_gain_base": round(market_net_gain_base, 2),
            "linked_entries": len(linked_entries),
            "conversion_entries": len(conversions_data),
        },
        "advances": advances_data,
        "repayments": repayments_data,
        "conversions": conversions_data,
        "ledger_links": linked_entries,
    }


@app.on_event("startup")
async def startup_event():
    init_reimbursements_db()


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/flow")
async def flow_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/archive")
async def archive_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/reimbursements")
async def reimbursements_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/accounts")
async def get_accounts():
    return categorize_accounts(get_account_balances(False), CATEGORIES)


@app.get("/api/archive")
async def get_archive():
    return categorize_accounts(get_account_balances(True), ARCHIVE_CATEGORIES)


@app.get("/api/summary")
async def get_summary():
    return {"net": summarize_balances(False)}


@app.get("/api/transactions")
async def get_transactions(limit: int = 50):
    entries, errors, options = loader.load_file(LEDGER_PATH)
    transactions = []
    for entry in reversed(entries):
        if hasattr(entry, 'postings') and hasattr(entry, 'narration'):
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
                transactions.append({
                    "date": str(entry.date),
                    "payee": entry.payee or "",
                    "narration": entry.narration or "",
                    "category": "Reimbursement",
                    "icon": "🧾",
                    "amount": amount,
                    "currency": currency,
                    "type": "expense" if is_advance else "income"
                })
                if len(transactions) >= limit:
                    break
                continue

            for posting in entry.postings:
                if posting.account.startswith("Expenses:") or posting.account.startswith("Income:"):
                    info = {"name": posting.account.split(":")[-1], "icon": "💸" if posting.account.startswith("Expenses:") else "💰"}
                    amount = float(posting.units.number) if posting.units else 0
                    transactions.append({
                        "date": str(entry.date), "payee": entry.payee or "", "narration": entry.narration or "",
                        "category": info["name"], "icon": info["icon"], "amount": amount,
                        "currency": posting.units.currency if posting.units else "",
                        "type": "expense" if posting.account.startswith("Expenses:") else "income"
                    })
                    break
        if len(transactions) >= limit:
            break
    return transactions


@app.get("/api/account/{account_id:path}/history")
async def get_account_history(account_id: str):
    """获取账户余额时间序列"""
    entries, errors, options = loader.load_file(LEDGER_PATH)

    daily_balance = {}
    running_balance = 0

    for entry in entries:
        if hasattr(entry, 'postings'):
            for posting in entry.postings:
                if posting.account == account_id and posting.units:
                    date_str = str(entry.date)
                    running_balance += float(posting.units.number)
                    daily_balance[date_str] = round(running_balance, 2)

    history = [{"date": d, "balance": b} for d, b in sorted(daily_balance.items())]
    info = get_account_info(account_id)

    return {
        "account": account_id,
        "name": info["name"],
        "currency": info["currency"],
        "history": history
    }


@app.get("/api/account/{account_id:path}/transactions")
async def get_account_transactions(account_id: str, limit: int = 100):
    """获取账户交易明细"""
    entries, errors, options = loader.load_file(LEDGER_PATH)
    transactions = []

    for entry in reversed(entries):
        if hasattr(entry, 'postings') and hasattr(entry, 'narration'):
            account_posting = None
            other_posting = None

            for posting in entry.postings:
                if posting.account == account_id:
                    account_posting = posting
                elif posting.account != "Equity:Opening-Balances":
                    other_posting = posting

            if account_posting and account_posting.units:
                amount = float(account_posting.units.number)
                counterpart = ""
                if other_posting:
                    counterpart = other_posting.account.split(":")[-1]
                    if other_posting.account.startswith("Expenses:"):
                        counterpart = f"→ {counterpart}"
                    elif other_posting.account.startswith("Income:"):
                        counterpart = f"← {counterpart}"
                    elif other_posting.account.startswith("Assets:") or other_posting.account.startswith("Liabilities:"):
                        counterpart = f"↔ {counterpart}"

                transactions.append({
                    "date": str(entry.date),
                    "payee": entry.payee or "",
                    "narration": entry.narration or "",
                    "counterpart": counterpart,
                    "amount": round(amount, 2),
                    "currency": account_posting.units.currency
                })

        if len(transactions) >= limit:
            break

    info = get_account_info(account_id)
    return {
        "account": account_id,
        "name": info["name"],
        "currency": info["currency"],
        "transactions": transactions
    }


@app.get("/api/networth/history")
async def get_networth_history():
    """Return balances per currency without an assumed exchange rate."""
    entries, errors, options = loader.load_file(LEDGER_PATH)
    if errors:
        raise HTTPException(status_code=400, detail="Ledger validation failed")
    daily = {}
    running = {}
    for entry in entries:
        for posting in getattr(entry, "postings", ()):
            if posting.units and posting.account.startswith(("Assets:", "Liabilities:")):
                currency = posting.units.currency
                running[currency] = running.get(currency, 0) + float(posting.units.number)
                daily[str(entry.date)] = dict(running)
    return {"history": [{"date": day, "balances": {c: round(v, 2) for c, v in values.items()}} for day, values in sorted(daily.items())]}


@app.get("/api/reimbursements")
async def list_reimbursements(status: Optional[str] = None, person: Optional[str] = None, q: Optional[str] = None):
    conn = get_db()
    where = []
    params = []

    if status:
        where.append("status = ?")
        params.append(status)
    if person:
        where.append("(person_name LIKE ? OR person_tag LIKE ?)")
        params.extend([f"%{person}%", f"%{person}%"])
    if q:
        where.append("(title LIKE ? OR person_name LIKE ? OR person_tag LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

    sql = "SELECT id FROM reimbursement_cases"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, id DESC"

    _, txid_index = get_reimbursement_ledger_entries()
    case_ids = [r["id"] for r in conn.execute(sql, tuple(params)).fetchall()]
    results = []
    for case_id in case_ids:
        full = compute_case_summary(conn, case_id, txid_index)
        results.append({
            "id": full["id"],
            "person_name": full["person_name"],
            "person_tag": full["person_tag"],
            "title": full["title"],
            "base_currency": full["base_currency"],
            "status": full["status"],
            "created_at": full["created_at"],
            "updated_at": full["updated_at"],
            "summary": full["summary"],
        })

    conn.close()
    return {"items": results}


@app.post("/api/reimbursements")
async def create_reimbursement_case(payload: ReimbursementCaseCreate):
    now = now_iso()
    conn = get_db()
    cur = conn.execute(
        """
        INSERT INTO reimbursement_cases
        (person_name, person_tag, title, description, base_currency, status, notes, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            payload.person_name.strip(),
            (payload.person_tag or "").strip() or None,
            payload.title.strip(),
            payload.description,
            payload.base_currency.strip().upper(),
            payload.notes,
            now,
            now,
        ),
    )
    conn.commit()
    case_id = cur.lastrowid
    case = compute_case_summary(conn, case_id)
    conn.close()
    return case


@app.get("/api/reimbursements/{case_id}")
async def get_reimbursement_case(case_id: int):
    conn = get_db()
    _, txid_index = get_reimbursement_ledger_entries()
    case = compute_case_summary(conn, case_id, txid_index)
    conn.close()
    return case


@app.get("/api/reimbursements/{case_id}/ledger-candidates")
async def get_reimbursement_candidates(case_id: int, q: Optional[str] = None, limit: int = 20):
    conn = get_db()
    case = conn.execute("SELECT * FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")

    existing_links = conn.execute("SELECT txid, case_id FROM reimbursement_links").fetchall()
    linked_case_by_txid = {row["txid"]: row["case_id"] for row in existing_links if row["txid"]}

    query = (q or "").strip().lower()
    if not query:
        query = " ".join(
            part for part in [case["person_name"], case["person_tag"], case["title"], case["description"], case["notes"]]
            if part
        ).lower()
    tokens = [token for token in query.replace("-", " ").split() if token]

    items, _ = get_reimbursement_ledger_entries()
    results = []
    for item in items:
        if tokens and not all(token in item["search_text"] for token in tokens):
            continue
        linked_case_id = None
        if item.get("txid"):
            linked_case_id = linked_case_by_txid.get(item["txid"])
        if linked_case_id is not None and linked_case_id != case_id:
            continue
        results.append({
            **item,
            "linked_case_id": linked_case_id,
            "is_linked_to_current_case": linked_case_id == case_id,
        })
        if len(results) >= limit:
            break

    conn.close()
    return {"items": results}


@app.post("/api/reimbursements/{case_id}/links")
async def add_reimbursement_link(case_id: int, payload: LedgerLinkCreate):
    conn = get_db()
    case = conn.execute("SELECT * FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")

    role = payload.role.strip().lower()
    validate_link_role(role)
    _, txid_index = get_reimbursement_ledger_entries()
    txid = _normalize_txid(payload.txid)
    ledger_entry = txid_index.get(txid) if txid else None
    if not ledger_entry:
        conn.close()
        raise HTTPException(status_code=404, detail="Ledger entry not found")
    if role == "conversion" and ledger_entry["entry_kind"] != "conversion":
        conn.close()
        raise HTTPException(
            status_code=400,
            detail="Only FX conversion ledger entries can be linked with role `conversion`"
        )
    if role != "conversion" and ledger_entry["entry_kind"] == "conversion":
        conn.close()
        raise HTTPException(status_code=400, detail="FX conversion ledger entries must use role `conversion`")
    if case["base_currency"] not in ledger_entry["compatible_base_currencies"]:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Case base currency {case['base_currency']} does not match ledger entry currencies {ledger_entry['compatible_base_currencies']}"
        )

    existing = conn.execute("SELECT * FROM reimbursement_links WHERE txid = ?", (txid,)).fetchone()
    if existing and existing["case_id"] != case_id:
        conn.close()
        raise HTTPException(status_code=400, detail="This ledger entry is already linked to another reimbursement case")

    if existing:
        conn.execute(
            "UPDATE reimbursement_links SET role = ?, ledger_ref = ?, txid = ? WHERE id = ?",
            (role, ledger_entry["legacy_ledger_ref"], txid, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO reimbursement_links (case_id, ledger_ref, txid, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (case_id, ledger_entry["legacy_ledger_ref"], txid, role, now_iso()),
        )
    conn.execute("UPDATE reimbursement_cases SET updated_at = ? WHERE id = ?", (now_iso(), case_id))
    conn.commit()
    result = compute_case_summary(conn, case_id, txid_index)
    conn.close()
    return result


@app.delete("/api/reimbursements/{case_id}/links/{link_id}")
async def delete_reimbursement_link(case_id: int, link_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM reimbursement_links WHERE id = ? AND case_id = ?", (link_id, case_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Link not found")
    conn.execute("DELETE FROM reimbursement_links WHERE id = ?", (link_id,))
    conn.execute("UPDATE reimbursement_cases SET updated_at = ? WHERE id = ?", (now_iso(), case_id))
    conn.commit()
    _, txid_index = get_reimbursement_ledger_entries()
    result = compute_case_summary(conn, case_id, txid_index)
    conn.close()
    return result


@app.patch("/api/reimbursements/{case_id}")
async def update_reimbursement_case(case_id: int, payload: ReimbursementCaseUpdate):
    conn = get_db()
    exists = conn.execute("SELECT id FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")

    updates = []
    params = []
    if payload.person_name is not None:
        updates.append("person_name = ?")
        params.append(payload.person_name.strip())
    if payload.person_tag is not None:
        updates.append("person_tag = ?")
        params.append(payload.person_tag.strip() or None)
    if payload.title is not None:
        updates.append("title = ?")
        params.append(payload.title.strip())
    if payload.description is not None:
        updates.append("description = ?")
        params.append(payload.description)
    if payload.notes is not None:
        updates.append("notes = ?")
        params.append(payload.notes)
    if payload.status is not None:
        validate_status(payload.status)
        updates.append("status = ?")
        params.append(payload.status)

    if updates:
        updates.append("updated_at = ?")
        params.append(now_iso())
        params.append(case_id)
        conn.execute(f"UPDATE reimbursement_cases SET {', '.join(updates)} WHERE id = ?", tuple(params))
        conn.commit()

    case = compute_case_summary(conn, case_id)
    conn.close()
    return case


@app.post("/api/reimbursements/{case_id}/advances")
async def add_advance(case_id: int, payload: AdvanceCreate):
    conn = get_db()
    case = conn.execute("SELECT id, base_currency FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")

    txid = _normalize_txid(payload.txid)

    conn.execute(
        """
        INSERT INTO reimbursement_advances
        (case_id, date, amount, currency, account, payee_type, memo, txid, ledger_ref, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            payload.date,
            payload.amount,
            payload.currency.strip().upper(),
            payload.account,
            payload.payee_type,
            payload.memo,
            txid,
            payload.ledger_ref,
            now_iso(),
        ),
    )
    conn.execute("UPDATE reimbursement_cases SET updated_at = ? WHERE id = ?", (now_iso(), case_id))
    conn.commit()
    result = compute_case_summary(conn, case_id)
    conn.close()
    return result


@app.post("/api/reimbursements/{case_id}/repayments")
async def add_repayment(case_id: int, payload: RepaymentCreate):
    conn = get_db()
    case = conn.execute("SELECT id, base_currency FROM reimbursement_cases WHERE id = ?", (case_id,)).fetchone()
    if not case:
        conn.close()
        raise HTTPException(status_code=404, detail="Case not found")

    currency = payload.currency.strip().upper()
    base_currency = case["base_currency"]
    if currency != base_currency and payload.fx_rate_to_base is None and payload.fx_base_amount is None:
        conn.close()
        raise HTTPException(status_code=400, detail="Cross-currency repayment requires fx_rate_to_base or fx_base_amount")

    txid = _normalize_txid(payload.txid)

    conn.execute(
        """
        INSERT INTO reimbursement_repayments
        (case_id, date, amount, currency, account, memo, fx_rate_to_base, fx_base_amount, fx_source, txid, ledger_ref, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case_id,
            payload.date,
            payload.amount,
            currency,
            payload.account,
            payload.memo,
            payload.fx_rate_to_base,
            payload.fx_base_amount,
            payload.fx_source or ("manual" if currency != base_currency else None),
            txid,
            payload.ledger_ref,
            now_iso(),
        ),
    )
    conn.execute("UPDATE reimbursement_cases SET updated_at = ? WHERE id = ?", (now_iso(), case_id))
    conn.commit()
    result = compute_case_summary(conn, case_id)
    conn.close()
    return result


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ZLEDGER_DASHBOARD_PORT", "8080")))
