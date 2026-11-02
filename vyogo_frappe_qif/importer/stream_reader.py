# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import io
import os
from collections.abc import Iterator
from typing import Any, BinaryIO, TextIO

try:
	import frappe
except ImportError:
	frappe = None  # Standalone test compatibility


def get_hook_handlers(hook_name: str) -> list:
	"""Retrieve resolved hook handler functions from Frappe, if available."""
	if not frappe or not hasattr(frappe, "get_hooks"):
		return []
	try:
		hook_paths = frappe.get_hooks(hook_name) or []
		handlers = []
		for path in hook_paths:
			if callable(path):
				handlers.append(path)
			elif isinstance(path, str):
				handlers.append(frappe.get_attr(path))
		return handlers
	except Exception:
		return []


def stream_lines_from_stream(stream: BinaryIO | TextIO, chunk_size: int = 65536) -> Iterator[str]:
	"""Incrementally yield stripped lines from a binary or text stream in O(1) memory."""
	if isinstance(stream, io.TextIOBase):
		for line in stream:
			yield line.rstrip("\r\n").rstrip("\r")
		return

	text_stream = io.TextIOWrapper(
		stream,
		encoding="utf-8-sig",
		errors="replace",
		newline=None,
	)
	for line in text_stream:
		yield line.rstrip("\r\n").rstrip("\r")


def stream_lines_from_path(file_path: str, chunk_size: int = 65536) -> Iterator[str]:
	"""Stream lines from a local file path using universal newlines and UTF-8-sig."""
	with open(file_path, encoding="utf-8-sig", errors="replace", newline=None) as f:
		for line in f:
			yield line.rstrip("\r\n").rstrip("\r")


def _check_remote_file(file_doc: Any) -> None:
	"""Raise ValidationError if File is remote and no pluggable reader is configured."""
	is_remote = getattr(file_doc, "is_remote_file", 0)
	file_url = getattr(file_doc, "file_url", "") or ""
	if is_remote or file_url.startswith(("http://", "https://", "s3://", "gs://")):
		doc_name = getattr(file_doc, "name", "Unknown")
		msg = (
			f"Remote file storage detected for File '{doc_name}' ({file_url}), "
			"but no 'qif_file_line_reader' hook is configured to stream lines from remote storage."
		)
		if frappe and hasattr(frappe, "ValidationError"):
			raise frappe.ValidationError(msg)
		raise RuntimeError(msg)


def _resolve_local_path(file_doc: Any) -> str:
	"""Locate local file path from File document or throw FileNotFoundError."""
	local_path = None
	if hasattr(file_doc, "get_full_path"):
		try:
			local_path = file_doc.get_full_path()
		except Exception:
			pass

	file_url = getattr(file_doc, "file_url", "") or ""
	if not local_path and frappe and hasattr(frappe, "get_site_path") and file_url:
		trimmed = file_url.lstrip("/")
		local_path = frappe.get_site_path(*trimmed.split("/"))

	if local_path and os.path.exists(local_path):
		return local_path
	raise FileNotFoundError(f"File not found on local disk: {local_path or file_url}")


def get_file_line_iterator(file_doc: Any, file_path: str | None = None) -> Iterator[str]:
	"""Resolve and stream lines from a Frappe File document or file path in O(1) memory."""
	if file_path and os.path.exists(file_path):
		yield from stream_lines_from_path(file_path)
		return

	custom_readers = get_hook_handlers("qif_file_line_reader")
	if custom_readers:
		yield from custom_readers[-1](file_doc)
		return

	_check_remote_file(file_doc)
	local_path = _resolve_local_path(file_doc)
	yield from stream_lines_from_path(local_path)
