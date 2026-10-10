"""Integration tests use only disposable synthetic data, never a personal ledger."""
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen


class PrivateRuntime(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zledger-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "private"
        self.env = os.environ.copy()
        for key in ("FINANCES_LEDGER_PATH", "FINANCES_REIMBURSEMENTS_DB", "FINANCES_REPO", "PYTHONPATH"):
            self.env.pop(key, None)
        self.env.update(FINANCES_REPO=str(self.data), PYTHONUTF8="1")
        self.cli("init")

    def cli(self, *args, ok=True, env=None):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", "zledger", *args],
                                cwd=self.root, env=env or self.env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def add_account(self, name, currency="USD", alias=None):
        args = ["account", "add", "--name", name, "--currency", currency]
        if alias:
            args += ["--alias", alias]
        self.cli(*args)

    def txid(self, output):
        match = re.search(r'\btxid:\s*"?(t\d+)\b', output)
        self.assertIsNotNone(match, output)
        return match[1]

    def amount_file(self):
        return next(p for p in (self.data / "ledger").glob("*.beancount") if p.stem.isdecimal())


class CLITests(PrivateRuntime):
    def prepare(self):
        self.add_account("Assets:TestSource", alias="test-source")
        self.add_account("Assets:TestDestination", alias="test-destination")
        self.add_account("Expenses:TestCategory", alias="test-expense")
        self.add_account("Income:TestCategory", alias="test-income")

    def test_empty_initialization_and_existing_data_preserved(self):
        self.assertEqual(self.amount_file().read_text(encoding="utf-8"), "")
        self.assertIn("Equity:Adjustments", (self.data / "ledger/accounts.beancount").read_text())
        self.cli("balance")
        self.cli("summary")
        self.cli("init", ok=False)
        self.assertEqual(self.amount_file().read_text(encoding="utf-8"), "")

    def test_installed_package_has_no_source_checkout_dependency(self):
        p = subprocess.run([sys.executable, "-c", "import zledger,app;from pathlib import Path;print(zledger.__file__);assert (Path(app.__file__).parent/'static/index.html').is_file()"],
                           cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("site-packages", p.stdout)

    def test_installed_console_entrypoint_outputs_utf8(self):
        self.add_account("Assets:TestSource", "EUR", "临时测试别名")
        env = self.env.copy()
        env.pop("PYTHONUTF8", None)
        env["PYTHONIOENCODING"] = "ascii"
        entry = Path(sys.executable).parent / ("zledger.exe" if os.name == "nt" else "zledger")
        p = subprocess.run([str(entry), "account", "show", "--account", "临时测试别名"],
                           cwd=self.root, env=env, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8"))
        self.assertIn("临时测试别名", p.stdout.decode("utf-8"))

    def test_default_location_uses_platform_data_directory(self):
        env = self.env.copy()
        env.pop("FINANCES_REPO")
        env.update(LOCALAPPDATA=str(self.root / "platform"), XDG_DATA_HOME=str(self.root / "platform"))
        self.cli("init", env=env)
        self.assertTrue((self.root / "platform/zledger/main.beancount").is_file())
        self.cli("balance", env=env)

    def test_initialization_rejects_source_directory(self):
        package = Path(__file__).resolve().parents[1]
        target = package / "private-test-must-not-exist"
        self.cli("init", "--directory", str(target), ok=False)
        self.assertFalse(target.exists())

    def test_account_definitions_aliases_and_no_default_currency_guess(self):
        self.add_account("Assets:TestSource", "EUR", "temporary-alias")
        self.assertIn("EUR", self.cli("account", "show", "--account", "temporary-alias"))
        before = (self.data / "ledger/accounts.beancount").read_bytes()
        self.cli("account", "add", "--name", "Assets:TestSource", "--currency", "EUR", ok=False)
        self.assertEqual(before, (self.data / "ledger/accounts.beancount").read_bytes())
        self.cli("account", "add", "--name", "bad\nname", "--currency", "EUR", ok=False)
        self.assertEqual(before, (self.data / "ledger/accounts.beancount").read_bytes())

    def test_income_expense_transfer_adjust_delete_and_readback(self):
        self.prepare()
        income = self.txid(self.cli("record", "income", "--amount", "10", "--account", "test-source", "--category", "test-income", "--payer", "Synthetic payer", "--memo", "Temporary test"))
        expense = self.txid(self.cli("record", "expense", "--amount", "2", "--account", "test-source", "--category", "test-expense", "--payee", "Synthetic merchant", "--memo", "临时测试"))
        self.cli("record", "transfer", "--amount", "3", "--from", "test-source", "--to", "test-destination", "--memo", "Temporary transfer")
        self.cli("record", "adjust", "--account", "test-source", "--actual-balance", "4", "--memo", "Temporary adjustment")
        self.assertIn("临时测试", self.cli("transaction", "show", "--txid", expense))
        self.assertIn("4.00", self.cli("balance", "--account", "TestSource"))
        self.cli("record", "delete", "--txid", expense)
        self.cli("transaction", "show", "--txid", expense, ok=False)
        self.cli("transaction", "show", "--txid", income)
        self.cli("txid", "backfill")

    def test_unknown_and_ambiguous_alias_do_not_write(self):
        self.prepare()
        self.add_account("Assets:Other", alias="test-source")
        before = self.amount_file().read_bytes()
        for name in ("test-source", "unknown-private-account"):
            self.cli("record", "expense", "--amount", "1", "--account", name, "--category", "test-expense", "--payee", "Synthetic", "--memo", "Temporary", ok=False)
        self.assertEqual(before, self.amount_file().read_bytes())

    def test_multicurrency_transfer_and_year_file(self):
        self.add_account("Assets:TestSource", "USD")
        self.add_account("Assets:TestDestination", "EUR")
        before = self.amount_file().read_bytes()
        self.cli("record", "transfer", "--amount", "2", "--from", "TestSource", "--to", "TestDestination", "--memo", "Temporary FX", ok=False)
        self.assertEqual(before, self.amount_file().read_bytes())
        self.cli("record", "transfer", "--amount", "2", "--received", "1", "--from", "TestSource", "--to", "TestDestination", "--memo", "Temporary FX")
        next_year = str(int(self.amount_file().stem) + 1)
        self.cli("year", "--add", next_year)
        self.cli("year", "--add", next_year, ok=False)
        self.assertTrue((self.data / f"ledger/{next_year}.beancount").is_file())

    def test_multicurrency_account_cannot_be_guessed_for_write(self):
        self.add_account("Assets:TestMulti", "USD")
        self.add_account("Expenses:TestCategory", "USD")
        p = self.data / "ledger/accounts.beancount"
        p.write_text(p.read_text().replace("Assets:TestMulti USD", "Assets:TestMulti USD,EUR"), encoding="utf-8")
        before = self.amount_file().read_bytes()
        self.cli("record", "expense", "--amount", "1", "--account", "TestMulti", "--category", "TestCategory", "--payee", "Synthetic", "--memo", "Temporary", ok=False)
        self.assertEqual(before, self.amount_file().read_bytes())
        self.assertIn("EUR", self.cli("balance", "--account", "TestMulti"))

    def test_private_display_metadata_and_archived_write_rejection(self):
        self.add_account("Assets:TestSource", "EUR")
        (self.data / "accounts.local.json").write_text(json.dumps({"Assets:TestSource": {"name": "Temporary display", "archived": True}}), encoding="utf-8")
        self.assertNotIn("Temporary display", self.cli("balance"))
        self.assertIn("Temporary display", self.cli("balance", "--archived"))
        self.cli("record", "adjust", "--account", "TestSource", "--delta", "1", "--memo", "Temporary", ok=False)

    def test_invalid_existing_ledger_rolls_back_transaction_append(self):
        self.prepare()
        p = self.amount_file()
        p.write_text("This is deliberately invalid synthetic input\n", encoding="utf-8")
        before = p.read_bytes()
        self.cli("record", "expense", "--amount", "1", "--account", "test-source", "--category", "test-expense", "--payee", "Synthetic", "--memo", "Temporary", ok=False)
        self.assertEqual(before, p.read_bytes())

    def test_failed_deletion_restores_original_bytes(self):
        self.prepare()
        txid = self.txid(self.cli("record", "expense", "--amount", "1", "--account", "test-source", "--category", "test-expense", "--payee", "Synthetic", "--memo", "Temporary"))
        p = self.amount_file()
        p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n") + b"Invalid synthetic input\r\n")
        before = p.read_bytes()
        self.cli("record", "delete", "--txid", txid, ok=False)
        self.assertEqual(before, p.read_bytes())

    def test_database_override_cannot_escape_private_directory(self):
        env = dict(self.env, FINANCES_REIMBURSEMENTS_DB=str(self.root / "outside.db"))
        self.cli("reimbursement", "list", ok=False, env=env)
        self.assertFalse((self.root / "outside.db").exists())

    def test_reimbursement_link_unlink_and_cross_case_duplicate_protection(self):
        self.add_account("Assets:TestSource")
        self.add_account("Assets:Receivables:TestPerson")
        advance = self.txid(self.cli("record", "transfer", "--amount", "2", "--from", "TestSource", "--to", "TestPerson", "--memo", "Temporary advance"))
        output = self.cli("reimbursement", "create", "--person", "Synthetic person", "--title", "Temporary case", "--base-currency", "USD")
        case = re.search(r"case #(\d+)", output, re.I)[1]
        self.cli("reimbursement", "link", "--case", case, "--txid", advance, "--role", "advance")
        self.assertIn("2.00", self.cli("reimbursement", "show", "--case", case))
        other = self.cli("reimbursement", "create", "--person", "Synthetic person", "--title", "Other temporary case", "--base-currency", "USD")
        other_case = re.search(r"case #(\d+)", other, re.I)[1]
        self.cli("reimbursement", "link", "--case", other_case, "--txid", advance, "--role", "advance", ok=False)
        self.cli("reimbursement", "unlink", "--case", case, "--txid", advance)
        self.cli("transaction", "show", "--txid", advance)


class APITests(PrivateRuntime):
    def test_dashboard_cli_start_status_stop_uses_private_state(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.env["ZLEDGER_DASHBOARD_PORT"] = str(sock.getsockname()[1])
        self.addCleanup(lambda: self.cli("dashboard", "stop"))
        self.assertIn("Dashboard started", self.cli("dashboard", "--no-open"))
        self.assertIn("Dashboard running", self.cli("dashboard", "status"))
        self.cli("dashboard", "stop")
        self.assertIn("not running", self.cli("dashboard", "status"))

    def test_packaged_dashboard_and_api_with_isolated_process(self):
        self.add_account("Assets:TestSource", "EUR")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                                   cwd=self.root, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        def stop():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
        self.addCleanup(stop)
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while True:
            try:
                with urlopen(base + "/api/summary", timeout=1) as r:
                    summary = json.load(r)
                break
            except URLError:
                if time.monotonic() > deadline:
                    self.fail("Isolated dashboard did not start")
                time.sleep(.1)
        self.assertEqual(summary["net"], {"EUR": 0.0})
        for route in ("/", "/flow", "/archive", "/reimbursements"):
            with urlopen(base + route) as r:
                body = r.read().decode("utf-8")
            self.assertIn("个人账本", body)
            self.assertNotIn("cdn", body.lower())
        for route in ("/api/accounts", "/api/archive", "/api/transactions", "/api/networth/history", "/api/reimbursements"):
            with urlopen(base + route) as r:
                json.load(r)
        self.assertTrue((self.data / "state/finance-reimbursements.db").is_file())


if __name__ == "__main__":
    unittest.main()
