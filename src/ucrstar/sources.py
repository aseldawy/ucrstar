from __future__ import annotations

import json
import logging
import re
import ssl
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is a project dependency
    certifi = None

LOGGER = logging.getLogger(__name__)

ARCGIS_ITEM_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")
ARCGIS_SERVICE_RE = re.compile(r"/(?:FeatureServer|MapServer)(?:/\d+)?/?$", re.IGNORECASE)
DIRECT_DOWNLOAD_SUFFIXES = {
    ".csv",
    ".geoparquet",
    ".geojson",
    ".json",
    ".jsonl",
    ".gpkg",
    ".ndjson",
    ".parquet",
    ".shp",
    ".tsv",
    ".zip",
}
DIRECT_ARCGIS_ITEM_PREPARATION = {"required": False, "format": "source"}


@dataclass
class PreparedSource:
    path: Path
    source: dict[str, Any]
    _tempdir: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> "PreparedSource":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._tempdir is not None:
            self._tempdir.cleanup()
            self._tempdir = None


def prepare_input_source(value: str) -> PreparedSource:
    if is_multi_url(value):
        return prepare_multi_remote_files(value)
    if is_url(value):
        return prepare_remote_source(value)
    return prepare_local_source(value)


def source_reference(value: str, *, probe_remote: bool = True) -> dict[str, Any]:
    if is_multi_url(value):
        return multi_remote_source_reference(value, probe_remote=probe_remote)
    if is_url(value):
        return remote_source_reference(value, probe_remote=probe_remote)
    return prepare_local_source(value).source


def split_source_urls(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def is_multi_url(value: str) -> bool:
    urls = split_source_urls(value)
    return len(urls) > 1 and all(is_url(url) for url in urls)


def multi_remote_source_reference(value: str, *, probe_remote: bool = True) -> dict[str, Any]:
    urls = split_source_urls(value)
    files = [remote_file_metadata(url, probe_remote=probe_remote) for url in urls]
    modified_values = [
        file_metadata.get("modified_at")
        for file_metadata in files
        if file_metadata.get("modified_at")
    ]
    return {
        "type": "multi_remote_file",
        "url": "\n".join(urls),
        "accessed_at": utc_now_iso(),
        "modified_at": max(modified_values) if modified_values else None,
        "metadata": {
            "urls": urls,
            "files": files,
            "file_count": len(files),
        },
    }


def remote_file_metadata(url: str, probe_remote: bool = True) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(url)
    headers = safe_head_url(url) if probe_remote else {}
    filename = filename_from_url_or_headers(url, headers)
    return {
        "url": url,
        "path": parsed.path,
        "netloc": parsed.netloc,
        "filename": filename,
        "content_type": headers.get("Content-Type"),
        "content_length": headers.get("Content-Length"),
        "modified_at": timestamp_from_http(headers.get("Last-Modified")),
    }


def remote_source_reference(url: str, *, probe_remote: bool = True) -> dict[str, Any]:
    metadata = remote_file_metadata(url, probe_remote=probe_remote)
    return {
        "type": "remote_file",
        "url": url,
        "accessed_at": utc_now_iso(),
        "modified_at": metadata.get("modified_at"),
        "metadata": {
            "url": metadata["url"],
            "path": metadata["path"],
            "netloc": metadata["netloc"],
            "filename": metadata["filename"],
            "content_type": metadata.get("content_type"),
            "content_length": metadata.get("content_length"),
        },
    }


def prepare_local_source(value: str) -> PreparedSource:
    path = Path(value)
    stat = path.stat()
    return PreparedSource(
        path=path,
        source={
            "type": "local",
            "url": str(path.expanduser().resolve()),
            "accessed_at": utc_now_iso(),
            "modified_at": timestamp_from_epoch(stat.st_mtime),
            "metadata": {
                "path": str(path.expanduser().resolve()),
                "size_bytes": stat.st_size,
            },
        },
    )


def prepare_remote_source(url: str) -> PreparedSource:
    parsed = urllib.parse.urlparse(url)
    if is_arcgis_service_url(url):
        return prepare_arcgis_service(url, original_url=url)

    item_id = parse_arcgis_item_id(url)
    if item_id:
        try:
            item = fetch_arcgis_item(item_id)
            service_url = item.get("url")
            if service_url and is_arcgis_service_url(service_url):
                return prepare_arcgis_service(service_url, original_url=url, item=item)
            if is_downloadable_arcgis_item(item):
                return prepare_arcgis_item_data(item, original_url=url)
        except Exception:
            LOGGER.exception("Could not resolve ArcGIS item metadata for %s", url)
            raise

    hub_item = resolve_hub_item(url)
    if hub_item:
        service_url = hub_item.get("url")
        if service_url and is_arcgis_service_url(service_url):
            return prepare_arcgis_service(service_url, original_url=url, item=hub_item)

    if Path(parsed.path).suffix.lower() in DIRECT_DOWNLOAD_SUFFIXES:
        return prepare_remote_file(url)

    raise ValueError(
        "Unsupported remote dataset URL. Use a direct downloadable file URL, "
        "an ArcGIS item URL, an Esri Hub dataset URL, or a public FeatureServer layer URL."
    )


def prepare_arcgis_item_data(item: dict[str, Any], *, original_url: str) -> PreparedSource:
    item_id = item["id"]
    tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-arcgis-item-")
    filename = safe_filename(item.get("name")) or filename_for_arcgis_item(item)
    downloaded_path = Path(tempdir.name) / filename
    data_url = f"https://www.arcgis.com/sharing/rest/content/items/{item_id}/data"
    LOGGER.info(
        "Downloading ArcGIS item %s (%s) to %s",
        item_id,
        item.get("title"),
        downloaded_path,
    )
    download_url(data_url, downloaded_path)
    return PreparedSource(
        path=downloaded_path,
        source={
            "type": arcgis_source_type(original_url, item),
            "url": original_url,
            "accessed_at": utc_now_iso(),
            "modified_at": arcgis_modified_at(item),
            "metadata": {
                "item": slim_arcgis_item(item),
                "item_id": item_id,
                "title": item.get("title"),
                "description": clean_html(item.get("description") or item.get("snippet")),
                "download_url": data_url,
                "downloaded_path": str(downloaded_path),
                "prepared_path": str(downloaded_path),
                "conversion": DIRECT_ARCGIS_ITEM_PREPARATION,
                "size_bytes": item.get("size"),
            },
        },
        _tempdir=tempdir,
    )


def prepare_remote_file(url: str) -> PreparedSource:
    tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-source-")
    headers = head_url(url)
    filename = filename_from_url_or_headers(url, headers)
    target = Path(tempdir.name) / filename
    LOGGER.info("Downloading remote dataset %s to %s", url, target)
    download_url(url, target)
    return PreparedSource(
        path=target,
        source={
            "type": "remote_file",
            "url": url,
            "accessed_at": utc_now_iso(),
            "modified_at": timestamp_from_http(headers.get("Last-Modified")),
            "metadata": {
                "content_type": headers.get("Content-Type"),
                "content_length": headers.get("Content-Length"),
                "downloaded_path": str(target),
            },
        },
        _tempdir=tempdir,
    )


def prepare_multi_remote_files(value: str) -> PreparedSource:
    reference = multi_remote_source_reference(value)
    tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-sources-")
    target_dir = Path(tempdir.name)
    downloaded_files = []
    used_names: set[str] = set()
    for index, file_metadata in enumerate(reference["metadata"]["files"], start=1):
        filename = unique_download_filename(
            str(file_metadata.get("filename") or f"dataset-{index}.geojson"),
            used_names,
        )
        target = target_dir / filename
        LOGGER.info("Downloading remote dataset part %s to %s", file_metadata["url"], target)
        download_url(file_metadata["url"], target)
        downloaded = {**file_metadata, "downloaded_path": str(target)}
        downloaded_files.append(downloaded)
    return PreparedSource(
        path=target_dir,
        source={
            **reference,
            "metadata": {
                **reference["metadata"],
                "files": downloaded_files,
                "downloaded_path": str(target_dir),
            },
        },
        _tempdir=tempdir,
    )


def unique_download_filename(filename: str, used_names: set[str]) -> str:
    safe_name = safe_filename(filename) or "dataset.geojson"
    stem = Path(safe_name).stem or "dataset"
    suffix = Path(safe_name).suffix
    candidate = safe_name
    index = 2
    while candidate in used_names:
        candidate = f"{stem}_{index}{suffix}"
        index += 1
    used_names.add(candidate)
    return candidate


def prepare_arcgis_service(
    service_url: str,
    *,
    original_url: str,
    item: dict[str, Any] | None = None,
) -> PreparedSource:
    layer_url = resolve_arcgis_layer_url(service_url)
    layer = fetch_json(layer_url, {"f": "json"})
    tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-esri-")
    safe_name = safe_filename(item.get("title") if item else layer.get("name")) or "esri_dataset"
    target = Path(tempdir.name) / f"{safe_name}.geojson"
    LOGGER.info("Exporting Esri layer %s to %s", layer_url, target)
    export_arcgis_layer_geojson(layer_url, layer, target)

    modified_at = arcgis_modified_at(item) or arcgis_layer_modified_at(layer)
    return PreparedSource(
        path=target,
        source={
            "type": arcgis_source_type(original_url, item),
            "url": original_url,
            "accessed_at": utc_now_iso(),
            "modified_at": modified_at,
            "metadata": {
                "item": slim_arcgis_item(item) if item else None,
                "layer": slim_arcgis_layer(layer),
                "original_schema": layer.get("fields") or [],
                "item_id": item.get("id") if item else None,
                "resolved_url": layer_url,
                "title": (item or {}).get("title") or layer.get("name"),
                "description": clean_html((item or {}).get("description") or layer.get("description")),
                "attributes": esri_attribute_metadata(layer),
            },
        },
        _tempdir=tempdir,
    )


def current_source_state(source: dict[str, Any]) -> dict[str, Any]:
    source_type = source.get("type")
    url = source.get("url")
    metadata = source.get("metadata") or {}

    if source_type == "local" and url:
        return prepare_local_source(url).source

    if source_type == "remote_file" and url:
        current = remote_source_reference(url)
        current["metadata"] = {
            **metadata,
            **current.get("metadata", {}),
        }
        return current

    if source_type == "multi_remote_file" and url:
        current = multi_remote_source_reference(url)
        current["metadata"] = {
            **metadata,
            **current.get("metadata", {}),
        }
        return current

    item_id = metadata.get("item_id")
    if item_id:
        item = fetch_arcgis_item(item_id)
        return {
            "type": source_type,
            "url": url,
            "accessed_at": utc_now_iso(),
            "modified_at": arcgis_modified_at(item),
            "metadata": {
                **metadata,
                "item": slim_arcgis_item(item),
                "title": item.get("title") or metadata.get("title"),
                "description": clean_html(item.get("description")) or metadata.get("description"),
            },
        }

    resolved_url = metadata.get("resolved_url")
    if resolved_url and is_arcgis_service_url(resolved_url):
        layer_url = resolve_arcgis_layer_url(resolved_url)
        layer = fetch_json(layer_url, {"f": "json"})
        return {
            "type": source_type,
            "url": url,
            "accessed_at": utc_now_iso(),
            "modified_at": arcgis_layer_modified_at(layer),
            "metadata": {
                **metadata,
                "layer": slim_arcgis_layer(layer),
                "original_schema": layer.get("fields") or metadata.get("original_schema") or [],
                "resolved_url": layer_url,
                "attributes": esri_attribute_metadata(layer),
            },
        }

    if url and is_url(url):
        headers = head_url(url)
        return {
            "type": source_type,
            "url": url,
            "accessed_at": utc_now_iso(),
            "modified_at": timestamp_from_http(headers.get("Last-Modified")),
            "metadata": {
                **metadata,
                "content_type": headers.get("Content-Type"),
                "content_length": headers.get("Content-Length"),
            },
        }

    return {**source, "accessed_at": utc_now_iso()}


def export_arcgis_layer_geojson(layer_url: str, layer: dict[str, Any], target: Path) -> None:
    total = arcgis_feature_count(layer_url)
    page_size = int(layer.get("maxRecordCount") or 2000)
    page_size = max(1, min(page_size, 5000))
    object_id_field = layer.get("objectIdField")

    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with target.open("w", encoding="utf-8") as output:
        output.write('{"type":"FeatureCollection","features":[')
        first = True
        offset = 0
        while True:
            params: dict[str, Any] = {
                "f": "geojson",
                "where": "1=1",
                "outFields": "*",
                "returnGeometry": "true",
                "outSR": "4326",
                "resultOffset": str(offset),
                "resultRecordCount": str(page_size),
            }
            if object_id_field:
                params["orderByFields"] = object_id_field
            payload = fetch_json(layer_url.rstrip("/") + "/query", params, method="POST")
            features = payload.get("features") or []
            for feature in features:
                if not first:
                    output.write(",")
                first = False
                output.write(json.dumps(feature, separators=(",", ":")))
                written += 1
            if not features or (total is not None and written >= total):
                break
            offset += len(features)
        output.write("]}")
    LOGGER.info("Exported %d feature(s) from Esri layer", written)


def arcgis_feature_count(layer_url: str) -> int | None:
    try:
        payload = fetch_json(
            layer_url.rstrip("/") + "/query",
            {"f": "json", "where": "1=1", "returnCountOnly": "true"},
            method="POST",
        )
        count = payload.get("count")
        return int(count) if count is not None else None
    except Exception:
        LOGGER.info("Could not read Esri feature count; exporting until pages are empty")
        return None


def resolve_arcgis_layer_url(url: str) -> str:
    clean = url.split("?", 1)[0].rstrip("/")
    match = re.search(r"(.*/(?:FeatureServer|MapServer))(?:/(\d+))?$", clean, re.IGNORECASE)
    if not match:
        raise ValueError(f"Not an ArcGIS FeatureServer or MapServer URL: {url}")
    service_url, layer_id = match.groups()
    if layer_id is not None:
        return f"{service_url}/{layer_id}"
    service = fetch_json(service_url, {"f": "json"})
    layers = service.get("layers") or []
    if not layers:
        raise ValueError(f"ArcGIS service has no layers: {service_url}")
    return f"{service_url}/{layers[0]['id']}"


def fetch_arcgis_item(item_id: str) -> dict[str, Any]:
    item = fetch_json(
        f"https://www.arcgis.com/sharing/rest/content/items/{item_id}",
        {"f": "json"},
    )
    if item.get("error"):
        raise ValueError(item["error"].get("message") or f"ArcGIS item not found: {item_id}")
    return item


def resolve_hub_item(url: str) -> dict[str, Any] | None:
    parsed = urllib.parse.urlparse(url)
    if "hub.arcgis.com" not in parsed.netloc.lower() or "/datasets/" not in parsed.path:
        return None
    slug = parsed.path.split("/datasets/", 1)[1].split("/", 1)[0]
    if not slug:
        return None
    try:
        payload = fetch_json(
            "https://hub.arcgis.com/api/search/v1",
            {"filter[slug]": slug, "page[size]": "1"},
        )
    except urllib.error.HTTPError:
        return None
    data = payload.get("data") or []
    if not data:
        return None
    attributes = data[0].get("attributes") or {}
    item_id = (
        data[0].get("id")
        or attributes.get("itemId")
        or (attributes.get("source") or {}).get("id")
    )
    return fetch_arcgis_item(item_id) if item_id else None


def parse_arcgis_item_id(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("id") and ARCGIS_ITEM_RE.fullmatch(query["id"][0]):
        return query["id"][0]
    match = ARCGIS_ITEM_RE.search(url)
    return match.group(1) if match else None


def is_downloadable_arcgis_item(item: dict[str, Any]) -> bool:
    if item.get("id") is None:
        return False
    if item.get("url") and not item.get("size"):
        return False
    item_type = (item.get("type") or "").lower()
    keywords = {str(value).lower() for value in item.get("typeKeywords") or []}
    return bool(
        item.get("size")
        or "zip" in keywords
        or item_type
        in {
            "csv",
            "file geodatabase",
            "geojson",
            "geopackage",
            "shapefile",
        }
    )


def filename_for_arcgis_item(item: dict[str, Any]) -> str:
    item_type = (item.get("type") or "").lower()
    suffix = {
        "csv": ".csv",
        "file geodatabase": ".gdb.zip",
        "geojson": ".geojson",
        "geopackage": ".gpkg",
        "shapefile": ".zip",
    }.get(item_type, "")
    base = safe_filename(item.get("title")) or item["id"]
    return f"{base}{suffix}"


def is_arcgis_service_url(url: str) -> bool:
    return bool(ARCGIS_SERVICE_RE.search(url.split("?", 1)[0].rstrip("/")))


def arcgis_source_type(original_url: str, item: dict[str, Any] | None) -> str:
    if "hub.arcgis.com" in urllib.parse.urlparse(original_url).netloc.lower():
        return "esri_hub"
    if item:
        return "arcgis_item"
    return "esri_feature_layer"


def is_url(value: str) -> bool:
    return urllib.parse.urlparse(value).scheme in {"http", "https"}


def fetch_json(url: str, params: dict[str, Any] | None = None, *, method: str = "GET") -> dict[str, Any]:
    body = None
    request_url = normalize_request_url(url)
    encoded = urllib.parse.urlencode(params or {}).encode("utf-8")
    if method == "POST":
        body = encoded
    elif encoded:
        request_url = append_query_params(request_url, encoded.decode("utf-8"))
    request = urllib.request.Request(
        request_url,
        data=body,
        headers={"User-Agent": "ucrstar/0.1"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
            raw = response.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                snippet = raw.decode("utf-8", errors="replace")[:500]
                raise ValueError(
                    "ArcGIS returned non-JSON response from "
                    f"{request_url} (status={getattr(response, 'status', 'unknown')}, "
                    f"content_type={response.headers.get('Content-Type')!r}, "
                    f"body={snippet!r})"
                ) from exc
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        raise ValueError(
            f"ArcGIS request failed for {request_url} "
            f"(status={exc.code}, content_type={exc.headers.get('Content-Type')!r}, body={body[:500]!r})"
        ) from exc


def download_url(url: str, target: Path) -> None:
    request = urllib.request.Request(
        normalize_request_url(url),
        headers={"User-Agent": "ucrstar/0.1"},
    )
    with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)


def head_url(url: str) -> dict[str, str]:
    request = urllib.request.Request(
        normalize_request_url(url),
        headers={"User-Agent": "ucrstar/0.1"},
        method="HEAD",
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
            return dict(response.headers.items())
    except urllib.error.HTTPError:
        request = urllib.request.Request(
            normalize_request_url(url),
            headers={"User-Agent": "ucrstar/0.1"},
        )
        with urllib.request.urlopen(request, timeout=30, context=ssl_context()) as response:
            return dict(response.headers.items())


def safe_head_url(url: str) -> dict[str, str]:
    try:
        return head_url(url)
    except urllib.error.URLError:
        LOGGER.warning("Could not reach remote URL %s while recording source metadata", url)
        return {}


def normalize_request_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parsed.path, safe="/%:@")
    return urllib.parse.urlunsplit(parsed._replace(path=path))


def append_query_params(url: str, encoded_params: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = f"{parsed.query}&{encoded_params}" if parsed.query else encoded_params
    return urllib.parse.urlunsplit(parsed._replace(query=query))


def ssl_context() -> ssl.SSLContext:
    if certifi is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def filename_from_url_or_headers(url: str, headers: dict[str, str]) -> str:
    disposition = headers.get("Content-Disposition") or ""
    match = re.search(r'filename="?([^";]+)"?', disposition)
    if match:
        return safe_filename(match.group(1))
    path_name = Path(urllib.parse.urlparse(url).path).name
    return safe_filename(path_name) or "dataset.geojson"


def safe_filename(value: str | None) -> str:
    if not value:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned[:120]


def timestamp_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def timestamp_from_http(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def arcgis_modified_at(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    value = item.get("modified")
    if value is None:
        return None
    return timestamp_from_epoch(float(value) / 1000.0)


def arcgis_layer_modified_at(layer: dict[str, Any]) -> str | None:
    editing_info = layer.get("editingInfo") or {}
    value = editing_info.get("lastEditDate")
    if value is None:
        return None
    return timestamp_from_epoch(float(value) / 1000.0)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slim_arcgis_item(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if not item:
        return None
    return {
        key: item.get(key)
        for key in (
            "id",
            "title",
            "name",
            "type",
            "url",
            "owner",
            "modified",
            "snippet",
            "description",
            "licenseInfo",
            "accessInformation",
            "tags",
        )
        if item.get(key) is not None
    }


def slim_arcgis_layer(layer: dict[str, Any]) -> dict[str, Any]:
    return {
        key: layer.get(key)
        for key in (
            "id",
            "name",
            "type",
            "geometryType",
            "description",
            "copyrightText",
            "maxRecordCount",
            "objectIdField",
            "extent",
        )
        if layer.get(key) is not None
    }


def esri_attribute_metadata(layer: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = []
    for field in layer.get("fields") or []:
        raw_type = field.get("type")
        attributes.append(
            {
                "name": field.get("name"),
                "type": normalize_esri_field_type(raw_type),
                "esri_type": raw_type,
                "description": field.get("alias") or field.get("name"),
            }
        )
    return attributes


def normalize_esri_field_type(value: str | None) -> str | None:
    if not value:
        return None
    type_name = value.removeprefix("esriFieldType")
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
    }.get(type_name, type_name or value)


def clean_html(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", text).strip()
