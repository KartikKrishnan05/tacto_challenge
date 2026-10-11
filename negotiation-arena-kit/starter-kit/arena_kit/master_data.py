"""Build and load one JSON master file from a procurement data package.

SQLite is used only during the one-time conversion. Runtime consumers read the generated JSON;
the read-only SQL API is backed by an in-memory SQLite copy populated from that JSON.
"""

import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock

MASTER_FILENAME = "master.json"
MASTER_FORMAT_VERSION = 1
RELEASE_DIRNAME = "release-sunday"
_SKIP_DIRS = {".git", "__pycache__", "temp"}
_MASTER_LOCK = RLock()
_MASTER_CACHE = {}
log = logging.getLogger("arena")


def find_release(root, release=None):
    """Resolve an explicit release or the default release folder beside the package."""
    root = Path(root).expanduser().resolve()
    candidates = [Path(release).expanduser()] if release else [root / RELEASE_DIRNAME, root.parent / RELEASE_DIRNAME]
    return next((path.resolve() for path in candidates if (path / "facts.json").is_file()), None)


def _source_files(root, release_root=None):
    root = Path(root).resolve()
    release_root = Path(release_root).resolve() if release_root else None
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or path.name == ".DS_Store" or path.name == MASTER_FILENAME:
            continue
        relative = path.relative_to(root)
        if any(part in _SKIP_DIRS for part in relative.parts):
            continue
        if release_root and path.is_relative_to(release_root):
            continue
        files.append(path)
    return sorted(files)


def _manifest(root, release_root=None):
    entries = []
    for label, base, files in (
        ("package", Path(root).resolve(), _source_files(root, release_root)),
        ("release", Path(release_root).resolve() if release_root else None,
         _source_files(release_root) if release_root else []),
    ):
        if base is None:
            continue
        for path in files:
            stat = path.stat()
            entries.append((label, path.relative_to(base).as_posix(), stat.st_size, stat.st_mtime_ns))
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return entries, hashlib.sha256(encoded).hexdigest()


def _quote_identifier(value):
    return '"' + str(value).replace('"', '""') + '"'


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON data file {path}: {exc}") from exc


def _resource_field_types(root):
    types = {}
    for path in sorted(Path(root).rglob("datapackage.json")):
        if RELEASE_DIRNAME in path.relative_to(root).parts:
            continue
        try:
            metadata = _read_json(path)
        except ValueError:
            continue
        for resource in metadata.get("resources", []):
            name = Path(resource.get("path") or resource.get("name") or "").stem
            schema = resource.get("schema") or {}
            fields = schema.get("fields", [])
            if name and fields:
                types[name] = {field.get("name"): field.get("type", "string") for field in fields}
    return types


def _resource_primary_keys(root):
    primary_keys = {}
    for path in sorted(Path(root).rglob("datapackage.json")):
        if RELEASE_DIRNAME in path.relative_to(root).parts:
            continue
        try:
            metadata = _read_json(path)
        except ValueError:
            continue
        for resource in metadata.get("resources", []):
            name = Path(resource.get("path") or resource.get("name") or "").stem
            key = resource.get("schema", {}).get("primaryKey", [])
            if isinstance(key, str):
                key = [key]
            if name and isinstance(key, list):
                primary_keys[name] = key
    return primary_keys


def _coerce_csv(value, field_type):
    if value == "":
        return None
    kind = (field_type or "").lower()
    if kind in ("integer", "int"):
        try:
            return int(value)
        except ValueError:
            return value
    if kind in ("number", "float", "double"):
        try:
            return float(value)
        except ValueError:
            return value
    if kind in ("boolean", "bool"):
        if value.lower() in ("true", "1"):
            return True
        if value.lower() in ("false", "0"):
            return False
    return value


def _same_csv_value(csv_value, database_value, declared_type):
    if database_value is None:
        return csv_value == ""
    if csv_value == "" and database_value == "":
        return True
    kind = (declared_type or "").upper()
    if kind in ("BOOLEAN", "BOOL"):
        def as_boolean(value):
            if isinstance(value, bool):
                return value
            if str(value).lower() in ("true", "1"):
                return True
            if str(value).lower() in ("false", "0"):
                return False
            return None

        return as_boolean(csv_value) is not None and as_boolean(csv_value) == as_boolean(database_value)
    if any(token in kind for token in ("INT", "REAL", "NUM", "DEC", "FLOAT", "DOUBLE")):
        try:
            return Decimal(csv_value) == Decimal(str(database_value))
        except (InvalidOperation, TypeError, ValueError):
            return False
    return str(database_value) == csv_value


def _verify_csv_export(path, columns, rows, schema, resource_field_types=None):
    """Fail conversion if a CSV table export disagrees with its SQLite source table."""
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        csv_columns = reader.fieldnames or []
        if csv_columns != columns:
            raise ValueError(f"CSV export columns disagree with SQLite for {path}")
        csv_rows = list(reader)
    if len(csv_rows) != len(rows):
        raise ValueError(f"CSV export row count disagrees with SQLite for {path}: {len(csv_rows)} vs {len(rows)}")
    types = {field["name"]: field.get("type", "") for field in schema}
    types.update(resource_field_types or {})
    for index, (csv_row, database_row) in enumerate(zip(csv_rows, rows, strict=True)):
        if any(not _same_csv_value(csv_row[column], database_row.get(column), types.get(column)) for column in columns):
            raise ValueError(f"CSV export row {index + 2} disagrees with SQLite for {path}")


def _load_segment(root, exclude_root=None):
    root = Path(root).resolve()
    files = _source_files(root, exclude_root)
    metadata = {}
    facts = {}
    json_files = {}
    text_files = {}
    binary_files = {}
    cases = {}
    tables = {}
    csv_paths = []
    database_paths = []

    for path in files:
        relative = path.relative_to(root).as_posix()
        if path.name == "datapackage.json":
            value = _read_json(path)
            if not metadata:
                metadata = value
            else:
                json_files[relative] = value
        elif path.name == "facts.json":
            value = _read_json(path)
            if isinstance(value, dict):
                facts.update(value)
        elif path.suffix.lower() == ".sqlite" or path.suffix.lower() == ".db":
            database_paths.append(path)
        elif path.suffix.lower() == ".csv":
            csv_paths.append(path)
        elif path.suffix.lower() == ".json":
            value = _read_json(path)
            json_files[relative] = value
            if "cases" in Path(relative).parts and isinstance(value, dict) and value.get("case_id"):
                cases[value["case_id"]] = value
        else:
            try:
                text_files[relative] = path.read_text(encoding="utf-8")
            except (UnicodeError, OSError):
                binary_files[relative] = base64.b64encode(path.read_bytes()).decode("ascii")

    for db_path in sorted(database_paths):
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            table_names = [
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ) if not row[0].startswith("sqlite_")
            ]
            for table in table_names:
                table_info = list(connection.execute(f"PRAGMA table_info({_quote_identifier(table)})"))
                schema = [{"name": row[1], "type": row[2] or ""} for row in table_info]
                rows = [dict(row) for row in connection.execute(f"SELECT * FROM {_quote_identifier(table)}")]
                primary_key = [row[1] for row in sorted(table_info, key=lambda row: row[5]) if row[5]]
                if table not in tables:
                    tables[table] = {"schema": schema, "primary_key": primary_key, "rows": rows}
                    continue
                previous = tables[table]
                if previous["schema"] != schema:
                    raise ValueError(f"SQLite schemas disagree for table {table!r} in {db_path}")
                merged_key = previous.get("primary_key") or primary_key
                if previous.get("primary_key") and primary_key and previous["primary_key"] != primary_key:
                    raise ValueError(f"SQLite primary keys disagree for table {table!r} in {db_path}")
                previous["primary_key"] = merged_key
                key_fields = merged_key or [field["name"] for field in schema]
                seen = {tuple(row.get(field) for field in key_fields): row for row in previous["rows"]}
                for row in rows:
                    key = tuple(row.get(field) for field in key_fields)
                    if key in seen:
                        if seen[key] != row:
                            raise ValueError(f"conflicting SQLite rows for table {table!r}, key {key!r}")
                        continue
                    previous["rows"].append(row)
                    seen[key] = row
        finally:
            connection.close()

    field_types = _resource_field_types(root)
    primary_keys = _resource_primary_keys(root)
    for path in csv_paths:
        table = path.stem
        if table in tables:
            meta = tables[table]
            _verify_csv_export(
                path,
                [field["name"] for field in meta["schema"]],
                meta["rows"],
                meta["schema"],
                field_types.get(table),
            )
            continue
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = reader.fieldnames or []
            rows = [
                {key: _coerce_csv(value, field_types.get(table, {}).get(key)) for key, value in row.items()}
                for row in reader
            ]
        schema = [{"name": column, "type": field_types.get(table, {}).get(column, "")} for column in columns]
        tables[table] = {"schema": schema, "primary_key": primary_keys.get(table, []), "rows": rows}

    return {
        "metadata": metadata,
        "facts": facts,
        "tables": tables,
        "text_files": text_files,
        "json_files": json_files,
        "binary_files_base64": binary_files,
        "cases": cases,
        "source_files": [path.relative_to(root).as_posix() for path in files],
    }


def build_master_data(root, release_root=None):
    root = Path(root).expanduser().resolve()
    package = _load_segment(root, exclude_root=release_root)
    release = _load_segment(release_root) if release_root else None
    if release:
        package["facts"].update(release["facts"])
    _, fingerprint = _manifest(root, release_root)
    return {
        "format_version": MASTER_FORMAT_VERSION,
        "source_fingerprint": fingerprint,
        "generated_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "package": package,
        "release": release,
    }


def load_master_data(root, release_root=None):
    """Load a current master file, rebuilding it atomically when source files changed."""
    root = Path(root).expanduser().resolve()
    release_root = Path(release_root).resolve() if release_root else None
    _, fingerprint = _manifest(root, release_root)
    path = root / MASTER_FILENAME
    cache_key = (str(path), fingerprint)
    with _MASTER_LOCK:
        if cache_key in _MASTER_CACHE:
            return _MASTER_CACHE[cache_key]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("format_version") != MASTER_FORMAT_VERSION or data.get("source_fingerprint") != fingerprint:
                data = None
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            data = None
        if data is None:
            data = build_master_data(root, release_root)
            temporary = None
            try:
                root.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=root, prefix=f".{MASTER_FILENAME}.", suffix=".tmp", delete=False
                ) as handle:
                    temporary = Path(handle.name)
                    json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.write("\n")
                os.replace(temporary, path)
            except OSError as exc:
                if temporary:
                    temporary.unlink(missing_ok=True)
                log.warning("could not save %s; using the converted data in memory: %s", path, exc)
        _MASTER_CACHE.clear()
        _MASTER_CACHE[cache_key] = data
        return data


def build_master_file(root, release_root=None):
    """Force a fresh conversion and atomically save `<root>/master.json`."""
    root = Path(root).expanduser().resolve()
    data = build_master_data(root, release_root)
    path = root / MASTER_FILENAME
    temporary = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=root, prefix=f".{MASTER_FILENAME}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        if temporary:
            temporary.unlink(missing_ok=True)
        raise
    with _MASTER_LOCK:
        _MASTER_CACHE.clear()
    return path, data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Combine procurement data and documents into one master JSON file.")
    parser.add_argument("data_dir", nargs="?", default=os.environ.get("ARENA_DATA"))
    parser.add_argument("--release", help="optional Sunday release directory")
    args = parser.parse_args(argv)
    if not args.data_dir:
        parser.error("provide a data directory or set ARENA_DATA")
    release = find_release(args.data_dir, args.release)
    path, data = build_master_file(args.data_dir, release)
    tables = data["package"]["tables"]
    row_count = sum(len(table["rows"]) for table in tables.values())
    document_count = len(data["package"]["text_files"])
    print(f"Wrote {path}: {len(tables)} tables, {row_count} rows, {document_count} text files")


if __name__ == "__main__":
    main()
