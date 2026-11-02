# Copyright (c) 2026, Vyogo Technologies and contributors
# For license information, please see license.txt

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime
from typing import Any


class QifStreamParser:
	"""
	High-performance, memory-efficient stream parser for QIF files.
	Supports Bank (!Type:Bank), Cash (!Type:Cash), and Credit Card (!Type:CCard).
	Disambiguates same-day identical transactions using deterministic occurrence hashing.
	"""

	DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y")

	def __init__(self, stream: Iterator[str] | list[str], date_format: str | None = None):
		self.stream = stream
		self.preferred_date_format = date_format
		self.occurrence_counter: dict[tuple[str, float, str], int] = {}

	def _parse_date(self, val: str) -> str:
		val = val.strip().replace("'", "/").replace(".", "/").replace("-", "/")
		formats_to_try = [self.preferred_date_format] if self.preferred_date_format else []
		for fmt in self.DATE_FORMATS:
			if fmt and fmt not in formats_to_try:
				formats_to_try.append(fmt)

		for fmt in formats_to_try:
			try:
				dt = datetime.strptime(val, fmt)
				return dt.strftime("%Y-%m-%d")
			except (ValueError, TypeError):
				continue

		# Fallback to current date or throw
		raise ValueError(f"Unable to parse date string: '{val}'")

	def iter_records(self) -> Iterator[dict[str, Any]]:
		record: dict[str, Any] = {}

		for line in self.stream:
			line = line.strip()
			if not line or line.startswith("!"):
				continue

			if line == "^":
				if "date" in record and "amount" in record:
					payee = record.get("payee", "")
					record.get("memo", "")
					amount = record["amount"]
					date_str = record["date"]

					key = (date_str, amount, payee)
					seq = self.occurrence_counter.get(key, 0) + 1
					self.occurrence_counter[key] = seq
					record["occurrence_idx"] = seq

					# Build deterministic hash: (date + amount + payee + occurrence)
					hash_str = f"{date_str}|{amount:.2f}|{payee}|{seq}"
					record["dedupe_hash"] = hashlib.sha256(hash_str.encode("utf-8")).hexdigest()[:24]

					yield record
				record = {}
				continue

			code, val = line[0], line[1:].strip()
			if code == "D":
				record["date"] = self._parse_date(val)
			elif code == "T":
				clean_val = val.replace(",", "").replace("$", "")
				record["amount"] = round(float(clean_val), 2)
			elif code == "P":
				record["payee"] = val
			elif code == "M":
				record["memo"] = val
			elif code == "N":
				record["reference_no"] = val
