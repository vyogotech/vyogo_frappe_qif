# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from vyogo_frappe_qif.importer.staging import stage_qif_content
from vyogo_frappe_qif.importer.tenant_compat import get_current_tenant_id
from vyogo_frappe_qif.importer.worker import (
    process_qif_batch,
    retry_failed_rows,
    undo_qif_import,
)


@frappe.whitelist()
def upload_and_stage_qif(
    import_session: str,
    file_url: str | None = None,
    content: str | None = None,
    company: str | None = None,
    tenant_id: str | None = None,
    date_format: str | None = None,
):
    """API endpoint to stage raw QIF file contents into tabQIF Staging Record."""
    if not content:
        if not file_url:
            frappe.throw("Either 'file_url' or 'content' is required.")
        file_doc = frappe.get_doc("File", {"file_url": file_url})
        content = file_doc.get_content()

    resolved_tenant = tenant_id or get_current_tenant_id()

    result = stage_qif_content(
        import_session=import_session,
        content=content,
        company=company,
        tenant_id=resolved_tenant,
        date_format=date_format,
    )
    return result


@frappe.whitelist()
def start_qif_import(import_session: str, batch_size: int = 100, tenant_id: str | None = None):
    """API endpoint to start asynchronous chained batch processing."""
    resolved_tenant = tenant_id or get_current_tenant_id()
    frappe.enqueue(
        method="vyogo_frappe_qif.importer.worker.process_qif_batch",
        queue="default",
        import_session=import_session,
        user=frappe.session.user,
        tenant_id=resolved_tenant,
        batch_size=int(batch_size),
    )
    return {"status": "ok", "message": f"Processing queued for {import_session}."}


@frappe.whitelist()
def undo_import(import_session: str, tenant_id: str | None = None):
    """API endpoint to safely undo a QIF import session."""
    resolved_tenant = tenant_id or get_current_tenant_id()
    return undo_qif_import(
        import_session=import_session,
        user=frappe.session.user,
        tenant_id=resolved_tenant,
    )


@frappe.whitelist()
def retry_failed(import_session: str, tenant_id: str | None = None):
    """API endpoint to retry failed rows in an import session."""
    resolved_tenant = tenant_id or get_current_tenant_id()
    return retry_failed_rows(
        import_session=import_session,
        user=frappe.session.user,
        tenant_id=resolved_tenant,
    )
