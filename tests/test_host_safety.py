# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

"""
The app is safe inside a host that already manages tenancy, behaves like
ERPNext's own Bank Statement Import, and has no side effects on import.
"""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import frappe
import pytest

from vyogo_frappe_qif.importer import tenant_compat, worker


@pytest.fixture
def host_patched_base_document(monkeypatch):
	"""A host (e.g. frappe_microservice) that already patched BaseDocument with its provider."""

	def host_provider():
		return "tenant-of-the-request"

	base_document = types.SimpleNamespace(
		BaseDocument=type(
			"BaseDocument",
			(),
			{"_tenant_valid_dict_patched": True, "_tenant_id_provider": staticmethod(host_provider)},
		)
	)
	lib_patch = MagicMock()
	monkeypatch.setitem(sys.modules, "frappe.model", types.SimpleNamespace(base_document=base_document))
	monkeypatch.setitem(sys.modules, "frappe.model.base_document", base_document)
	monkeypatch.setitem(
		sys.modules,
		"frappe_microservice.tenant",
		types.SimpleNamespace(patch_valid_dict_for_tenant_id=lib_patch),
	)
	monkeypatch.setitem(sys.modules, "frappe_microservice", types.SimpleNamespace())
	monkeypatch.setattr(tenant_compat, "is_multitenant_environment", lambda: True)
	return base_document.BaseDocument, host_provider, lib_patch


def test_an_existing_host_patch_and_its_tenant_provider_are_left_alone(host_patched_base_document):
	base_document, host_provider, lib_patch = host_patched_base_document

	tenant_compat.ensure_tenant_valid_dict_patch()

	lib_patch.assert_not_called()
	assert base_document._tenant_id_provider() == host_provider()


def test_the_batch_loop_does_not_reset_a_host_provider(host_patched_base_document):
	base_document, host_provider, lib_patch = host_patched_base_document

	with (
		patch.object(worker, "_is_cancel_requested", return_value=False),
		patch.object(worker, "_process_batch_core", return_value=0),
		patch.object(worker, "_finalize_import_session", return_value={"status": "Success"}),
	):
		worker.process_pending_rows("BSI-1", tenant_id="tnt-1")

	lib_patch.assert_not_called()
	assert base_document._tenant_id_provider() == host_provider()


def test_importing_the_package_patches_nothing(monkeypatch):
	"""Hooks install the Bank Statement Import patches on sites with the app; importing must not."""
	patch_bsi = MagicMock()
	monkeypatch.setitem(
		sys.modules,
		"vyogo_frappe_qif.importer.erpnext_patch",
		types.SimpleNamespace(patch_bank_statement_import=patch_bsi, patch_all=patch_bsi),
	)
	with patch("vyogo_frappe_qif.importer.tenant_compat.ensure_tenant_valid_dict_patch") as ensure:
		importlib.reload(importlib.import_module("vyogo_frappe_qif"))
		importlib.reload(worker)

	ensure.assert_not_called()
	patch_bsi.assert_not_called()


def test_the_duplicate_check_ignores_cancelled_bank_transactions():
	import_doc = types.SimpleNamespace(bank_account="ACC-1")
	row = types.SimpleNamespace(date="2026-07-01")
	with patch.object(frappe.db, "exists", return_value=None) as exists:
		worker._find_existing_bank_transaction(import_doc, row, 0.0, 10.0, "REF-1", None)

	(_doctype, filters), _kwargs = exists.call_args
	assert filters["docstatus"] == ["!=", 2]


def _bank_transaction():
	bt = MagicMock()
	bt.name = "BT-1"
	bt.docstatus = 0
	bt.flags = types.SimpleNamespace(ignore_permissions=False)
	return bt


def _insert(submit_after_import: int, bt: MagicMock) -> str:
	import_doc = types.SimpleNamespace(
		company="Co", bank_account="ACC-1", submit_after_import=submit_after_import
	)
	row = types.SimpleNamespace(date="2026-07-01")
	row_dict = {"description": "COFFEE"}
	with patch.object(frappe, "get_doc", create=True, return_value=bt):
		return worker._insert_bank_transaction(row, row_dict, import_doc, None, 0.0, 10.0, "REF-1")


def test_lines_are_submitted_when_the_import_says_so():
	"""ERPNext's own import submits each Bank Transaction when submit_after_import is set."""
	bt = _bank_transaction()
	assert _insert(1, bt) == "BT-1"
	bt.insert.assert_called_once()
	bt.submit.assert_called_once()
	assert bt.flags.ignore_permissions is True


def test_lines_stay_drafts_otherwise():
	bt = _bank_transaction()
	_insert(0, bt)
	bt.submit.assert_not_called()


def test_a_line_that_fails_to_submit_fails_only_that_row():
	bt = _bank_transaction()
	bt.submit.side_effect = RuntimeError("Fiscal year missing")
	row = types.SimpleNamespace(
		name="Q-1",
		tenant_id=None,
		amount=-10.0,
		date="2026-07-01",
		payee="X",
		memo="",
		description="X",
		reference_no="REF-1",
		occurrence_idx=1,
		dedupe_hash="",
	)
	import_doc = types.SimpleNamespace(company="Co", bank_account="ACC-1", submit_after_import=1)
	with (
		patch.object(frappe, "get_doc", create=True, return_value=bt),
		patch.object(worker, "_find_existing_bank_transaction", return_value=None),
		patch.object(worker, "_handle_row_error") as row_error,
		patch.object(frappe.db, "savepoint", create=True),
	):
		worker._process_staging_row(row, import_doc, "BSI-1", None, None, lambda _r, _a: "REF-1")

	row_error.assert_called_once()
	assert "Fiscal year missing" in str(row_error.call_args.args[3])
