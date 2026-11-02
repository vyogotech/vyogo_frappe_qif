# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from typing import Any

try:
	import frappe
	from frappe.utils import now
except ImportError:
	frappe = None
	from datetime import datetime

	def now():
		return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


from vyogo_frappe_qif.importer.qif_parser import QifStreamParser
from vyogo_frappe_qif.importer.tenant_compat import get_current_tenant_id

DEFAULT_STAGING_BATCH_SIZE = 2500

STAGING_COLUMNS = [
	"name",
	"tenant_id",
	"import_session",
	"company",
	"date",
	"amount",
	"payee",
	"memo",
	"description",
	"reference_no",
	"occurrence_idx",
	"dedupe_hash",
	"status",
	"creation",
	"modified",
	"owner",
	"modified_by",
]


def compute_dedupe_hash(
	tenant_id: str | None,
	company: str | None,
	date: str,
	amount: float,
	payee: str | None,
	occurrence_idx: int = 1,
) -> str:
	"""Compute deterministic SHA256 deduplication hash for statement transactions."""
	raw_str = f"{tenant_id or ''}|{company or ''}|{date}|{amount:.2f}|{payee or ''}|{occurrence_idx or 1}"
	return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()[:24]


def _generate_record_id() -> str:
	if frappe and hasattr(frappe, "generate_hash"):
		return frappe.generate_hash(length=12)
	return uuid.uuid4().hex[:12]


def _build_staging_row(
	rec: dict[str, Any],
	import_session: str,
	company: str | None,
	tenant_id: str | None,
	ts: str,
	user: str,
) -> tuple:
	"""Transform a normalized statement record dict into a tabQIF Staging Record tuple."""
	rec_name = _generate_record_id()
	amount = float(rec["amount"])
	dedupe_hash = rec.get("dedupe_hash") or compute_dedupe_hash(
		tenant_id=tenant_id,
		company=company,
		date=str(rec["date"]),
		amount=amount,
		payee=rec.get("payee"),
		occurrence_idx=int(rec.get("occurrence_idx", 1)),
	)
	raw_desc = rec.get("description")
	desc_val = str(raw_desc) if raw_desc is not None else ""
	return (
		rec_name,
		tenant_id,
		import_session,
		company,
		rec["date"],
		amount,
		rec.get("payee", "")[:140] if rec.get("payee") else "",
		rec.get("memo", "") or "",
		desc_val,
		rec.get("reference_no", "")[:140] if rec.get("reference_no") else "",
		int(rec.get("occurrence_idx", 1)),
		dedupe_hash,
		"Pending",
		ts,
		ts,
		user,
		user,
	)


def _flush_staging_batch(rows: list) -> None:
	"""Flush pending staged rows to the database."""
	if rows and frappe and hasattr(frappe, "db"):
		frappe.db.bulk_insert("QIF Staging Record", STAGING_COLUMNS, rows)


def _handle_staging_error(import_session: str, exc: Exception) -> None:
	"""Log error on staging failure to standard Frappe Error Log."""
	if frappe and hasattr(frappe, "log_error"):
		tb = frappe.get_traceback() if hasattr(frappe, "get_traceback") else str(exc)
		frappe.log_error(
			title=f"Statement Staging Ingestion Failed: {import_session}",
			message=tb,
			reference_doctype="Bank Statement Import",
			reference_name=import_session,
		)


def stage_records(
	import_session: str,
	records: Iterable[dict[str, Any]],
	company: str | None = None,
	tenant_id: str | None = None,
	batch_size: int = DEFAULT_STAGING_BATCH_SIZE,
) -> dict[str, Any]:
	"""
	Format-agnostic streaming ingestion of statement records into tabQIF Staging Record.

	Consumes any iterable of record dicts (QIF, OFX, CSV) line-by-line and flushes
	in chunks of `batch_size` via MariaDB bulk_insert to maintain O(1) bounded memory.
	"""
	if not tenant_id:
		tenant_id = get_current_tenant_id()

	rows = []
	ts = now()
	user = getattr(getattr(frappe, "session", None), "user", None) or "Administrator"
	total_count = 0

	try:
		for rec in records:
			rows.append(_build_staging_row(rec, import_session, company, tenant_id, ts, user))
			total_count += 1
			if len(rows) >= batch_size:
				_flush_staging_batch(rows)
				rows = []

		_flush_staging_batch(rows)
		if frappe and hasattr(frappe, "db"):
			if hasattr(frappe.db, "exists") and frappe.db.exists("Bank Statement Import", import_session):
				frappe.db.set_value(
					"Bank Statement Import",
					import_session,
					{
						"qif_total_rows": total_count,
						"qif_processed_rows": 0,
						"qif_completed_rows": 0,
						"qif_skipped_rows": 0,
						"qif_failed_rows": 0,
					},
					update_modified=False,
				)
			frappe.db.commit()

		return {"import_session": import_session, "total_staged": total_count, "tenant_id": tenant_id}
	except Exception as exc:
		_handle_staging_error(import_session, exc)
		raise exc


def stage_qif_content(
	import_session: str,
	content: str | Iterable[str],
	company: str | None = None,
	tenant_id: str | None = None,
	date_format: str | None = None,
	batch_size: int = DEFAULT_STAGING_BATCH_SIZE,
) -> dict[str, Any]:
	"""
	Stream and stage raw QIF statements into tabQIF Staging Record.

	Accepts either an Iterable[str] of lines (for O(1) true streaming) or a raw string
	for backward compatibility.
	"""
	if isinstance(content, str):
		lines: Iterable[str] = iter(content.splitlines())
	else:
		lines = content

	parser = QifStreamParser(lines, date_format=date_format)
	return stage_records(
		import_session=import_session,
		records=parser.iter_records(),
		company=company,
		tenant_id=tenant_id,
		batch_size=batch_size,
	)
