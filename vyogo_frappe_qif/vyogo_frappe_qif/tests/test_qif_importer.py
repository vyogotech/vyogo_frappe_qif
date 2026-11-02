# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
import frappe
from vyogo_frappe_qif.importer.qif_parser import QifStreamParser
from vyogo_frappe_qif.importer.staging import stage_qif_content
from vyogo_frappe_qif.importer.worker import process_qif_batch, undo_qif_import

SAMPLE_QIF = """!Type:Bank
D12/05/2026
T-55.00
POFFICE SUPPLIES PTY LTD
MStationery
N1001
^
D12/05/2026
T-55.00
POFFICE SUPPLIES PTY LTD
MStationery second batch
N1002
^
D15/05/2026
T1250.00
PCLIENT DIRECT DEPOSIT
MInvoice 9981
^
"""


def _ensure_test_bank_account() -> str:
    if not frappe.db.exists("Bank", "Vyogo Test Bank"):
        frappe.get_doc({"doctype": "Bank", "bank_name": "Vyogo Test Bank"}).insert(ignore_permissions=True)

    acc = frappe.db.get_value(
        "Account",
        {"company": "Vyogo Technologies", "account_type": "Bank"},
        "name",
    ) or "11510 - Every Day Bank Account - VYOGO"

    full_name = "Vyogo Operating Test Account - Vyogo Test Bank"
    if frappe.db.exists("Bank Account", full_name):
        return full_name

    ba = frappe.get_doc(
        {
            "doctype": "Bank Account",
            "account_name": "Vyogo Operating Test Account",
            "bank": "Vyogo Test Bank",
            "company": "Vyogo Technologies",
            "account": acc,
            "is_company_account": 1,
        }
    ).insert(ignore_permissions=True)
    return ba.name


class TestQifImporter(unittest.TestCase):
    def test_qif_stream_parser(self):
        """Test stream parsing, date format sniffing, and occurrence disambiguation."""
        lines = SAMPLE_QIF.splitlines()
        parser = QifStreamParser(lines, date_format="%d/%m/%Y")
        records = list(parser.iter_records())

        self.assertEqual(len(records), 3)

        # First row
        row1 = records[0]
        self.assertEqual(row1["date"], "2026-05-12")
        self.assertEqual(row1["amount"], -55.00)
        self.assertEqual(row1["payee"], "OFFICE SUPPLIES PTY LTD")
        self.assertEqual(row1["occurrence_idx"], 1)

        # Second row: same day identical charge has occurrence_idx=2
        row2 = records[1]
        self.assertEqual(row2["date"], "2026-05-12")
        self.assertEqual(row2["amount"], -55.00)
        self.assertEqual(row2["occurrence_idx"], 2)

        # Dedupe hashes must be distinct
        self.assertNotEqual(row1["dedupe_hash"], row2["dedupe_hash"])

        # Third row: credit / deposit
        row3 = records[2]
        self.assertEqual(row3["date"], "2026-05-15")
        self.assertEqual(row3["amount"], 1250.00)

    def test_staging_multitenancy(self):
        """Test fast staging and tenant_id stamping."""
        test_session = f"BSI-TEST-{frappe.generate_hash(length=6)}"
        test_tenant = "tenant-vyogo-001"

        res = stage_qif_content(
            import_session=test_session,
            content=SAMPLE_QIF,
            company="Vyogo Technologies",
            tenant_id=test_tenant,
        )

        self.assertEqual(res["total_staged"], 3)
        self.assertEqual(res["tenant_id"], test_tenant)

        # Verify in database
        staged_rows = frappe.get_all(
            "QIF Staging Record",
            filters={"import_session": test_session},
            fields=["name", "tenant_id", "status", "occurrence_idx"],
        )
        self.assertEqual(len(staged_rows), 3)
        for r in staged_rows:
            self.assertEqual(r.tenant_id, test_tenant)
            self.assertEqual(r.status, "Pending")

        # Cleanup
        frappe.db.delete("QIF Staging Record", {"import_session": test_session})
        frappe.db.commit()

    def test_batch_processing_and_idempotency(self):
        """Test chunk worker converting staging rows to Bank Transactions with idempotency."""
        bank_account = _ensure_test_bank_account()
        test_tenant = "tenant-vyogo-002"

        # Create parent Bank Statement Import
        bsi = frappe.get_doc(
            {
                "doctype": "Bank Statement Import",
                "company": "Vyogo Technologies",
                "bank_account": bank_account,
                "status": "Pending",
            }
        ).insert(ignore_permissions=True)
        test_session = bsi.name

        stage_qif_content(
            import_session=test_session,
            content=SAMPLE_QIF,
            company="Vyogo Technologies",
            tenant_id=test_tenant,
        )

        # Run worker synchronously
        process_qif_batch(import_session=test_session, user="Administrator", batch_size=10)

        # Verify staging records are marked Completed
        staged = frappe.get_all(
            "QIF Staging Record",
            filters={"import_session": test_session},
            fields=["name", "status", "bank_transaction"],
        )
        self.assertEqual(len(staged), 3)
        for s in staged:
            self.assertEqual(s.status, "Completed")
            self.assertTrue(s.bank_transaction)
            self.assertTrue(frappe.db.exists("Bank Transaction", s.bank_transaction))

        # Test idempotency: re-running does not create duplicate Bank Transactions
        count_before = frappe.db.count("Bank Transaction", {"bank_account": bank_account})
        # Reset staging status to Pending to simulate re-processing
        frappe.db.sql(
            "UPDATE `tabQIF Staging Record` SET status='Pending' WHERE import_session=%s",
            (test_session,),
        )
        process_qif_batch(import_session=test_session, user="Administrator", batch_size=10)
        count_after = frappe.db.count("Bank Transaction", {"bank_account": bank_account})
        self.assertEqual(count_before, count_after)

        # Cleanup via Undo
        undo_res = undo_qif_import(test_session)
        self.assertEqual(undo_res["status"], "ok")
        self.assertEqual(undo_res["deleted_count"], 3)

        # Cleanup parent and staging
        frappe.db.delete("QIF Staging Record", {"import_session": test_session})
        frappe.db.delete("Bank Statement Import", {"name": test_session})
        frappe.db.commit()

    def test_bank_transaction_tenant_persistence(self):
        """Test that Bank Transaction.tenant_id is persisted even when not in Frappe DocType model."""
        bank_account = _ensure_test_bank_account()
        test_tenant = "tenant-vyogo-audit-007"

        bsi = frappe.get_doc(
            {
                "doctype": "Bank Statement Import",
                "company": "Vyogo Technologies",
                "bank_account": bank_account,
                "status": "Pending",
            }
        ).insert(ignore_permissions=True)
        test_session = bsi.name

        stage_qif_content(
            import_session=test_session,
            content=SAMPLE_QIF,
            company="Vyogo Technologies",
            tenant_id=test_tenant,
        )

        process_qif_batch(
            import_session=test_session,
            user="Administrator",
            tenant_id=test_tenant,
            batch_size=10,
        )

        staged = frappe.get_all(
            "QIF Staging Record",
            filters={"import_session": test_session},
            fields=["name", "bank_transaction"],
        )
        self.assertEqual(len(staged), 3)

        if frappe.db.has_column("Bank Transaction", "tenant_id"):
            for s in staged:
                db_tenant = frappe.db.get_value("Bank Transaction", s.bank_transaction, "tenant_id")
                self.assertEqual(db_tenant, test_tenant)

        # Cleanup via Undo
        undo_qif_import(test_session, tenant_id=test_tenant)
        frappe.db.delete("QIF Staging Record", {"import_session": test_session})
        frappe.db.delete("Bank Statement Import", {"name": test_session})
        frappe.db.commit()

    def test_ledger_guard_blocks_undo_for_reconciled(self):
        """Test that undo_qif_import raises error if any Bank Transaction is Reconciled."""
        bank_account = _ensure_test_bank_account()

        bsi = frappe.get_doc(
            {
                "doctype": "Bank Statement Import",
                "company": "Vyogo Technologies",
                "bank_account": bank_account,
                "status": "Pending",
            }
        ).insert(ignore_permissions=True)
        test_session = bsi.name

        stage_qif_content(
            import_session=test_session,
            content=SAMPLE_QIF,
            company="Vyogo Technologies",
        )
        process_qif_batch(import_session=test_session, user="Administrator", batch_size=10)

        # Reconcile one transaction artificially
        staged = frappe.get_all(
            "QIF Staging Record",
            filters={"import_session": test_session},
            pluck="bank_transaction",
        )
        bt_to_reconcile = [b for b in staged if b][0]
        frappe.db.set_value("Bank Transaction", bt_to_reconcile, "status", "Reconciled")
        frappe.db.commit()

        # Attempting undo must throw
        with self.assertRaises(frappe.ValidationError):
            undo_qif_import(test_session)

        # Clean up: unreconcile and then undo
        frappe.db.set_value("Bank Transaction", bt_to_reconcile, "status", "Pending")
        frappe.db.commit()
        undo_qif_import(test_session)

        frappe.db.delete("QIF Staging Record", {"import_session": test_session})
        frappe.db.delete("Bank Statement Import", {"name": test_session})
        frappe.db.commit()

    def test_row_failure_logs_to_error_log(self):
        """Test that row-level failures create a record in tabError Log with traceback."""
        from unittest.mock import patch
        from erpnext.accounts.doctype.bank_transaction.bank_transaction import BankTransaction

        bank_account = _ensure_test_bank_account()
        bsi = frappe.get_doc(
            {
                "doctype": "Bank Statement Import",
                "company": "Vyogo Technologies",
                "bank_account": bank_account,
                "status": "Pending",
            }
        ).insert(ignore_permissions=True)
        test_session = bsi.name

        stage_qif_content(
            import_session=test_session,
            content=SAMPLE_QIF,
            company="Vyogo Technologies",
        )

        # Mock an exception on BankTransaction.insert to simulate a system/validation failure
        with patch.object(BankTransaction, "insert", side_effect=ValueError("Simulated DB error")):
            process_qif_batch(import_session=test_session, user="Administrator", batch_size=10)

        # Verify staging records are marked Failed
        failed_records = frappe.get_all(
            "QIF Staging Record",
            filters={"import_session": test_session, "status": "Failed"},
            fields=["name", "error_message"],
        )
        self.assertEqual(len(failed_records), 3)
        self.assertIn("Simulated DB error", failed_records[0].error_message)

        # Verify record in Frappe's tabError Log
        error_logs = frappe.get_all(
            "Error Log",
            filters={"reference_doctype": "Bank Statement Import", "reference_name": test_session},
            fields=["name", "method", "error"],
        )
        self.assertGreaterEqual(len(error_logs), 1)
        self.assertIn("Simulated DB error", error_logs[0].error)

        # Cleanup
        frappe.db.delete("Error Log", {"reference_name": test_session})
        frappe.db.delete("QIF Staging Record", {"import_session": test_session})
        frappe.db.delete("Bank Statement Import", {"name": test_session})
        frappe.db.commit()

    def test_all_in_one_import_qif_api(self):
        """Test the all-in-one import_qif API method."""
        import os
        import tempfile
        from vyogo_frappe_qif.api import import_qif

        bank_account = _ensure_test_bank_account()
        with tempfile.NamedTemporaryFile("w+", suffix=".qif", delete=False) as tmp:
            tmp.write(SAMPLE_QIF)
            tmp_path = tmp.name

        try:
            res = import_qif(
                file_path=tmp_path,
                company="Vyogo Technologies",
                bank_account=bank_account,
                async_run=False,
            )
            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["total_staged"], 3)

            # Cleanup
            undo_qif_import(res["import_session"])
            frappe.db.delete("QIF Staging Record", {"import_session": res["import_session"]})
            frappe.db.delete("Bank Statement Import", {"name": res["import_session"]})
            frappe.db.commit()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def test_bank_statement_import_allows_qif_file(self):
        """Test that Bank Statement Import can be saved with a .qif file without Template Error."""
        from vyogo_frappe_qif.importer.erpnext_patch import patch_bank_statement_import
        patch_bank_statement_import()

        bank_account = _ensure_test_bank_account()
        bsi = frappe.get_doc({
            "doctype": "Bank Statement Import",
            "company": "Vyogo Technologies",
            "bank_account": bank_account,
            "import_file": "/private/files/myob_test.qif",
            "status": "Pending",
        })
        bsi.insert(ignore_permissions=True)
        self.assertTrue(bsi.name)

        # Cleanup
        frappe.db.delete("Bank Statement Import", {"name": bsi.name})
        frappe.db.commit()


