# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe

BANK_STATEMENT_IMPORT_CUSTOM_FIELDS = {
	"Bank Statement Import": [
		{
			"fieldname": "qif_total_rows",
			"label": "QIF Total Rows",
			"fieldtype": "Int",
			"insert_after": "import_file",
			"read_only": 1,
			"no_copy": 1,
			"default": "0",
		},
		{
			"fieldname": "qif_processed_rows",
			"label": "QIF Processed Rows",
			"fieldtype": "Int",
			"insert_after": "qif_total_rows",
			"read_only": 1,
			"no_copy": 1,
			"default": "0",
		},
		{
			"fieldname": "qif_completed_rows",
			"label": "QIF Completed Rows",
			"fieldtype": "Int",
			"insert_after": "qif_processed_rows",
			"read_only": 1,
			"no_copy": 1,
			"default": "0",
		},
		{
			"fieldname": "qif_skipped_rows",
			"label": "QIF Skipped Rows",
			"fieldtype": "Int",
			"insert_after": "qif_completed_rows",
			"read_only": 1,
			"no_copy": 1,
			"default": "0",
		},
		{
			"fieldname": "qif_failed_rows",
			"label": "QIF Failed Rows",
			"fieldtype": "Int",
			"insert_after": "qif_skipped_rows",
			"read_only": 1,
			"no_copy": 1,
			"default": "0",
		},
		{
			"fieldname": "qif_cancel_requested",
			"label": "QIF Cancel Requested",
			"fieldtype": "Check",
			"insert_after": "qif_failed_rows",
			"read_only": 0,
			"no_copy": 1,
			"default": "0",
		},
	]
}


def ensure_custom_fields() -> None:
	"""Idempotently install custom progress and cancel fields on Bank Statement Import."""
	if not frappe or not hasattr(frappe, "db"):
		return
	try:
		from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

		create_custom_fields(BANK_STATEMENT_IMPORT_CUSTOM_FIELDS, ignore_validate=True)
	except Exception:
		pass
