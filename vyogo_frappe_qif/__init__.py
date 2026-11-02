# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

__version__ = "0.0.1"

try:
    from vyogo_frappe_qif.importer.tenant_compat import ensure_tenant_valid_dict_patch
    ensure_tenant_valid_dict_patch()
except Exception:
    pass
