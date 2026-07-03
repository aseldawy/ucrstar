from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Iterable

import starlet
from flask import Flask, Response, current_app, jsonify, request, stream_with_context

from .catalog import DatasetCatalog


DEFAULT_BATCH_SIZE = 1000


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    app.config.from_mapping(
        DATASETS_DIR=Path("datasets"),
        DATABASE=Path("instance/ucrstar2.sqlite"),
        QUERY_BATCH_SIZE=DEFAULT_BATCH_SIZE,
    )
    if config:
        app.config.update(config)

    @app.errorhandler(ValueError)
    def bad_request(error: ValueError) -> tuple[Response, int]:
        return jsonify({"error": str(error)}), 400

    @app.get("/health.json")
    def health() -> Response:
        return jsonify({"status": "ok"})

    @app.post("/admin/sync-datasets")
    def sync_datasets() -> Response:
        rows = catalog().sync()
        return jsonify({"datasets": rows, "count": len(rows)})

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
            }
        }
        return jsonify({"datasets": catalog().list(filters)})

    @app.get("/datasets/<dataset_ref>.json")
    def dataset_details(dataset_ref: str) -> Response:
        catalog().sync()
        dataset = catalog().get(dataset_ref)
        if dataset is None:
            return jsonify({"error": "dataset not found"}), 404
        return jsonify(dataset)

    @app.route("/datasets/<dataset_ref>/download.<fmt>", methods=["GET", "POST"])
    def download(dataset_ref: str, fmt: str) -> Response:
        catalog().sync()
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
        catalog().sync()
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
    @app.get("/tiles/<dataset_ref>/<int:z>/<int:x>/<int:y>.mvt")
    def tile(dataset_ref: str, z: int, x: int, y: int) -> Response:
        catalog().sync()
        dataset = require_dataset(dataset_ref)
        if isinstance(dataset, Response):
            return dataset
        data = starlet.get_tile(dataset_path(dataset["name"]), z, x, y)
        return Response(data, mimetype="application/vnd.mapbox-vector-tile")

    return app


def catalog() -> DatasetCatalog:
    return DatasetCatalog(
        Path(current_app.config["DATABASE"]),
        Path(current_app.config["DATASETS_DIR"]),
    )


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
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
