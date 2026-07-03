from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import starlet


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    num_features INTEGER,
    num_coordinates INTEGER,
    geometry_types TEXT NOT NULL DEFAULT '[]',
    mbr TEXT,
    schema_json TEXT,
    citation_json TEXT,
    visualization_type TEXT,
    visualization_url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name);
CREATE INDEX IF NOT EXISTS idx_datasets_size ON datasets(size_bytes);
"""


LIST_COLUMNS = [
    "id",
    "name",
    "description",
    "size_bytes",
    "num_features",
    "num_coordinates",
    "geometry_types",
    "mbr",
]


@dataclass(frozen=True)
class DatasetCatalog:
    db_path: Path
    datasets_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        object.__setattr__(self, "datasets_dir", Path(self.datasets_dir))

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def sync(self) -> list[dict[str, Any]]:
        self.init_db()
        synced: list[dict[str, Any]] = []
        names = starlet.list_datasets(self.datasets_dir)

        with self.connect() as conn:
            known = {
                row["name"]: row["id"]
                for row in conn.execute("SELECT id, name FROM datasets")
            }
            for name in names:
                dataset_id = known.get(name, str(uuid.uuid4()))
                row = self._build_row(dataset_id, name)
                self._upsert(conn, row)
                synced.append(row)
        return synced

    def list(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        self.init_db()
        clauses: list[str] = []
        values: list[Any] = []

        text = filters.get("q")
        if text:
            clauses.append("(name LIKE ? OR COALESCE(description, '') LIKE ?)")
            like = f"%{text}%"
            values.extend([like, like])

        name = filters.get("name")
        if name:
            clauses.append("name LIKE ?")
            values.append(f"%{name}%")

        description = filters.get("description")
        if description:
            clauses.append("COALESCE(description, '') LIKE ?")
            values.append(f"%{description}%")

        geometry_type = filters.get("geometry_type")
        if geometry_type:
            clauses.append("geometry_types LIKE ?")
            values.append(f"%{geometry_type}%")

        for param, op in (("min_size", ">="), ("max_size", "<=")):
            if filters.get(param):
                clauses.append(f"size_bytes {op} ?")
                values.append(int(filters[param]))

        sql = f"SELECT {', '.join(LIST_COLUMNS)} FROM datasets"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY name"

        with self.connect() as conn:
            return [decode_row(row) for row in conn.execute(sql, values)]

    def get(self, dataset_id_or_name: str) -> dict[str, Any] | None:
        self.init_db()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM datasets WHERE id = ?",
                (dataset_id_or_name,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM datasets WHERE name = ?",
                    (dataset_id_or_name,),
                ).fetchone()
            return decode_row(row) if row is not None else None

    def _build_row(self, dataset_id: str, name: str) -> dict[str, Any]:
        dataset_dir = self.datasets_dir / name
        metadata = starlet.get_dataset_metadata(dataset_dir)
        summary = starlet.get_dataset_summary(dataset_dir) or {}
        geometry_entries = summary.get("geometry") or []

        geom_types: set[str] = set()
        num_coordinates = None
        num_features = None
        mbr = metadata.get("bbox")

        for entry in geometry_entries:
            geom_types.update((entry.get("geom_types") or {}).keys())
            if entry.get("total_points") is not None:
                num_coordinates = (num_coordinates or 0) + int(entry["total_points"])
            if entry.get("mbr") is not None:
                mbr = entry["mbr"]
            geom_counts = entry.get("geom_types") or {}
            if geom_counts and num_features is None:
                num_features = sum(int(v) for v in geom_counts.values())

        schema = _schema_from_summary(summary)
        visualization_type = "MVT" if metadata.get("has_mvt") else None
        visualization_url = (
            f"/datasets/{dataset_id}/tiles" + "/{z}/{x}/{y}.mvt"
            if visualization_type == "MVT"
            else None
        )

        return {
            "id": dataset_id,
            "name": name,
            "description": summary.get("description"),
            "size_bytes": int(metadata.get("size_bytes") or 0),
            "num_features": num_features,
            "num_coordinates": num_coordinates,
            "geometry_types": sorted(geom_types),
            "mbr": mbr,
            "schema_json": schema,
            "citation_json": summary.get("citation"),
            "visualization_type": visualization_type,
            "visualization_url": visualization_url,
            "metadata_json": metadata,
            "summary_json": summary,
        }

    @staticmethod
    def _upsert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO datasets (
                id, name, description, size_bytes, num_features,
                num_coordinates, geometry_types, mbr, schema_json,
                citation_json, visualization_type, visualization_url,
                metadata_json, summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                size_bytes = excluded.size_bytes,
                num_features = excluded.num_features,
                num_coordinates = excluded.num_coordinates,
                geometry_types = excluded.geometry_types,
                mbr = excluded.mbr,
                schema_json = excluded.schema_json,
                citation_json = excluded.citation_json,
                visualization_type = excluded.visualization_type,
                visualization_url = excluded.visualization_url,
                metadata_json = excluded.metadata_json,
                summary_json = excluded.summary_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                row["id"],
                row["name"],
                row["description"],
                row["size_bytes"],
                row["num_features"],
                row["num_coordinates"],
                json.dumps(row["geometry_types"]),
                json.dumps(row["mbr"]),
                json.dumps(row["schema_json"]),
                json.dumps(row["citation_json"]),
                row["visualization_type"],
                row["visualization_url"],
                json.dumps(row["metadata_json"]),
                json.dumps(row["summary_json"]),
            ),
        )


def decode_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in (
        "geometry_types",
        "mbr",
        "schema_json",
        "citation_json",
        "metadata_json",
        "summary_json",
    ):
        if key in result:
            result[key] = json.loads(result[key]) if result[key] else None
    if "schema_json" in result:
        result["schema"] = result.pop("schema_json")
    if "citation_json" in result:
        result["citation"] = result.pop("citation_json")
    if "visualization_url" in result:
        result["visualization"] = {
            "type": result.pop("visualization_type"),
            "url": result.pop("visualization_url"),
        }
    return result


def _schema_from_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    for attr in summary.get("attributes") or []:
        schema.append(
            {
                "name": attr.get("name"),
                "type": attr.get("type") or attr.get("role"),
                "description": attr.get("description"),
            }
        )
    for geom in summary.get("geometry") or []:
        schema.append(
            {
                "name": geom.get("name", "geometry"),
                "type": "geometry",
                "description": geom.get("description"),
                "geometry_types": sorted((geom.get("geom_types") or {}).keys()),
            }
        )
    return schema
