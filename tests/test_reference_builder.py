# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import patch

from vyogo_frappe_qif.importer.reference import (
	default_reference_builder,
	resolve_reference_builder,
)


def custom_sample_builder(row: dict, bank_account: str) -> str:
	return f"CUSTOM-{bank_account}-{row['date']}-{row['occurrence_idx']}"


class TestReferenceBuilder(unittest.TestCase):
	def setUp(self):
		self.sample_row = {
			"date": "2026-05-12",
			"amount": -55.00,
			"payee": "OFFICE SUPPLIES",
			"memo": "Stationery",
			"reference_no": "INV-1001",
			"occurrence_idx": 1,
			"dedupe_hash": "a1b2c3d4e5f6789012345678",
		}
		self.bank_account = "CBA-Operating-01"

	def test_default_reference_builder_with_reference_no(self):
		"""Default builder uses reference_no if present."""
		ref = default_reference_builder(self.sample_row, self.bank_account)
		self.assertEqual(ref, "INV-1001")

	def test_default_reference_builder_fallback_to_hash(self):
		"""Default builder falls back to QIF-<hash[:12]> if reference_no is missing."""
		row_no_ref = dict(self.sample_row, reference_no=None)
		ref = default_reference_builder(row_no_ref, self.bank_account)
		self.assertEqual(ref, "QIF-a1b2c3d4e5f6")

	def test_resolve_explicit_callable(self):
		"""Explicit callable is used directly."""
		builder = resolve_reference_builder(custom_sample_builder)
		ref = builder(self.sample_row, self.bank_account)
		self.assertEqual(ref, "CUSTOM-CBA-Operating-01-2026-05-12-1")

	def test_resolve_explicit_dotted_path(self):
		"""Explicit dotted path string is resolved to callable."""
		dotted = f"{custom_sample_builder.__module__}.custom_sample_builder"
		builder = resolve_reference_builder(dotted)
		ref = builder(self.sample_row, self.bank_account)
		self.assertEqual(ref, "CUSTOM-CBA-Operating-01-2026-05-12-1")

	def test_resolve_via_hooks(self):
		"""Hook resolution picks up configured hook."""
		with patch(
			"vyogo_frappe_qif.importer.reference.get_hook_reference_builders",
			return_value=[custom_sample_builder],
		):
			builder = resolve_reference_builder(None)
			ref = builder(self.sample_row, self.bank_account)
			self.assertEqual(ref, "CUSTOM-CBA-Operating-01-2026-05-12-1")

	def test_resolve_default_fallback(self):
		"""When no explicit arg and no hook, falls back to default_reference_builder."""
		with patch(
			"vyogo_frappe_qif.importer.reference.get_hook_reference_builders",
			return_value=[],
		):
			builder = resolve_reference_builder(None)
			self.assertIs(builder, default_reference_builder)
