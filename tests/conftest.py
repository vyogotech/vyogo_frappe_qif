# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

import importlib
import sys
import uuid
from typing import Any

if "frappe" not in sys.modules:
    class FakeDB:
        def bulk_insert(self, doctype: str, fields: list, values: list):
            pass

        def commit(self):
            pass

    class FakeSession:
        user = "Administrator"

    class ValidationError(Exception):
        pass

    class FakeFrappe:
        def __init__(self):
            self.conf: dict[str, Any] = {}
            self.session = FakeSession()
            self.db = FakeDB()
            self.ValidationError = ValidationError

        def _(self, msg: str) -> str:
            return msg

        def get_hooks(self, hook_name: str) -> list:
            return []

        def get_installed_apps(self) -> list:
            return []

        def generate_hash(self, length: int = 12) -> str:
            return uuid.uuid4().hex[:length]

        def get_attr(self, dotted_path: str) -> Any:
            mod_name, func_name = dotted_path.rsplit(".", 1)
            mod = importlib.import_module(mod_name)
            return getattr(mod, func_name)

        def log_error(self, title: str = "", message: str = "", reference_doctype: str = "", reference_name: str = ""):
            pass

        def get_traceback(self) -> str:
            return ""

    sys.modules["frappe"] = FakeFrappe()

    class FakeFrappeUtils:
        @staticmethod
        def now() -> str:
            from datetime import datetime
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sys.modules["frappe.utils"] = FakeFrappeUtils()
