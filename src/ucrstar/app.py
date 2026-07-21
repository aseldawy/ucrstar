from __future__ import annotations

import csv
import io
import json
import math
import struct
import zlib
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import starlet
from flask import (
    Flask,
    Response,
    abort,
    current_app,
    jsonify,
    request,
    send_from_directory,
    stream_with_context,
)
from werkzeug.serving import WSGIRequestHandler

from .catalog import DatasetCatalog
from .config import load_config
from .llm import llm_from_config


DEFAULT_BATCH_SIZE = 1000


class TimingWSGIRequestHandler(WSGIRequestHandler):
    """Werkzeug request handler that appends request duration to access logs."""

    def handle(self) -> None:
        self._request_start = time.perf_counter()
        super().handle()

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        elapsed_ms = (time.perf_counter() - getattr(self, "_request_start", time.perf_counter())) * 1000
        self.log(
            "info",
            '%s - - [%s] "%s" %s %s %.2fms',
            self.address_string(),
            self.log_date_time_string(),
            self.requestline,
            code,
            size,
            elapsed_ms,
        )


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATASETS_DIR=Path("datasets"),
        DATABASE=Path("instance/ucrstar.sqlite"),
        QUERY_BATCH_SIZE=DEFAULT_BATCH_SIZE,
        UCRSTAR2_CONFIG=load_config(),
    )
    if config:
        app.config.update(config)
    app.extensions["ucrstar_catalog"] = DatasetCatalog(
        Path(app.config["DATABASE"]),
        Path(app.config["DATASETS_DIR"]),
    )

    @app.get("/")
    def index() -> Response:
        if not current_app.debug:
            abort(404)
        return send_from_directory(app.static_folder, "index.html")

    @app.errorhandler(ValueError)
    def bad_request(error: ValueError) -> tuple[Response, int]:
        return jsonify({"error": str(error)}), 400

    @app.get("/health.json")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @app.get("/datasets.json")
    def datasets() -> Response:
        catalog().sync()
        filters = {
            key: value
            for key, value in request.args.items()
            if key
            in {
                "q",
                "name",
                "description",
                "geometry_type",
                "min_size",
                "max_size",
                "state",
                "repository",
            }
        }
        filters.setdefault("state", "published")
        if request.args.get("semantic") == "1" and filters.get("q"):
            llm = llm_client()
            llm_config = current_app.config["UCRSTAR2_CONFIG"].get("llm", {})
            if getattr(llm, "enabled", False) and llm_config.get("semantic_search", True):
                try:
                    results = catalog().semantic_search(
                        filters["q"],
                        llm,
                        filters,
                        limit=int(llm_config.get("search_limit", 20)),
                    )
                    if results:
                        return jsonify({"datasets": [redact_source(dataset) for dataset in results], "search": "semantic"})
                except Exception:
                    if not llm_config.get("fallback_on_error", True):
                        raise
        return jsonify({"datasets": [redact_source(dataset) for dataset in catalog().list(filters)]})

    @app.get("/repositories.json")
    def repositories() -> Response:
        catalog().sync()
        return jsonify(
            {
                "repositories": [
                    public_repository(repository)
                    for repository in catalog().list_repositories()
                ]
            }
        )

    @app.get("/repositories/<repository_ref>/datasets.json")
    def repository_datasets(repository_ref: str) -> Response:
        catalog().sync()
        repository = catalog().get_repository(repository_ref)
        if repository is None:
            return jsonify({"error": "repository not found"}), 404
        filters = {
            key: value
            for key, value in request.args.items()
            if key
            in {
                "q",
                "name",
                "description",
                "geometry_type",
                "min_size",
                "max_size",
                "state",
            }
        }
        filters.setdefault("state", "published")
        filters["repository"] = repository["id"]
        return jsonify(
            {
                "repository": public_repository(repository),
                "datasets": [redact_source(dataset) for dataset in catalog().list(filters)],
            }
        )

    @app.get("/datasets/<dataset_ref>.json")
    def dataset_details(dataset_ref: str) -> Response:
        dataset = catalog().get(dataset_ref)
        if dataset is None:
            return jsonify({"error": "dataset not found"}), 404
        return jsonify(redact_source(dataset))

    @app.get("/datasets/<dataset_ref>/style.json")
    def dataset_style(dataset_ref: str) -> Response:
        style = catalog().style(dataset_ref)
        if style is None:
            return jsonify({"error": "dataset not found"}), 404
        return jsonify(style)

    @app.get("/datasets/<dataset_ref>/histogram.png")
    def dataset_histogram(dataset_ref: str) -> Response:
        dataset = require_dataset(dataset_ref)
        if isinstance(dataset, Response):
            return dataset

        size = int(request.args.get("size", "256"))
        if size < 32 or size > 1024:
            return jsonify({"error": "size must be between 32 and 1024"}), 400

        histogram_path = dataset_path(dataset["name"]) / "histograms" / "global.npy"
        if not histogram_path.exists():
            return jsonify({"error": "histogram not found"}), 404

        png = histogram_png(histogram_path, size)
        return Response(
            png,
            mimetype="image/png",
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.route("/datasets/<dataset_ref>/download.<fmt>", methods=["GET", "POST"])
    def download(dataset_ref: str, fmt: str) -> Response:
        dataset = require_dataset(dataset_ref)
        if isinstance(dataset, Response):
            return dataset
        geometry = request_geometry()
        dataset_dir = dataset_path(dataset["name"])

        if fmt == "geojson":
            return Response(
                stream_with_context(iter_geojson(dataset_dir, geometry)),
                mimetype="application/geo+json",
                headers={"Content-Disposition": f"attachment; filename={dataset['name']}.geojson"},
            )
        if fmt == "csv":
            return Response(
                stream_with_context(iter_csv(dataset_dir, geometry)),
                mimetype="text/csv",
                headers={"Content-Disposition": f"attachment; filename={dataset['name']}.csv"},
            )
        if fmt in {"parquet", "zip"}:
            return jsonify(
                {
                    "error": f"streaming .{fmt} exports are not implemented",
                    "detail": (
                        "This endpoint avoids materializing the full query result before "
                        "responding. GeoJSON and CSV stream batch-by-batch; Parquet and "
                        "zipped shapefile need a format-specific streaming writer."
                    ),
                }
            ), 501
        return jsonify({"error": f"unsupported format: {fmt}"}), 400

    @app.get("/datasets/<dataset_ref>/sample.<fmt>")
    def sample(dataset_ref: str, fmt: str) -> Response:
        dataset = require_dataset(dataset_ref)
        if isinstance(dataset, Response):
            return dataset
        geometry = request_geometry()
        if geometry is None:
            return jsonify({"error": "sample requires MBR=x1,y1,x2,y2"}), 400

        record = starlet.get_sample_record(dataset_path(dataset["name"]), geometry)
        if record is None:
            return jsonify({"error": "no matching record"}), 404
        if fmt == "json":
            return jsonify(strip_geometry(record))
        if fmt == "geojson":
            return jsonify(to_feature(record))
        return jsonify({"error": f"unsupported sample format: {fmt}"}), 400

    @app.get("/datasets/<dataset_ref>/tiles/<int:z>/<int:x>/<int:y>.mvt")
    def tile(dataset_ref: str, z: int, x: int, y: int) -> Response:
        dataset = require_dataset(dataset_ref)
        if isinstance(dataset, Response):
            return dataset
        data = starlet.get_tile(dataset_path(dataset["name"]), z, x, y)
        return Response(data, mimetype="application/vnd.mapbox-vector-tile")

    @app.get("/<path:filename>")
    def debug_static_file(filename: str) -> Response:
        if not current_app.debug:
            abort(404)
        static_root = Path(app.static_folder).resolve()
        static_path = (static_root / filename).resolve()
        try:
            static_path.relative_to(static_root)
        except ValueError:
            abort(404)
        if not static_path.is_file():
            abort(404)
        return send_from_directory(app.static_folder, filename)

    return app


def catalog() -> DatasetCatalog:
    return current_app.extensions["ucrstar_catalog"]


def llm_client() -> Any:
    return llm_from_config(current_app.config["UCRSTAR2_CONFIG"])


def dataset_path(name: str) -> Path:
    return Path(current_app.config["DATASETS_DIR"]) / name


def require_dataset(dataset_ref: str) -> dict[str, Any] | Response:
    dataset = catalog().get(dataset_ref)
    if dataset is None:
        return jsonify({"error": "dataset not found"}), 404
    return dataset


def request_geometry() -> tuple[float, float, float, float] | dict[str, Any] | None:
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if payload:
            return payload
    raw = request.args.get("MBR") or request.args.get("bbox")
    if not raw:
        return None
    try:
        parts = [float(value) for value in raw.split(",")]
    except ValueError as exc:
        raise ValueError("MBR must contain four comma-separated numbers") from exc
    if len(parts) != 4:
        raise ValueError("MBR must contain four comma-separated numbers")
    return tuple(parts)  # type: ignore[return-value]


def redact_source(dataset: dict[str, Any]) -> dict[str, Any]:
    """Hide local filesystem details from HTTP responses while preserving local state."""
    source = dataset.get("source")
    if not source or source.get("type") != "local":
        return dataset
    redacted = dict(dataset)
    redacted["source"] = {
        **source,
        "url": None,
        "metadata": {},
    }
    return redacted


def public_repository(repository: dict[str, Any]) -> dict[str, Any]:
    """Return the public repository summary used by REST endpoints."""
    return {
        "id": repository.get("id"),
        "short_name": repository.get("short_name"),
        "url": repository.get("url"),
        "description": repository.get("description"),
        "repository_type": repository.get("repository_type"),
        "is_default": repository.get("is_default"),
        "total_datasets": repository.get("total_datasets", 0),
    }


def iter_batches(dataset_dir: Path, geometry: Any) -> Iterable[Any]:
    batch_size = int(current_app.config["QUERY_BATCH_SIZE"])
    if geometry is None:
        geometry = (-180.0, -90.0, 180.0, 90.0)
    return starlet.query_dataset(dataset_dir, geometry, batch_size=batch_size)


def iter_geojson(dataset_dir: Path, geometry: Any) -> Iterable[str]:
    first = True
    yield '{"type":"FeatureCollection","features":['
    for batch in iter_batches(dataset_dir, geometry):
        for record in json.loads(batch.to_json())["features"]:
            if not first:
                yield ","
            first = False
            yield json.dumps(record, separators=(",", ":"))
    yield "]}"


def iter_csv(dataset_dir: Path, geometry: Any) -> Iterable[str]:
    fieldnames: list[str] | None = None
    buffer = io.StringIO()
    writer: csv.DictWriter[str] | None = None

    for batch in iter_batches(dataset_dir, geometry):
        records = [strip_geometry(row) for row in batch.to_dict(orient="records")]
        if not records:
            continue
        if fieldnames is None:
            fieldnames = list(records[0].keys())
            writer = csv.DictWriter(buffer, fieldnames=fieldnames)
            writer.writeheader()
            yield flush(buffer)
        assert writer is not None
        for record in records:
            writer.writerow(record)
            yield flush(buffer)


def flush(buffer: io.StringIO) -> str:
    value = buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)
    return value


def strip_geometry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: normalize_json(value)
        for key, value in record.items()
        if key != "geometry"
    }


def to_feature(record: dict[str, Any]) -> dict[str, Any]:
    geometry = record.get("geometry")
    if hasattr(geometry, "__geo_interface__"):
        geometry = geometry.__geo_interface__
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": strip_geometry(record),
    }


def normalize_json(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def histogram_png(histogram_path: Path, size: int = 256) -> bytes:
    data = np.load(histogram_path, mmap_mode="r")
    if data.ndim != 2:
        raise ValueError("histogram array must be two-dimensional")

    sampled = downsample_grid(data, size)
    scaled = scale_grayscale(sampled)
    return encode_png_grayscale(scaled)


def downsample_grid(data: np.ndarray, size: int) -> np.ndarray:
    height, width = data.shape
    y_edges = np.linspace(0, height, size + 1, dtype=int)
    x_edges = np.linspace(0, width, size + 1, dtype=int)
    output = np.zeros((size, size), dtype=np.float64)

    for y in range(size):
        y0, y1 = y_edges[y], y_edges[y + 1]
        if y1 <= y0:
            y1 = min(height, y0 + 1)
        for x in range(size):
            x0, x1 = x_edges[x], x_edges[x + 1]
            if x1 <= x0:
                x1 = min(width, x0 + 1)
            output[y, x] = float(np.sum(data[y0:y1, x0:x1]))
    return output


def scale_grayscale(data: np.ndarray) -> np.ndarray:
    finite = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    transformed = np.log1p(np.maximum(finite, 0.0))
    max_value = float(np.max(transformed))
    if max_value <= 0:
        return np.full(transformed.shape, 245, dtype=np.uint8)
    normalized = transformed / max_value
    return (255 - np.round(normalized * 255)).astype(np.uint8)


def encode_png_grayscale(pixels: np.ndarray) -> bytes:
    if pixels.dtype != np.uint8 or pixels.ndim != 2:
        raise ValueError("pixels must be a two-dimensional uint8 array")

    height, width = pixels.shape
    raw = b"".join(b"\x00" + pixels[y].tobytes() for y in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(kind)
        checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
