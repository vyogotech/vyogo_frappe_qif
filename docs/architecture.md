# Vyogo Frappe QIF — Technical Architecture

This document describes the internal design, concurrency model, memory optimization, error recovery, and multi-tenant isolation mechanisms of `vyogo_frappe_qif`.

---

## 1. High-Speed Ingestion Pipeline

### O(1) Memory Streaming Parser
Standard Frappe file importers read entire uploaded files into memory, parse them into a full array of document objects, and iterate over them. For large bank statements (100k+ transactions, 50MB+ QIF files), this causes severe Python memory consumption and worker OOM crashes.

`vyogo_frappe_qif` uses `QifStreamParser`:
- Pure generator reading lines iteratively via `iter_records()`.
- State machine tracks current record attributes (`D`, `T`, `P`, `M`, `N`, `L`, `C`) and yields clean dicts upon encountering the record delimiter (`^`).
- Sniffs date formats automatically by evaluating the first valid date string against common date patterns (`%d/%m/%Y`, `%m/%d/%Y`, `%Y-%m-%d`, etc.).

### Batch Staging via MariaDB `bulk_insert`
Parsed records are not immediately converted to ERPNext `Bank Transaction` documents. Instead:
- They are accumulated in batches of 2,500 records.
- Injected directly into MariaDB `tabQIF Staging Record` using `frappe.db.bulk_insert()`.
- Bypasses document-level ORM overhead during raw ingestion, enabling ingestion rates exceeding **5,000 records/second**.

---

## 2. Occurrence Indexing & Deduplication

QIF records lack globally unique transaction IDs from financial institutions. On any given day, an account may have multiple legitimate identical charges (e.g. three $4.50 coffee purchases or multiple transit fees).

To disambiguate legitimate repeats while rejecting duplicate statement uploads:
1. `QifStreamParser` tracks a daily occurrence dictionary: `(date, amount, payee) -> count`.
2. Each transaction receives an `occurrence_idx` (1, 2, 3...).
3. A deterministic deduplication hash is generated:
   $$\text{hash} = \text{SHA256}(\text{tenant\_id} \parallel \text{company} \parallel \text{date} \parallel \text{amount} \parallel \text{payee} \parallel \text{occurrence\_idx})[:24]$$

---

## 3. Transaction Isolation & Worker Architecture

### Chained Asynchronous Workers
Ledger creation runs asynchronously via Frappe Redis Queue (`default` queue):
1. Worker queries next `batch_size` (default: 100) records with `status='Pending'`.
2. Each record is processed inside an individual database `SAVEPOINT`:
   ```python
   frappe.db.savepoint(f"qif_{row.name}")
   ```
3. If an individual record fails validation (e.g., closed accounting fiscal period, invalid bank account link):
   - Only that record rolls back to its savepoint.
   - The error is captured in `QIF Staging Record.error_message`.
   - The full traceback is logged to Frappe's standard `tabError Log` table.
   - The remaining 99 records in the batch proceed without interruption.
4. The batch commits (`frappe.db.commit()`), updates Socket.io progress (`qif_import_progress`), and enqueues the next batch until all records are processed.

---

## 4. Ledger Guard & Safe Rollback

Unlike standard imports which leave orphaned transactions on partial failures:
- Every `Bank Transaction` created has its `name` linked back to the `QIF Staging Record`.
- The `undo_qif_import` method performs a safety check (**Ledger Guard**):
  ```sql
  SELECT name FROM `tabBank Transaction` WHERE name IN (...) AND status = 'Reconciled'
  ```
- If any transaction has been reconciled, rollback is strictly blocked to maintain accounting ledger integrity.
- If no transactions are reconciled, all created `Bank Transaction` documents are safely removed, and staging records reset to `Pending`.

---

## 5. Multi-Tenant SaaS Integration

When running in a multi-tenant environment (such as Vyogo SaaS Platform):
- Tables use a raw `ALTER TABLE ... ADD COLUMN tenant_id` structure.
- `tenant_compat.ensure_tenant_valid_dict_patch()` monkey-patches `BaseDocument.get_valid_dict()` so that `tenant_id` is serialized in Frappe's SQL insert statements.
- Worker queue jobs restore the caller's session user and tenant state (`frappe.session["tenant_id"]` and `frappe.local.current_tenant_id`) preventing background worker default to `'SYSTEM'`.
- Post-insert database verification guarantees table column consistency.
