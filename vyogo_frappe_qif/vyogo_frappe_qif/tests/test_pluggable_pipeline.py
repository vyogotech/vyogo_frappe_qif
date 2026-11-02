# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from vyogo_frappe_qif.api import import_qif, start_qif_import
from vyogo_frappe_qif.importer.custom_fields import ensure_custom_fields
from vyogo_frappe_qif.importer.staging import stage_qif_content, stage_records
from vyogo_frappe_qif.importer.stream_reader import get_file_line_iterator
from vyogo_frappe_qif.importer.worker import (
	clear_staging_bank_transaction_links,
	process_pending_rows,
	process_qif_batch,
	undo_qif_import,
)
from vyogo_frappe_qif.vyogo_frappe_qif.tests.test_qif_importer import (
	SAMPLE_QIF,
	_ensure_test_bank_account,
)


def custom_pipeline_ref_builder(row: dict, bank_account: str) -> str:
	"""Custom reference scheme: HOST-<bank_account_suffix>-<date>-<amount>-<occurrence>."""
	acc_code = bank_account.split("-")[0].strip()[-4:]
	return f"HOST-{acc_code}-{row['date']}-{abs(row['amount']):.2f}-{row.get('occurrence_idx', 1)}"


class TestPluggablePipeline(unittest.TestCase):
	def setUp(self):
		ensure_custom_fields()
		self.bank_account = _ensure_test_bank_account()

	def test_custom_reference_builder_and_dedupe(self):
		"""Custom reference builder sets reference_number and skips existing on re-upload."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		session1 = bsi.name

		stage_qif_content(session1, SAMPLE_QIF, company="Vyogo Technologies")
		process_qif_batch(
			import_session=session1,
			user="Administrator",
			batch_size=10,
			reference_builder=custom_pipeline_ref_builder,
		)

		staged1 = frappe.get_all(
			"QIF Staging Record",
			filters={"import_session": session1},
			fields=["name", "status", "bank_transaction"],
		)
		self.assertEqual(len(staged1), 3)
		for s in staged1:
			self.assertEqual(s.status, "Completed")
			bt = frappe.get_doc("Bank Transaction", s.bank_transaction)
			self.assertTrue(bt.reference_number.startswith("HOST-"))

		# Re-upload the same statement under a new session with the same builder
		bsi2 = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)
		session2 = bsi2.name

		stage_qif_content(session2, SAMPLE_QIF, company="Vyogo Technologies")
		process_qif_batch(
			import_session=session2,
			user="Administrator",
			batch_size=10,
			reference_builder=custom_pipeline_ref_builder,
		)

		staged2 = frappe.get_all(
			"QIF Staging Record",
			filters={"import_session": session2},
			fields=["name", "status", "bank_transaction"],
		)
		self.assertEqual(len(staged2), 3)
		for s in staged2:
			# All 3 rows must be Skipped because custom reference matched earlier rows
			self.assertEqual(s.status, "Skipped")
			self.assertTrue(s.bank_transaction)

		# Cleanup
		undo_qif_import(session1)
		frappe.db.delete("QIF Staging Record", {"import_session": session1})
		frappe.db.delete("QIF Staging Record", {"import_session": session2})
		frappe.db.delete("Bank Statement Import", {"name": session1})
		frappe.db.delete("Bank Statement Import", {"name": session2})
		frappe.db.commit()

	def test_configurable_queue_threading(self):
		"""Explicit queue is passed to frappe.enqueue and threaded through chained batches."""
		with patch("frappe.enqueue") as mock_enqueue:
			start_qif_import("SESSION-TEST-Q", batch_size=50, queue="priority-queue")
			mock_enqueue.assert_called_once()
			self.assertEqual(mock_enqueue.call_args.kwargs.get("queue"), "priority-queue")

	def test_remote_file_line_reader_hook_and_validation(self):
		"""Remote file throws ValidationError when no hook is present, and streams when hook is registered."""
		fake_remote_file = MagicMock()
		fake_remote_file.name = "REMOTE-FILE-001"
		fake_remote_file.is_remote_file = 1
		fake_remote_file.file_url = "s3://bucket/statement.qif"

		with self.assertRaises(frappe.ValidationError):
			list(get_file_line_iterator(fake_remote_file))

		# Now mock registering the qif_file_line_reader hook
		def fake_hook(file_doc):
			yield "!Type:Bank"
			yield "D01/01/2026"
			yield "T100.00"
			yield "^"

		with patch("vyogo_frappe_qif.importer.stream_reader.get_hook_handlers", return_value=[fake_hook]):
			lines = list(get_file_line_iterator(fake_remote_file))
			self.assertEqual(len(lines), 4)
			self.assertEqual(lines[0], "!Type:Bank")

	def test_staging_record_manual_creation_blocked(self):
		"""QIF Staging Records are system-managed and block manual insertion."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		try:
			staging_doc = frappe.get_doc(
				{
					"doctype": "QIF Staging Record",
					"import_session": bsi.name,
					"date": "2026-05-12",
					"amount": 100.0,
				}
			)
			with self.assertRaises(frappe.PermissionError) as cm:
				staging_doc.insert(ignore_permissions=True)
			self.assertIn("system-managed", str(cm.exception))
		finally:
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.db.commit()

	def test_sync_import_1000_rows(self):
		"""A 1,000-row sync import completes in-process and writes progress to Bank Statement Import."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		def generate_1000_lines():
			yield "!Type:Bank"
			for i in range(1000):
				yield "D12/05/2026"
				yield f"T-{10.0 + (i % 50):.2f}"
				yield f"PPAYEE {i}"
				yield f"MMemo {i}"
				yield f"NREF{i}"
				yield "^"

		try:
			res = import_qif(
				bank_statement_import=bsi.name,
				content=generate_1000_lines(),
				batch_size=250,
				async_run=False,
			)
			self.assertEqual(res["status"], "completed")
			self.assertEqual(res["total_staged"], 1000)

			bsi.reload()
			self.assertEqual(bsi.status, "Success")
			self.assertEqual(bsi.qif_total_rows, 1000)
			self.assertEqual(bsi.qif_processed_rows, 1000)
			self.assertEqual(bsi.qif_completed_rows, 1000)
			self.assertEqual(bsi.qif_failed_rows, 0)
			self.assertEqual(bsi.qif_skipped_rows, 0)

			completed = frappe.db.count(
				"QIF Staging Record", {"import_session": bsi.name, "status": "Completed"}
			)
			self.assertEqual(completed, 1000)
		finally:
			undo_qif_import(bsi.name)
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.db.commit()

	def test_progress_fields_match_staging_counts_after_each_batch(self):
		"""Progress fields match staging counts after each batch."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		records = [
			{
				"date": "2026-05-12",
				"amount": -(10.0 + i),
				"payee": f"Progress Payee {i}",
				"memo": f"Memo {i}",
				"reference_no": f"PROG-{i}",
				"occurrence_idx": 1,
			}
			for i in range(6)
		]
		try:
			stage_records(bsi.name, records, company="Vyogo Technologies", batch_size=10)
			bsi.reload()
			self.assertEqual(bsi.qif_total_rows, 6)
			self.assertEqual(bsi.qif_processed_rows, 0)

			process_pending_rows(bsi.name, batch_size=2)
			bsi.reload()
			self.assertEqual(bsi.qif_total_rows, 6)
			self.assertEqual(bsi.qif_processed_rows, 6)
			self.assertEqual(bsi.qif_completed_rows, 6)
			self.assertEqual(bsi.qif_failed_rows, 0)
			self.assertEqual(bsi.qif_skipped_rows, 0)
			self.assertEqual(bsi.status, "Success")
		finally:
			undo_qif_import(bsi.name)
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.db.commit()

	def test_staged_description_preserved_untruncated_over_140_chars(self):
		"""A staged description reaches Bank Transaction unchanged, including text longer than 140 chars."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		long_desc = "INVOICE PAYMENT FOR CONSULTING SERVICES RENDERED " + ("1234567890" * 20)
		self.assertGreater(len(long_desc), 140)

		records = [
			{
				"date": "2026-05-12",
				"amount": -350.0,
				"payee": "Very Long Payee Name Cap Test",
				"memo": "Standard Memo",
				"description": long_desc,
				"reference_no": "DESC-TEST-001",
				"occurrence_idx": 1,
			}
		]
		try:
			stage_records(bsi.name, records, company="Vyogo Technologies")
			process_pending_rows(bsi.name, batch_size=10)

			rec_name = frappe.get_all(
				"QIF Staging Record", filters={"import_session": bsi.name}, pluck="name", limit=1
			)[0]
			staging_row = frappe.get_doc("QIF Staging Record", rec_name)
			self.assertEqual(staging_row.status, "Completed")
			self.assertEqual(staging_row.description, long_desc)

			bt = frappe.get_doc("Bank Transaction", staging_row.bank_transaction)
			self.assertEqual(bt.description, long_desc)
		finally:
			undo_qif_import(bsi.name)
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.db.commit()

	def test_cancel_requested_flag_stops_and_skips_remaining(self):
		"""Setting qif_cancel_requested mid-import stops processing and leaves remaining rows Skipped."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		records = [
			{
				"date": "2026-05-12",
				"amount": -(100.0 + i),
				"payee": f"Cancel Payee {i}",
				"memo": f"Cancel Memo {i}",
				"reference_no": f"CANCEL-{i}",
				"occurrence_idx": 1,
			}
			for i in range(5)
		]
		try:
			stage_records(bsi.name, records, company="Vyogo Technologies")
			frappe.db.set_value("Bank Statement Import", bsi.name, "qif_cancel_requested", 1)
			frappe.db.commit()

			res = process_pending_rows(bsi.name, batch_size=2)
			self.assertEqual(res["skipped"], 5)
			self.assertEqual(res["completed"], 0)

			bsi.reload()
			self.assertEqual(bsi.qif_skipped_rows, 5)
			self.assertEqual(bsi.qif_completed_rows, 0)

			staged = frappe.get_all(
				"QIF Staging Record",
				filters={"import_session": bsi.name},
				fields=["status", "bank_transaction"],
			)
			self.assertEqual(len(staged), 5)
			for s in staged:
				self.assertEqual(s.status, "Skipped")
				self.assertIsNone(s.bank_transaction)
		finally:
			undo_qif_import(bsi.name)
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.db.commit()

	def test_delete_imported_bank_transaction_without_link_exists_error(self):
		"""A Bank Transaction created by QIF import can be deleted without LinkExistsError."""
		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		records = [
			{
				"date": "2026-05-12",
				"amount": -75.0,
				"payee": "Deletable Payee",
				"memo": "Delete Me",
				"reference_no": "DEL-001",
				"occurrence_idx": 1,
			}
		]
		try:
			stage_records(bsi.name, records, company="Vyogo Technologies")
			process_pending_rows(bsi.name, batch_size=10)

			rec_name = frappe.get_all(
				"QIF Staging Record", filters={"import_session": bsi.name}, pluck="name", limit=1
			)[0]
			staging_row = frappe.get_doc("QIF Staging Record", rec_name)
			self.assertEqual(staging_row.status, "Completed")
			bt_name = staging_row.bank_transaction
			self.assertTrue(bt_name)

			frappe.delete_doc("Bank Transaction", bt_name)
			frappe.db.commit()
			self.assertFalse(frappe.db.exists("Bank Transaction", bt_name))

			staging_row.reload()
			self.assertIsNone(staging_row.bank_transaction)

			count = clear_staging_bank_transaction_links("NONEXISTENT-BT")
			self.assertEqual(count, 0)
		finally:
			undo_qif_import(bsi.name)
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.db.commit()

	def test_qif_file_preview_and_save_no_template_error(self):
		"""Attaching and previewing a .qif file must not trigger Template Error."""
		qif_data = "!Type:Bank\nD01/01/2026\nT150.25\nPTest Vendor\nMInvoice 101\n^\n"
		file_doc = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "preview_sample.qif",
				"content": qif_data,
				"is_private": 1,
			}
		).insert(ignore_permissions=True)

		bsi = frappe.get_doc(
			{
				"doctype": "Bank Statement Import",
				"company": "Vyogo Technologies",
				"bank_account": self.bank_account,
				"import_file": file_doc.file_url,
				"status": "Pending",
			}
		).insert(ignore_permissions=True)

		try:
			bsi.validate()
			self.assertEqual(bsi.template_options, "{}")

			from vyogo_frappe_qif.importer.erpnext_patch import get_preview_from_template

			preview = get_preview_from_template(bsi.name, import_file=file_doc.file_url)
			self.assertIn("columns", preview)
			col_names = [c.get("header_title") for c in preview["columns"]]
			self.assertIn("Bank Account", col_names)
			self.assertGreaterEqual(len(preview["data"]), 1)

			from frappe.core.doctype.data_import.importer import ImportFile

			import_file_obj = ImportFile("Bank Transaction", file=file_doc.file_url)
			self.assertGreaterEqual(len(import_file_obj.raw_data), 2)
		finally:
			frappe.db.delete("Bank Statement Import", {"name": bsi.name})
			frappe.delete_doc("File", file_doc.name, force=1)
			frappe.db.commit()
