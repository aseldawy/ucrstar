from __future__ import annotations

import argparse
import csv
import json
import logging
from logging.handlers import TimedRotatingFileHandler
import re
import shutil
import sys
import uuid
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import starlet

LOGGER = logging.getLogger(__name__)

if __package__:
    from .app import TimingWSGIRequestHandler, create_app
    from .catalog import DatasetCatalog, dataset_relative_path, validate_dataset_name
    from .config import configure_runtime, load_config
    from .esri_hub import EsriHubClient, HubDataset
    from .llm import llm_from_config
    from .sources import clean_html, current_source_state, safe_filename, source_reference, prepare_input_source, utc_now_iso
    from .sources import clean_html, current_source_state, fetch_json, is_arcgis_service_url, source_reference, prepare_input_source, utc_now_iso
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ucrstar.app import create_app
    from ucrstar.app import TimingWSGIRequestHandler
    from ucrstar.catalog import DatasetCatalog, dataset_relative_path, validate_dataset_name
    from ucrstar.config import configure_runtime, load_config
    from ucrstar.esri_hub import EsriHubClient, HubDataset
    from ucrstar.llm import llm_from_config
    from ucrstar.sources import clean_html, current_source_state, safe_filename, source_reference, prepare_input_source, utc_now_iso
    from ucrstar.sources import clean_html, current_source_state, fetch_json, is_arcgis_service_url, source_reference, prepare_input_source, utc_now_iso


def main() -> None:
    parser = argparse.ArgumentParser(prog="ucrstar")
    parser.add_argument("--datasets-dir", default="datasets")
    parser.add_argument("--database", default="instance/ucrstar.sqlite")
    parser.add_argument("--config", default="ucrstar.config.json")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--log-output", choices=["file", "stdout"], default=None)
    parser.add_argument("--log-dir", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode for development error pages and the interactive debugger.",
    )
    serve.add_argument(
        "--reload",
        action="store_true",
        help="Enable Flask's auto-reloader. Off by default so Ctrl+C stops one process.",
    )

    add_dataset = subparsers.add_parser(
        "add-dataset",
        aliases=["add-datasets"],
        help="Build a Starlet dataset and add it to the catalog.",
    )
    add_dataset.add_argument("input_path", nargs="+")
    add_dataset.add_argument("--name")
    add_dataset.add_argument(
        "--description",
        help="Dataset description to store with the source and catalog entry.",
    )
    add_dataset.add_argument(
        "--schema-doc",
        type=Path,
        help=(
            "JSON file with source metadata, for example "
            '{"description":"...","attributes":[{"name":"field","description":"..."}]}.'
        ),
    )
    add_dataset.add_argument("--overwrite", action="store_true")
    add_dataset.add_argument(
        "--create-only",
        action="store_true",
        help="Only register the dataset source in the database; do not build or publish it.",
    )
    add_dataset.add_argument(
        "--no-download",
        action="store_true",
        help="Disable UCR Star-generated downloads for this dataset while keeping the source link.",
    )
    add_dataset.add_argument("--zoom", type=int, default=None)
    add_dataset.add_argument("--partition-size", type=int)
    add_dataset.add_argument("--threshold", type=int)
    add_dataset.add_argument(
        "--no-covering-bbox",
        action="store_true",
        default=None,
        help="Do not write per-row bounding boxes for faster spatial pruning.",
    )
    add_dataset.add_argument("--pmtiles", action="store_true", default=None)

    process_dataset = subparsers.add_parser(
        "process-dataset",
        help="Process registered datasets through Starlet, enrichment, and publishing.",
    )
    process_dataset.add_argument(
        "dataset",
        nargs="?",
        help="Dataset ID or name. If omitted, process queued datasets one at a time.",
    )
    process_dataset.add_argument("--limit", type=int, help="Maximum number of queued datasets to process.")
    process_dataset.add_argument(
        "--state",
        choices=["created", "downloaded", "processed", "ready", "error"],
        help="Queued state to process when no dataset is specified.",
    )
    process_dataset.add_argument("--overwrite", action="store_true")
    add_build_arguments(process_dataset)

    delete_dataset = subparsers.add_parser(
        "delete-dataset",
        help="Delete a dataset directory and remove it from the catalog.",
    )
    delete_dataset.add_argument("dataset")
    delete_dataset.add_argument(
        "--missing-ok",
        action="store_true",
        help="Do not fail if the dataset is not found.",
    )

    refresh = subparsers.add_parser(
        "refresh",
        help="Refresh source-backed datasets when their source has been modified.",
    )
    add_refresh_arguments(refresh)

    refresh_datasets_parser = subparsers.add_parser(
        "refresh-datasets",
        help="Refresh source-backed datasets when their source has been modified.",
    )
    add_refresh_arguments(refresh_datasets_parser)

    refresh_repositories_parser = subparsers.add_parser(
        "refresh-repositories",
        aliases=["refresh-repository"],
        help="Refresh one repository, or all repositories when omitted.",
    )
    refresh_repositories_parser.add_argument(
        "repository",
        nargs="?",
        help="Repository ID, short name, or URL. Omit to refresh all non-default repositories.",
    )
    refresh_repositories_parser.add_argument("--force", action="store_true", help="Refresh datasets even if timestamps match.")
    refresh_repositories_parser.add_argument(
        "--create-only",
        action="store_true",
        help="Only update source records; do not build or rebuild datasets.",
    )
    add_build_arguments(refresh_repositories_parser)

    list_datasets_parser = subparsers.add_parser(
        "list-datasets",
        help="List datasets in the catalog.",
    )
    list_datasets_parser.add_argument("--format", choices=["table", "csv", "json"], default="table")
    list_datasets_parser.add_argument("--state", default="all")
    list_datasets_parser.add_argument("--repository")

    list_repositories_parser = subparsers.add_parser(
        "list-repositories",
        help="List repositories in the catalog.",
    )
    list_repositories_parser.add_argument("--format", choices=["table", "csv", "json"], default="table")

    dataset_parser = subparsers.add_parser(
        "dataset",
        help="Print a dataset as JSON.",
    )
    dataset_parser.add_argument("dataset")

    args = parser.parse_args()
    project_config = load_config(args.config)
    configure_runtime(project_config)
    logging_config = project_config.get("logging") or {}
    configure_logging(
        args.log_level or logging_config.get("level") or "INFO",
        output=args.log_output or logging_config.get("output") or "file",
        log_dir=args.log_dir or logging_config.get("dir") or "log",
    )
    config = {
        "DATASETS_DIR": args.datasets_dir,
        "DATABASE": args.database,
        "UCRSTAR2_CONFIG": project_config,
    }
    catalog = DatasetCatalog(args.database, args.datasets_dir)

    if args.command in {"add-dataset", "add-datasets"}:
        input_source = combined_input_source(args.input_path)
        LOGGER.info("Adding dataset from %s", input_source)
        if is_ezesri_catalog_url(input_source) or is_esri_hub_repository_url(input_source):
            added = add_dataset_from_source(
                catalog,
                input_source,
                args.name,
                args.datasets_dir,
                args.overwrite,
                args.create_only,
                build_kwargs_from_args(args),
                project_config,
                downloads_enabled=not args.no_download,
            )
            LOGGER.info("Added %d dataset(s).", len(added) if isinstance(added, list) else 1)
            return
        source_metadata = source_metadata_from_args(args)
        source = apply_source_metadata(
            registration_source(input_source, probe_remote=not args.create_only),
            source_metadata,
        )
        existing = find_dataset_by_source(catalog, source)
        if existing is None:
            candidate_name = args.name or dataset_name_from_source(source, source_name_path(input_source, source))
            existing = catalog.get(candidate_name)
        if existing is not None:
            log_existing_dataset_skip(input_source, existing)
            return
        added = add_dataset_from_source(
            catalog,
            input_source,
            args.name,
            args.datasets_dir,
            args.overwrite,
            args.create_only,
            build_kwargs_from_args(args),
            project_config,
            source_metadata,
            downloads_enabled=not args.no_download,
        )
        if isinstance(added, list):
            LOGGER.info("Added %d dataset(s).", len(added))
        else:
            LOGGER.info("Added dataset %s with ID %s.", added["name"], added["id"])
        return
    if args.command == "list-datasets":
        filters = {"state": args.state}
        if args.repository:
            filters["repository"] = args.repository
        write_dataset_list(catalog.list(filters), args.format)
        return
    if args.command == "list-repositories":
        write_repository_list(catalog.list_repositories(), args.format)
        return
    if args.command == "dataset":
        dataset = catalog.get(args.dataset)
        if dataset is None:
            raise SystemExit(f"Dataset not found: {args.dataset}")
        print(json.dumps(dataset, indent=2, sort_keys=True))
        return
    if args.command == "process-dataset":
        processed = process_datasets(
            catalog,
            args.datasets_dir,
            args.dataset,
            args.limit,
            args.state,
            args.overwrite,
            build_kwargs_from_args(args),
            project_config,
        )
        LOGGER.info("Processing complete. Processed %d dataset(s).", processed)
        return
    if args.command == "delete-dataset":
        dataset = catalog.get(args.dataset)
        if dataset is None:
            if args.missing_ok:
                LOGGER.info("Dataset '%s' was not found.", args.dataset)
                return
            raise SystemExit(f"Dataset not found: {args.dataset}")

        LOGGER.info(
            "Deleting dataset %s with ID %s from %s",
            dataset["name"],
            dataset["id"],
            args.datasets_dir,
        )
        starlet.delete_dataset(args.datasets_dir, dataset["name"], missing_ok=args.missing_ok)
        catalog.delete(dataset["id"])
        LOGGER.info("Deleted dataset %s with ID %s.", dataset["name"], dataset["id"])
        return
    if args.command in {"refresh", "refresh-datasets"}:
        refreshed = refresh_datasets(
            catalog,
            args.datasets_dir,
            args.dataset,
            args.force,
            build_kwargs_from_args(args),
            project_config,
        )
        LOGGER.info("Refresh complete. Refreshed %d dataset(s).", refreshed)
        return
    if args.command in {"refresh-repositories", "refresh-repository"}:
        refreshed = refresh_repositories(
            catalog,
            args.datasets_dir,
            args.repository,
            args.force,
            args.create_only,
            build_kwargs_from_args(args),
            project_config,
        )
        LOGGER.info("Repository refresh complete. Updated %d dataset(s).", refreshed)
        return
    if args.command == "serve":
        app = create_app(config)
        print(f"Serving UCR Star at {server_url(args.host, args.port)}", flush=True)
        app.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            use_reloader=args.reload,
            request_handler=TimingWSGIRequestHandler,
        )


def server_url(host: str, port: int) -> str:
    """Return a browser-friendly URL for the configured development server."""
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def combined_input_source(values: list[str]) -> str:
    if len(values) == 1:
        return values[0]
    if any(is_ezesri_catalog_url(value) or is_esri_hub_repository_url(value) for value in values):
        raise SystemExit("Repository imports accept exactly one input URL.")
    return "\n".join(values)


def add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--zoom", type=int, default=None)
    parser.add_argument("--partition-size", type=int)
    parser.add_argument("--threshold", type=int)
    parser.add_argument(
        "--no-covering-bbox",
        action="store_true",
        default=None,
        help="Do not write per-row bounding boxes for faster spatial pruning.",
    )
    parser.add_argument("--pmtiles", action="store_true", default=None)


def source_metadata_from_args(args: argparse.Namespace) -> dict[str, Any] | None:
    metadata: dict[str, Any] = {}
    if getattr(args, "schema_doc", None):
        metadata.update(load_schema_doc(args.schema_doc))
    if getattr(args, "description", None):
        metadata["description"] = clean_text(args.description)
    return metadata or None


def load_schema_doc(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
    except OSError as exc:
        raise SystemExit(f"Could not read schema doc {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Schema doc {path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SystemExit("Schema doc must be a JSON object.")

    metadata: dict[str, Any] = {}
    description = clean_text(payload.get("description"))
    if description:
        metadata["description"] = description

    attributes = payload.get("attributes")
    if attributes is not None:
        if not isinstance(attributes, list):
            raise SystemExit("Schema doc 'attributes' must be a list.")
        normalized_attributes = []
        for index, attribute in enumerate(attributes, start=1):
            if not isinstance(attribute, dict):
                raise SystemExit(f"Schema doc attribute #{index} must be an object.")
            name = str(attribute.get("name") or "").strip()
            if not name:
                raise SystemExit(f"Schema doc attribute #{index} is missing 'name'.")
            normalized: dict[str, Any] = {"name": name}
            attr_type = attribute.get("type")
            if attr_type:
                normalized["type"] = str(attr_type)
            attr_description = clean_text(attribute.get("description"))
            if attr_description:
                normalized["description"] = attr_description
            normalized_attributes.append(normalized)
        metadata["attributes"] = normalized_attributes

    return metadata


def apply_source_metadata(
    source: dict[str, Any],
    metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    if not metadata:
        return source
    updated = dict(source)
    existing_metadata = dict(updated.get("metadata") or {})
    merged = {**existing_metadata, **metadata}
    if existing_metadata.get("attributes") and metadata.get("attributes"):
        merged["attributes"] = merge_source_attributes(
            existing_metadata["attributes"],
            metadata["attributes"],
        )
    updated["metadata"] = merged
    return updated


def merge_source_attributes(
    existing_attributes: Any,
    new_attributes: Any,
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for attributes in (existing_attributes, new_attributes):
        for attribute in attributes if isinstance(attributes, list) else []:
            if not isinstance(attribute, dict):
                continue
            name = str(attribute.get("name") or "").strip()
            if not name:
                continue
            if name not in merged:
                merged[name] = {"name": name}
                order.append(name)
            merged[name].update(attribute)
            merged[name]["name"] = name
    return [merged[name] for name in order]


def add_refresh_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "dataset",
        nargs="?",
        help="Dataset ID or name. If omitted, check all datasets with source information.",
    )
    parser.add_argument("--force", action="store_true", help="Refresh even if timestamps match.")
    add_build_arguments(parser)


def build_kwargs_from_args(args: argparse.Namespace) -> dict[str, Any]:
    build_kwargs: dict[str, Any] = {}
    if args.zoom is not None:
        build_kwargs["zoom"] = args.zoom
    else:
        build_kwargs["zoom"] = int(starlet.get_config()["mvt"]["zoom"])
    build_kwargs["covering_bbox"] = not bool(args.no_covering_bbox)
    if args.pmtiles is not None:
        build_kwargs["pmtiles"] = args.pmtiles
    if args.partition_size is not None:
        build_kwargs["partition_size"] = args.partition_size
    if args.threshold is not None:
        build_kwargs["threshold"] = args.threshold
    return build_kwargs


def add_dataset_from_source(
    catalog: DatasetCatalog,
    input_path_or_url: str,
    dataset_name: str | None,
    datasets_dir: str | Path,
    overwrite: bool,
    create_only: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
    source_metadata: dict[str, Any] | None = None,
    downloads_enabled: bool = True,
) -> dict[str, Any] | list[dict[str, Any]]:
    if is_ezesri_catalog_url(input_path_or_url):
        if dataset_name:
            raise ValueError("--name cannot be used when adding an ezesri catalog")
        return add_ezesri_catalog(
            catalog,
            input_path_or_url,
            datasets_dir,
            overwrite,
            create_only,
            build_kwargs,
            project_config,
        )

    if is_esri_hub_repository_url(input_path_or_url):
        if dataset_name:
            raise ValueError("--name cannot be used when adding an Esri Hub repository")
        return add_esri_hub_repository(
            catalog,
            input_path_or_url,
            datasets_dir,
            overwrite,
            create_only,
            build_kwargs,
            project_config,
        )

    if create_only:
        source = apply_source_metadata(
            registration_source(input_path_or_url, probe_remote=False),
            source_metadata,
        )
        existing = find_dataset_by_source(catalog, source)
        if existing is not None:
            log_existing_dataset_skip(input_path_or_url, existing)
            return existing
        dataset_name = dataset_name or dataset_name_from_source(source, source_name_path(input_path_or_url, source))
        existing = catalog.get(dataset_name)
        if existing is not None:
            log_existing_dataset_skip(input_path_or_url, existing)
            return existing
        dataset = catalog.register_source(
            dataset_name,
            source,
            description=(source.get("metadata") or {}).get("description"),
            overwrite=overwrite,
            downloads_enabled=downloads_enabled,
        )
        LOGGER.info("Registered dataset '%s' in created state", dataset["name"])
        return dataset

    source = apply_source_metadata(registration_source(input_path_or_url), source_metadata)
    existing = find_dataset_by_source(catalog, source)
    if existing is not None:
        log_existing_dataset_skip(input_path_or_url, existing)
        return existing
    dataset_name = dataset_name or dataset_name_from_source(source, source_name_path(input_path_or_url, source))
    existing = catalog.get(dataset_name)
    if existing is not None:
        log_existing_dataset_skip(input_path_or_url, existing)
        return existing
    dataset = catalog.register_source(
        dataset_name,
        source,
        description=(source.get("metadata") or {}).get("description"),
        overwrite=overwrite,
        downloads_enabled=downloads_enabled,
    )
    try:
        return process_registered_dataset(catalog, dataset, datasets_dir, overwrite, build_kwargs, project_config)
    except urllib.error.URLError as exc:
        LOGGER.warning(
            "Keeping dataset '%s' in the catalog even though the source URL could not be reached: %s",
            dataset["name"],
            exc,
        )
        record_dataset_error(catalog, dataset, exc)
        return catalog.get(dataset["id"]) or dataset
    except Exception as exc:
        record_dataset_error(catalog, dataset, exc)
        raise


def add_ezesri_catalog(
    catalog: DatasetCatalog,
    catalog_url: str,
    datasets_dir: str | Path,
    overwrite: bool,
    create_only: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> list[dict[str, Any]]:
    payload = fetch_json(catalog_url)
    services = payload.get("services") or []
    added: list[dict[str, Any]] = []
    used_names: set[str] = set()
    repository = ensure_repository(
        catalog,
        catalog_url,
        "ezesri_directory",
        metadata={
            "generated_at": payload.get("generated"),
            "service_count": len(services),
        },
        project_config=project_config,
    )

    LOGGER.info("Scanning ezesri catalog %s (%d service entries)", catalog_url, len(services))
    for source in ezesri_catalog_sources(catalog_url, payload):
        existing = find_dataset_by_source(catalog, source)
        if existing is not None and not overwrite:
            catalog.set_dataset_repository(existing["id"], repository["id"])
            log_existing_dataset_skip(source["metadata"]["title"], existing)
            continue
        dataset_name = unique_dataset_name(
            catalog,
            repository_dataset_name(
                repository,
                dataset_name_from_source(source, Path(source["metadata"]["title"])),
            ),
            source,
            used_names,
            overwrite=overwrite,
        )
        if dataset_name is None:
            LOGGER.info("Skipping existing ezesri catalog dataset '%s'", source["metadata"]["title"])
            continue

        try:
            dataset = catalog.register_source(
                dataset_name,
                source,
                description=(source.get("metadata") or {}).get("description"),
                repository_id=repository["id"],
                overwrite=overwrite,
            )
            LOGGER.info("Registered ezesri catalog dataset '%s' in created state", dataset["name"])
            if not create_only:
                dataset = process_registered_dataset(
                    catalog,
                    dataset,
                    datasets_dir,
                    overwrite,
                    build_kwargs,
                    project_config,
                )
            added.append(dataset)
            used_names.add(dataset_name)
        except Exception as exc:
            existing = catalog.get(dataset_name)
            if existing is not None:
                catalog.update_state(existing["id"], "error", error_message=str(exc))
            LOGGER.exception("Failed to add ezesri catalog dataset '%s'", dataset_name)
            if create_only:
                raise
    LOGGER.info("ezesri catalog scan added %d eligible dataset(s)", len(added))
    return added


def ezesri_catalog_sources(catalog_url: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    generated_at = payload.get("generated")
    sources: list[dict[str, Any]] = []
    for service in payload.get("services") or []:
        if not is_supported_ezesri_service(service):
            continue

        service_url = str(service.get("url") or "").rstrip("/")
        layers = service.get("layers") or []
        if layers:
            for layer in layers:
                layer_id = layer.get("id")
                if layer_id is None:
                    continue
                sources.append(ezesri_catalog_source(catalog_url, generated_at, service, layer))
        else:
            sources.append(ezesri_catalog_source(catalog_url, generated_at, service, None))
    return sources


def ezesri_catalog_source(
    catalog_url: str,
    generated_at: str | None,
    service: dict[str, Any],
    layer: dict[str, Any] | None,
) -> dict[str, Any]:
    service_url = str(service.get("url") or "").rstrip("/")
    layer_id = layer.get("id") if layer else None
    source_url = f"{service_url}/{layer_id}" if layer_id is not None else service_url
    service_title = str(service.get("title") or service.get("id") or source_url)
    layer_name = str((layer or {}).get("name") or "")
    title = f"{service_title} - {layer_name}" if layer_name and int(service.get("layerCount") or 0) != 1 else service_title
    item_id = str(service.get("id") or "")
    canonical_id = arcgis_canonical_id(item_id, service_url, layer_id)

    return {
        "type": "ezesri_directory",
        "url": source_url,
        "accessed_at": utc_now_iso(),
        "modified_at": None,
        "metadata": {
            "repository": {
                "type": "ezesri_directory",
                "catalog_url": catalog_url,
                "generated_at": generated_at,
            },
            "canonical_id": canonical_id,
            "arcgis_item_id": item_id if is_arcgis_item_id(item_id) else None,
            "service_url": service_url,
            "resolved_url": source_url,
            "layer_id": layer_id,
            "layer": layer,
            "title": title,
            "description": clean_text(service.get("description")),
            "category": service.get("category"),
            "category_key": service.get("categoryKey"),
            "owner": service.get("owner"),
            "tags": service.get("tags") or [],
            "capabilities": service.get("capabilities"),
            "max_record_count": service.get("maxRecordCount"),
            "directory_service": service,
        },
    }


def is_supported_ezesri_service(service: dict[str, Any]) -> bool:
    service_url = str(service.get("url") or "")
    if not is_arcgis_service_url(service_url):
        return False
    capabilities = {value.strip().lower() for value in str(service.get("capabilities") or "").split(",")}
    if "query" not in capabilities:
        return False
    layers = service.get("layers") or []
    return not layers or any(str(layer.get("type") or "").startswith("esriGeometry") for layer in layers)


def unique_dataset_name(
    catalog: DatasetCatalog,
    base_name: str,
    source: dict[str, Any],
    used_names: set[str],
    *,
    overwrite: bool,
) -> str | None:
    if overwrite:
        return base_name
    if catalog.get(base_name) is not None:
        return None
    if base_name not in used_names:
        return base_name

    metadata = source.get("metadata") or {}
    canonical_id = str(metadata.get("canonical_id") or uuid.uuid4().hex)
    suffix = re.sub(r"[^A-Za-z0-9_.-]+", "_", canonical_id).strip("._")[-32:]
    candidate = f"{base_name}_{suffix}" if suffix else base_name
    if candidate not in used_names and catalog.get(candidate) is None:
        return candidate
    return None


def repository_dataset_name(repository: dict[str, Any], dataset_name: str) -> str:
    prefix = safe_dataset_name_segment(str(repository.get("short_name") or "repository"))
    leaf = safe_dataset_name_segment(dataset_name)
    return f"{prefix}/{leaf}"


def safe_dataset_name_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return cleaned[:80] or "dataset"


def add_esri_hub_repository(
    catalog: DatasetCatalog,
    repository_url: str,
    datasets_dir: str | Path,
    overwrite: bool,
    create_only: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> list[dict[str, Any]]:
    client = EsriHubClient(repository_url)
    added: list[dict[str, Any]] = []
    repository = ensure_repository(
        catalog,
        client.site_url,
        "esri_hub",
        metadata={
            "search_base_url": client.search_base_url,
            "download_base_url": client.download_base_url,
            "input_url": repository_url,
        },
        project_config=project_config,
    )
    LOGGER.info("Scanning Esri Hub repository %s", client.site_url)
    for hub_dataset in client.iter_datasets(page_size=100):
        metadata = hub_dataset_metadata(client, hub_dataset)
        if not is_supported_hub_dataset(hub_dataset, metadata):
            LOGGER.debug(
                "Skipping Esri Hub dataset '%s' (%s): unsupported type or no vector source",
                hub_dataset.title,
                hub_dataset.id,
            )
            continue

        source = esri_hub_source(client, hub_dataset, metadata)
        existing = find_dataset_by_source(catalog, source)
        if existing is not None:
            catalog.set_dataset_repository(existing["id"], repository["id"])
            log_existing_dataset_skip(hub_dataset.title, existing)
            continue
        dataset_name = repository_dataset_name(
            repository,
            dataset_name_from_source(source, Path(hub_dataset.title)),
        )
        existing = catalog.get(dataset_name)
        if existing is not None:
            catalog.set_dataset_repository(existing["id"], repository["id"])
            log_existing_dataset_skip(hub_dataset.title, existing)
            continue
        try:
            dataset = catalog.register_source(
                dataset_name,
                source,
                description=(source.get("metadata") or {}).get("description"),
                repository_id=repository["id"],
                overwrite=overwrite,
            )
            LOGGER.info("Registered Esri Hub dataset '%s' in created state", dataset["name"])
            if not create_only:
                dataset = process_registered_dataset(
                    catalog,
                    dataset,
                    datasets_dir,
                    overwrite,
                    build_kwargs,
                    project_config,
                )
            added.append(dataset)
        except Exception as exc:
            existing = catalog.get(dataset_name)
            if existing is not None:
                record_dataset_error(catalog, existing, exc)
            LOGGER.exception("Failed to add Esri Hub dataset '%s'", dataset_name)
            if not create_only:
                continue
            raise
    LOGGER.info("Esri Hub repository scan added %d eligible dataset(s)", len(added))
    return added


def hub_dataset_metadata(client: EsriHubClient, dataset: HubDataset) -> dict[str, Any]:
    try:
        return client.metadata(dataset.id)
    except Exception:
        LOGGER.exception("Could not fetch full Esri Hub metadata for '%s'; using search record", dataset.id)
        return {
            "record": dataset.record,
            "properties": dataset.properties,
            "download_links": [link.__dict__ for link in client.download_links(dataset)],
        }


def is_supported_hub_dataset(dataset: HubDataset, metadata: dict[str, Any]) -> bool:
    item_type = (dataset.item_type or "").lower()
    if item_type in {"feature service", "feature layer"}:
        return True
    if metadata.get("layer"):
        return True
    formats = {str(link.get("format", "")).lower() for link in metadata.get("download_links") or []}
    return bool(formats & {"geojson", "shapefile", "filegdb", "fgdb", "geopackage", "csv"})


def esri_hub_source(
    client: EsriHubClient,
    dataset: HubDataset,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    item = metadata.get("arcgis_item") or {}
    source_url = item_url(dataset.item_id)
    modified_at = timestamp_from_millis(item.get("modified"))
    description = clean_text(
        item.get("description")
        or item.get("snippet")
        or dataset.properties.get("description")
        or dataset.properties.get("snippet")
    )
    title = item.get("title") or dataset.title
    return {
        "type": "esri_hub",
        "url": source_url,
        "accessed_at": utc_now_iso(),
        "modified_at": modified_at,
        "metadata": {
            "repository": {
                "site_url": client.site_url,
                "search_base_url": client.search_base_url,
                "download_base_url": client.download_base_url,
            },
            "record_id": dataset.id,
            "item_id": dataset.item_id,
            "layer_id": dataset.layer_id,
            "title": title,
            "description": description,
            "type": dataset.item_type,
            "service_url": dataset.service_url,
            "download_formats": dataset.download_formats,
            "download_links": metadata.get("download_links") or [],
            "hub": metadata,
        },
    }


def ensure_repository(
    catalog: DatasetCatalog,
    repository_url: str,
    repository_type: str,
    *,
    metadata: dict[str, Any] | None = None,
    project_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or update a repository row for a dataset collection URL."""
    existing = catalog.get_repository(repository_url)
    profile = repository_profile(repository_url, repository_type, metadata or {}, project_config or {})
    short_name = profile["short_name"]
    if existing is None:
        short_name = unique_repository_short_name(catalog, short_name)
    elif existing["short_name"] != short_name:
        short_name = existing["short_name"]
    return catalog.upsert_repository(
        short_name,
        repository_url,
        description=profile["description"],
        repository_type=repository_type,
        metadata=metadata or {},
    )


def repository_profile(
    repository_url: str,
    repository_type: str,
    metadata: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, str]:
    """Build a compact repository short name and description."""
    parsed = urllib_parse(repository_url)
    host = parsed.netloc.lower().removeprefix("www.")
    base = host.split(".")[0] if host else repository_type
    if repository_type == "ezesri_directory":
        base = "ezesri"
        description = "ezesri ArcGIS service directory."
    elif repository_type == "esri_hub":
        description = f"Esri Hub repository at {host}."
    else:
        description = f"Dataset repository at {host or repository_url}."
    short_name = safe_repository_short_name(base)

    llm = llm_from_config(project_config)
    if llm.enabled:
        try:
            generated = llm.complete_json(
                "Return compact JSON for a geospatial dataset repository. "
                "Use keys short_name and description. short_name must be lowercase, "
                "URL-safe, and at most 40 characters. description must be at most 180 characters. "
                f"Repository: {json.dumps({'url': repository_url, 'type': repository_type, 'metadata': metadata}, separators=(',', ':'))}"
            )
            if isinstance(generated, dict):
                short_name = safe_repository_short_name(str(generated.get("short_name") or short_name)) or short_name
                if generated.get("description"):
                    description = str(generated["description"])[:180]
        except Exception:
            LOGGER.exception("LLM repository profile generation failed for %s", repository_url)

    return {"short_name": short_name or "repository", "description": description}


def unique_repository_short_name(catalog: DatasetCatalog, short_name: str) -> str:
    """Return a repository short name that does not collide with an existing row."""
    base = safe_repository_short_name(short_name) or "repository"
    candidate = base
    index = 2
    while catalog.get_repository(candidate) is not None:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def safe_repository_short_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    return cleaned[:40]


def is_esri_hub_repository_url(value: str) -> bool:
    parsed = urllib_parse(value)
    if "hub.arcgis.com" not in parsed.netloc.lower():
        return False
    return "/datasets/" not in parsed.path.lower()


def is_ezesri_catalog_url(value: str) -> bool:
    parsed = urllib_parse(value)
    if parsed.netloc.lower() not in {"ezesri.com", "www.ezesri.com"}:
        return False
    return parsed.path.rstrip("/").lower().endswith("/catalog.json")


def urllib_parse(value: str):
    import urllib.parse

    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme:
        parsed = urllib.parse.urlparse(f"https://{value}")
    return parsed


def item_url(item_id: str) -> str:
    return f"https://www.arcgis.com/home/item.html?id={item_id}"


def timestamp_from_millis(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return None


def clean_text(value: Any) -> str | None:
    if not value:
        return None
    return clean_html(str(value))


def arcgis_canonical_id(item_id: str, service_url: str, layer_id: Any) -> str:
    layer_suffix = f":{layer_id}" if layer_id is not None else ""
    if is_arcgis_item_id(item_id):
        return f"arcgis-item:{item_id.lower()}{layer_suffix}"
    return f"arcgis-service:{normalize_service_url(service_url)}{layer_suffix}"


def is_arcgis_item_id(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"[0-9a-fA-F]{32}", value))


def normalize_service_url(service_url: str) -> str:
    parsed = urllib_parse(service_url)
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def registration_source(input_path_or_url: str, *, probe_remote: bool = True) -> dict[str, Any]:
    return source_reference(input_path_or_url, probe_remote=probe_remote)


def source_name_path(input_path_or_url: str, source: dict[str, Any]) -> Path:
    if source["type"] != "local":
        metadata = source.get("metadata") or {}
        name = metadata.get("filename") or metadata.get("path") or metadata.get("netloc") or "dataset"
        return Path(name)
    return Path(input_path_or_url)


def process_datasets(
    catalog: DatasetCatalog,
    datasets_dir: str | Path,
    dataset_ref: str | None,
    limit: int | None,
    state: str | None,
    overwrite: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> int:
    catalog.sync()
    if dataset_ref:
        dataset = catalog.get(dataset_ref)
        if dataset is None:
            raise SystemExit(f"Dataset not found: {dataset_ref}")
        datasets = [dataset]
    else:
        datasets = catalog.processable(state=state, limit=limit)

    processed = 0
    for dataset in datasets:
        try:
            process_registered_dataset(
                catalog,
                dataset,
                datasets_dir,
                overwrite,
                build_kwargs,
                project_config,
            )
            processed += 1
        except Exception as exc:
            record_dataset_error(catalog, dataset, exc)
            LOGGER.exception("Processing failed for dataset '%s'", dataset["name"])
            if dataset_ref:
                raise
    return processed


def process_registered_dataset(
    catalog: DatasetCatalog,
    dataset: dict[str, Any],
    datasets_dir: str | Path,
    overwrite: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, Any]:
    source = dataset.get("source") or {}
    source_url = source.get("url")
    if not source_url:
        raise ValueError(f"Dataset has no source URL: {dataset['name']}")

    datasets_root = Path(datasets_dir)
    dataset_name = dataset["name"]
    dataset_dir = datasets_root / dataset_relative_path(dataset_name)
    replace_existing_dir = dataset_dir.exists()
    temp_name = f"{dataset_name}__build_{uuid.uuid4().hex[:12]}" if replace_existing_dir else dataset_name
    backup_name = f"{dataset_name}__backup_{uuid.uuid4().hex[:12]}" if replace_existing_dir else None
    build_dir = datasets_root / dataset_relative_path(temp_name)
    backup_dir = datasets_root / dataset_relative_path(backup_name) if backup_name else None
    LOGGER.info("Processing dataset '%s' from %s", dataset["name"], source_url)
    prepared = prepare_dataset_source(dataset_dir, source_url, source)
    try:
        if prepared.source.get("type") != "local" and prepared.source.get("url") == source_url:
            catalog.update_state(dataset["id"], "downloaded")
        build_dataset(prepared.path, datasets_root, temp_name, True, build_kwargs)
        if prepared.source.get("type") != "local":
            if replace_existing_dir or not prepared_path_is_cached_download(dataset_dir, prepared.path):
                persist_source_copy(build_dir, prepared)
        write_source_summary(build_dir, prepared.source)
        if replace_existing_dir and backup_dir is not None:
            swap_dataset_dirs(dataset_dir, build_dir, backup_dir)
            cleanup_dataset_dir(datasets_root, backup_name)
        catalog.sync()
        catalog.update_metadata(dataset_name, {"max_zoom": int(build_kwargs["zoom"])})
        processed = catalog.get(dataset_name)
        if processed is None:
            raise RuntimeError(f"Dataset was built but not found in catalog: {dataset_name}")
        processed = catalog.update_source(processed["id"], prepared.source) or processed
        processed = catalog.update_state(processed["id"], "processed") or processed

        llm = llm_from_config(project_config)
        if llm.enabled:
            LOGGER.info(
                "LLM enrichment enabled: provider=%s chat_model=%s embedding_model=%s",
                llm.provider,
                llm.chat_model,
                llm.embedding_model,
            )
            processed = catalog.enrich(processed["id"], llm) or processed
        else:
            LOGGER.info("LLM enrichment disabled")
        processed = catalog.update_state(processed["id"], "ready") or processed
        processed = catalog.update_state(processed["id"], "published") or processed
        LOGGER.info("Published dataset %s with ID %s.", processed["name"], processed["id"])
        return processed
    except Exception:
        if replace_existing_dir and backup_dir is not None and backup_dir.exists() and not dataset_dir.exists():
            backup_dir.rename(dataset_dir)
        raise
    finally:
        if hasattr(prepared, "cleanup"):
            prepared.cleanup()
        if replace_existing_dir:
            cleanup_dataset_dir(datasets_root, temp_name)
            if backup_name is not None:
                cleanup_dataset_dir(datasets_root, backup_name)


def build_dataset(
    input_path: Path,
    datasets_dir: str | Path,
    dataset_name: str,
    overwrite: bool,
    build_kwargs: dict[str, Any],
) -> None:
    validate_dataset_name(dataset_name)
    dataset_dir = Path(datasets_dir) / dataset_relative_path(dataset_name)
    dataset_dir.parent.mkdir(parents=True, exist_ok=True)
    LOGGER.info("Building Starlet dataset '%s' under %s", dataset_name, datasets_dir)
    starlet.add_dataset(
        str(input_path),
        str(datasets_dir),
        name=dataset_name,
        overwrite=overwrite,
        **build_kwargs,
    )
    LOGGER.info("Starlet build finished for dataset '%s'", dataset_name)


def dataset_name_from_source(source: dict[str, Any], input_path: Path) -> str:
    metadata = source.get("metadata") or {}
    raw_name = metadata.get("title") or input_path.stem
    return safe_dataset_name_segment(str(raw_name))


def sync_source_and_enrich(
    catalog: DatasetCatalog,
    dataset_name: str,
    source: dict[str, Any],
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, Any]:
    LOGGER.info("Syncing catalog metadata")
    catalog.sync()
    catalog.update_metadata(dataset_name, {"max_zoom": int(build_kwargs["zoom"])})
    dataset = catalog.get(dataset_name)
    if dataset is None:
        raise RuntimeError(f"Dataset was built but not found in catalog: {dataset_name}")
    dataset = catalog.update_source(dataset["id"], source) or dataset

    llm = llm_from_config(project_config)
    if llm.enabled:
        LOGGER.info(
            "LLM enrichment enabled: provider=%s chat_model=%s embedding_model=%s",
            llm.provider,
            llm.chat_model,
            llm.embedding_model,
        )
        dataset = catalog.enrich(dataset["id"], llm) or dataset
    else:
        LOGGER.info("LLM enrichment disabled")
    return dataset


def refresh_datasets(
    catalog: DatasetCatalog,
    datasets_dir: str | Path,
    dataset_ref: str | None,
    force: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> int:
    catalog.sync()
    if dataset_ref:
        datasets = [catalog.get(dataset_ref)]
    else:
        datasets = [catalog.get(row["id"]) for row in catalog.list({})]
    refreshed = 0
    for dataset in [value for value in datasets if value is not None]:
        try:
            source = dataset.get("source") or {}
            if not source.get("url"):
                LOGGER.info("Skipping dataset '%s': no source information", dataset["name"])
                continue
            current = current_source_state(source)
            if not force and not source_is_newer(source, current):
                LOGGER.info("Dataset '%s' is up to date", dataset["name"])
                catalog.update_source(dataset["id"], current)
                continue
            LOGGER.info("Refreshing dataset '%s' from %s", dataset["name"], source["url"])
            refresh_dataset(
                catalog,
                dataset,
                datasets_dir,
                current.get("url") or source["url"],
                force,
                build_kwargs,
                project_config,
            )
            refreshed += 1
        except Exception:
            LOGGER.exception("Refresh failed for dataset '%s'", dataset["name"])
            if dataset_ref:
                raise
    return refreshed


def refresh_repositories(
    catalog: DatasetCatalog,
    datasets_dir: str | Path,
    repository_ref: str | None,
    force: bool,
    create_only: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> int:
    """Refresh one repository, or all non-default repositories."""
    catalog.sync()
    if repository_ref:
        repository = catalog.get_repository(repository_ref)
        if repository is None:
            raise SystemExit(f"Repository not found: {repository_ref}")
        repositories = [repository]
    else:
        repositories = [
            repository
            for repository in catalog.list_repositories()
            if not repository.get("is_default")
        ]

    updated = 0
    for repository in repositories:
        updated += refresh_repository(
            catalog,
            datasets_dir,
            repository,
            force,
            create_only,
            build_kwargs,
            project_config,
        )
    return updated


def refresh_repository(
    catalog: DatasetCatalog,
    datasets_dir: str | Path,
    repository: dict[str, Any],
    force: bool,
    create_only: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> int:
    """Rescan a repository and reconcile the catalog datasets that belong to it."""
    LOGGER.info("Refreshing repository '%s' from %s", repository["short_name"], repository["url"])
    sources = repository_sources(repository)
    seen_keys = {source_identity(source) for source in sources}
    seen_keys.discard(None)
    existing = [
        dataset
        for row in catalog.list({"state": "all", "repository": repository["id"]})
        if (dataset := catalog.get(row["id"])) is not None
    ]
    existing_by_key = {
        source_identity(dataset.get("source") or {}): dataset
        for dataset in existing
        if source_identity(dataset.get("source") or {}) is not None
    }

    updated = 0
    used_names = {dataset["name"] for dataset in existing}
    for source in sources:
        key = source_identity(source)
        if key is None:
            continue
        dataset = existing_by_key.get(key)
        if dataset is None:
            dataset_name = unique_dataset_name(
                catalog,
                repository_dataset_name(
                    repository,
                    dataset_name_from_source(
                        source,
                        Path((source.get("metadata") or {}).get("title") or "dataset"),
                    ),
                ),
                source,
                used_names,
                overwrite=False,
            )
            if dataset_name is None:
                continue
            dataset = catalog.register_source(
                dataset_name,
                source,
                description=(source.get("metadata") or {}).get("description"),
                repository_id=repository["id"],
                overwrite=False,
            )
            LOGGER.info("Registered new repository dataset '%s'", dataset["name"])
            used_names.add(dataset_name)
            updated += 1
            if create_only:
                continue
            try:
                process_registered_dataset(catalog, dataset, datasets_dir, False, build_kwargs, project_config)
            except Exception as exc:
                record_dataset_error(catalog, dataset, exc)
                LOGGER.exception("Processing failed for new repository dataset '%s'", dataset["name"])
            continue

        current = source
        source = dataset.get("source") or source
        catalog.update_source(dataset["id"], current)
        if create_only:
            updated += 1
            continue
        if force or source_is_newer(source, current):
            try:
                refresh_dataset(
                    catalog,
                    dataset,
                    datasets_dir,
                    current.get("url") or source.get("url"),
                    force,
                    build_kwargs,
                    project_config,
                )
                updated += 1
            except Exception as exc:
                record_dataset_error(catalog, dataset, exc)
                LOGGER.exception("Refresh failed for repository dataset '%s'", dataset["name"])

    for dataset in existing:
        key = source_identity(dataset.get("source") or {})
        if key not in seen_keys:
            LOGGER.info("Removing dataset '%s' because it no longer appears in repository '%s'", dataset["name"], repository["short_name"])
            try:
                starlet.delete_dataset(str(datasets_dir), dataset["name"], missing_ok=True)
            except Exception:
                LOGGER.exception("Could not delete dataset directory for '%s'", dataset["name"])
            catalog.delete(dataset["id"])
            updated += 1

    return updated


def repository_sources(repository: dict[str, Any]) -> list[dict[str, Any]]:
    """Discover current dataset sources for a repository row."""
    repository_type = repository.get("repository_type")
    url = repository.get("url")
    if repository_type == "ezesri_directory":
        return ezesri_catalog_sources(url, fetch_json(url))
    if repository_type == "esri_hub":
        client = EsriHubClient(url)
        sources: list[dict[str, Any]] = []
        for hub_dataset in client.iter_datasets(page_size=100):
            metadata = hub_dataset_metadata(client, hub_dataset)
            if is_supported_hub_dataset(hub_dataset, metadata):
                sources.append(esri_hub_source(client, hub_dataset, metadata))
        return sources
    raise ValueError(f"Unsupported repository type: {repository_type}")


def refresh_dataset(
    catalog: DatasetCatalog,
    dataset: dict[str, Any],
    datasets_dir: str | Path,
    source_url: str,
    force: bool,
    build_kwargs: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, Any]:
    datasets_root = Path(datasets_dir)
    name = dataset["name"]
    temp_name = f"{name}__refresh_{uuid.uuid4().hex[:12]}"
    backup_name = f"{name}__backup_{uuid.uuid4().hex[:12]}"
    temp_dir = datasets_root / dataset_relative_path(temp_name)
    backup_dir = datasets_root / dataset_relative_path(backup_name)
    dataset_dir = datasets_root / dataset_relative_path(name)

    try:
        prepared = prepare_dataset_source(dataset_dir, source_url, dataset.get("source") or {})
        try:
            if (
                prepared.source.get("type") != "local"
                and not prepared_path_is_cached_download(dataset_dir, prepared.path)
            ):
                persist_source_copy(dataset_dir, prepared)
            build_dataset(prepared.path, datasets_root, temp_name, True, build_kwargs)
            if prepared.source.get("type") != "local":
                persist_source_copy(temp_dir, prepared)
            write_source_summary(temp_dir, prepared.source)
            swap_dataset_dirs(dataset_dir, temp_dir, backup_dir)
            cleanup_dataset_dir(datasets_root, backup_name)
            refreshed = sync_source_and_enrich(
                catalog,
                name,
                prepared.source,
                build_kwargs,
                project_config,
            )
            LOGGER.info("Refreshed dataset %s with ID %s.", refreshed["name"], refreshed["id"])
            return refreshed
        finally:
            if hasattr(prepared, "cleanup"):
                prepared.cleanup()
    except Exception:
        if backup_dir.exists() and not dataset_dir.exists():
            backup_dir.rename(dataset_dir)
        raise
    finally:
        cleanup_dataset_dir(datasets_root, temp_name)
        cleanup_dataset_dir(datasets_root, backup_name)


def swap_dataset_dirs(dataset_dir: Path, temp_dir: Path, backup_dir: Path) -> None:
    if not temp_dir.exists():
        raise RuntimeError(f"Refresh build did not create {temp_dir}")
    if dataset_dir.exists():
        dataset_dir.rename(backup_dir)
    try:
        temp_dir.rename(dataset_dir)
    except Exception:
        if backup_dir.exists() and not dataset_dir.exists():
            backup_dir.rename(dataset_dir)
        raise


def prepared_path_is_cached_download(dataset_dir: Path, prepared_path: Path) -> bool:
    try:
        prepared_path.resolve().relative_to((dataset_dir / "download").resolve())
        return True
    except OSError:
        return False
    except ValueError:
        return False


def cleanup_dataset_dir(datasets_dir: Path, name: str) -> None:
    target = datasets_dir / dataset_relative_path(name)
    if not target.exists():
        return
    try:
        starlet.delete_dataset(str(datasets_dir), name, missing_ok=True)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)


def prepare_dataset_source(dataset_dir: Path, source_url: str, source: dict[str, Any]) -> Any:
    """Prefer a current cached download when it is newer than the remote source."""
    current = source
    cached_path: Path | None = None
    if source.get("type") != "local":
        try:
            current = source if source.get("modified_at") else current_source_state(source)
        except (urllib.error.URLError, urllib.error.HTTPError) as exc:
            current = {**source, "accessed_at": utc_now_iso()}
            cached_path = cached_download_path(dataset_dir, current)
            if cached_path is not None:
                LOGGER.warning(
                    "Could not reach remote source %s; using cached source copy at %s",
                    source_url,
                    cached_path,
                )
                return SimplePreparedSource(path=cached_path, source=current)
            raise exc

        cached_path = cached_download_path(dataset_dir, current)
        current_modified = parse_timestamp(current.get("modified_at"))
        cached_modified = timestamp_from_path(cached_path) if cached_path else None
        if cached_path and cached_modified is not None:
            if current_modified is None or cached_modified >= current_modified:
                LOGGER.info("Using cached source copy for %s", source_url)
                return SimplePreparedSource(path=cached_path, source=current)
    try:
        prepared = prepare_input_source(source_url)
    except (urllib.error.URLError, urllib.error.HTTPError):
        if source.get("type") != "local":
            fallback_path = cached_path or cached_download_path(dataset_dir, current)
            if fallback_path is not None:
                LOGGER.warning(
                    "Could not download remote source %s; using cached source copy at %s",
                    source_url,
                    fallback_path,
                )
                return SimplePreparedSource(path=fallback_path, source=current)
        raise
    prepared.source = merge_prepared_source_metadata(prepared.source, source)
    return prepared


def merge_prepared_source_metadata(
    prepared_source: dict[str, Any],
    stored_source: dict[str, Any],
) -> dict[str, Any]:
    stored_metadata = stored_source.get("metadata") or {}
    if not stored_metadata:
        return prepared_source
    updated = dict(prepared_source)
    prepared_metadata = dict(updated.get("metadata") or {})
    merged_metadata = {**stored_metadata, **prepared_metadata}
    if stored_metadata.get("attributes") and prepared_metadata.get("attributes"):
        merged_metadata["attributes"] = merge_source_attributes(
            prepared_metadata["attributes"],
            stored_metadata["attributes"],
        )
    updated["metadata"] = merged_metadata
    return updated


def cached_download_path(dataset_dir: Path, source: dict[str, Any]) -> Path | None:
    download_dir = dataset_dir / "download"
    if not download_dir.exists():
        return None
    if source.get("type") == "multi_remote_file":
        return download_dir if any(download_dir.iterdir()) else None
    metadata = source.get("metadata") or {}
    candidates = []
    filename = metadata.get("filename")
    if filename:
        candidates.append(download_dir / str(filename))
    title = metadata.get("title")
    if title:
        candidates.append(download_dir / f"{safe_filename(str(title))}.geojson")
        candidates.append(download_dir / safe_filename(str(title)))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    files = [path for path in download_dir.iterdir() if path.is_file()]
    if len(files) == 1:
        return files[0]
    if files:
        return max(files, key=lambda path: path.stat().st_mtime)
    return None


def find_dataset_by_source(catalog: DatasetCatalog, source: dict[str, Any]) -> dict[str, Any] | None:
    """Return the first catalog dataset whose source fingerprint matches ``source``."""
    source_key = source_identity(source)
    if source_key is None:
        return None
    for row in catalog.list({"state": "all"}):
        dataset = catalog.get(row["id"])
        if dataset is None:
            continue
        current = dataset.get("source")
        if current and source_identity(current) == source_key:
            return dataset
    return None


def source_identity(source: dict[str, Any]) -> tuple[Any, ...] | None:
    """Build a stable source fingerprint for duplicate detection."""
    if not source:
        return None

    source_type = source.get("type")
    url = source.get("url")
    metadata = source.get("metadata") or {}

    if source_type in {"local", "remote_file"} and url:
        return (source_type, url)

    if source_type == "esri_hub" and url:
        repository = metadata.get("repository") or {}
        return (
            source_type,
            url,
            repository.get("site_url"),
            metadata.get("record_id"),
            metadata.get("item_id"),
            metadata.get("layer_id"),
            metadata.get("service_url"),
        )

    return (source_type, url, json.dumps(metadata, sort_keys=True, default=str))


def log_existing_dataset_skip(source_label: str, existing: dict[str, Any]) -> None:
    """Log a warning when a dataset source already exists in the catalog."""
    existing_source = existing.get("source") or {}
    LOGGER.warning(
        "Skipping dataset '%s': already exists as '%s' (id=%s, type=%s, url=%s)",
        source_label,
        existing["name"],
        existing["id"],
        existing_source.get("type"),
        existing_source.get("url"),
    )


def record_dataset_error(catalog: DatasetCatalog, dataset: dict[str, Any], exc: BaseException) -> None:
    """Store a dataset error in the catalog for later inspection."""
    try:
        catalog.update_state(dataset["id"], "error", error_message=str(exc))
    except Exception:
        LOGGER.exception("Could not record error for dataset '%s'", dataset.get("name"))


def write_dataset_list(datasets: list[dict[str, Any]], output_format: str) -> None:
    rows = [
        {
            "name": dataset.get("name"),
            "id": dataset.get("id"),
            "status": dataset.get("dataset_state"),
            "size": dataset.get("size_bytes"),
        }
        for dataset in datasets
    ]
    if output_format == "json":
        print(json.dumps(rows, indent=2))
        return
    if output_format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=["name", "id", "status", "size"])
        writer.writeheader()
        writer.writerows(rows)
        return
    if not rows:
        print("No datasets found.")
        return
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    widths = {
        "name": max(len("name"), *(len(str(row["name"])) for row in rows)),
        "id": max(len("id"), *(len(str(row["id"])) for row in rows)),
        "status": max(len("status"), *(len(str(row["status"])) for row in rows)),
        "size": max(len("size"), *(len(human_readable_size(row["size"])) for row in rows)),
    }
    print(
        f"{'name'.ljust(widths['name'])}  "
        f"{'id'.ljust(widths['id'])}  "
        f"{'status'.ljust(widths['status'])}  "
        f"{'size'.rjust(widths['size'])}"
    )
    for row in rows:
        print(
            f"{str(row['name']).ljust(widths['name'])}  "
            f"{str(row['id']).ljust(widths['id'])}  "
            f"{str(row['status']).ljust(widths['status'])}  "
            f"{human_readable_size(row['size']).rjust(widths['size'])}"
        )
    print()
    print("Status summary")
    summary_name_width = max(len("status"), *(len(status) for status in status_counts))
    summary_count_width = max(len("count"), *(len(str(count)) for count in status_counts.values()))
    print(f"{'status'.ljust(summary_name_width)}  {'count'.rjust(summary_count_width)}")
    for status in sorted(status_counts):
        print(f"{status.ljust(summary_name_width)}  {str(status_counts[status]).rjust(summary_count_width)}")


def write_repository_list(repositories: list[dict[str, Any]], output_format: str) -> None:
    rows = [
        {
            "short_name": repository.get("short_name"),
            "id": repository.get("id"),
            "datasets": repository.get("total_datasets", 0),
            "url": repository.get("url"),
            "description": repository.get("description"),
        }
        for repository in repositories
    ]
    if output_format == "json":
        print(json.dumps(rows, indent=2))
        return
    if output_format == "csv":
        writer = csv.DictWriter(sys.stdout, fieldnames=["short_name", "id", "datasets", "url", "description"])
        writer.writeheader()
        writer.writerows(rows)
        return
    if not rows:
        print("No repositories found.")
        return
    columns = ["short_name", "id", "datasets", "url"]
    widths = {
        column: max(len(column), *(len(str(row[column])) for row in rows))
        for column in columns
    }
    print(
        f"{'short_name'.ljust(widths['short_name'])}  "
        f"{'id'.ljust(widths['id'])}  "
        f"{'datasets'.rjust(widths['datasets'])}  "
        f"{'url'.ljust(widths['url'])}"
    )
    for row in rows:
        print(
            f"{str(row['short_name']).ljust(widths['short_name'])}  "
            f"{str(row['id']).ljust(widths['id'])}  "
            f"{str(row['datasets']).rjust(widths['datasets'])}  "
            f"{str(row['url']).ljust(widths['url'])}"
        )


def human_readable_size(value: Any) -> str:
    """Format a byte count for table output."""
    try:
        size = float(value or 0)
    except (TypeError, ValueError):
        return "0b"
    units = ["b", "kb", "mb", "gb", "tb"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    if index == 0:
        return f"{int(size)}{units[index]}"
    return f"{size:.1f}{units[index]}"


def persist_source_copy(dataset_dir: Path, prepared: Any) -> None:
    """Keep a durable copy of downloaded source files under <dataset>/download."""
    source = getattr(prepared, "source", {}) or {}
    if source.get("type") == "local":
        return

    source_path = getattr(prepared, "path", None)
    if source_path is None:
        return

    download_dir = dataset_dir / "download"
    download_dir.mkdir(parents=True, exist_ok=True)

    source_name = Path(source_path).name
    destination = download_dir / source_name
    if Path(source_path).is_dir():
        for child in Path(source_path).iterdir():
            if not child.is_file():
                continue
            destination = download_dir / child.name
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.copy2(child, destination)
        return
    if destination.exists():
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    shutil.copy2(source_path, destination)


def timestamp_from_path(path: Path | None) -> datetime | None:
    if path is None or not path.exists():
        return None
    if path.is_dir():
        timestamps = [
            child.stat().st_mtime
            for child in path.rglob("*")
            if child.exists()
        ]
        if timestamps:
            return datetime.fromtimestamp(max(timestamps), tz=timezone.utc)
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


@dataclass(frozen=True)
class SimplePreparedSource:
    path: Path
    source: dict[str, Any]

    def cleanup(self) -> None:
        return


def source_is_newer(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    stored_time = parse_timestamp(stored.get("modified_at"))
    current_time = parse_timestamp(current.get("modified_at"))
    if current_time is None:
        return False
    if stored_time is None:
        return True
    return current_time > stored_time


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def write_source_summary(dataset_dir: Path, source: dict[str, Any]) -> None:
    summary = starlet.get_dataset_summary(dataset_dir) or {}
    metadata = source.get("metadata") or {}
    description = metadata.get("description")
    if description and not summary.get("description"):
        summary["description"] = description

    source_attributes = {
        attr.get("name"): attr
        for attr in metadata.get("attributes") or []
        if attr.get("name")
    }
    attributes = []
    for attr in summary.get("attributes") or []:
        updated = dict(attr)
        source_attr = source_attributes.get(updated.get("name"))
        if source_attr:
            if source_attr.get("type") and not updated.get("type"):
                updated["type"] = source_attr.get("type")
            if source_attr.get("description") and not updated.get("description"):
                updated["description"] = source_attr.get("description")
        attributes.append(updated)
    summary["attributes"] = attributes

    item = metadata.get("item") or {}
    if item and not summary.get("citation"):
        summary["citation"] = {
            "title": item.get("title"),
            "text": item.get("accessInformation") or item.get("licenseInfo"),
            "url": source.get("url"),
        }
    summary["source"] = source

    summary_path = dataset_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def configure_logging(level_name: str, *, output: str = "file", log_dir: str | Path = "log") -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    root = logging.getLogger()
    retained_handlers = [
        handler
        for handler in root.handlers
        if handler.__class__.__module__.startswith("_pytest.")
    ]
    root.handlers.clear()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    if output == "stdout":
        handler: logging.Handler = logging.StreamHandler(sys.stdout)
    else:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            log_path / "ucrstar.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
    handler.setFormatter(formatter)
    root.addHandler(handler)
    for retained_handler in retained_handlers:
        root.addHandler(retained_handler)
    logging.getLogger("werkzeug").setLevel(level)


if __name__ == "__main__":
    main()
