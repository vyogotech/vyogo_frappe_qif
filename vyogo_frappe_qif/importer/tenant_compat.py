# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from typing import Any


def is_multitenant_environment() -> bool:
    """Check if the current site has multi-tenant architecture enabled.
    
    Compatible with:
    - Vyogo SaaS Platform (saas_platform app installed)
    - Custom multi-tenant schemas where `tenant_id` column exists on Bank Transaction
    - Vanilla single-tenant open-source ERPNext (returns False)
    """
    try:
        if "saas_platform" in frappe.get_installed_apps():
            return True
        return bool(frappe.db.has_column("Bank Transaction", "tenant_id"))
    except Exception:
        return False


def get_current_tenant_id() -> str | None:
    """Resolve active tenant_id across different runtime contexts.
    
    Resolves cleanly in:
    - Web session request
    - Background RQ job / microservice context
    - Returns None safely in open-source single-tenant ERPNext
    """
    if not is_multitenant_environment():
        return None

    # 1. Local thread / microservice context
    local_tenant = getattr(frappe.local, "current_tenant_id", None)
    if local_tenant and local_tenant != "SYSTEM":
        return str(local_tenant)

    # 2. Frappe session context
    session_tenant = getattr(frappe.local, "session", {}).get("data", {}).get("tenant_id")
    if not session_tenant and hasattr(frappe, "session") and isinstance(frappe.session, dict):
        session_tenant = frappe.session.get("tenant_id")
    if session_tenant and session_tenant != "SYSTEM":
        return str(session_tenant)

    # 3. User document lookup (if not Administrator)
    if hasattr(frappe, "session") and getattr(frappe.session, "user", None):
        if frappe.session.user != "Administrator":
            try:
                if frappe.db.has_column("User", "tenant_id"):
                    user_tenant = frappe.db.get_value("User", frappe.session.user, "tenant_id")
                    if user_tenant and user_tenant != "SYSTEM":
                        return str(user_tenant)
            except Exception:
                pass

    return None


def setup_worker_tenant_context(user_email: str | None = None, tenant_id: str | None = None) -> None:
    """Restores user and tenant session state in background worker queues."""
    if user_email and user_email != "Administrator":
        try:
            frappe.set_user(user_email)
        except Exception:
            pass

    if tenant_id:
        frappe.local.current_tenant_id = tenant_id
        if hasattr(frappe, "session") and isinstance(frappe.session, dict):
            frappe.session["tenant_id"] = tenant_id
            frappe.session["tenant_id_user"] = user_email or getattr(frappe.session, "user", "Administrator")


def ensure_tenant_valid_dict_patch() -> None:
    """Monkey-patch BaseDocument.get_valid_dict so that raw ALTER TABLE `tenant_id` columns persist.
    
    In multi-tenant SaaS architecture (saas_platform), `tenant_id` is provisioned via raw
    DDL rather than DocField metadata on standard ERPNext DocTypes. Frappe's default
    `BaseDocument.get_valid_dict()` only serializes columns found in `meta.get_valid_columns()`,
    silently dropping `tenant_id` and leaving it at the database column DEFAULT ('SYSTEM').
    
    In vanilla single-tenant open source ERPNext, this is a no-op.
    """
    if not is_multitenant_environment():
        return

    # Check if saas_platform or frappe_microservice already patched it
    try:
        from saas_platform.utils.tenant import patch_valid_dict_for_tenant_id
        patch_valid_dict_for_tenant_id()
        return
    except ImportError:
        pass

    try:
        from frappe_microservice.tenant import patch_valid_dict_for_tenant_id
        patch_valid_dict_for_tenant_id()
        return
    except ImportError:
        pass

    from frappe.model.base_document import BaseDocument

    if getattr(BaseDocument, "_tenant_valid_dict_patched", False):
        return

    original_get_valid_dict = BaseDocument.get_valid_dict

    def _patched_get_valid_dict(self, *args, **kwargs):
        d = original_get_valid_dict(self, *args, **kwargs)
        tenant_id = getattr(self, "tenant_id", None)
        if tenant_id and "tenant_id" not in d:
            d["tenant_id"] = tenant_id
        return d

    BaseDocument.get_valid_dict = _patched_get_valid_dict
    BaseDocument._tenant_valid_dict_patched = True

