# Vyogo Frappe QIF — Technical Architecture

This document describes the internal design, streaming model, concurrency controls, pluggable interfaces, memory optimization, error recovery, and multi-tenant isolation mechanisms of `vyogo_frappe_qif`.

---

## 1. True $O(1)$ Memory Ingestion Pipeline

### Zero-Materialization Stream Reader (`stream_reader.py`)
Traditional Frappe file importers read entire uploaded files into RAM via `file_doc.get_content()` or `f.read()`, splitting them into massive memory lists (`content.splitlines()`). For multi-megabyte statement files (100k+ transactions), this causes Python process memory ballooning and Out-Of-Memory (OOM) crashes in worker queues.

`vyogo_frappe_qif` enforces strict $O(1)$ memory streaming:
1. **Incremental Stream Decoding**:
   - `get_file_line_iterator(file_doc, file_path)` streams lines directly from disk or byte streams.
   - Decodes incrementally using `utf-8-sig` with universal newlines, seamlessly supporting CRLF (`\r\n`), LF (`\n`), classic Mac CR (`\r`), and UTF-8 Byte Order Marks (`\ufeff`).
2. **Pluggable Remote Storage Hook (`qif_file_line_reader`)**:
   - For remote files (S3, GCS, Azure Blob, MinIO), statements are streamed directly over HTTP/gRPC without downloading the entire file to disk:
     ```python
     # hooks.py
     qif_file_line_reader = ["my_app.storage.stream_file_lines"]
     ```
   - If a remote file is encountered and no hook is configured, a descriptive `frappe.ValidationError` is immediately raised.

### Format-Agnostic Staging Engine (`staging.py`)
Raw statements are decoupled from database persistence via `stage_records()`:
- Accepts any generator or `Iterable[dict[str, Any]]` conforming to the normalized schema (`date`, `amount`, `payee`, `memo`, `reference_no`, `occurrence_idx`, `dedupe_hash`).
- Batches records into chunks of 2,500 rows.
- Flushes directly into MariaDB `tabQIF Staging Record` using raw SQL `frappe.db.bulk_insert()`.
- Releases memory after each chunk, maintaining bounded peak memory (< 20MB even for statements with 200,000+ records).

---

## 2. Pluggable Reference Builder & Deduplication

In standard accounting systems, deduplication schemes and reference numbers vary widely across banking providers and internal ERP configurations.

### Pluggable Resolver Hierarchy
The worker resolves the reference generator via:
1. **Explicit argument**: `reference_builder` passed to `import_qif()`, `start_qif_import()`, or `process_qif_batch()`.
2. **Frappe Hook**: `qif_reference_builder` registered in `hooks.py`:
   ```python
   qif_reference_builder = ["my_app.bank.custom_reference_scheme"]
   ```
3. **Default scheme**:
   $$\text{reference\_number} = \text{row.reference\_no} \lor \text{"QIF-" + row.dedupe\_hash[:12]}$$

### Dual-Use Idempotency Guarantee
The resolved builder is executed with signature:
```python
builder(row: dict, bank_account: str) -> str
```
where `row` includes:
- `date`, `amount`, `payee`, `memo`, `reference_no`, `occurrence_idx`, `dedupe_hash`, `description`.

The exact resulting string is used for:
1. The `Bank Transaction.reference_number` column.
2. The idempotency filter querying existing records in `tabBank Transaction`.

Re-uploading the same statement under the same or custom scheme automatically matches existing transactions and marks staging rows as `Skipped`.

---

## 3. Configurable Queue Routing

Background statement processing can be routed to dedicated Redis Queues to prevent starvation of short interactive tasks:

### Queue Resolution Hierarchy:
1. Explicit runtime argument: `queue="custom_queue"`
2. Site configuration (`site_config.json`):
   ```json
   {
     "qif_import_queue": "financial_ingestion"
   }
   ```
3. Frappe Hook: `qif_import_queue = ["financial_ingestion"]`
4. Default fallback: `"long"` (because financial imports are long-running batch jobs).

The resolved queue name is threaded across the asynchronous chain: each batch automatically enqueues the next batch on the exact same queue.

---

## 4. Transaction Isolation & Worker Architecture

Ledger creation runs in batch chunks (default: 100 rows):
1. **Row-Level Savepoint Isolation**:
   ```python
   savepoint_id = f"qif_{row.name.replace('-', '_')}"
   frappe.db.savepoint(savepoint_id)
   ```
2. **Independent Failure Recovery**:
   If an individual record fails (e.g. invalid account currency, closed fiscal period):
   - The worker rolls back strictly to that row's `SAVEPOINT`.
   - The error is captured in `QIF Staging Record.error_message`.
   - Full diagnostic traceback is recorded in Frappe's standard `tabError Log`.
   - Subsequent rows continue without interruption.
3. **Socket.io Progress Notifications**:
   Emits `qif_import_progress` events after each batch commit with completed, skipped, and failed counters.

### 4.1 Host-Driven In-Process Batch Loop (`process_pending_rows`)
For hosts requiring custom execution environments or job runners without background RQ queues:
- `process_pending_rows(import_session, batch_size=100, user=None, tenant_id=None, reference_builder=None, should_stop=None) -> dict`
- Executes batch by batch inside the calling process with periodic database commits.
- Synchronous invocation via `import_qif(async_run=False)` runs the entire statement in-process to completion.
- Reuses the shared `_process_batch_core()` implementation with zero code duplication.

### 4.2 Resource Progress Fields & Standard REST Querying
Rather than requiring bespoke websocket listeners, `Bank Statement Import` features standard persisted read-only counters:
- `qif_total_rows`, `qif_processed_rows`, `qif_completed_rows`, `qif_skipped_rows`, `qif_failed_rows`.
- Synchronized to MariaDB after each batch commit and at finalisation.
- Queryable via `GET /api/resource/Bank Statement Import/<name>`.

### 4.3 Cancellation Flow (`qif_cancel_requested`)
- Users or API callers set `qif_cancel_requested = 1` on `Bank Statement Import`.
- Checked before each batch in both background queue and in-process loops.
- When set:
  1. Remaining `Pending` staging records are updated in bulk to `Skipped`.
  2. Further batch chaining is suppressed.
  3. Session is finalised and status updated.

---

## 5. Ledger Guard & Safe Rollback

To eliminate orphan records and safeguard audit integrity:
- Every generated `Bank Transaction` is linked to its `QIF Staging Record`.
- `undo_qif_import()` executes the **Ledger Guard**:
  ```sql
  SELECT name, reference_number 
  FROM `tabBank Transaction` 
  WHERE name IN (...) AND status = 'Reconciled'
  ```
- If any transaction has already been reconciled by an accountant in the ERPNext Bank Reconciliation Tool, rollback is strictly aborted.
- If all transactions are unreconciled, they are deleted in a single transactional step and staging rows reset to `Pending`.

### 5.1 Safe Bank Transaction Deletion (`on_trash` Hook)
When an accountant or test deletes an individual `Bank Transaction`:
- The `doc_events["Bank Transaction"]["on_trash"]` handler triggers `on_bank_transaction_trash()`.
- Automatically executes `UPDATE tabQIF Staging Record SET bank_transaction = NULL WHERE bank_transaction = %s`.
- Clears the foreign key reference before Frappe checks `check_if_doc_is_linked()`, preventing `frappe.LinkExistsError`.
- Exposed as `clear_staging_bank_transaction_links(bank_transaction)` for standalone execution.

---

## 6. Multi-Tenant SaaS Integration

In multi-tenant SaaS environments (e.g. `saas_platform`):
- `tenant_compat.py` dynamically activates multi-tenant mode.
- Monkey-patches `BaseDocument.get_valid_dict()` so raw DDL `tenant_id` columns persist during `insert()`.
- Background worker restores user and tenant session contexts (`frappe.local.current_tenant_id` and `frappe.session.tenant_id`) preventing jobs from falling back to `'SYSTEM'`.
- Idempotency checks and SQL queries are strictly scoped by `tenant_id`.
