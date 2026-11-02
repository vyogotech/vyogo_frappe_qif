# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import tracemalloc
import unittest
from collections.abc import Iterator

from vyogo_frappe_qif.importer.staging import stage_qif_content


def generate_qif_stream(count: int = 200000) -> Iterator[str]:
	"""Pure generator yielding QIF lines one by one without materializing in memory."""
	yield "!Type:Bank"
	for i in range(count):
		yield "D12/05/2026"
		yield f"T-{10 + (i % 100):.2f}"
		yield f"PPAYEE {i % 50}"
		yield f"MMemo entry {i}"
		yield f"NREF{i}"
		yield "^"


class TestMemoryStreaming(unittest.TestCase):
	def test_streaming_memory_bounded_200k_records(self):
		"""Verify that streaming 200,000 transactions through the pipeline maintains O(1) bounded memory."""
		record_count = 200000
		batch_size = 2500

		import frappe

		bulk_insert_count = 0

		def fake_bulk_insert(doctype, columns, rows):
			nonlocal bulk_insert_count
			bulk_insert_count += 1
			assert doctype == "QIF Staging Record"
			assert len(rows) <= batch_size

		orig_bulk = getattr(frappe.db, "bulk_insert", None)
		orig_commit = getattr(frappe.db, "commit", None)
		frappe.db.bulk_insert = fake_bulk_insert
		frappe.db.commit = lambda: None

		tracemalloc.start()
		tracemalloc.reset_peak()

		try:
			res = stage_qif_content(
				import_session="MEM-SESSION-001",
				content=generate_qif_stream(record_count),
				company="Vyogo Technologies",
				tenant_id="tenant-bench",
				batch_size=batch_size,
			)
			_current_mem, peak_mem = tracemalloc.get_traced_memory()
		finally:
			tracemalloc.stop()
			frappe.db.bulk_insert = orig_bulk
			frappe.db.commit = orig_commit

		self.assertEqual(res["total_staged"], record_count)
		self.assertEqual(bulk_insert_count, record_count // batch_size)

		peak_mb = peak_mem / (1024 * 1024)
		# 200,000 transactions if held in memory would exceed 150MB.
		# With O(1) streaming in batches of 2,500, peak memory overhead is strictly bounded (< 15MB).
		self.assertLess(
			peak_mb,
			20.0,
			f"Peak memory was {peak_mb:.2f} MB, which exceeds the O(1) bounded threshold of 20.0 MB",
		)
