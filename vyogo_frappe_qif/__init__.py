# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

__version__ = "0.0.1"

try:
	from vyogo_frappe_qif.importer.tenant_compat import ensure_tenant_valid_dict_patch

	ensure_tenant_valid_dict_patch()
except Exception:
	pass

try:
	from vyogo_frappe_qif.importer.erpnext_patch import patch_bank_statement_import

	patch_bank_statement_import()
except Exception:
	pass
