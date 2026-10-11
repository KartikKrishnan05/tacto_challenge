"""Read-only access to the generated JSON master: tables, citable IDs, documents and cases."""

import base64
import fnmatch
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import time
from datetime import date
from functools import cached_property
from pathlib import Path
from threading import RLock

from .master_data import find_release, load_master_data

MAX_ROWS = 200
RELEASE = "release-sunday"
log = logging.getLogger("arena")
_CONTEXT_VERSION = 3
_COMPANY_CONTEXT_FILES = ("company.md", "SCORECARD.md", "KNOWN_ISSUES.md")


def _quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


class DataPackage:
    def __init__(self, root, release=None):
        """Load the package's master JSON, creating or refreshing it from source files if needed."""
        self.root = Path(root).expanduser().resolve()
        release = release or os.environ.get("ARENA_RELEASE")
        self.release = find_release(self.root, release)
        self.master = load_master_data(self.root, self.release)
        self.facts = self.master["package"].get("facts", {})
        self.metadata = self.master["package"].get("metadata", {})
        self.db = None  # compatibility attribute: runtime lookup no longer opens world.sqlite
        self.db_path = None
        self._query_db = None
        self._db_lock = RLock()
        if not self.facts and not self.master["package"].get("tables"):
            raise FileNotFoundError(f"no facts.json or .sqlite under {self.root}")

    @property
    def _package_tables(self):
        return self.master["package"].get("tables", {})

    @property
    def _release_tables(self):
        return (self.master.get("release") or {}).get("tables", {})

    @cached_property
    def _table_schemas(self):
        """Union package and release columns, preserving the first declared type per column."""
        schemas = {}
        for tables in (self._package_tables, self._release_tables):
            for table, meta in tables.items():
                fields = schemas.setdefault(table, [])
                names = {field["name"] for field in fields}
                for field in meta.get("schema", []):
                    if field["name"] not in names:
                        fields.append(dict(field))
                        names.add(field["name"])
        return schemas

    @cached_property
    def _schema(self):
        return {
            table: [field["name"] for field in fields]
            for table, fields in self._table_schemas.items()
        }

    def _table_primary_key(self, table):
        return (
            self._package_tables.get(table, {}).get("primary_key")
            or self._release_tables.get(table, {}).get("primary_key")
            or []
        )

    def tables(self):
        """{table: {"columns": [...], "rows": n}}"""
        output = {}
        for table in self._schema:
            output[table] = {
                "columns": [
                    f"{field['name']} {field.get('type', '')}".strip()
                    for field in self._table_schemas[table]
                ],
                "rows": len(self._combined_rows(table)),
            }
        return output

    def query(self, sql, params=(), limit=MAX_ROWS):
        if sql.lstrip().split(None, 1)[0].lower() not in {"select", "with"}:
            raise ValueError("read-only: SELECT or WITH statements only")
        with self._db_lock:
            cur = self._query_connection().execute(sql, params)
            return [dict(r) for r in cur.fetchmany(limit)]

    def _query_connection(self):
        """Materialize the JSON tables into SQLite only for callers that need general SQL."""
        if self._query_db is not None:
            return self._query_db
        if not self._schema:
            raise ValueError("this package has no tabular data")
        connection = sqlite3.connect(":memory:", check_same_thread=False)
        connection.row_factory = sqlite3.Row
        for table in self._schema:
            schema = self._table_schemas.get(table, [])
            if not schema:
                continue
            definition = ", ".join(
                f'{_quote_identifier(field["name"])} {field.get("type") or "TEXT"}' for field in schema
            )
            connection.execute(f'CREATE TABLE {_quote_identifier(table)} ({definition})')
            columns = [field["name"] for field in schema]
            placeholders = ", ".join("?" for _ in columns)
            insert = f'INSERT INTO {_quote_identifier(table)} VALUES ({placeholders})'
            rows = self._combined_rows(table)
            connection.executemany(insert, [tuple(row.get(column) for column in columns) for row in rows])
        connection.execute("PRAGMA query_only = ON")
        self._query_db = connection
        return connection

    def _combined_rows(self, table):
        """Package rows plus release-only rows, deduplicated by table key."""
        base = self._package_tables.get(table, {}).get("rows", [])
        extra = self._release_tables.get(table, {}).get("rows", [])
        if not extra:
            return base
        key_fields = self._table_primary_key(table)
        if not key_fields:
            columns = self._schema.get(table, [])
            key_fields = ["id"] if "id" in columns else []
        if not key_fields:
            merged = list(base)
            for row in extra:
                if row not in merged:
                    merged.append(row)
            return merged
        seen = {tuple(row.get(key) for key in key_fields) for row in base}
        merged = list(base)
        for row in extra:
            key = tuple(row.get(column) for column in key_fields)
            if key not in seen:
                merged.append(row)
                seen.add(key)
        return merged

    @cached_property
    def _ids(self):
        """Index citable table IDs; rows remain backed by the in-memory master JSON."""
        index = {}
        for table, columns in self._schema.items():
            if not columns:
                continue
            primary_key = self._table_primary_key(table)
            key = "id" if "id" in columns else (primary_key[0] if primary_key else columns[0])
            source_tables = (
                (None, self._package_tables.get(table, {}).get("rows", [])),
                (self.release, self._release_tables.get(table, {}).get("rows", [])),
            )
            for source_root, rows in source_tables:
                for row in rows:
                    value = row.get(key)
                    if isinstance(value, str) and ":" in value:
                        indexed = {"table": table, **row}
                        if source_root:
                            indexed["root"] = str(source_root)
                        index[value] = indexed
        return index

    def lookup(self, ref):
        """The fact behind a citable ID (from facts.json, else its table row), or None."""
        if fact := self.facts.get(ref):
            return fact
        return self._lookup_row(ref)

    def _lookup_row(self, ref):
        """Fetch one indexed ID from the master tables."""
        if not isinstance(ref, str) or ":" not in ref:
            return None
        return self._ids.get(ref)

    def _release_rows(self, table, where, order_by=None, limit=10):
        """Filter the rows contributed by the Sunday release."""
        rows = self._release_tables.get(table, {}).get("rows", [])
        return self._filter_rows(table, where, order_by, limit, rows, source_root=self.release)

    def _select_rows(self, table, where, order_by=None, limit=10):
        """Select a bounded slice from the master JSON; missing columns are ignored."""
        return self._filter_rows(table, where, order_by, limit, self._combined_rows(table))

    def _filter_rows(self, table, where, order_by, limit, source_rows, source_root=None):
        columns = self._schema.get(table, [])
        if not columns:
            return []
        conditions = []
        for column, op, value in where:
            if column not in columns:
                continue
            conditions.append((column, op, value))
        if where and not conditions:
            return []
        cap = max(1, min(int(limit), MAX_ROWS))
        matches = []
        for row in source_rows:
            include = True
            for column, op, value in conditions:
                field = row.get(column)
                if op == "=" and field != value:
                    include = False
                elif op == "IN" and field not in value:
                    include = False
                elif op == "LIKE" and not fnmatch.fnmatchcase(
                    str(field or "").casefold(), str(value).casefold().replace("%", "*")
                ):
                    include = False
                elif op == "IS" and ((field is None) != (value is None)):
                    include = False
                if not include:
                    break
            if include:
                item = {"table": table, **row}
                if source_root:
                    item["root"] = str(source_root)
                matches.append(item)
        if order_by and order_by[0] in columns and order_by[1] in ("ASC", "DESC"):
            column, direction = order_by

            def sort_key(row):
                value = row.get(column)
                if value is None:
                    return (0, 0, "")
                if isinstance(value, (int, float)):
                    return (1, 0, value)
                return (1, 1, str(value))

            if direction == "DESC":
                present = [row for row in matches if row.get(column) is not None]
                missing = [row for row in matches if row.get(column) is None]
                present.sort(key=lambda row: sort_key(row)[1:], reverse=True)
                matches = present + missing
            else:
                matches.sort(key=sort_key)
        return matches[:cap]

    def _data_fingerprint(self):
        return self.master["source_fingerprint"]

    @staticmethod
    def _normalise_ids(values, prefix):
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return []
        return sorted({value for value in values if isinstance(value, str) and value.startswith(prefix)})

    def negotiation_context(self, case, cache_dir=None):
        """Supplier, product and buyer dossiers, cached for this case and data-package revision.

        Both sides are always searched. A missing supplier record never suppresses the product
        lookup, and a sparse supplier record is supplemented by the shared product dossier.
        """
        started = time.perf_counter()
        suppliers = self._normalise_ids(case.get("suppliers", []), "sup:")
        parts = self._normalise_ids(case.get("parts", []), "part:")
        fingerprint = self._data_fingerprint()
        identity = {
            "version": _CONTEXT_VERSION,
            "data": fingerprint,
            "case_id": case.get("case_id"),
            "suppliers": suppliers,
            "parts": parts,
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
        cache_dir = Path(cache_dir) if cache_dir else Path(__file__).resolve().parents[1] / "temp"
        cache_file = cache_dir / f"negotiation_context_{key}.json"
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("cache_key") == key and isinstance(cached.get("context"), dict):
                log.info(
                    "negotiation dossier cache hit for %s (%.0f ms)",
                    case.get("case_id"),
                    (time.perf_counter() - started) * 1000,
                )
                return cached["context"]
        except (
            FileNotFoundError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            pass

        supplier_contexts = {sid: self._supplier_context(sid, parts) for sid in suppliers}
        product = self._product_context(parts, suppliers)
        company = self._company_context(suppliers, parts)
        context = {
            "product": product,
            "suppliers": supplier_contexts,
            "company": company,
            "checked": {"supplier_ids": suppliers, "part_ids": parts},
        }
        temporary = None
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=cache_dir, prefix=f".{cache_file.name}.", suffix=".tmp", delete=False
            ) as handle:
                temporary = Path(handle.name)
                json.dump({"cache_key": key, "context": context}, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, cache_file)
        except OSError:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            # Negotiation can continue if the cache directory is unavailable.
        log.info(
            "negotiation dossier cache miss for %s (%.0f ms)",
            case.get("case_id"),
            (time.perf_counter() - started) * 1000,
        )
        return context

    def _company_context(self, suppliers, parts):
        """Read buyer policies and targeted internal purchasing records for this case."""
        documents = {}
        for name in _COMPANY_CONTEXT_FILES:
            try:
                documents[name] = self.document(name)
            except (FileNotFoundError, OSError, UnicodeError):
                documents[name] = ""

        sourcing_rows = self._select_rows("sourcing", [("part_id", "IN", parts)], limit=60) if parts else []
        sources_by_part = {}
        for part_id in parts:
            part_sources = [row for row in sourcing_rows if row.get("part_id") == part_id]
            usable = [row for row in part_sources if row.get("status") in ("active", "qualified")]
            alternatives = [row for row in usable if row.get("supplier_id") not in suppliers]
            sources_by_part[part_id] = {
                "active_source_count": sum(row.get("status") == "active" for row in part_sources),
                "qualified_source_count": sum(row.get("status") == "qualified" for row in part_sources),
                "has_qualified_alternative": bool(alternatives),
                "alternative_source_count": len(alternatives),
            }

        spend = self._select_rows(
            "spend_cube", [("supplier_id", "IN", suppliers)], ("fiscal_year", "DESC"), 30
        ) if suppliers else []
        forecasts = self._select_rows(
            "forecasts", [("part_id", "IN", parts)], ("snapshot_month", "DESC"), min(30, max(1, len(parts) * 4))
        ) if parts else []
        return {
            "policy_documents": documents,
            "approved_sources_by_part": sources_by_part,
            "supplier_relationship_spend": spend,
            "demand_forecasts": forecasts,
        }

    def _supplier_context(self, supplier_id, parts):
        supplier_only = [("supplier_id", "=", supplier_id)]

        supplier = self._select_rows("suppliers", [("id", "=", supplier_id)], limit=1)
        intel = self._select_rows("supplier_intel", supplier_only, limit=1)
        contract_rows = self._select_rows("contracts", supplier_only, ("valid_from", "DESC"), 4)
        contract_ids = [row.get("id") for row in contract_rows if row.get("id")]
        clauses = self._select_rows("contract_clauses", [("contract_id", "IN", contract_ids)], limit=14)
        scorecards = self._select_rows("scorecards", supplier_only, ("period_end", "DESC"), 4)
        history = self._select_rows("negotiation_history", supplier_only, ("date", "DESC"), 5)
        if parts:
            specific_history = self._select_rows(
                "negotiation_history", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)],
                ("date", "DESC"), 6,
            )
            seen_events = {row.get("event_id") for row in specific_history}
            history = specific_history + [row for row in history if row.get("event_id") not in seen_events][:2]
        price_conditions = self._select_rows(
            "price_conditions", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)],
            ("valid_from", "DESC"), 6,
        ) if parts else []
        po_lines = self._select_rows(
            "po_lines", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)],
            ("order_date", "DESC"), 5,
        ) if parts else []
        quotes = self._select_rows(
            "quotes", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)],
            ("quote_date", "DESC"), 4,
        ) if parts else []
        quality = self._select_rows(
            "quality_notifications", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)],
            ("qn_date", "DESC"), 5,
        ) if parts else []
        receipts = self._select_rows(
            "goods_receipts", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)],
            ("posting_date", "DESC"), 5,
        ) if parts else []
        news = self._select_rows("news", supplier_only, ("date", "DESC"), 4)
        news += self._release_rows("news", supplier_only, ("date", "DESC"), 4)
        news = sorted({row.get("id"): row for row in news if row.get("id")}.values(),
                      key=lambda row: row.get("date") or "", reverse=True)[:4]
        documents = self._select_rows("documents", supplier_only, ("date", "DESC"), 20)
        documents += self._release_rows("documents", supplier_only, ("date", "DESC"), 12)
        documents = list({row.get("id"): row for row in documents if row.get("id")}.values())
        topics = ("price", "payment", "capacity", "liquidity", "commercial", "portfolio", "volume", "annual")
        documents.sort(
            key=lambda row: (
                row.get("kind") in ("call_note", "meeting_note"),
                sum(word in (row.get("subject") or "").lower() for word in topics),
                row.get("date") or "",
            ),
            reverse=True,
        )
        documents = documents[:6]
        for row in documents:
            try:
                row["excerpt"] = self.document(row.get("id"))[:900]
            except (OSError, UnicodeError, ValueError, TypeError):
                row["excerpt"] = ""
        payment = []
        if "invoices" in self._schema:
            supplier_invoices = [
                row for row in self._combined_rows("invoices") if row.get("supplier_id") == supplier_id
            ]
            late_days = []
            for invoice in supplier_invoices:
                if not invoice.get("paid_date") or not invoice.get("due_date"):
                    continue
                try:
                    late_days.append(
                        (date.fromisoformat(invoice["paid_date"]) - date.fromisoformat(invoice["due_date"])).days
                    )
                except (TypeError, ValueError):
                    continue
            payment = [{
                "invoice_count": len(supplier_invoices),
                "avg_days_to_payment_after_due": round(sum(late_days) / len(late_days), 1) if late_days else None,
                "discounts_taken": sum((invoice.get("discount_taken") or 0) > 0 for invoice in supplier_invoices),
            }]
        invoice_rows = self._select_rows("invoices", supplier_only, ("invoice_date", "DESC"), 4)
        sourcing = self._select_rows(
            "sourcing", [("supplier_id", "=", supplier_id), ("part_id", "IN", parts)], limit=4
        ) if parts else self._select_rows("sourcing", supplier_only, limit=4)
        payment_found = bool(payment and (payment[0].get("invoice_count") or 0))
        found = any(
            (
                supplier,
                intel,
                contract_rows,
                scorecards,
                history,
                price_conditions,
                po_lines,
                quotes,
                quality,
                receipts,
                news,
                documents,
                invoice_rows,
                sourcing,
                payment_found,
            )
        )
        return {
            "availability": "supplier records found" if found else "no supplier-specific records found",
            "supplier_master_record_found": bool(supplier),
            "supplier_master": supplier,
            "supplier_intel": intel,
            "sourcing": sourcing,
            "contracts": contract_rows,
            "contract_clauses": clauses,
            "scorecards": scorecards,
            "negotiation_history": history[:8],
            "price_conditions_for_case_parts": price_conditions,
            "purchase_orders_for_case_parts": po_lines,
            "quotes_for_case_parts": quotes,
            "quality_for_case_parts": quality,
            "goods_receipts_for_case_parts": receipts,
            "news": news,
            "correspondence": documents,
            "payment_behavior_summary": payment,
            "recent_invoices": invoice_rows,
        }

    def _product_context(self, parts, suppliers):
        out = {"availability": "product records found" if parts else "no product IDs in case", "parts": {}}
        for part_id in parts:
            part = self._select_rows("parts", [("id", "=", part_id)], limit=1)
            drivers = self._select_rows("cost_drivers", [("part_id", "=", part_id)], limit=8)
            series = []
            for driver in drivers:
                series_id = driver.get("series_id")
                if not series_id:
                    continue
                series_info = self._select_rows("index_series", [("id", "=", series_id)], limit=1)
                values = self._select_rows(
                    "index_values", [("series_id", "=", series_id)], ("month", "DESC"), 6
                )
                series.append({"driver": driver, "series": series_info, "recent_values": list(reversed(values))})
            source_rows = {
                "should_cost": self._select_rows(
                    "should_cost", [("part_id", "=", part_id)], ("estimate_date", "DESC"), 3
                ),
                "historical_prices": self._select_rows(
                    "price_conditions", [("part_id", "=", part_id)], ("valid_from", "DESC"), 7
                ),
                "quotes": self._select_rows("quotes", [("part_id", "=", part_id)], ("quote_date", "DESC"), 7),
                "rfqs": self._select_rows("rfqs", [("part_id", "=", part_id)], ("rfq_date", "DESC"), 4),
                "negotiation_history": self._select_rows(
                    "negotiation_history", [("part_id", "=", part_id)], ("date", "DESC"), 6
                ),
                "cost_drivers_and_indices": series,
            }
            release_documents = self._release_rows(
                "documents", [("part_ids", "LIKE", f"%{part_id}%")], ("date", "DESC"), 5
            )
            for row in release_documents:
                try:
                    row["excerpt"] = self.document(row.get("id"))[:900]
                except (OSError, UnicodeError, ValueError, TypeError):
                    row["excerpt"] = ""
            source_rows["release_documents"] = release_documents
            part_row = part[0] if part else None
            terms = []
            if part_row:
                for field in ("description", "material_group", "grade", "category"):
                    value = part_row.get(field)
                    if isinstance(value, str) and len(value.strip()) > 2:
                        terms.append(value.strip().split()[0])
            matches = {}
            for term in terms[:4]:
                pattern = f"%{term}%"
                rows = self._select_rows(
                    "news", [("headline", "LIKE", pattern)], ("date", "DESC"), 3
                ) + self._select_rows("news", [("body", "LIKE", pattern)], ("date", "DESC"), 3)
                rows += self._release_rows("news", [("headline", "LIKE", pattern)], ("date", "DESC"), 3)
                rows += self._release_rows("news", [("body", "LIKE", pattern)], ("date", "DESC"), 3)
                for row in rows:
                    if row.get("id"):
                        matches[row["id"]] = row
            source_rows["product_news_matches"] = list(matches.values())[:5]
            out["parts"][part_id] = {"part": part_row, **source_rows}
        out["searched_supplier_ids"] = suppliers
        if parts and not any(
            rows.get("part") or any(v for k, v in rows.items() if k != "part")
            for rows in out["parts"].values()
        ):
            out["availability"] = "no product-specific records found"
        return out

    def ids(self, prefix):
        """All citable IDs starting with `prefix`, sorted, e.g. ids("idx:GP19-245113@")."""
        return sorted(i for i in self.facts.keys() | self._ids.keys() if i.startswith(prefix))

    def document(self, ref):
        """A document's text by ID (row with a path or body column) or by package-relative path."""
        row = self._lookup_row(ref) or self.facts.get(ref)
        source = "package"
        if row:
            for col in ("body", "text", "content"):
                if row.get(col):
                    return row[col]
            if self.release and row.get("root") == str(self.release):
                source = "release"
            ref = next((row[c] for c in ("path", "file") if row.get(c)), None)
            if ref is None:
                return json.dumps(row, ensure_ascii=False)
        if not isinstance(ref, str):
            raise FileNotFoundError(ref)
        relative = Path(ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise FileNotFoundError(ref)
        key = relative.as_posix()
        if key.startswith(f"{RELEASE}/"):
            source, key = "release", key[len(RELEASE) + 1:]
        segment = (self.master.get(source) or {}) if source == "release" else self.master["package"]
        files = segment.get("text_files", {})
        if key in files:
            return files[key]
        binary = segment.get("binary_files_base64", {})
        if key in binary:
            return base64.b64decode(binary[key]).decode("utf-8")
        raise FileNotFoundError(ref)

    @cached_property
    def cases(self):
        """Practice (and demo) case files in the package: {case_id: case}."""
        out = dict(self.master["package"].get("cases", {}))
        out.update((self.master.get("release") or {}).get("cases", {}))
        return out
