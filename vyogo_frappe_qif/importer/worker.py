# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import frappe
from frappe.utils import flt, now

from vyogo_frappe_qif.importer.queue_compat import get_import_queue
from vyogo_frappe_qif.importer.reference import resolve_reference_builder
from vyogo_frappe_qif.importer.tenant_compat import (
	ensure_tenant_valid_dict_patch,
	setup_worker_tenant_context,
)

logger = logging.getLogger(__name__)

ensure_tenant_valid_dict_patch()


def _build_row_dict(row: Any) -> dict[str, Any]:
	payee = getattr(row, "payee", "") or ""
	memo = getattr(row, "memo", "") or ""
	desc = f"{payee} {memo}".strip() or "QIF Transaction"
	return {
		"name": getattr(row, "name", ""),
		"date": str(row.date),
		"amount": float(row.amount),
		"payee": payee,
		"memo": memo,
		"reference_no": getattr(row, "reference_no", "") or "",
		"occurrence_idx": getattr(row, "occurrence_idx", 1),
		"dedupe_hash": getattr(row, "dedupe_hash", "") or "",
		"description": desc,
	}


def _insert_bank_transaction(
	row: Any,
	row_dict: dict[str, Any],
	import_doc: Any,
	row_tenant: str | None,
	deposit: float,
	withdrawal: float,
	ref_no: str,
) -> str:
	bt_data = {
		"doctype": "Bank Transaction",
		"company": import_doc.company,
		"bank_account": import_doc.bank_account,
		"date": row.date,
		"deposit": flt(deposit, 2),
		"withdrawal": flt(withdrawal, 2),
		"description": row_dict["description"],
		"reference_number": ref_no,
		"status": "Pending",
	}
	if row_tenant and frappe.db.has_column("Bank Transaction", "tenant_id"):
		bt_data["tenant_id"] = row_tenant

	bt_doc = frappe.get_doc(bt_data)
	if row_tenant:
		bt_doc.tenant_id = row_tenant
	bt_doc.insert(ignore_permissions=True)

	if row_tenant and frappe.db.has_column("Bank Transaction", "tenant_id"):
		actual_tenant = frappe.db.get_value("Bank Transaction", bt_doc.name, "tenant_id")
		if actual_tenant != row_tenant:
			frappe.db.set_value(
				"Bank Transaction", bt_doc.name, "tenant_id", row_tenant, update_modified=False
			)

	return bt_doc.name


def _handle_row_error(row: Any, import_session: str, savepoint_id: str, exc: Exception) -> None:
	frappe.db.rollback(save_point=savepoint_id)
	err_msg = str(exc)[:500]
	frappe.db.set_value(
		"QIF Staging Record",
		row.name,
		{"status": "Failed", "error_message": err_msg, "processed_at": now()},
		update_modified=False,
	)
	logger.warning("Failed importing QIF staging row %s: %s", row.name, err_msg)
	frappe.log_error(
		title=f"QIF Import Row Failed: {row.name}",
		message=frappe.get_traceback(),
		reference_doctype="Bank Statement Import",
		reference_name=import_session,
	)


def _find_existing_bank_transaction(
	import_doc: Any, row: Any, deposit: float, withdrawal: float, ref_no: str, row_tenant: str | None
) -> str | None:
	filters = {
		"bank_account": import_doc.bank_account,
		"date": row.date,
		"deposit": deposit,
		"withdrawal": withdrawal,
		"reference_number": ref_no,
	}
	if row_tenant and frappe.db.has_column("Bank Transaction", "tenant_id"):
		filters["tenant_id"] = row_tenant
	return frappe.db.exists("Bank Transaction", filters)


def _process_staging_row(
	row: Any,
	import_doc: Any,
	import_session: str,
	tenant_id: str | None,
	user: str | None,
	ref_builder: Callable[[dict[str, Any], str], str],
) -> None:
	savepoint_id = f"qif_{row.name.replace('-', '_')}"
	frappe.db.savepoint(savepoint_id)
	row_tenant = row.tenant_id or tenant_id
	if row_tenant:
		setup_worker_tenant_context(user_email=user, tenant_id=row_tenant)

	try:
		deposit = row.amount if row.amount > 0 else 0.0
		withdrawal = abs(row.amount) if row.amount < 0 else 0.0
		row_dict = _build_row_dict(row)
		ref_no = ref_builder(row_dict, import_doc.bank_account)

		existing = _find_existing_bank_transaction(import_doc, row, deposit, withdrawal, ref_no, row_tenant)
		if existing:
			frappe.db.set_value(
				"QIF Staging Record",
				row.name,
				{"status": "Skipped", "bank_transaction": existing, "processed_at": now()},
				update_modified=False,
			)
			return

		bt_name = _insert_bank_transaction(row, row_dict, import_doc, row_tenant, deposit, withdrawal, ref_no)
		frappe.db.set_value(
			"QIF Staging Record",
			row.name,
			{
				"status": "Completed",
				"bank_transaction": bt_name,
				"processed_at": now(),
				"error_message": None,
			},
			update_modified=False,
		)
	except Exception as exc:
		_handle_row_error(row, import_session, savepoint_id, exc)


def _chain_next_batch(
	import_session: str,
	user: str | None,
	tenant_id: str | None,
	batch_size: int,
	reference_builder: str | Callable | None,
	queue: str | None,
) -> None:
	resolved_queue = get_import_queue(queue)
	ref_arg = (
		reference_builder
		if isinstance(reference_builder, str)
		else (
			f"{reference_builder.__module__}.{reference_builder.__name__}"
			if callable(reference_builder)
			and hasattr(reference_builder, "__name__")
			and reference_builder.__name__ != "<lambda>"
			else None
		)
	)
	frappe.enqueue(
		method="vyogo_frappe_qif.importer.worker.process_qif_batch",
		queue=resolved_queue,
		import_session=import_session,
		user=user,
		tenant_id=tenant_id,
		batch_size=batch_size,
		reference_builder=ref_arg,
		queue_name=resolved_queue,
	)


def _handle_batch_crash(import_session: str, exc: Exception) -> None:
	frappe.log_error(
		title=f"QIF Batch Pipeline Crash: {import_session}",
		message=frappe.get_traceback(),
		reference_doctype="Bank Statement Import",
		reference_name=import_session,
	)
	frappe.db.set_value("Bank Statement Import", import_session, "status", "Error")
	frappe.db.commit()


def process_qif_batch(
	import_session: str,
	batch_size: int = 100,
	company: str | None = None,
	bank_account: str | None = None,
	user: str | None = None,
	tenant_id: str | None = None,
	reference_builder: str | Callable | None = None,
	queue: str | None = None,
) -> dict[str, Any] | None:
	"""Asynchronous chunked worker processing QIF Staging Records into ERPNext Bank Transactions."""
	ensure_tenant_valid_dict_patch()
	setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)
	ref_builder = resolve_reference_builder(reference_builder)

	try:
		pending_records = frappe.get_all(
			"QIF Staging Record",
			filters={"import_session": import_session, "status": "Pending"},
			fields=[
				"name",
				"tenant_id",
				"company",
				"date",
				"amount",
				"payee",
				"memo",
				"reference_no",
				"dedupe_hash",
				"occurrence_idx",
			],
			order_by="creation asc",
			limit=batch_size,
		)
		if not pending_records:
			_finalize_import_session(import_session, user)
			return None

		import_doc = frappe.get_cached_doc("Bank Statement Import", import_session)
		for row in pending_records:
			_process_staging_row(row, import_doc, import_session, tenant_id, user, ref_builder)

		frappe.db.commit()
		_publish_progress(import_session, user)
		_chain_next_batch(import_session, user, tenant_id, batch_size, reference_builder, queue)
		return {"status": "in_progress", "import_session": import_session}
	except Exception as exc:
		_handle_batch_crash(import_session, exc)
		raise


def _publish_progress(import_session: str, user: str | None = None) -> None:
	counts = frappe.db.sql(
		"""
        SELECT status, COUNT(*) as cnt
        FROM `tabQIF Staging Record`
        WHERE import_session = %s
        GROUP BY status
        """,
		(import_session,),
		as_dict=True,
	)
	summary = {row.status: row.cnt for row in counts}
	total = sum(summary.values())
	done = summary.get("Completed", 0) + summary.get("Skipped", 0) + summary.get("Failed", 0)

	frappe.publish_realtime(
		"qif_import_progress",
		{
			"import_session": import_session,
			"processed": done,
			"total": total,
			"completed": summary.get("Completed", 0),
			"failed": summary.get("Failed", 0),
			"skipped": summary.get("Skipped", 0),
		},
		user=user,
	)


def _finalize_import_session(import_session: str, user: str | None = None) -> None:
	failed_count = frappe.db.count(
		"QIF Staging Record", {"import_session": import_session, "status": "Failed"}
	)
	status = "Success" if failed_count == 0 else "Partial Success"

	frappe.db.set_value("Bank Statement Import", import_session, "status", status)
	frappe.db.commit()

	frappe.publish_realtime(
		"msgprint",
		f"QIF Import for {import_session} completed with status: {status}.",
		user=user,
	)


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
