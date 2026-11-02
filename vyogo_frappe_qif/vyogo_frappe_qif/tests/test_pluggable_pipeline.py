# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from vyogo_frappe_qif.api import import_qif, start_qif_import
from vyogo_frappe_qif.importer.staging import stage_qif_content
from vyogo_frappe_qif.importer.stream_reader import get_file_line_iterator
from vyogo_frappe_qif.importer.worker import process_qif_batch, undo_qif_import
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
