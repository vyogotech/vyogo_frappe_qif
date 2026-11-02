# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import logging
from typing import Any, Dict, List
import frappe
from frappe.utils import flt, now
from vyogo_frappe_qif.importer.tenant_compat import (
    ensure_tenant_valid_dict_patch,
    setup_worker_tenant_context,
)

logger = logging.getLogger(__name__)

# Ensure patch is applied whenever worker module is imported
ensure_tenant_valid_dict_patch()


def process_qif_batch(
    import_session: str,
    batch_size: int = 100,
    company: str | None = None,
    bank_account: str | None = None,
    user: str | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any] | None:
    """
    Asynchronous chunked worker (batch_size=100) processing QIF Staging Records
    into standard ERPNext Bank Transactions.
    
    Architecture features:
    - Open source clean ERPNext compatibility (runs smoothly without multi-tenant modules).
    - Multi-tenant awareness (cooperates with saas_platform / raw DDL tenant_id column).
    - Row-level isolation using transactional SAVEPOINTs.
    - Full ledger guard and idempotency verification.
    """
    ensure_tenant_valid_dict_patch()
    setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)

    try:
        # 1. Fetch next batch of pending records
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
            ],
            order_by="creation asc",
            limit=batch_size,
        )

        if not pending_records:
            # All records processed
            _finalize_import_session(import_session, user)
            return None

        import_doc = frappe.get_cached_doc("Bank Statement Import", import_session)

        # 2. Iterate records with row-level SAVEPOINT
        for row in pending_records:
            savepoint_id = f"qif_{row.name.replace('-', '_')}"
            frappe.db.savepoint(savepoint_id)

            row_tenant = row.tenant_id or tenant_id
            if row_tenant:
                setup_worker_tenant_context(user_email=user, tenant_id=row_tenant)

            try:
                deposit = row.amount if row.amount > 0 else 0.0
                withdrawal = abs(row.amount) if row.amount < 0 else 0.0
                ref_no = row.reference_no or f"QIF-{row.dedupe_hash[:12]}"

                # Multi-tenant idempotency check on bank account & transaction signature
                filters = {
                    "bank_account": import_doc.bank_account,
                    "date": row.date,
                    "deposit": deposit,
                    "withdrawal": withdrawal,
                    "reference_number": ref_no,
                }
                if row_tenant and frappe.db.has_column("Bank Transaction", "tenant_id"):
                    filters["tenant_id"] = row_tenant

                existing = frappe.db.exists("Bank Transaction", filters)
                if existing:
                    frappe.db.set_value(
                        "QIF Staging Record",
                        row.name,
                        {"status": "Skipped", "bank_transaction": existing, "processed_at": now()},
                        update_modified=False,
                    )
                    continue

                # Standard ERPNext DocType Creation
                bt_data = {
                    "doctype": "Bank Transaction",
                    "company": import_doc.company,
                    "bank_account": import_doc.bank_account,
                    "date": row.date,
                    "deposit": flt(deposit, 2),
                    "withdrawal": flt(withdrawal, 2),
                    "description": f"{row.payee or ''} {row.memo or ''}".strip() or "QIF Transaction",
                    "reference_number": ref_no,
                    "status": "Pending",
                }
                if row_tenant and frappe.db.has_column("Bank Transaction", "tenant_id"):
                    bt_data["tenant_id"] = row_tenant

                bt_doc = frappe.get_doc(bt_data)
                if row_tenant:
                    bt_doc.tenant_id = row_tenant

                bt_doc.insert(ignore_permissions=True)

                # Post-insert persistence guarantee:
                # In multi-tenant environments (e.g. saas_platform), tenant_id is added via raw
                # DDL ALTER TABLE and is not part of DocType metadata (meta.get_valid_columns()).
                # If hooks or vanilla get_valid_dict dropped it or set it to 'SYSTEM', directly
                # enforce the correct tenant_id on the database table.
                if row_tenant and frappe.db.has_column("Bank Transaction", "tenant_id"):
                    actual_tenant = frappe.db.get_value("Bank Transaction", bt_doc.name, "tenant_id")
                    if actual_tenant != row_tenant:
                        frappe.db.set_value(
                            "Bank Transaction",
                            bt_doc.name,
                            "tenant_id",
                            row_tenant,
                            update_modified=False,
                        )

                frappe.db.set_value(
                    "QIF Staging Record",
                    row.name,
                    {
                        "status": "Completed",
                        "bank_transaction": bt_doc.name,
                        "processed_at": now(),
                        "error_message": None,
                    },
                    update_modified=False,
                )

            except Exception as exc:
                # Rollback this individual row to savepoint
                frappe.db.rollback(save_point=savepoint_id)
                err_msg = str(exc)[:500]
                frappe.db.set_value(
                    "QIF Staging Record",
                    row.name,
                    {"status": "Failed", "error_message": err_msg, "processed_at": now()},
                    update_modified=False,
                )
                logger.warning("Failed importing QIF staging row %s: %s", row.name, err_msg)
                
                # Record in standard Frappe Error Log table
                frappe.log_error(
                    title=f"QIF Import Row Failed: {row.name}",
                    message=frappe.get_traceback(),
                    reference_doctype="Bank Statement Import",
                    reference_name=import_session,
                )

        # 3. Commit this batch
        frappe.db.commit()

        # 4. Publish real-time progress via Socket.io
        _publish_progress(import_session, user)

        # 5. Chain the next batch asynchronously
        frappe.enqueue(
            method="vyogo_frappe_qif.importer.worker.process_qif_batch",
            queue="default",
            import_session=import_session,
            user=user,
            tenant_id=tenant_id,
            batch_size=batch_size,
        )
        return {"status": "in_progress", "import_session": import_session}

    except Exception as exc:
        frappe.log_error(
            title=f"QIF Batch Pipeline Crash: {import_session}",
            message=frappe.get_traceback(),
            reference_doctype="Bank Statement Import",
            reference_name=import_session,
        )
        frappe.db.set_value("Bank Statement Import", import_session, "status", "Error")
        frappe.db.commit()
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
    failed_count = frappe.db.count("QIF Staging Record", {"import_session": import_session, "status": "Failed"})
    status = "Success" if failed_count == 0 else "Partial Success"

    frappe.db.set_value("Bank Statement Import", import_session, "status", status)
    frappe.db.commit()

    frappe.publish_realtime(
        "msgprint",
        f"QIF Import for {import_session} completed with status: {status}.",
        user=user,
    )


def undo_qif_import(import_session: str, user: str | None = None, tenant_id: str | None = None) -> Dict[str, Any]:
    """
    Safely rolls back an entire QIF import session:
    1. Verifies that no created Bank Transaction is already Reconciled (Ledger Guard).
    2. Deletes unreconciled Bank Transactions.
    3. Resets staging records to Pending.
    """
    setup_worker_tenant_context(user_email=user, tenant_id=tenant_id)

    bt_names = frappe.get_all(
        "QIF Staging Record",
        filters={"import_session": import_session},
        pluck="bank_transaction",
    )
    bt_names = [b for b in bt_names if b]

    if not bt_names:
        return {"status": "ok", "message": "No bank transactions to delete."}

    # Ledger Guard Check
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

    # Clean delete of un-reconciled bank transactions
    for bt in bt_names:
        if frappe.db.exists("Bank Transaction", bt):
            frappe.delete_doc("Bank Transaction", bt, force=1, ignore_permissions=True)

    # Reset staging records
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


def retry_failed_rows(import_session: str, user: str | None = None, tenant_id: str | None = None) -> Dict[str, Any]:
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

    frappe.enqueue(
        method="vyogo_frappe_qif.importer.worker.process_qif_batch",
        queue="default",
        import_session=import_session,
        user=user,
        tenant_id=tenant_id,
    )
    return {"status": "ok", "message": "Queued retry for failed records."}
