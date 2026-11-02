# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _


def patch_bank_statement_import() -> None:
    """
    Monkey-patches ERPNext's BankStatementImport to allow .qif statements.
    
    Why this is required:
    ERPNext's BankStatementImport inherits from DataImport and restricts file extensions
    to .csv, .xlsx, .xls (plus hardcoded .txt for MT940). When a user attaches a .qif file,
    BankStatementImport.validate() invokes Importer.read_content(), which throws:
    'Template Error: Import template should be of type .csv, .xlsx or .xls'.
    
    This patch safely intercepts QIF files, bypasses standard CSV/XLSX template validation,
    and redirects start_import() to vyogo_frappe_qif's high-speed streaming pipeline.
    """
    try:
        from erpnext.accounts.doctype.bank_statement_import.bank_statement_import import (
            BankStatementImport,
        )
    except ImportError:
        # ERPNext not installed or accounts module unavailable
        return

    if getattr(BankStatementImport, "_qif_patched", False):
        return

    original_validate = BankStatementImport.validate
    original_start_import = BankStatementImport.start_import

    def _patched_validate(self):
        # If it's a QIF file, bypass CSV/XLSX Importer template validation
        file_url = (getattr(self, "import_file", None) or "").lower()
        if file_url.endswith(".qif"):
            # Set default template options to avoid null errors in downstream ERPNext views
            if not self.template_options:
                self.template_options = "{}"
            self.template_warnings = ""
            return
        return original_validate(self)

    def _patched_start_import(self):
        file_url = (getattr(self, "import_file", None) or "").lower()
        if file_url.endswith(".qif"):
            from vyogo_frappe_qif.api import import_qif
            return import_qif(bank_statement_import=self.name, async_run=True)
        return original_start_import(self)

    BankStatementImport.validate = _patched_validate
    BankStatementImport.start_import = _patched_start_import
    BankStatementImport._qif_patched = True
