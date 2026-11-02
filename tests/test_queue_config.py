# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from vyogo_frappe_qif.importer.queue_compat import get_import_queue


class TestQueueConfig(unittest.TestCase):
	def test_explicit_queue_takes_precedence(self):
		"""Explicit queue argument always wins."""
		q = get_import_queue(explicit_queue="urgent")
		self.assertEqual(q, "urgent")

	def test_site_config_precedence_over_hook_and_default(self):
		"""frappe.conf['qif_import_queue'] takes precedence if no explicit queue."""
		mock_conf = MagicMock()
		mock_conf.get.side_effect = lambda k, default=None: (
			"custom_site_queue" if k == "qif_import_queue" else default
		)

		with patch("vyogo_frappe_qif.importer.queue_compat.get_site_conf", return_value=mock_conf):
			with patch("vyogo_frappe_qif.importer.queue_compat.get_hook_queues", return_value=["hook_queue"]):
				q = get_import_queue(None)
				self.assertEqual(q, "custom_site_queue")

	def test_hook_precedence_over_default(self):
		"""Hook takes precedence if no explicit arg and no site config."""
		mock_conf = MagicMock()
		mock_conf.get.return_value = None

		with patch("vyogo_frappe_qif.importer.queue_compat.get_site_conf", return_value=mock_conf):
			with patch("vyogo_frappe_qif.importer.queue_compat.get_hook_queues", return_value=["hook_queue"]):
				q = get_import_queue(None)
				self.assertEqual(q, "hook_queue")

	def test_default_fallback_is_long(self):
		"""Default fallback queue is 'long' for large batch imports."""
		mock_conf = MagicMock()
		mock_conf.get.return_value = None

		with patch("vyogo_frappe_qif.importer.queue_compat.get_site_conf", return_value=mock_conf):
			with patch("vyogo_frappe_qif.importer.queue_compat.get_hook_queues", return_value=[]):
				q = get_import_queue(None)
				self.assertEqual(q, "long")
