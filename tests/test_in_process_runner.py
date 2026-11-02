# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

import frappe

from vyogo_frappe_qif.importer.worker import (
	clear_staging_bank_transaction_links,
	process_pending_rows,
)


class TestInProcessRunner(unittest.TestCase):
	def setUp(self):
		self.orig_db = getattr(frappe, "db", None)

	def tearDown(self):
		frappe.db = self.orig_db

	def test_process_pending_rows_batch_loop(self):
		"""process_pending_rows processes all batches in-process without enqueueing."""
		# Simulate 3 batches: 100 rows, 100 rows, 0 rows (done)
		mock_core = MagicMock(side_effect=[100, 50, 0])
		mock_progress = MagicMock(
			return_value={"total": 150, "processed": 150, "completed": 150, "failed": 0}
		)
		mock_finalize = MagicMock(return_value={"status": "Success", "total": 150, "completed": 150})
		mock_cancel = MagicMock(return_value=False)
		mock_commit = MagicMock()

		with (
			patch("vyogo_frappe_qif.importer.worker._process_batch_core", mock_core),
			patch("vyogo_frappe_qif.importer.worker._update_session_progress", mock_progress),
			patch("vyogo_frappe_qif.importer.worker._finalize_import_session", mock_finalize),
			patch("vyogo_frappe_qif.importer.worker._is_cancel_requested", mock_cancel),
			patch("frappe.db.commit", mock_commit),
		):
			res = process_pending_rows("SESSION-SYNC-1", batch_size=100)

		self.assertEqual(res["status"], "Success")
		self.assertEqual(mock_core.call_count, 3)
		# After each of the 2 non-empty batches, commit and progress are called
		self.assertEqual(mock_commit.call_count, 2)
		self.assertEqual(mock_progress.call_count, 2)
		mock_finalize.assert_called_once_with("SESSION-SYNC-1", None)

	def test_process_pending_rows_should_stop_callback(self):
		"""should_stop callback halts the loop before the batch, marking remaining pending skipped."""
		mock_core = MagicMock(side_effect=[100, 100])
		mock_mark_skipped = MagicMock(return_value=50)
		mock_finalize = MagicMock(return_value={"status": "Partial Success", "skipped": 50})
		mock_cancel = MagicMock(return_value=False)

		# Stop before batch 2
		calls = 0

		def stopper():
			nonlocal calls
			calls += 1
			return calls > 1

		with (
			patch("vyogo_frappe_qif.importer.worker._process_batch_core", mock_core),
			patch("vyogo_frappe_qif.importer.worker._update_session_progress"),
			patch("vyogo_frappe_qif.importer.worker._mark_pending_as_skipped", mock_mark_skipped),
			patch("vyogo_frappe_qif.importer.worker._finalize_import_session", mock_finalize),
			patch("vyogo_frappe_qif.importer.worker._is_cancel_requested", mock_cancel),
			patch("frappe.db.commit"),
		):
			res = process_pending_rows("SESSION-STOP-1", batch_size=100, should_stop=stopper)

		self.assertEqual(res["status"], "Partial Success")
		self.assertEqual(mock_core.call_count, 1)  # Only 1 batch executed before stop
		mock_mark_skipped.assert_called_once_with("SESSION-STOP-1")
		mock_finalize.assert_called_once_with("SESSION-STOP-1", None)

	def test_process_pending_rows_cancellation_flag(self):
		"""qif_cancel_requested on Bank Statement Import immediately skips remaining and finalises."""
		mock_core = MagicMock()
		mock_mark_skipped = MagicMock(return_value=80)
		mock_finalize = MagicMock(return_value={"status": "Success", "skipped": 80})

		with (
			patch("vyogo_frappe_qif.importer.worker._is_cancel_requested", return_value=True),
			patch("vyogo_frappe_qif.importer.worker._mark_pending_as_skipped", mock_mark_skipped),
			patch("vyogo_frappe_qif.importer.worker._finalize_import_session", mock_finalize),
			patch("vyogo_frappe_qif.importer.worker._process_batch_core", mock_core),
		):
			res = process_pending_rows("SESSION-CANCEL-1", batch_size=100)

		self.assertEqual(res["status"], "Success")
		mock_core.assert_not_called()
		mock_mark_skipped.assert_called_once_with("SESSION-CANCEL-1")
		mock_finalize.assert_called_once_with("SESSION-CANCEL-1", None)

	def test_clear_staging_bank_transaction_links(self):
		"""clear_staging_bank_transaction_links updates tabQIF Staging Record setting link to null."""
		mock_sql = MagicMock(return_value=2)
		with patch("frappe.db.sql", mock_sql):
			res = clear_staging_bank_transaction_links("BT-TEST-0001")
			self.assertEqual(res, 2)
			mock_sql.assert_called_once()
			self.assertIn("UPDATE `tabQIF Staging Record`", mock_sql.call_args[0][0])
			self.assertIn("SET bank_transaction = NULL", mock_sql.call_args[0][0])
			self.assertEqual(mock_sql.call_args[0][1], ("BT-TEST-0001",))
