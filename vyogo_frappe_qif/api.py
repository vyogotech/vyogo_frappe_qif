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
def import_qif(
    bank_statement_import: str | None = None,
    file_url: str | None = None,
    file_path: str | None = None,
    company: str | None = None,
    bank_account: str | None = None,
    date_format: str | None = None,
    batch_size: int = 100,
    async_run: bool | str = True,
):
    """
    Simple all-in-one entrypoint to import a QIF statement file.
    
    Can be called directly from:
    1. Desk UI: Automatically called by the 'Start QIF Import' button on Bank Statement Import.
    2. Bench CLI:
       bench --site <site> execute vyogo_frappe_qif.api.import_qif --kwargs '{"bank_statement_import": "BSI-2026-00001"}'
    3. Python Console / Scripts:
       frappe.call("vyogo_frappe_qif.api.import_qif", bank_statement_import="BSI-2026-00001")
    4. REST API (POST /api/method/vyogo_frappe_qif.api.import_qif)
    """
    if isinstance(async_run, str):
        async_run = async_run.lower() not in ("0", "false", "no")

    if bank_statement_import:
        bsi = frappe.get_doc("Bank Statement Import", bank_statement_import)
        file_url = file_url or getattr(bsi, "import_file", None)
        company = company or getattr(bsi, "company", None)
        bank_account = bank_account or getattr(bsi, "bank_account", None)
        session_id = bsi.name
    else:
        if not bank_account or not company:
            frappe.throw("Both 'company' and 'bank_account' are required when 'bank_statement_import' is not provided.")
        bsi = frappe.get_doc({
            "doctype": "Bank Statement Import",
            "company": company,
            "bank_account": bank_account,
            "status": "Pending",
        }).insert(ignore_permissions=True)
        session_id = bsi.name

    # Load content
    content = None
    if file_path:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    elif file_url:
        file_doc = frappe.get_doc("File", {"file_url": file_url})
        content = file_doc.get_content()
    else:
        frappe.throw("A valid file attached to Bank Statement Import, file_url, or file_path is required.")

    resolved_tenant = get_current_tenant_id()

    # 1. Stage records into tabQIF Staging Record
    stage_res = stage_qif_content(
        import_session=session_id,
        content=content,
        company=company,
        tenant_id=resolved_tenant,
        date_format=date_format,
    )

    # 2. Process records (Async via RQ by default, or Synchronous)
    if async_run:
        frappe.enqueue(
            method="vyogo_frappe_qif.importer.worker.process_qif_batch",
            queue="default",
            import_session=session_id,
            user=frappe.session.user,
            tenant_id=resolved_tenant,
            batch_size=int(batch_size),
        )
        return {
            "status": "queued",
            "import_session": session_id,
            "total_staged": stage_res["total_staged"],
            "message": f"Successfully staged {stage_res['total_staged']} records. Background worker queued.",
        }
    else:
        worker_res = process_qif_batch(
            import_session=session_id,
            batch_size=int(batch_size),
            company=company,
            bank_account=bank_account,
            user=frappe.session.user,
            tenant_id=resolved_tenant,
        )
        return {
            "status": "completed",
            "import_session": session_id,
            "total_staged": stage_res["total_staged"],
            "worker_result": worker_res,
        }


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
