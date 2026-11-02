# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

import frappe

from vyogo_frappe_qif.importer.queue_compat import get_import_queue
from vyogo_frappe_qif.importer.staging import stage_qif_content
from vyogo_frappe_qif.importer.stream_reader import get_file_line_iterator
from vyogo_frappe_qif.importer.tenant_compat import get_current_tenant_id
from vyogo_frappe_qif.importer.worker import (
	clear_staging_bank_transaction_links,
	process_pending_rows,
	process_qif_batch,
	retry_failed_rows,
	undo_qif_import,
)


def _resolve_session(
	bank_statement_import: str | None,
	file_url: str | None,
	company: str | None,
	bank_account: str | None,
) -> tuple[str, str | None, str | None, str | None]:
	if bank_statement_import:
		bsi = frappe.get_doc("Bank Statement Import", bank_statement_import)
		return (
			bsi.name,
			file_url or getattr(bsi, "import_file", None),
			company or getattr(bsi, "company", None),
			bank_account or getattr(bsi, "bank_account", None),
		)

	if not bank_account or not company:
		frappe.throw(
			"Both 'company' and 'bank_account' are required when 'bank_statement_import' is not provided."
		)
	bsi = frappe.get_doc(
		{
			"doctype": "Bank Statement Import",
			"company": company,
			"bank_account": bank_account,
			"status": "Pending",
		}
	).insert(ignore_permissions=True)
	return bsi.name, file_url, company, bank_account


def _get_statement_lines(
	file_path: str | None, file_url: str | None, content: str | Iterable[str] | None = None
) -> Iterator[str]:
	if content is not None:
		return iter(content.splitlines()) if isinstance(content, str) else content
	if file_path:
		return get_file_line_iterator(None, file_path=file_path)
	if file_url:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		return get_file_line_iterator(file_doc)
	frappe.throw(
		"A valid file attached to Bank Statement Import, file_url, file_path, or content is required."
	)


def _enqueue_import_job(
	session_id: str,
	queue: str,
	batch_size: int,
	tenant_id: str | None,
	ref_builder: str | Callable | None,
) -> None:
	ref_arg = ref_builder if isinstance(ref_builder, str) else None
	frappe.enqueue(
		method="vyogo_frappe_qif.importer.worker.process_qif_batch",
		queue=queue,
		import_session=session_id,
		user=frappe.session.user,
		tenant_id=tenant_id,
		batch_size=int(batch_size),
		reference_builder=ref_arg,
		queue_name=queue,
	)


@frappe.whitelist()
def import_qif(
	bank_statement_import: str | None = None,
	file_url: str | None = None,
	file_path: str | None = None,
	content: str | Iterable[str] | None = None,
	company: str | None = None,
	bank_account: str | None = None,
	date_format: str | None = None,
	batch_size: int = 100,
	async_run: bool | str = True,
	queue: str | None = None,
	reference_builder: str | Callable | None = None,
) -> dict[str, Any]:
	"""Streaming entrypoint to import a QIF statement file."""
	if isinstance(async_run, str):
		async_run = async_run.lower() not in ("0", "false", "no")

	session_id, file_url, company, bank_account = _resolve_session(
		bank_statement_import, file_url, company, bank_account
	)
	lines = _get_statement_lines(file_path, file_url, content)
	tenant = get_current_tenant_id()
	stage_res = stage_qif_content(session_id, lines, company, tenant, date_format)
	resolved_queue = get_import_queue(queue)

	if async_run:
		_enqueue_import_job(session_id, resolved_queue, batch_size, tenant, reference_builder)
		return {
			"status": "queued",
			"import_session": session_id,
			"total_staged": stage_res["total_staged"],
			"queue": resolved_queue,
			"message": f"Successfully staged {stage_res['total_staged']} records. Worker queued on '{resolved_queue}'.",
		}

	res = process_pending_rows(
		import_session=session_id,
		batch_size=int(batch_size),
		user=frappe.session.user,
		tenant_id=tenant,
		reference_builder=reference_builder,
	)
	return {
		"status": "completed",
		"import_session": session_id,
		"total_staged": stage_res["total_staged"],
		"worker_result": res,
	}


@frappe.whitelist()
def upload_and_stage_qif(
	import_session: str,
	file_url: str | None = None,
	content: str | None = None,
	company: str | None = None,
	tenant_id: str | None = None,
	date_format: str | None = None,
) -> dict[str, Any]:
	"""API endpoint to stage raw QIF file contents into tabQIF Staging Record."""
	if content:
		lines: Any = content
	elif file_url:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		lines = get_file_line_iterator(file_doc)
	else:
		frappe.throw("Either 'file_url' or 'content' is required.")

	return stage_qif_content(
		import_session=import_session,
		content=lines,
		company=company,
		tenant_id=tenant_id or get_current_tenant_id(),
		date_format=date_format,
	)


@frappe.whitelist()
def start_qif_import(
	import_session: str,
	batch_size: int = 100,
	tenant_id: str | None = None,
	queue: str | None = None,
	reference_builder: str | Callable | None = None,
) -> dict[str, Any]:
	"""API endpoint to start asynchronous chained batch processing."""
	resolved_tenant = tenant_id or get_current_tenant_id()
	resolved_queue = get_import_queue(queue)
	ref_arg = reference_builder if isinstance(reference_builder, str) else None
	frappe.enqueue(
		method="vyogo_frappe_qif.importer.worker.process_qif_batch",
		queue=resolved_queue,
		import_session=import_session,
		user=frappe.session.user,
		tenant_id=resolved_tenant,
		batch_size=int(batch_size),
		reference_builder=ref_arg,
		queue_name=resolved_queue,
	)
	return {"status": "ok", "message": f"Processing queued on '{resolved_queue}' for {import_session}."}


@frappe.whitelist()
def undo_import(import_session: str, tenant_id: str | None = None) -> dict[str, Any]:
	"""API endpoint to safely undo a QIF import session."""
	return undo_qif_import(
		import_session=import_session,
		user=frappe.session.user,
		tenant_id=tenant_id or get_current_tenant_id(),
	)


@frappe.whitelist()
def retry_failed(
	import_session: str,
	tenant_id: str | None = None,
	queue: str | None = None,
	reference_builder: str | Callable | None = None,
) -> dict[str, Any]:
	"""API endpoint to retry failed rows in an import session."""
	return retry_failed_rows(
		import_session=import_session,
		user=frappe.session.user,
		tenant_id=tenant_id or get_current_tenant_id(),
		reference_builder=reference_builder,
		queue=queue,
	)


__all__ = [
	"clear_staging_bank_transaction_links",
	"import_qif",
	"process_pending_rows",
	"retry_failed",
	"start_qif_import",
	"undo_import",
	"upload_and_stage_qif",
]
