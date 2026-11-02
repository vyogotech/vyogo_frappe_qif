# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

from typing import Any

try:
	import frappe
except ImportError:
	frappe = None

DEFAULT_QIF_QUEUE = "long"


def get_site_conf() -> Any:
	"""Retrieve site configuration object from Frappe."""
	if frappe and hasattr(frappe, "conf") and frappe.conf is not None:
		return frappe.conf
	return None


def get_hook_queues() -> list[str]:
	"""Retrieve configured qif_import_queue hooks from Frappe."""
	if not frappe or not hasattr(frappe, "get_hooks"):
		return []
	try:
		raw = frappe.get_hooks("qif_import_queue") or []
		queues = []
		for q in raw:
			if isinstance(q, str) and q.strip():
				queues.append(q.strip())
		return queues
	except Exception:
		return []


def get_import_queue(explicit_queue: str | None = None) -> str:
	"""
	Resolve the Redis queue name for asynchronous QIF batch worker execution.

	Resolution hierarchy:
	1. Explicit queue parameter (e.g. 'short', 'default', 'long')
	2. frappe.conf.get('qif_import_queue')
	3. frappe.get_hooks('qif_import_queue')[-1]
	4. Default: 'long' (because large statements are long-running jobs)
	"""
	if explicit_queue and isinstance(explicit_queue, str) and explicit_queue.strip():
		return explicit_queue.strip()

	conf = get_site_conf()
	if conf:
		conf_queue = conf.get("qif_import_queue")
		if conf_queue and isinstance(conf_queue, str) and conf_queue.strip():
			return conf_queue.strip()

	hook_queues = get_hook_queues()
	if hook_queues:
		return hook_queues[-1]

	return DEFAULT_QIF_QUEUE
