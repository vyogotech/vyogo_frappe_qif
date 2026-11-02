# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

try:
	from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
		BankStatementImport as _BaseBankStatementImport,
	)
except ImportError:
	_BaseBankStatementImport = object


def _build_preview_columns() -> list[dict[str, Any]]:
	return [
		frappe._dict({"header_title": _("Sr. No"), "skip_import": True}),
		frappe._dict(
			{"header_title": _("Date"), "df": {"fieldname": "date", "label": _("Date"), "fieldtype": "Date"}}
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
		frappe._dict(
			{
				"header_title": _("Bank Account"),
				"df": {"fieldname": "bank_account", "label": _("Bank Account"), "fieldtype": "Link"},
			}
		),
	]


def build_qif_preview(file_path: str) -> frappe._dict:
	"""Generate a lightweight preview dictionary for QIF statement files."""
	from vyogo_frappe_qif.importer.qif_parser import QifStreamParser
	from vyogo_frappe_qif.importer.stream_reader import get_file_line_iterator

	columns = _build_preview_columns()
	rows = []
	total = 0
	try:
		file_name = frappe.db.get_value("File", {"file_url": file_path})
		if file_name:
			file_doc = frappe.get_doc("File", file_name)
			stream = get_file_line_iterator(file_doc)
			parser = QifStreamParser(stream)
			for idx, r in enumerate(parser.iter_records(), 1):
				total += 1
				if len(rows) < 50:
					rows.append(
						[
							idx,
							r.get("date"),
							str(r.get("amount")),
							r.get("payee") or "",
							r.get("memo") or "",
							"",
						]
					)
	except Exception:
		pass

	return frappe._dict(
		{
			"data": rows,
			"columns": columns,
			"warnings": [],
			"total_number_of_rows": total,
		}
	)


class CustomBankStatementImport(_BaseBankStatementImport):
	"""Overridden Bank Statement Import controller for seamless QIF ingestion."""

	def validate(self) -> None:
		file_url = (getattr(self, "import_file", None) or "").lower()
		if file_url.endswith(".qif"):
			if not getattr(self, "template_options", None):
				self.template_options = "{}"
			self.template_warnings = ""
			return
		if hasattr(super(), "validate"):
			super().validate()

	def get_preview_from_template(
		self, import_file: str | None = None, google_sheets_url: str | None = None
	) -> Any:
		file_path = import_file or getattr(self, "import_file", None) or ""
		if file_path.lower().endswith(".qif"):
			return build_qif_preview(file_path)
		if hasattr(super(), "get_preview_from_template"):
			return super().get_preview_from_template(import_file, google_sheets_url)
		return frappe._dict({"columns": _build_preview_columns(), "data": [], "warnings": []})

	def start_import(self) -> Any:
		file_url = (getattr(self, "import_file", None) or "").lower()
		if file_url.endswith(".qif"):
			from vyogo_frappe_qif.api import import_qif

			import_qif(bank_statement_import=self.name, async_run=True)
			return True
		if hasattr(super(), "start_import"):
			return super().start_import()
		return False


@frappe.whitelist()
def get_preview_from_template(
	data_import: str, import_file: str | None = None, google_sheets_url: str | None = None
) -> Any:
	"""Whitelisted override for get_preview_from_template supporting QIF files."""
	bsi = frappe.get_doc("Bank Statement Import", data_import)
	bsi.check_permission()
	file_path = import_file or getattr(bsi, "import_file", None) or ""
	if file_path.lower().endswith(".qif"):
		return build_qif_preview(file_path)
	from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
		get_preview_from_template as _orig_get_preview,
	)

	return _orig_get_preview(data_import, import_file, google_sheets_url)


@frappe.whitelist()
def form_start_import(data_import: str) -> Any:
	"""Whitelisted override for form_start_import supporting QIF files."""
	bsi = frappe.get_doc("Bank Statement Import", data_import)
	bsi.check_permission("write")
	file_path = (getattr(bsi, "import_file", None) or "").lower()
	if file_path.endswith(".qif"):
		from vyogo_frappe_qif.api import import_qif

		import_qif(bank_statement_import=bsi.name, async_run=True)
		return True
	from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
		form_start_import as _orig_form_start_import,
	)

	return _orig_form_start_import(data_import)


def doc_event_validate(doc: Any, method: str | None = None) -> None:
	"""Doc event hook on Bank Statement Import before saving."""
	file_url = (getattr(doc, "import_file", None) or "").lower()
	if file_url.endswith(".qif"):
		if not getattr(doc, "template_options", None):
			doc.template_options = "{}"
		doc.template_warnings = ""


def patch_all() -> None:
	"""Idempotently install compatibility patches for QIF statement handling."""
	patch_importer_for_qif()
	patch_bank_statement_import()


def patch_bank_statement_import() -> None:
	"""Idempotently install monkey-patches for Bank Statement Import if needed."""
	try:
		from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
			BankStatementImport,
		)
	except ImportError:
		return

	if getattr(BankStatementImport, "_qif_patched", False):
		return

	original_validate = BankStatementImport.validate

	def _patched_validate(self: Any) -> Any:
		file_url = (getattr(self, "import_file", None) or "").lower()
		if file_url.endswith(".qif"):
			if not getattr(self, "template_options", None):
				self.template_options = "{}"
			self.template_warnings = ""
			return
		return original_validate(self)

	BankStatementImport.validate = _patched_validate
	BankStatementImport._qif_patched = True


def patch_importer_for_qif() -> None:
	"""Patch Frappe's Importer.read_content to allow .qif file parsing without throwing Template Error."""
	try:
		from frappe.core.doctype.data_import.importer import Importer
	except ImportError:
		return

	if getattr(Importer, "_qif_read_patched", False):
		return

	original_read_content = Importer.read_content

	def _patched_read_content(self: Any, content: Any, extension: str) -> Any:
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
