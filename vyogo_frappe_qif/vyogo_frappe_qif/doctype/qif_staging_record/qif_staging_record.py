# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
import frappe
from frappe.model.document import Document


class QIFStagingRecord(Document):
    def validate(self):
        self._ensure_tenant_id()
        self._compute_dedupe_hash()

    def _ensure_tenant_id(self):
        """Multi-tenant isolation: Stamp tenant_id from active session if unset."""
        if not self.tenant_id:
            session_tenant = getattr(frappe.local, "session", {}).get("data", {}).get("tenant_id")
            if not session_tenant and frappe.session.user != "Administrator":
                session_tenant = frappe.db.get_value("User", frappe.session.user, "tenant_id")
            if session_tenant:
                self.tenant_id = session_tenant

    def _compute_dedupe_hash(self):
        """Compute deterministic occurrence hash for idempotency and duplicate checking."""
        if not self.dedupe_hash and self.date and self.amount is not None:
            raw_str = f"{self.tenant_id or ''}|{self.company or ''}|{self.date}|{self.amount:.2f}|{self.payee or ''}|{self.occurrence_idx or 1}"
            self.dedupe_hash = hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:24]
