# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import frappe
from frappe.utils import flt, now

from vyogo_frappe_qif.importer.lifecycle import (
	clear_staging_bank_transaction_links,
	on_bank_transaction_trash,
	retry_failed_rows,
	undo_qif_import,
)
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
	raw_desc = getattr(row, "description", None)
	if raw_desc is None and isinstance(row, dict):
		raw_desc = row.get("description")
	desc = raw_desc if raw_desc else (f"{payee} {memo}".strip() or "QIF Transaction")
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


def _is_cancel_requested(import_session: str) -> bool:
	if not frappe or not hasattr(frappe, "db"):
		return False
	if hasattr(frappe.db, "has_column") and not frappe.db.has_column(
		"Bank Statement Import", "qif_cancel_requested"
	):
		return False
	return bool(frappe.db.get_value("Bank Statement Import", import_session, "qif_cancel_requested"))


def _mark_pending_as_skipped(import_session: str) -> int:
	if not frappe or not hasattr(frappe, "db"):
		return 0
	count = frappe.db.sql(
		"""
		UPDATE `tabQIF Staging Record`
		SET status = 'Skipped', processed_at = %s
		WHERE import_session = %s AND status = 'Pending'
		""",
		(now(), import_session),
	)
	frappe.db.commit()
	return count or 0


def _update_session_progress(import_session: str, user: str | None = None) -> dict[str, int]:
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
	completed = summary.get("Completed", 0)
	skipped = summary.get("Skipped", 0)
	failed = summary.get("Failed", 0)
	pending = summary.get("Pending", 0)
	processed = completed + skipped + failed
	total = processed + pending

	if hasattr(frappe.db, "exists") and frappe.db.exists("Bank Statement Import", import_session):
		update_fields = {}
		for col, val in [
			("qif_total_rows", total),
			("qif_processed_rows", processed),
			("qif_completed_rows", completed),
			("qif_skipped_rows", skipped),
			("qif_failed_rows", failed),
		]:
			if not hasattr(frappe.db, "has_column") or frappe.db.has_column("Bank Statement Import", col):
				update_fields[col] = val

		if update_fields:
			frappe.db.set_value("Bank Statement Import", import_session, update_fields, update_modified=False)

	if hasattr(frappe, "publish_realtime"):
		frappe.publish_realtime(
			"qif_import_progress",
			{
				"import_session": import_session,
				"processed": processed,
				"total": total,
				"completed": completed,
				"failed": failed,
				"skipped": skipped,
			},
			user=user,
		)
	return {
		"total": total,
		"processed": processed,
		"completed": completed,
		"skipped": skipped,
		"failed": failed,
		"pending": pending,
	}


def _finalize_import_session(import_session: str, user: str | None = None) -> dict[str, Any]:
	progress = _update_session_progress(import_session, user)
	failed_count = progress.get("failed", 0)
	status = "Success" if failed_count == 0 else "Partial Success"

	if hasattr(frappe.db, "exists") and frappe.db.exists("Bank Statement Import", import_session):
		frappe.db.set_value("Bank Statement Import", import_session, "status", status)
	frappe.db.commit()

	if hasattr(frappe, "publish_realtime"):
		frappe.publish_realtime(
			"msgprint",
			f"QIF Import for {import_session} completed with status: {status}.",
			user=user,
		)
	return {
		"import_session": import_session,
		"status": status,
		**progress,
	}


def _process_batch_core(
	import_session: str,
	batch_size: int,
	tenant_id: str | None,
	user: str | None,
	ref_builder: Callable[[dict[str, Any], str], str],
) -> int:
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
			"description",
			"reference_no",
			"dedupe_hash",
			"occurrence_idx",
		],
		order_by="creation asc",
		limit=batch_size,
	)
	if not pending_records:
		return 0

	import_doc = frappe.get_cached_doc("Bank Statement Import", import_session)
	for row in pending_records:
		_process_staging_row(row, import_doc, import_session, tenant_id, user, ref_builder)

	return len(pending_records)


def process_pending_rows(
	import_session: str,
	batch_size: int = 100,
	user: str | None = None,
	tenant_id: str | None = None,
	reference_builder: str | Callable | None = None,
	should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
	"""In-process synchronous worker processing pending QIF Staging Records to completion."""
	ensure_tenant_valid_dict_patch()
	setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)
	ref_builder = resolve_reference_builder(reference_builder)

	while True:
		if _is_cancel_requested(import_session) or (should_stop and should_stop()):
			_mark_pending_as_skipped(import_session)
			return _finalize_import_session(import_session, user)

		processed = _process_batch_core(import_session, batch_size, tenant_id, user, ref_builder)
		if processed == 0:
			return _finalize_import_session(import_session, user)

		frappe.db.commit()
		_update_session_progress(import_session, user)


def process_qif_batch(
	import_session: str,
	batch_size: int = 100,
	company: str | None = None,
	bank_account: str | None = None,
	user: str | None = None,
	tenant_id: str | None = None,
	reference_builder: str | Callable | None = None,
	queue: str | None = None,
	queue_name: str | None = None,
	**kwargs: Any,
) -> dict[str, Any] | None:
	"""Asynchronous chunked worker processing QIF Staging Records into ERPNext Bank Transactions."""
	resolved_queue = queue_name or queue or kwargs.get("queue_name")
	ensure_tenant_valid_dict_patch()
	setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)
	ref_builder = resolve_reference_builder(reference_builder)

	try:
		if _is_cancel_requested(import_session):
			_mark_pending_as_skipped(import_session)
			_finalize_import_session(import_session, user)
			return None

		processed = _process_batch_core(import_session, batch_size, tenant_id, user, ref_builder)
		if processed == 0:
			_finalize_import_session(import_session, user)
			return None

		frappe.db.commit()
		_update_session_progress(import_session, user)
		_chain_next_batch(import_session, user, tenant_id, batch_size, reference_builder, resolved_queue)
		return {"status": "in_progress", "import_session": import_session}
	except Exception as exc:
		_handle_batch_crash(import_session, exc)
		raise


__all__ = [
	"clear_staging_bank_transaction_links",
	"on_bank_transaction_trash",
	"process_pending_rows",
	"process_qif_batch",
	"retry_failed_rows",
	"undo_qif_import",
]
