# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, call, patch

from vyogo_frappe_qif.importer.staging import (
    compute_dedupe_hash,
    stage_qif_content,
    stage_records,
)


class TestStagingRecords(unittest.TestCase):
    def test_compute_dedupe_hash_deterministic(self):
        """Dedupe hash computation is deterministic and properly combines fields."""
        h1 = compute_dedupe_hash("T1", "Comp", "2026-05-12", -55.0, "Payee", 1)
        h2 = compute_dedupe_hash("T1", "Comp", "2026-05-12", -55.0, "Payee", 1)
        h3 = compute_dedupe_hash("T1", "Comp", "2026-05-12", -55.0, "Payee", 2)
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 24)

    def test_stage_records_batching(self):
        """stage_records consumes an iterable and bulk-inserts in batches of batch_size."""
        records = (
            {
                "date": "2026-05-12",
                "amount": float(i),
                "payee": f"Payee {i}",
                "memo": f"Memo {i}",
                "reference_no": f"REF-{i}",
                "occurrence_idx": 1,
            }
            for i in range(5)
        )

        mock_bulk_insert = MagicMock()
        with patch("frappe.db.bulk_insert", mock_bulk_insert):
            with patch("frappe.db.commit"):
                res = stage_records(
                    import_session="SESSION-1",
                    records=records,
                    company="Vyogo Tech",
                    tenant_id="tenant-1",
                    batch_size=2,
                )

        self.assertEqual(res["total_staged"], 5)
        self.assertEqual(res["import_session"], "SESSION-1")
        # 5 records with batch_size=2 => 3 bulk_insert calls (sizes: 2, 2, 1)
        self.assertEqual(mock_bulk_insert.call_count, 3)
        call_batches = [len(call_args[0][2]) for call_args in mock_bulk_insert.call_args_list]
        self.assertEqual(call_batches, [2, 2, 1])

    def test_stage_qif_content_with_string_backward_compatibility(self):
        """stage_qif_content still works when a raw string is passed."""
        sample_qif = "!Type:Bank\nD12/05/2026\nT-50.00\n^\n"

        mock_stage_records = MagicMock(return_value={"total_staged": 1, "import_session": "S1"})
        with patch("vyogo_frappe_qif.importer.staging.stage_records", mock_stage_records):
            res = stage_qif_content("S1", sample_qif, company="Comp", tenant_id="T1")
            self.assertEqual(res["total_staged"], 1)

            # Ensure stage_records was called with a generator that yielded 1 record
            called_records = list(mock_stage_records.call_args.kwargs["records"])
            self.assertEqual(len(called_records), 1)
            self.assertEqual(called_records[0]["date"], "2026-05-12")
            self.assertEqual(called_records[0]["amount"], -50.00)

    def test_stage_qif_content_with_line_iterator(self):
        """stage_qif_content accepts an iterable/generator of lines directly."""
        def line_generator():
            yield "!Type:Bank"
            yield "D12/05/2026"
            yield "T1200.00"
            yield "^"

        mock_stage_records = MagicMock(return_value={"total_staged": 1, "import_session": "S2"})
        with patch("vyogo_frappe_qif.importer.staging.stage_records", mock_stage_records):
            stage_qif_content("S2", line_generator(), company="Comp")
            called_records = list(mock_stage_records.call_args.kwargs["records"])
            self.assertEqual(len(called_records), 1)
            self.assertEqual(called_records[0]["amount"], 1200.00)
