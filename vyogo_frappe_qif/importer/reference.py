# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:
	import frappe
except ImportError:
	frappe = None


def default_reference_builder(row: dict[str, Any], bank_account: str) -> str:
	"""Default reference number generator for Bank Transactions created from QIF."""
	explicit_ref = row.get("reference_no")
	if explicit_ref:
		return str(explicit_ref)[:140]

	dedupe_hash = row.get("dedupe_hash") or ""
	hash_chunk = dedupe_hash[:12] if dedupe_hash else "UNKNOWN"
	return f"QIF-{hash_chunk}"


def get_hook_reference_builders() -> list:
	"""Retrieve configured qif_reference_builder hooks from Frappe."""
	if not frappe or not hasattr(frappe, "get_hooks"):
		return []
	try:
		raw_hooks = frappe.get_hooks("qif_reference_builder") or []
		builders = []
		for hook in raw_hooks:
			if callable(hook):
				builders.append(hook)
			elif isinstance(hook, str):
				builders.append(frappe.get_attr(hook))
		return builders
	except Exception:
		return []


def resolve_reference_builder(
	explicit: str | Callable[[dict[str, Any], str], str] | None = None,
) -> Callable[[dict[str, Any], str], str]:
	"""
	Resolve the active reference number builder for Bank Transactions.

	Resolution hierarchy:
	1. Explicit callable or dotted path argument.
	2. frappe.get_hooks('qif_reference_builder')[-1]
	3. default_reference_builder
	"""
	if explicit:
		if callable(explicit):
			return explicit
		if isinstance(explicit, str) and frappe and hasattr(frappe, "get_attr"):
			return frappe.get_attr(explicit)
		elif isinstance(explicit, str):
			import importlib

			mod_name, func_name = explicit.rsplit(".", 1)
			mod = importlib.import_module(mod_name)
			return getattr(mod, func_name)

	hook_builders = get_hook_reference_builders()
	if hook_builders:
		return hook_builders[-1]

	return default_reference_builder
