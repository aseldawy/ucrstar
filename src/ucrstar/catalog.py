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

DATASET_STATES = {"created", "downloaded", "processed", "ready", "published", "error"}
PROCESSABLE_STATES = {"created", "downloaded", "processed", "ready", "error"}
DEFAULT_REPOSITORY_SHORT_NAME = "default"
DEFAULT_REPOSITORY_URL = "ucrstar://default"


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
            ensure_column(conn, "datasets", "dataset_state", "TEXT NOT NULL DEFAULT 'published'")
            ensure_column(conn, "datasets", "error_message", "TEXT")
            self._ensure_default_repository(conn)

    def sync(self) -> list[dict[str, Any]]:
        """Read Starlet dataset directories and upsert their metadata into SQLite."""
        self.init_db()
        synced: list[dict[str, Any]] = []
        names = starlet.list_datasets(self.datasets_dir)
        LOGGER.info("Discovered %d dataset directorie(s) in %s", len(names), self.datasets_dir)

        with self.connect() as conn:
            default_repository = self.default_repository()
            known = {
                row["name"]: (row["id"], row["repository_id"])
                for row in conn.execute("SELECT id, repository_id, name FROM datasets")
            }
            for name in names:
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

    def register_source(
        self,
        name: str,
        source: dict[str, Any],
        *,
        description: str | None = None,
        repository_id: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Register source metadata for a dataset before it has been processed."""
        self.init_db()
        if repository_id is None:
            repository_id = self.default_repository()["id"]
        existing = self.get(name)
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
                    dataset_state, error_message, metadata_json, summary_json
                )
                VALUES (?, ?, ?, ?, 0, '[]', ?, ?, ?, ?, ?, 'created', NULL, '{}', '{}')
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
        """Return the normalized MapLibre style for a dataset."""
        dataset = self.get(dataset_id_or_name)
        if dataset is None:
            return None
        return normalize_style(dataset.get("style"), dataset.get("geometry_types"))

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
            "dataset_state": "published",
            "error_message": None,
            "metadata_json": metadata,
            "summary_json": summary,
        }

    @staticmethod
    def _upsert(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        """Insert or update one dataset row while preserving source fields when possible."""
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
        result["visualization"] = {
            "type": result.pop("visualization_type"),
            "url": result.pop("visualization_url"),
        }
    if "style_json" in result:
        result["style"] = result.pop("style_json")
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


def normalize_style(style: Any, geometry_types: list[str] | None) -> dict[str, Any]:
    """Merge a partial/generated style with the default style for the geometry."""
    base = fallback_style(geometry_types)
    if not isinstance(style, dict):
        return base
    if isinstance(style.get("layers"), dict):
        for layer_type in ("fill", "line", "circle"):
            paint = normalize_layer_paint(style["layers"].get(layer_type), layer_type)
            if paint:
                base["layers"][layer_type].update(paint)
    return base


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
    return (
        isinstance(key, str)
        and key.startswith(f"{layer_type}-")
        and isinstance(value, str | int | float | bool | list | dict)
    )


def enrichment_payload(dataset: dict[str, Any]) -> dict[str, Any]:
    """Build the compact dataset payload sent to the LLM enrichment step."""
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
