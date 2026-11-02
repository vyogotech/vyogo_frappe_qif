# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any, Dict
import frappe
from frappe.utils import now
from vyogo_frappe_qif.importer.qif_parser import QifStreamParser
from vyogo_frappe_qif.importer.tenant_compat import get_current_tenant_id


def stage_qif_content(
    import_session: str,
    content: str,
    company: str | None = None,
    tenant_id: str | None = None,
    date_format: str | None = None,
) -> Dict[str, Any]:
    """
    High-speed staging of raw QIF statement rows into `tabQIF Staging Record`
    using batch SQL bulk_insert. Enforces multi-tenant isolation when active.
    """
    if not tenant_id:
        tenant_id = get_current_tenant_id()

    lines = content.splitlines()
    parser = QifStreamParser(lines, date_format=date_format)

    columns = [
        "name",
        "tenant_id",
        "import_session",
        "company",
        "date",
        "amount",
        "payee",
        "memo",
        "reference_no",
        "occurrence_idx",
        "dedupe_hash",
        "status",
        "creation",
        "modified",
        "owner",
        "modified_by",
    ]

    rows = []
    ts = now()
    user = frappe.session.user or "Administrator"
    total_count = 0

    try:
        for rec in parser.iter_records():
            rec_name = frappe.generate_hash(length=12)
            rows.append(
                (
                    rec_name,
                    tenant_id,
                    import_session,
                    company,
                    rec["date"],
                    rec["amount"],
                    rec.get("payee", "")[:140],
                    rec.get("memo", ""),
                    rec.get("reference_no", "")[:140],
                    rec["occurrence_idx"],
                    rec["dedupe_hash"],
                    "Pending",
                    ts,
                    ts,
                    user,
                    user,
                )
            )
            total_count += 1

            # Flush in batches of 2500
            if len(rows) >= 2500:
                frappe.db.bulk_insert("QIF Staging Record", columns, rows)
                rows.clear()

        if rows:
            frappe.db.bulk_insert("QIF Staging Record", columns, rows)

        frappe.db.commit()

        return {
            "import_session": import_session,
            "total_staged": total_count,
            "tenant_id": tenant_id,
        }
    except Exception as exc:
        frappe.log_error(
            title=f"QIF Staging Ingestion Failed: {import_session}",
            message=frappe.get_traceback(),
            reference_doctype="Bank Statement Import",
            reference_name=import_session,
        )
        raise exc
