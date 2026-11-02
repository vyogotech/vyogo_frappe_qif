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

		def sql(self, *args, **kwargs):
			return []

		def exists(self, *args, **kwargs):
			return None

		def get_value(self, *args, **kwargs):
			return None

		def set_value(self, *args, **kwargs):
			pass

		def count(self, *args, **kwargs):
			return 0

		def has_column(self, *args, **kwargs):
			return True

	class FakeSession:
		user = "Administrator"

	class ValidationError(Exception):
		pass

	class FakeFrappe:
		def __init__(self):
			self.conf: dict[str, Any] = {}
			self.session = FakeSession()
			self.db = FakeDB()
			self.local = type("FakeLocal", (), {})()
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

		def log_error(
			self, title: str = "", message: str = "", reference_doctype: str = "", reference_name: str = ""
		):
			pass

		def get_traceback(self) -> str:
			return ""

	sys.modules["frappe"] = FakeFrappe()

	class FakeFrappeUtils:
		@staticmethod
		def now() -> str:
			from datetime import datetime

			return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

		@staticmethod
		def flt(val: Any, precision: int | None = None) -> float:
			try:
				v = float(val)
				return round(v, precision) if precision is not None else v
			except (ValueError, TypeError):
				return 0.0

	sys.modules["frappe.utils"] = FakeFrappeUtils()
