# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib

import frappe
from frappe.model.document import Document

from vyogo_frappe_qif.importer.tenant_compat import get_current_tenant_id


class QIFStagingRecord(Document):
	def before_insert(self):
		frappe.throw(
			frappe._("QIF Staging Records are system-managed and cannot be created manually."),
			frappe.PermissionError,
		)

	def validate(self):
		self._ensure_tenant_id()
		self._compute_dedupe_hash()

	def _ensure_tenant_id(self):
		"""Stamp tenant_id from active session if unset and in multi-tenant environment."""
		if not self.tenant_id:
			resolved_tenant = get_current_tenant_id()
			if resolved_tenant:
				self.tenant_id = resolved_tenant

	def _compute_dedupe_hash(self):
		"""Compute deterministic occurrence hash for idempotency and duplicate checking."""
		if not self.dedupe_hash and self.date and self.amount is not None:
			raw_str = f"{self.tenant_id or ''}|{self.company or ''}|{self.date}|{self.amount:.2f}|{self.payee or ''}|{self.occurrence_idx or 1}"
			self.dedupe_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:24]
