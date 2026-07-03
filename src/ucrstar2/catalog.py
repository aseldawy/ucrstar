from __future__ import annotations

import json
import logging
import math
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import starlet

from .llm import fallback_style

LOGGER = logging.getLogger(__name__)


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
    style_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name);
CREATE INDEX IF NOT EXISTS idx_datasets_size ON datasets(size_bytes);

CREATE TABLE IF NOT EXISTS dataset_embeddings (
    dataset_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dataset_id, provider, model),
    FOREIGN KEY (dataset_id) REFERENCES datasets(id) ON DELETE CASCADE
);
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
            ensure_column(conn, "datasets", "style_json", "TEXT")

    def sync(self) -> list[dict[str, Any]]:
        self.init_db()
        synced: list[dict[str, Any]] = []
        names = starlet.list_datasets(self.datasets_dir)
        LOGGER.info("Discovered %d dataset directorie(s) in %s", len(names), self.datasets_dir)

        with self.connect() as conn:
            known = {
                row["name"]: row["id"]
                for row in conn.execute("SELECT id, name FROM datasets")
            }
            for name in names:
                dataset_id = known.get(name, str(uuid.uuid4()))
                LOGGER.info("Reading Starlet metadata for dataset '%s'", name)
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

    def semantic_search(
        self,
        query: str,
        llm: Any,
        filters: dict[str, str],
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.init_db()
        query_vector = llm.embed(query)
        provider, model = split_embedding_key(llm.embedding_key)

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT d.*, e.vector_json
                FROM datasets d
                JOIN dataset_embeddings e ON e.dataset_id = d.id
                WHERE e.provider = ? AND e.model = ?
                """,
                (provider, model),
            ).fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            dataset = decode_row(row)
            if not matches_filters(dataset, filters):
                continue
            vector = json.loads(row["vector_json"])
            scored.append((cosine_distance(query_vector, vector), dataset))

        scored.sort(key=lambda item: item[0])
        results: list[dict[str, Any]] = []
        for distance, dataset in scored[:limit]:
            dataset["search_score"] = distance
            results.append(dataset)
        return results

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

    def delete(self, dataset_id_or_name: str) -> dict[str, Any] | None:
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        self.init_db()
        with self.connect() as conn:
            conn.execute("DELETE FROM dataset_embeddings WHERE dataset_id = ?", (dataset["id"],))
            conn.execute("DELETE FROM datasets WHERE id = ?", (dataset["id"],))
        return dataset

    def style(self, dataset_id_or_name: str) -> dict[str, Any] | None:
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        return dataset.get("style") or fallback_style(dataset.get("geometry_types"))

    def enrich(self, dataset_id_or_name: str, llm: Any) -> dict[str, Any] | None:
        self.init_db()
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None

        summary = dataset.get("summary_json") or {}
        payload = enrichment_payload(dataset)
        LOGGER.info("Generating LLM enrichment for dataset '%s'", dataset["name"])
        enrichment = safe_enrichment(llm, payload)
        if enrichment:
            LOGGER.info("LLM enrichment returned fields: %s", ", ".join(sorted(enrichment)))
        else:
            LOGGER.info("LLM enrichment returned no updates for dataset '%s'", dataset["name"])
        description = enrichment.get("description") or dataset.get("description")
        schema = merge_attribute_descriptions(
            dataset.get("schema") or [],
            enrichment.get("attributes") or {},
        )
        style = normalize_style(enrichment.get("style"), dataset.get("geometry_types"))

        search_text = embedding_text(
            {
                **dataset,
                "description": description,
                "schema": schema,
                "style": style,
                "summary_json": summary,
            }
        )

        with self.connect() as conn:
            conn.execute(
                """
                UPDATE datasets
                SET description = ?, schema_json = ?, style_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    description,
                    json.dumps(schema),
                    json.dumps(style),
                    dataset["id"],
                ),
            )
            try:
                LOGGER.info(
                    "Creating dataset embedding with %s",
                    getattr(llm, "embedding_key", "unknown embedding model"),
                )
                vector = llm.embed(search_text)
            except Exception:
                LOGGER.exception("LLM embedding failed for dataset '%s'", dataset["name"])
                vector = []
            if vector:
                provider, model = split_embedding_key(llm.embedding_key)
                conn.execute(
                    """
                    INSERT INTO dataset_embeddings (
                        dataset_id, provider, model, dimensions, vector_json, text
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset_id, provider, model) DO UPDATE SET
                        dimensions = excluded.dimensions,
                        vector_json = excluded.vector_json,
                        text = excluded.text,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        dataset["id"],
                        provider,
                        model,
                        len(vector),
                        json.dumps(vector),
                        search_text,
                    ),
                )
                LOGGER.info("Stored %d-dimensional dataset embedding", len(vector))
        return self.get(dataset["id"])

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
            "style_json": fallback_style(sorted(geom_types)),
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
                style_json, metadata_json, summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description = COALESCE(datasets.description, excluded.description),
                size_bytes = excluded.size_bytes,
                num_features = excluded.num_features,
                num_coordinates = excluded.num_coordinates,
                geometry_types = excluded.geometry_types,
                mbr = excluded.mbr,
                schema_json = COALESCE(datasets.schema_json, excluded.schema_json),
                citation_json = excluded.citation_json,
                visualization_type = excluded.visualization_type,
                visualization_url = excluded.visualization_url,
                style_json = COALESCE(datasets.style_json, excluded.style_json),
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
                json.dumps(row["style_json"]),
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
        "style_json",
        "metadata_json",
        "summary_json",
        "vector_json",
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
    if "style_json" in result:
        result["style"] = result.pop("style_json")
    result.pop("vector_json", None)
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


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def matches_filters(dataset: dict[str, Any], filters: dict[str, str]) -> bool:
    if filters.get("name") and filters["name"].lower() not in dataset["name"].lower():
        return False
    if filters.get("description"):
        description = (dataset.get("description") or "").lower()
        if filters["description"].lower() not in description:
            return False
    if filters.get("geometry_type"):
        geometry_types = [value.lower() for value in dataset.get("geometry_types") or []]
        if filters["geometry_type"].lower() not in geometry_types:
            return False
    if filters.get("min_size") and dataset.get("size_bytes", 0) < int(filters["min_size"]):
        return False
    if filters.get("max_size") and dataset.get("size_bytes", 0) > int(filters["max_size"]):
        return False
    return True


def cosine_distance(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 1.0
    return 1.0 - (dot / (left_norm * right_norm))


def split_embedding_key(key: str) -> tuple[str, str]:
    provider, _, model = key.partition(":")
    return provider, model


def safe_enrichment(llm: Any, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return llm.enrich_dataset(payload) or {}
    except Exception:
        LOGGER.exception(
            "LLM enrichment failed with provider=%s chat_model=%s",
            getattr(llm, "provider", "unknown"),
            getattr(llm, "chat_model", "unknown"),
        )
        if getattr(getattr(llm, "settings", None), "fallback_on_error", True):
            return {}
        raise


def merge_attribute_descriptions(
    schema: list[dict[str, Any]],
    descriptions: dict[str, Any],
) -> list[dict[str, Any]]:
    merged = []
    for field in schema:
        updated = dict(field)
        name = updated.get("name")
        if name in descriptions:
            value = descriptions[name]
            updated["description"] = (
                value.get("description") if isinstance(value, dict) else str(value)
            )
        merged.append(updated)
    return merged


def normalize_style(style: Any, geometry_types: list[str] | None) -> dict[str, Any]:
    base = fallback_style(geometry_types)
    if not isinstance(style, dict):
        return base
    if isinstance(style.get("layers"), dict):
        for layer_type in ("fill", "line", "circle"):
            paint = style["layers"].get(layer_type)
            if isinstance(paint, dict):
                base["layers"][layer_type].update(paint)
    if style.get("source_layer"):
        base["source_layer"] = style["source_layer"]
    return base


def enrichment_payload(dataset: dict[str, Any]) -> dict[str, Any]:
    summary = dataset.get("summary_json") or {}
    return {
        "id": dataset.get("id"),
        "name": dataset.get("name"),
        "description": dataset.get("description"),
        "size_bytes": dataset.get("size_bytes"),
        "num_features": dataset.get("num_features"),
        "num_coordinates": dataset.get("num_coordinates"),
        "geometry_types": dataset.get("geometry_types"),
        "mbr": dataset.get("mbr"),
        "schema": dataset.get("schema"),
        "summary": {
            "attributes": summary.get("attributes", []),
            "geometry": summary.get("geometry", []),
        },
    }


def embedding_text(dataset: dict[str, Any]) -> str:
    schema_parts = []
    for field in dataset.get("schema") or []:
        schema_parts.append(
            " ".join(
                str(value)
                for value in (
                    field.get("name"),
                    field.get("type"),
                    field.get("description"),
                )
                if value
            )
        )
    return "\n".join(
        str(value)
        for value in (
            dataset.get("name"),
            dataset.get("description"),
            " ".join(dataset.get("geometry_types") or []),
            " ".join(schema_parts),
            json.dumps(dataset.get("summary_json") or {}, separators=(",", ":")),
        )
        if value
    )
