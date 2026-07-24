from __future__ import annotations

import ast
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

DATASET_STATES = {"created", "downloaded", "processed", "ready", "published", "error"}
PROCESSABLE_STATES = {"created", "downloaded", "processed", "ready", "error"}
DEFAULT_REPOSITORY_SHORT_NAME = "default"
DEFAULT_REPOSITORY_URL = "ucrstar://default"
GEOJSON_MAX_BYTES = 1_000_000
STYLE_SOURCE_ID = "dataset"
VECTOR_SOURCE_LAYER = "layer0"
CATEGORICAL_MIN_COVERAGE = 0.8
DATASET_STYLE_LAYER_TYPES = {
    "fill",
    "line",
    "circle",
    "symbol",
    "heatmap",
    "fill-extrusion",
}
CATEGORICAL_COLORS = [
    "#4477aa",
    "#ee6677",
    "#228833",
    "#ccbb44",
    "#66ccee",
    "#aa3377",
    "#bbbbbb",
    "#000000",
    "#e69f00",
    "#56b4e9",
    "#009e73",
    "#f0e442",
    "#0072b2",
    "#d55e00",
    "#cc79a7",
    "#332288",
    "#88ccee",
    "#44aa99",
    "#999933",
    "#882255",
]


def dataset_relative_path(name: str) -> Path:
    """Return a safe relative path for a logical dataset name."""
    validate_dataset_name(name)
    return Path(*name.split("/"))


def validate_dataset_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("dataset name is required")
    if name.startswith("/") or "\\" in name:
        raise ValueError(f"invalid dataset name: {name}")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"invalid dataset name: {name}")
    if any(any(ord(char) < 32 for char in part) for part in parts):
        raise ValueError(f"invalid dataset name: {name}")


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS datasets (
    id TEXT PRIMARY KEY,
    repository_id TEXT,
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
    source_type TEXT,
    source_url TEXT,
    source_accessed_at TEXT,
    source_modified_at TEXT,
    source_metadata_json TEXT,
    downloads_enabled INTEGER NOT NULL DEFAULT 1,
    dataset_state TEXT NOT NULL DEFAULT 'published',
    error_message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (repository_id) REFERENCES repositories(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name);
CREATE INDEX IF NOT EXISTS idx_datasets_repository_id ON datasets(repository_id);
CREATE INDEX IF NOT EXISTS idx_datasets_size ON datasets(size_bytes);

CREATE TABLE IF NOT EXISTS repositories (
    id TEXT PRIMARY KEY,
    short_name TEXT NOT NULL UNIQUE,
    url TEXT NOT NULL UNIQUE,
    description TEXT,
    repository_type TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_repositories_short_name ON repositories(short_name);

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
    "repository_id",
    "name",
    "description",
    "size_bytes",
    "num_features",
    "num_coordinates",
    "geometry_types",
    "mbr",
    "dataset_state",
    "error_message",
]


@dataclass(frozen=True)
class DatasetCatalog:
    db_path: Path
    datasets_dir: Path

    def __post_init__(self) -> None:
        """Normalize catalog paths after dataclass initialization."""
        object.__setattr__(self, "db_path", Path(self.db_path))
        object.__setattr__(self, "datasets_dir", Path(self.datasets_dir))

    def connect(self) -> sqlite3.Connection:
        """Open a SQLite connection configured to return rows by column name."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        """Create or migrate the catalog tables needed by the application."""
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            ensure_column(conn, "datasets", "repository_id", "TEXT")
            ensure_column(conn, "datasets", "style_json", "TEXT")
            ensure_column(conn, "datasets", "source_type", "TEXT")
            ensure_column(conn, "datasets", "source_url", "TEXT")
            ensure_column(conn, "datasets", "source_accessed_at", "TEXT")
            ensure_column(conn, "datasets", "source_modified_at", "TEXT")
            ensure_column(conn, "datasets", "source_metadata_json", "TEXT")
            ensure_column(conn, "datasets", "downloads_enabled", "INTEGER NOT NULL DEFAULT 1")
            ensure_column(conn, "datasets", "dataset_state", "TEXT NOT NULL DEFAULT 'published'")
            ensure_column(conn, "datasets", "error_message", "TEXT")
            self._ensure_default_repository(conn)

    def sync(self) -> list[dict[str, Any]]:
        """Read Starlet dataset directories and upsert their metadata into SQLite."""
        self.init_db()
        synced: list[dict[str, Any]] = []
        discovered_names = list(starlet.list_datasets(self.datasets_dir))

        with self.connect() as conn:
            default_repository = self.default_repository()
            known = {
                row["name"]: (row["id"], row["repository_id"])
                for row in conn.execute("SELECT id, repository_id, name FROM datasets")
            }
            names = set(discovered_names)
            for name in known:
                dataset_dir = self.datasets_dir / dataset_relative_path(name)
                if dataset_dir.exists():
                    names.add(name)
            sorted_names = sorted(names)
            LOGGER.info("Discovered %d dataset directorie(s) in %s", len(sorted_names), self.datasets_dir)
            for name in sorted_names:
                validate_dataset_name(name)
                existing = known.get(name)
                dataset_id = existing[0] if existing else str(uuid.uuid4())
                LOGGER.info("Reading Starlet metadata for dataset '%s'", name)
                row = self._build_row(dataset_id, name)
                row["repository_id"] = existing[1] if existing and existing[1] else default_repository["id"]
                self._upsert(conn, row)
                synced.append(row)
        return synced

    def list(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        """Return lightweight dataset rows that match text, size, geometry, and state filters."""
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

        state = filters.get("state")
        if state and state != "all":
            if state not in DATASET_STATES:
                raise ValueError(f"Unsupported dataset state: {state}")
            clauses.append("dataset_state = ?")
            values.append(state)

        repository = filters.get("repository")
        if repository:
            repository_row = self.get_repository(repository)
            if repository_row is None:
                return []
            clauses.append("repository_id = ?")
            values.append(repository_row["id"])

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

    def list_repositories(self) -> list[dict[str, Any]]:
        """Return repositories with lightweight dataset counts."""
        self.init_db()
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*,
                       COUNT(d.id) AS total_datasets
                FROM repositories r
                LEFT JOIN datasets d ON d.repository_id = r.id
                GROUP BY r.id
                ORDER BY r.is_default DESC, r.short_name
                """
            ).fetchall()
        return [decode_repository_row(row) for row in rows]

    def get_repository(self, repository_id_or_name: str) -> dict[str, Any] | None:
        """Fetch a repository by UUID, short name, or URL."""
        self.init_db()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM repositories WHERE id = ?",
                (repository_id_or_name,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM repositories WHERE short_name = ?",
                    (repository_id_or_name,),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM repositories WHERE url = ?",
                    (repository_id_or_name,),
                ).fetchone()
        return decode_repository_row(row) if row is not None else None

    def default_repository(self) -> dict[str, Any]:
        """Return the internal repository used for directly added datasets."""
        self.init_db()
        repository = self.get_repository(DEFAULT_REPOSITORY_SHORT_NAME)
        if repository is None:
            raise RuntimeError("Default repository could not be initialized")
        return repository

    def upsert_repository(
        self,
        short_name: str,
        url: str,
        *,
        description: str | None = None,
        repository_type: str = "repository",
        metadata: dict[str, Any] | None = None,
        is_default: bool = False,
    ) -> dict[str, Any]:
        """Insert or update a source repository and return its catalog row."""
        self.init_db()
        repository_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO repositories (
                    id, short_name, url, description, repository_type, is_default, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    short_name = excluded.short_name,
                    description = COALESCE(excluded.description, repositories.description),
                    repository_type = excluded.repository_type,
                    is_default = excluded.is_default,
                    metadata_json = excluded.metadata_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    repository_id,
                    short_name,
                    url,
                    description,
                    repository_type,
                    1 if is_default else 0,
                    json.dumps(metadata or {}),
                ),
            )
        repository = self.get_repository(url)
        if repository is None:
            raise RuntimeError(f"Repository was not stored: {url}")
        return repository

    def semantic_search(
        self,
        query: str,
        llm: Any,
        filters: dict[str, str],
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Rank embedded datasets by vector similarity to a natural-language query."""
        self.init_db()
        query_vector = llm.embed(query)
        provider, model = split_embedding_key(llm.embedding_key)
        effective_filters = dict(filters)
        if effective_filters.get("repository"):
            repository = self.get_repository(effective_filters["repository"])
            if repository is None:
                return []
            effective_filters["repository"] = repository["id"]

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
            if not matches_filters(dataset, effective_filters):
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
        """Fetch a full dataset row by UUID first, then by dataset name."""
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
        """Delete a dataset row and its embeddings, returning the deleted dataset."""
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        self.init_db()
        with self.connect() as conn:
            conn.execute("DELETE FROM dataset_embeddings WHERE dataset_id = ?", (dataset["id"],))
            conn.execute("DELETE FROM datasets WHERE id = ?", (dataset["id"],))
        return dataset

    def update_source(self, dataset_id_or_name: str, source: dict[str, Any]) -> dict[str, Any] | None:
        """Replace the stored source provenance for an existing dataset."""
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        self.init_db()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE datasets
                SET source_type = ?,
                    source_url = ?,
                    source_accessed_at = ?,
                    source_modified_at = ?,
                    source_metadata_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    source.get("type"),
                    source.get("url"),
                    source.get("accessed_at"),
                    source.get("modified_at"),
                    json.dumps(source.get("metadata") or {}),
                    dataset["id"],
                ),
            )
        return self.get(dataset["id"])

    def update_metadata(
        self, dataset_id_or_name: str, metadata: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Merge additional dataset metadata into metadata_json."""
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        merged = {**(dataset.get("metadata_json") or {}), **metadata}
        self.init_db()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE datasets
                SET metadata_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (json.dumps(merged), dataset["id"]),
            )
        return self.get(dataset["id"])

    def register_source(
        self,
        name: str,
        source: dict[str, Any],
        *,
        description: str | None = None,
        repository_id: str | None = None,
        overwrite: bool = False,
        downloads_enabled: bool = True,
    ) -> dict[str, Any]:
        """Register source metadata for a dataset before it has been processed."""
        self.init_db()
        if repository_id is None:
            repository_id = self.default_repository()["id"]
        existing = self.get(name)
        validate_dataset_name(name)
        if existing is not None and not overwrite:
            raise ValueError(f"Dataset already exists: {name}")

        if existing is not None:
            dataset_id = existing["id"]
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE datasets
                    SET description = ?,
                        repository_id = ?,
                        size_bytes = 0,
                        num_features = NULL,
                        num_coordinates = NULL,
                        geometry_types = '[]',
                        mbr = NULL,
                        schema_json = NULL,
                        citation_json = NULL,
                        visualization_type = NULL,
                        visualization_url = NULL,
                        style_json = NULL,
                        source_type = ?,
                        source_url = ?,
                        source_accessed_at = ?,
                        source_modified_at = ?,
                        source_metadata_json = ?,
                        downloads_enabled = ?,
                        dataset_state = 'created',
                        error_message = NULL,
                        metadata_json = '{}',
                        summary_json = '{}',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        description,
                        repository_id,
                        source.get("type"),
                        source.get("url"),
                        source.get("accessed_at"),
                        source.get("modified_at"),
                        json.dumps(source.get("metadata") or {}),
                        1 if downloads_enabled else 0,
                        dataset_id,
                    ),
                )
            return self.get(dataset_id) or existing

        dataset_id = str(uuid.uuid4())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO datasets (
                    id, repository_id, name, description, size_bytes, geometry_types,
                    source_type, source_url, source_accessed_at,
                    source_modified_at, source_metadata_json,
                    downloads_enabled,
                    dataset_state, error_message, metadata_json, summary_json
                )
                VALUES (?, ?, ?, ?, 0, '[]', ?, ?, ?, ?, ?, ?, 'created', NULL, '{}', '{}')
                """,
                (
                    dataset_id,
                    repository_id,
                    name,
                    description,
                    source.get("type"),
                    source.get("url"),
                    source.get("accessed_at"),
                    source.get("modified_at"),
                    json.dumps(source.get("metadata") or {}),
                    1 if downloads_enabled else 0,
                ),
            )
        return self.get(dataset_id) or {}

    def update_state(
        self,
        dataset_id_or_name: str,
        state: str,
        *,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        """Move a dataset to a lifecycle state and optionally store an error message."""
        if state not in DATASET_STATES:
            raise ValueError(f"Unsupported dataset state: {state}")
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        self.init_db()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE datasets
                SET dataset_state = ?,
                    error_message = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state, error_message, dataset["id"]),
            )
        return self.get(dataset["id"])

    def set_dataset_repository(
        self,
        dataset_id_or_name: str,
        repository_id: str,
    ) -> dict[str, Any] | None:
        """Attach an existing dataset to a repository."""
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        self.init_db()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE datasets
                SET repository_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (repository_id, dataset["id"]),
            )
        return self.get(dataset["id"])

    def processable(self, *, state: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """List datasets that are eligible for deferred processing."""
        self.init_db()
        if state:
            if state not in DATASET_STATES:
                raise ValueError(f"Unsupported dataset state: {state}")
            states = [state]
        else:
            states = sorted(PROCESSABLE_STATES)
        placeholders = ", ".join("?" for _ in states)
        sql = f"SELECT * FROM datasets WHERE dataset_state IN ({placeholders}) ORDER BY created_at, name"
        values: list[Any] = list(states)
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        with self.connect() as conn:
            return [decode_row(row) for row in conn.execute(sql, values)]

    def style(self, dataset_id_or_name: str) -> dict[str, Any] | None:
        """Return the complete MapLibre style document for a dataset."""
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        return normalize_style(dataset.get("style"), dataset.get("geometry_types"), dataset)

    def enrich(self, dataset_id_or_name: str, llm: Any) -> dict[str, Any] | None:
        """Use an LLM to improve descriptions, schema text, style, and embeddings."""
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
        style = normalize_style(
            enrichment.get("style"),
            dataset.get("geometry_types"),
            dataset,
        )

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

    @staticmethod
    def _ensure_default_repository(conn: sqlite3.Connection) -> None:
        """Create the internal repository and attach unassigned datasets to it."""
        repository_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO repositories (
                id, short_name, url, description, repository_type, is_default, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, 1, '{}')
            ON CONFLICT(url) DO UPDATE SET
                short_name = excluded.short_name,
                description = COALESCE(repositories.description, excluded.description),
                repository_type = excluded.repository_type,
                is_default = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                repository_id,
                DEFAULT_REPOSITORY_SHORT_NAME,
                DEFAULT_REPOSITORY_URL,
                "Datasets added directly to UCR Star.",
                "internal",
            ),
        )
        default_id = conn.execute(
            "SELECT id FROM repositories WHERE url = ?",
            (DEFAULT_REPOSITORY_URL,),
        ).fetchone()["id"]
        conn.execute(
            "UPDATE datasets SET repository_id = ? WHERE repository_id IS NULL",
            (default_id,),
        )

    def _build_row(self, dataset_id: str, name: str) -> dict[str, Any]:
        """Build a catalog row from Starlet metadata and summary files."""
        dataset_dir = self.datasets_dir / dataset_relative_path(name)
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
        size_bytes = int(metadata.get("size_bytes") or 0)
        use_geojson = size_bytes < GEOJSON_MAX_BYTES
        has_vector_tiles = bool(
            metadata.get("has_mvt")
            or metadata.get("has_pmtiles")
            or int(metadata.get("mvt_tile_count") or 0) > 0
        )
        visualization_type = (
            "GeoJSON" if use_geojson else "VectorTile" if has_vector_tiles else None
        )
        if visualization_type == "GeoJSON":
            visualization_url = f"/datasets/{dataset_id}/download.geojson"
        elif visualization_type == "VectorTile":
            visualization_url = f"/datasets/{dataset_id}/tiles" + "/{z}/{x}/{y}.mvt"
        else:
            visualization_url = None
        visualization = {
            "type": visualization_type,
            "url": visualization_url,
        }
        if "max_zoom" in metadata:
            visualization["max_zoom"] = metadata["max_zoom"]

        return {
            "id": dataset_id,
            "name": name,
            "description": summary.get("description"),
            "size_bytes": size_bytes,
            "num_features": num_features,
            "num_coordinates": num_coordinates,
            "geometry_types": sorted(geom_types),
            "mbr": mbr,
            "schema_json": schema,
            "citation_json": summary.get("citation"),
            "visualization_type": visualization_type,
            "visualization_url": visualization_url,
            "style_json": normalize_style(
                fallback_style(sorted(geom_types)),
                sorted(geom_types),
                {
                    "id": dataset_id,
                    "name": name,
                    "visualization": visualization,
                },
            ),
            "dataset_state": "published",
            "error_message": None,
            "metadata_json": metadata,
            "summary_json": summary,
        }

    @staticmethod
    def _upsert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        """Insert or update one dataset row while preserving source fields when possible."""
        existing = conn.execute(
            "SELECT metadata_json FROM datasets WHERE name = ?",
            (row["name"],),
        ).fetchone()
        if existing is not None:
            incoming_metadata = row.get("metadata_json") or {}
            existing_metadata = json.loads(existing["metadata_json"] or "{}")
            row["metadata_json"] = {**existing_metadata, **incoming_metadata}
        conn.execute(
            """
            INSERT INTO datasets (
                id, repository_id, name, description, size_bytes, num_features,
                num_coordinates, geometry_types, mbr, schema_json,
                citation_json, visualization_type, visualization_url,
                style_json, source_type, source_url, source_accessed_at,
                source_modified_at, source_metadata_json, dataset_state,
                error_message, metadata_json, summary_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                repository_id = COALESCE(excluded.repository_id, datasets.repository_id),
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
                source_type = COALESCE(excluded.source_type, datasets.source_type),
                source_url = COALESCE(excluded.source_url, datasets.source_url),
                source_accessed_at = COALESCE(
                    excluded.source_accessed_at,
                    datasets.source_accessed_at
                ),
                source_modified_at = COALESCE(
                    excluded.source_modified_at,
                    datasets.source_modified_at
                ),
                source_metadata_json = COALESCE(
                    excluded.source_metadata_json,
                    datasets.source_metadata_json
                ),
                dataset_state = excluded.dataset_state,
                error_message = excluded.error_message,
                metadata_json = excluded.metadata_json,
                summary_json = excluded.summary_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                row["id"],
                row.get("repository_id"),
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
                row.get("source_type"),
                row.get("source_url"),
                row.get("source_accessed_at"),
                row.get("source_modified_at"),
                json.dumps(row.get("source_metadata_json")) if row.get("source_metadata_json") else None,
                row["dataset_state"],
                row["error_message"],
                json.dumps(row["metadata_json"]),
                json.dumps(row["summary_json"]),
            ),
        )


def decode_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a SQLite row into the API-facing dataset dictionary shape."""
    result = dict(row)
    for key in (
        "geometry_types",
        "mbr",
        "schema_json",
        "citation_json",
        "style_json",
        "source_metadata_json",
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
        visualization_type = result.pop("visualization_type")
        visualization_url = result.pop("visualization_url")
        metadata = result.get("metadata_json") or {}
        result["visualization"] = visualization_metadata(
            visualization_type,
            visualization_url,
            max_zoom=metadata.get("max_zoom"),
        )
    if "style_json" in result:
        result["style"] = result.pop("style_json")
    if "downloads_enabled" in result:
        result["downloads_enabled"] = bool(result["downloads_enabled"])
    if "source_type" in result:
        result["source"] = {
            "type": result.pop("source_type"),
            "url": result.pop("source_url", None),
            "accessed_at": result.pop("source_accessed_at", None),
            "modified_at": result.pop("source_modified_at", None),
            "metadata": result.pop("source_metadata_json", None) or {},
        }
    result.pop("vector_json", None)
    return result


def decode_repository_row(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a repository SQLite row into the API-facing shape."""
    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json", "{}") or "{}")
    result["is_default"] = bool(result.get("is_default"))
    if "total_datasets" in result:
        result["total_datasets"] = int(result["total_datasets"] or 0)
    return result


def _schema_from_summary(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract catalog schema entries from Starlet summary attributes and geometry."""
    schema: list[dict[str, Any]] = []
    for attr in summary.get("attributes") or []:
        schema.append(
            {
                "name": attr.get("name"),
                "type": normalize_schema_type(attr.get("type") or attr.get("role")),
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


def normalize_schema_type(value: Any) -> Any:
    """Convert source-specific schema type names into consistent display values."""
    if not isinstance(value, str) or not value:
        return value
    type_name = value.removeprefix("esriFieldType")
    if type_name == value:
        return value
    return {
        "OID": "OID",
        "GlobalID": "GlobalID",
        "GUID": "GUID",
        "SmallInteger": "Integer",
        "Integer": "Integer",
        "Single": "Float",
        "Double": "Double",
        "String": "String",
        "Date": "Date",
        "Blob": "Blob",
        "Raster": "Raster",
        "Geometry": "Geometry",
        "XML": "XML",
    }.get(type_name, type_name)


def ensure_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    column_type: str,
) -> None:
    """Add a SQLite column during lightweight migrations when it is missing."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def matches_filters(dataset: dict[str, Any], filters: dict[str, str]) -> bool:
    """Check whether an in-memory dataset row satisfies REST query filters."""
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
    if filters.get("state") and filters["state"] != "all":
        if dataset.get("dataset_state") != filters["state"]:
            return False
    if filters.get("repository"):
        if dataset.get("repository_id") != filters["repository"]:
            return False
    return True


def cosine_distance(left: list[float], right: list[float]) -> float:
    """Return cosine distance between two embedding vectors."""
    if not left or not right or len(left) != len(right):
        return 1.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 1.0
    return 1.0 - (dot / (left_norm * right_norm))


def split_embedding_key(key: str) -> tuple[str, str]:
    """Split a provider:model embedding key into database columns."""
    provider, _, model = key.partition(":")
    return provider, model


def safe_enrichment(llm: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Run LLM enrichment and optionally fall back to no updates on failure."""
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
    """Merge generated attribute descriptions into existing schema entries."""
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


def visualization_metadata(
    visualization_type: Any,
    url: Any,
    max_zoom: int | None = None,
) -> dict[str, Any]:
    """Build the discriminated API object used by map clients."""
    result = {"type": visualization_type, "url": url}
    if visualization_type == "VectorTile":
        result.update(
            {
                "format": "mvt",
                "tiles": [url],
                "source_layer": VECTOR_SOURCE_LAYER,
            }
        )
        if max_zoom is not None:
            result["max_zoom"] = max_zoom
    elif visualization_type == "GeoJSON":
        result.update({"format": "geojson", "download_url": url})
    return result


def normalize_style(
    style: Any,
    geometry_types: list[str] | None,
    dataset: dict[str, Any] | None = None,
    *,
    enforce_inferred_category_safety: bool = True,
) -> dict[str, Any]:
    """Return a complete, dataset-bound MapLibre Style Specification document."""
    dataset = dataset or {}
    visualization = dataset.get("visualization") or {
        "type": "VectorTile",
        "url": "",
        "source_layer": VECTOR_SOURCE_LAYER,
    }
    source = maplibre_source(visualization)
    paints = fallback_style(geometry_types).get("layers", {})

    if isinstance(style, dict) and isinstance(style.get("layers"), dict):
        for layer_type in ("fill", "line", "circle"):
            paint = normalize_layer_paint(style["layers"].get(layer_type), layer_type)
            if paint:
                paints[layer_type].update(paint)

    layers = style.get("layers") if isinstance(style, dict) else None
    if isinstance(layers, list):
        normalized_layers = [
            normalize_maplibre_layer(layer, visualization)
            for layer in layers
            if isinstance(layer, dict)
            and layer.get("type") in DATASET_STYLE_LAYER_TYPES
        ]
        normalized_layers = [layer for layer in normalized_layers if layer]
    else:
        normalized_layers = default_maplibre_layers(paints, visualization)

    metadata = dict(style.get("metadata") or {}) if isinstance(style, dict) else {}
    if enforce_inferred_category_safety:
        repair_cosmetic_categorical_style(normalized_layers, metadata, dataset)
        reject_low_coverage_categorical_styles(
            normalized_layers,
            metadata,
            dataset,
            paints,
        )
    return {
        "version": 8,
        "name": str(style.get("name") or dataset.get("name") or "UCR Star dataset")
        if isinstance(style, dict)
        else str(dataset.get("name") or "UCR Star dataset"),
        "metadata": metadata,
        "sources": {STYLE_SOURCE_ID: source},
        "layers": normalized_layers or default_maplibre_layers(paints, visualization),
    }


def repair_cosmetic_categorical_style(
    layers: list[dict[str, Any]],
    metadata: dict[str, Any],
    dataset: dict[str, Any],
) -> None:
    """Replace categories based on color-storage fields with semantic categories."""
    cosmetic_property = None
    for layer in layers:
        for property_name in ("fill-color", "line-color", "circle-color"):
            expression = (layer.get("paint") or {}).get(property_name)
            candidate = categorical_expression_property(expression)
            if candidate and is_cosmetic_color_field(candidate):
                cosmetic_property = candidate
                break
        if cosmetic_property:
            break
    if not cosmetic_property:
        return

    category = semantic_category(dataset, excluded={cosmetic_property})
    if category is None:
        return
    field, values = category
    expression: list[Any] = ["match", ["get", field]]
    for index, value in enumerate(values):
        expression.extend([value, CATEGORICAL_COLORS[index % len(CATEGORICAL_COLORS)]])
    expression.append("#bdbdbd")

    for layer in layers:
        paint = layer.get("paint") or {}
        for property_name in ("fill-color", "line-color", "circle-color"):
            current = paint.get(property_name)
            if categorical_expression_property(current) == cosmetic_property:
                paint[property_name] = expression
    metadata["ucrstar:legend"] = {
        "type": "categorical",
        "property": field,
        "labels": {str(value): str(value) for value in values},
    }


def categorical_expression_property(expression: Any) -> str | None:
    if not isinstance(expression, list) or len(expression) < 3 or expression[0] != "match":
        return None
    input_expression = expression[1]
    if (
        isinstance(input_expression, list)
        and len(input_expression) >= 2
        and input_expression[0] == "get"
        and isinstance(input_expression[1], str)
    ):
        return input_expression[1]
    return None


def is_cosmetic_color_field(field: str) -> bool:
    normalized = field.lower().replace("-", "_")
    return any(token in normalized for token in ("color", "colour", "rgb", "hex"))


def semantic_category(
    dataset: dict[str, Any], excluded: set[str]
) -> tuple[str, list[Any]] | None:
    summary = dataset.get("summary_json") or {}
    schema = {field.get("name"): field for field in dataset.get("schema") or []}
    candidates = []
    for attribute in summary.get("attributes") or []:
        name = attribute.get("name")
        values = [entry.get("value") for entry in attribute.get("top_k") or []]
        values = [value for value in values if value is not None]
        distinct = int(attribute.get("approx_distinct") or len(values))
        if not name or name in excluded or not values or distinct < 2 or distinct > 30:
            continue
        searchable = " ".join(
            str(value).lower()
            for value in (
                name,
                attribute.get("description"),
                (schema.get(name) or {}).get("description"),
            )
            if value
        )
        if any(token in searchable for token in ("url", "address", "phone")):
            continue
        score = 0
        for token, weight in (
            ("name", 8),
            ("office", 7),
            ("category", 6),
            ("class", 5),
            ("type", 4),
            ("label", 4),
            ("status", 3),
            ("code", 1),
        ):
            if token in searchable:
                score += weight
        score += max(0, 30 - distinct) / 30
        candidates.append((score, name, values))
    if not candidates:
        return None
    _score, name, values = max(candidates, key=lambda candidate: candidate[0])
    return name, values


def reject_low_coverage_categorical_styles(
    layers: list[dict[str, Any]],
    metadata: dict[str, Any],
    dataset: dict[str, Any],
    fallback_paints: dict[str, Any],
) -> None:
    """Remove inferred categorical colors unless they demonstrably cover 80% of records."""
    rejected_properties: set[str] = set()
    for layer in layers:
        layer_type = layer.get("type")
        paint = layer.get("paint") or {}
        color_property = {
            "fill": "fill-color",
            "line": "line-color",
            "circle": "circle-color",
        }.get(layer_type)
        if not color_property:
            continue
        expression = paint.get(color_property)
        styled_paths = categorical_expression_paths(expression)
        if not styled_paths:
            continue
        coverage = categorical_path_coverage(dataset, styled_paths)
        if coverage is None or coverage < CATEGORICAL_MIN_COVERAGE:
            paint[color_property] = fallback_paints[layer_type][color_property]
            for path in styled_paths:
                rejected_properties.update(path)

    legend = metadata.get("ucrstar:legend")
    if isinstance(legend, dict) and legend.get("property") in rejected_properties:
        metadata.pop("ucrstar:legend", None)


def categorical_expression_values(expression: Any) -> set[Any]:
    if not isinstance(expression, list) or expression[0] != "match":
        return set()
    values: set[Any] = set()
    for index in range(2, len(expression) - 1, 2):
        value = expression[index]
        if isinstance(value, list):
            values.update(item for item in value if isinstance(item, str | int | float | bool))
        elif isinstance(value, str | int | float | bool):
            values.add(value)
    return values


def categorical_expression_paths(expression: Any) -> list[dict[str, Any]]:
    property_name = categorical_expression_property(expression)
    if not property_name:
        return []
    return match_expression_paths(expression, {}, property_name)


def match_expression_paths(
    expression: list[Any], parent_path: dict[str, Any], property_name: str
) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for index in range(2, len(expression) - 1, 2):
        value = expression[index]
        output = expression[index + 1]
        values = value if isinstance(value, list) else [value]
        for option in values:
            if not isinstance(option, str | int | float | bool):
                continue
            path = {**parent_path, property_name: option}
            if isinstance(output, str):
                paths.append(path)
            elif isinstance(output, list) and output and output[0] == "match":
                nested_property = categorical_expression_property(output)
                if nested_property:
                    paths.extend(match_expression_paths(output, path, nested_property))
    return paths


def categorical_path_coverage(
    dataset: dict[str, Any], styled_paths: list[dict[str, Any]]
) -> float | None:
    if not styled_paths:
        return None
    property_names = {name for path in styled_paths for name in path}
    if len(property_names) == 1:
        property_name = next(iter(property_names))
        categories = {path[property_name] for path in styled_paths}
        return categorical_coverage(dataset, property_name, categories)

    total_records = int(dataset.get("num_features") or 0)
    if total_records <= 0:
        return None
    attributes = (dataset.get("summary_json") or {}).get("attributes") or []
    covered = 0
    found_properties = False
    for attribute in attributes:
        for entry in attribute.get("top_k") or []:
            properties = structured_properties(entry.get("value"))
            if not properties:
                continue
            if property_names.issubset(properties):
                found_properties = True
            if any(
                all(properties.get(name) == expected for name, expected in path.items())
                for path in styled_paths
            ):
                covered += int(entry.get("count") or 0)
    return covered / total_records if found_properties else None


def categorical_coverage(
    dataset: dict[str, Any], property_name: str, categories: set[Any]
) -> float | None:
    total_records = int(dataset.get("num_features") or 0)
    if total_records <= 0 or not categories:
        return None
    attributes = (dataset.get("summary_json") or {}).get("attributes") or []

    direct = next(
        (attribute for attribute in attributes if attribute.get("name") == property_name),
        None,
    )
    if direct is not None:
        covered = sum(
            int(entry.get("count") or 0)
            for entry in direct.get("top_k") or []
            if entry.get("value") in categories
        )
        return covered / total_records

    covered = 0
    found_property = False
    for attribute in attributes:
        for entry in attribute.get("top_k") or []:
            mapped_value = structured_property_value(entry.get("value"), property_name)
            if mapped_value is None:
                continue
            found_property = True
            if mapped_value in categories:
                covered += int(entry.get("count") or 0)
    return covered / total_records if found_property else None


def structured_property_value(value: Any, property_name: str) -> Any:
    return structured_properties(value).get(property_name)


def structured_properties(value: Any) -> dict[str, Any]:
    structured = value
    if isinstance(value, str):
        try:
            structured = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return {}
    if isinstance(structured, dict):
        return structured
    if isinstance(structured, list):
        properties = {}
        for item in structured:
            if isinstance(item, tuple | list) and len(item) == 2 and isinstance(item[0], str):
                properties[item[0]] = item[1]
        return properties
    return {}


def maplibre_source(visualization: dict[str, Any]) -> dict[str, Any]:
    if visualization.get("type") == "GeoJSON":
        return {"type": "geojson", "data": visualization.get("url") or ""}
    source = {
        "type": "vector",
        "tiles": [visualization.get("url") or ""],
    }
    if "max_zoom" in visualization:
        source["maxzoom"] = visualization["max_zoom"]
    return source


def normalize_maplibre_layer(
    layer: dict[str, Any], visualization: dict[str, Any]
) -> dict[str, Any] | None:
    layer_type = layer.get("type")
    layer_id = layer.get("id")
    if not isinstance(layer_id, str) or not layer_id:
        return None
    normalized = {"id": layer_id, "type": layer_type, "source": STYLE_SOURCE_ID}
    if visualization.get("type") == "VectorTile":
        normalized["source-layer"] = visualization.get("source_layer") or VECTOR_SOURCE_LAYER
    for key in ("minzoom", "maxzoom", "filter", "layout"):
        if key in layer:
            normalized[key] = layer[key]
    if layer_type in DATASET_STYLE_LAYER_TYPES:
        normalized["paint"] = normalize_layer_paint(layer.get("paint"), layer_type)
    return normalized


def default_maplibre_layers(
    paints: dict[str, Any], visualization: dict[str, Any]
) -> list[dict[str, Any]]:
    source_layer = (
        {"source-layer": visualization.get("source_layer") or VECTOR_SOURCE_LAYER}
        if visualization.get("type") == "VectorTile"
        else {}
    )
    common = {"source": STYLE_SOURCE_ID, **source_layer}
    return [
        {
            "id": "fill",
            "type": "fill",
            **common,
            "filter": ["==", ["geometry-type"], "Polygon"],
            "paint": paints["fill"],
        },
        {
            "id": "outline",
            "type": "line",
            **common,
            "filter": ["==", ["geometry-type"], "Polygon"],
            "paint": paints["line"],
        },
        {
            "id": "lines",
            "type": "line",
            **common,
            "filter": ["==", ["geometry-type"], "LineString"],
            "paint": paints["line"],
        },
        {
            "id": "points",
            "type": "circle",
            **common,
            "filter": ["==", ["geometry-type"], "Point"],
            "paint": paints["circle"],
        },
    ]


def normalize_layer_paint(layer_style: Any, layer_type: str) -> dict[str, Any]:
    """Flatten and filter paint properties for a single MapLibre layer type."""
    if not isinstance(layer_style, dict):
        return {}

    paint: dict[str, Any] = {}
    for key, value in layer_style.items():
        if is_supported_paint_property(key, value, layer_type):
            paint[key] = value

    nested_paint = layer_style.get("paint")
    if isinstance(nested_paint, dict):
        for key, value in nested_paint.items():
            if is_supported_paint_property(key, value, layer_type):
                paint[key] = value
    return paint


def is_supported_paint_property(key: Any, value: Any, layer_type: str) -> bool:
    """Return whether a generated paint property is safe to keep."""
    prefixes = (
        ("text-", "icon-")
        if layer_type == "symbol"
        else (f"{layer_type}-",)
    )
    return (
        isinstance(key, str)
        and key.startswith(prefixes)
        and isinstance(value, str | int | float | bool | list | dict)
    )


def enrichment_payload(dataset: dict[str, Any]) -> dict[str, Any]:
    """Build the compact dataset payload sent to the LLM enrichment step."""
    summary = dataset.get("summary_json") or {}
    payload = {
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
    source_metadata = (dataset.get("source") or {}).get("metadata") or {}
    hub_metadata = source_metadata.get("hub") or {}
    layer_metadata = hub_metadata.get("layer") or source_metadata.get("layer") or {}
    renderer = (layer_metadata.get("drawingInfo") or {}).get("renderer")
    if isinstance(renderer, dict):
        payload["source_style"] = {"format": "esri-renderer", "renderer": renderer}
    return payload


def embedding_text(dataset: dict[str, Any]) -> str:
    """Build the searchable text used to generate a dataset embedding."""
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
