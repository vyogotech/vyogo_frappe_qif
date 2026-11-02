# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import frappe

from vyogo_frappe_qif.importer.queue_compat import get_import_queue
from vyogo_frappe_qif.importer.tenant_compat import setup_worker_tenant_context


def clear_staging_bank_transaction_links(bank_transaction: str | Any) -> int:
	"""Clear bank_transaction link on QIF Staging Records pointing to a Bank Transaction."""
	name = bank_transaction if isinstance(bank_transaction, str) else getattr(bank_transaction, "name", None)
	if not name or not frappe or not hasattr(frappe, "db"):
		return 0
	count = frappe.db.sql(
		"""
		UPDATE `tabQIF Staging Record`
		SET bank_transaction = NULL
		WHERE bank_transaction = %s
		""",
		(name,),
	)
	return count or 0


def on_bank_transaction_trash(doc: Any, method: str | None = None) -> None:
	"""Doc event handler for Bank Transaction on_trash."""
	name = getattr(doc, "name", None)
	if name:
		clear_staging_bank_transaction_links(name)


def _check_ledger_guard(bt_names: list[str]) -> None:
	reconciled = frappe.db.sql(
		"""
        SELECT name, reference_number
        FROM `tabBank Transaction`
        WHERE name IN %(names)s AND status = 'Reconciled'
        """,
		{"names": tuple(bt_names)},
		as_dict=True,
	)
	if reconciled:
		frappe.throw(
			f"Cannot undo import: {len(reconciled)} transactions are already reconciled. "
			f"Please unreconcile them in the Bank Reconciliation Tool first.",
			title="Ledger Guard Protected",
		)


def undo_qif_import(
	import_session: str, user: str | None = None, tenant_id: str | None = None
) -> dict[str, Any]:
	"""Safely rolls back an entire QIF import session."""
	setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)
	bt_names = [
		b
		for b in frappe.get_all(
			"QIF Staging Record", filters={"import_session": import_session}, pluck="bank_transaction"
		)
		if b
	]
	if not bt_names:
		return {"status": "ok", "message": "No bank transactions to delete."}

	_check_ledger_guard(bt_names)
	for bt in bt_names:
		if frappe.db.exists("Bank Transaction", bt):
			frappe.delete_doc("Bank Transaction", bt, force=1, ignore_permissions=True)

	frappe.db.sql(
		"""
        UPDATE `tabQIF Staging Record`
        SET status = 'Pending', bank_transaction = NULL, processed_at = NULL, error_message = NULL
        WHERE import_session = %s
        """,
		(import_session,),
	)
	frappe.db.set_value("Bank Statement Import", import_session, "status", "Pending")
	frappe.db.commit()
	return {
		"status": "ok",
		"message": f"Successfully rolled back. Deleted {len(bt_names)} transactions.",
		"deleted_count": len(bt_names),
	}


def retry_failed_rows(
	import_session: str,
	user: str | None = None,
	tenant_id: str | None = None,
	reference_builder: str | Callable | None = None,
	queue: str | None = None,
) -> dict[str, Any]:
	"""Resets failed staging records to Pending and resumes batch processing."""
	setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)
	frappe.db.sql(
		"""
        UPDATE `tabQIF Staging Record`
        SET status = 'Pending', error_message = NULL
        WHERE import_session = %s AND status = 'Failed'
        """,
		(import_session,),
	)
	frappe.db.commit()

	resolved_queue = get_import_queue(queue)
	frappe.enqueue(
		method="vyogo_frappe_qif.importer.worker.process_qif_batch",
		queue=resolved_queue,
		import_session=import_session,
		user=user,
		tenant_id=tenant_id,
		reference_builder=reference_builder if isinstance(reference_builder, str) else None,
		queue_name=resolved_queue,
	)
	return {"status": "ok", "message": "Queued retry for failed records."}
