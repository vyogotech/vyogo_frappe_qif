# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _


def patch_all() -> None:
	"""Idempotently install all compatibility patches for QIF statement handling."""
	patch_importer_for_qif()
	patch_bank_statement_import()


def patch_importer_for_qif() -> None:
	"""Patch Frappe's Importer.read_content to allow .qif file parsing without throwing Template Error."""
	try:
		from frappe.core.doctype.data_import.importer import Importer
	except ImportError:
		return

	if getattr(Importer, "_qif_read_patched", False):
		return

	original_read_content = Importer.read_content

	def _patched_read_content(self, content, extension):
		ext = (extension or "").lower()
		if ext == "qif":
			from vyogo_frappe_qif.importer.qif_parser import QifStreamParser

			lines = (content or "").splitlines()
			parser = QifStreamParser(lines)
			headers = ["Date", "Amount", "Payee", "Memo", "Reference No"]
			data = [headers]
			for r in parser.iter_records():
				data.append(
					[
						r.get("date", ""),
						str(r.get("amount", "")),
						r.get("payee", "") or "",
						r.get("memo", "") or "",
						r.get("reference_no", "") or "",
					]
				)
			return data
		return original_read_content(self, content, extension)

	Importer.read_content = _patched_read_content
	Importer._qif_read_patched = True


def patch_bank_statement_import() -> None:
	"""Patch ERPNext's BankStatementImport to allow .qif statements without Template Error."""
	try:
		from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
			BankStatementImport,
		)
	except ImportError:
		return

	if getattr(BankStatementImport, "_qif_patched", False):
		return

	original_validate = BankStatementImport.validate
	original_start_import = BankStatementImport.start_import
	original_get_preview = getattr(BankStatementImport, "get_preview_from_template", None)

	def _patched_validate(self):
		file_url = (getattr(self, "import_file", None) or "").lower()
		if file_url.endswith(".qif"):
			if not getattr(self, "template_options", None):
				self.template_options = "{}"
			self.template_warnings = ""
			return
		return original_validate(self)

	def _patched_start_import(self):
		file_url = (getattr(self, "import_file", None) or "").lower()
		if file_url.endswith(".qif"):
			from vyogo_frappe_qif.api import import_qif

			return import_qif(bank_statement_import=self.name, async_run=True)
		return original_start_import(self)

	def _patched_get_preview(self, import_file=None, google_sheets_url=None):
		file_path = import_file or getattr(self, "import_file", None) or ""
		if file_path.lower().endswith(".qif"):
			from vyogo_frappe_qif.importer.qif_parser import QifStreamParser

			content = ""
			if frappe.db.exists("File", {"file_url": file_path}):
				file_doc = frappe.get_doc("File", {"file_url": file_path})
				content = file_doc.get_content()
			lines = (content or "").splitlines()
			parser = QifStreamParser(lines)
			records = list(parser.iter_records())

			columns = [
				frappe._dict({"header_title": _("Sr. No"), "skip_import": True}),
				frappe._dict(
					{
						"header_title": _("Date"),
						"df": {"fieldname": "date", "label": _("Date"), "fieldtype": "Date"},
					}
				),
				frappe._dict(
					{
						"header_title": _("Amount"),
						"df": {"fieldname": "deposit", "label": _("Amount"), "fieldtype": "Currency"},
					}
				),
				frappe._dict(
					{
						"header_title": _("Payee"),
						"df": {"fieldname": "description", "label": _("Payee"), "fieldtype": "Data"},
					}
				),
				frappe._dict(
					{
						"header_title": _("Memo"),
						"df": {"fieldname": "reference_number", "label": _("Memo"), "fieldtype": "Data"},
					}
				),
			]

			preview_rows = []
			for idx, r in enumerate(records[:50], 1):
				preview_rows.append([idx, r.get("date"), r.get("amount"), r.get("payee"), r.get("memo")])

			return frappe._dict(
				{
					"data": preview_rows,
					"columns": columns,
					"warnings": [],
					"total_number_of_rows": len(records),
				}
			)

		if original_get_preview:
			return original_get_preview(self, import_file=import_file, google_sheets_url=google_sheets_url)
		return None

	BankStatementImport.validate = _patched_validate
	BankStatementImport.start_import = _patched_start_import
	if original_get_preview:
		BankStatementImport.get_preview_from_template = _patched_get_preview
	BankStatementImport._qif_patched = True


def doc_event_validate(doc, method=None):
	"""Doc event hook on Bank Statement Import before saving."""
	file_url = (getattr(doc, "import_file", None) or "").lower()
	if file_url.endswith(".qif"):
		if not getattr(doc, "template_options", None):
			doc.template_options = "{}"
		doc.template_warnings = ""
