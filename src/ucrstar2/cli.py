from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import starlet

LOGGER = logging.getLogger(__name__)

if __package__:
    from .app import create_app
    from .catalog import DatasetCatalog
    from .config import load_config
    from .esri_hub import EsriHubClient, HubDataset
    from .llm import llm_from_config
    from .sources import clean_html, current_source_state, source_reference, prepare_input_source, utc_now_iso
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ucrstar2.app import create_app
    from ucrstar2.catalog import DatasetCatalog
    from ucrstar2.config import load_config
    from ucrstar2.esri_hub import EsriHubClient, HubDataset
    from ucrstar2.llm import llm_from_config
    from ucrstar2.sources import clean_html, current_source_state, source_reference, prepare_input_source, utc_now_iso


def main() -> None:
    parser = argparse.ArgumentParser(prog="ucrstar2")
    parser.add_argument("--datasets-dir", default="datasets")
    parser.add_argument("--database", default="instance/ucrstar2.sqlite")
    parser.add_argument("--config", default="ucrstar2.config.json")
    parser.add_argument("--log-level", default="INFO")
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
        help="Build a Starlet dataset and add it to the catalog.",
    )
    add_dataset.add_argument("input_path")
    add_dataset.add_argument("--name")
    add_dataset.add_argument("--overwrite", action="store_true")
    add_dataset.add_argument(
        "--create-only",
        action="store_true",
        help="Only register the dataset source in the database; do not build or publish it.",
    )
    add_dataset.add_argument("--zoom", type=int, default=7)
    add_dataset.add_argument("--partition-size", type=int)
    add_dataset.add_argument("--threshold", type=int)
    add_dataset.add_argument(
        "--no-covering-bbox",
        action="store_true",
        help="Do not write per-row bounding boxes for faster spatial pruning.",
    )
    add_dataset.add_argument("--pmtiles", action="store_true")

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

    args = parser.parse_args()
    configure_logging(args.log_level)
    project_config = load_config(args.config)
    config = {
        "DATASETS_DIR": args.datasets_dir,
        "DATABASE": args.database,
        "UCRSTAR2_CONFIG": project_config,
    }
    catalog = DatasetCatalog(args.database, args.datasets_dir)

    if args.command == "add-dataset":
        LOGGER.info("Adding dataset from %s", args.input_path)
        added = add_dataset_from_source(
            catalog,
            args.input_path,
            args.name,
            args.datasets_dir,
            args.overwrite,
            args.create_only,
            build_kwargs_from_args(args),
            project_config,
        )
        if isinstance(added, list):
            LOGGER.info("Added %d dataset(s).", len(added))
        else:
            LOGGER.info("Added dataset %s with ID %s.", added["name"], added["id"])
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
    if args.command == "serve":
        app = create_app(config)
        app.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            use_reloader=args.reload,
        )


def add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--zoom", type=int, default=7)
    parser.add_argument("--partition-size", type=int)
    parser.add_argument("--threshold", type=int)
    parser.add_argument(
        "--no-covering-bbox",
        action="store_true",
        help="Do not write per-row bounding boxes for faster spatial pruning.",
    )
    parser.add_argument("--pmtiles", action="store_true")


def add_refresh_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "dataset",
        nargs="?",
        help="Dataset ID or name. If omitted, check all datasets with source information.",
    )
    parser.add_argument("--force", action="store_true", help="Refresh even if timestamps match.")
    add_build_arguments(parser)


def build_kwargs_from_args(args: argparse.Namespace) -> dict[str, Any]:
    build_kwargs = {
        "zoom": args.zoom,
        "covering_bbox": not args.no_covering_bbox,
        "pmtiles": args.pmtiles,
    }
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
) -> dict[str, Any] | list[dict[str, Any]]:
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
        source = registration_source(input_path_or_url)
        dataset_name = dataset_name or dataset_name_from_source(source, source_name_path(input_path_or_url, source))
        dataset = catalog.register_source(
            dataset_name,
            source,
            description=(source.get("metadata") or {}).get("description"),
            overwrite=overwrite,
        )
        LOGGER.info("Registered dataset '%s' in created state", dataset["name"])
        return dataset

    source = registration_source(input_path_or_url)
    dataset_name = dataset_name or dataset_name_from_source(source, source_name_path(input_path_or_url, source))
    dataset = catalog.register_source(
        dataset_name,
        source,
        description=(source.get("metadata") or {}).get("description"),
        overwrite=overwrite,
    )
    try:
        return process_registered_dataset(catalog, dataset, datasets_dir, overwrite, build_kwargs, project_config)
    except Exception as exc:
        catalog.update_state(dataset["id"], "error", error_message=str(exc))
        raise


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
        dataset_name = dataset_name_from_source(source, Path(hub_dataset.title))
        try:
            dataset = catalog.register_source(
                dataset_name,
                source,
                description=(source.get("metadata") or {}).get("description"),
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
                catalog.update_state(existing["id"], "error", error_message=str(exc))
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


def is_esri_hub_repository_url(value: str) -> bool:
    parsed = urllib_parse(value)
    if "hub.arcgis.com" not in parsed.netloc.lower():
        return False
    return "/datasets/" not in parsed.path.lower()


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


def registration_source(input_path_or_url: str) -> dict[str, Any]:
    return source_reference(input_path_or_url)


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
            catalog.update_state(dataset["id"], "error", error_message=str(exc))
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

    LOGGER.info("Processing dataset '%s' from %s", dataset["name"], source_url)
    with prepare_input_source(source_url) as prepared:
        if prepared.source.get("type") != "local":
            catalog.update_state(dataset["id"], "downloaded")
        build_dataset(prepared.path, datasets_dir, dataset["name"], overwrite, build_kwargs)
        write_source_summary(Path(datasets_dir) / dataset["name"], prepared.source)
        catalog.sync()
        processed = catalog.get(dataset["name"])
        if processed is None:
            raise RuntimeError(f"Dataset was built but not found in catalog: {dataset['name']}")
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


def build_dataset(
    input_path: Path,
    datasets_dir: str | Path,
    dataset_name: str,
    overwrite: bool,
    build_kwargs: dict[str, Any],
) -> None:
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
    name = "".join(char if char.isalnum() or char in "._-" else "_" for char in raw_name)
    return name.strip("._") or "dataset"


def sync_source_and_enrich(
    catalog: DatasetCatalog,
    dataset_name: str,
    source: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, Any]:
    LOGGER.info("Syncing catalog metadata")
    catalog.sync()
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
    temp_dir = datasets_root / temp_name
    backup_dir = datasets_root / backup_name
    dataset_dir = datasets_root / name

    try:
        with prepare_input_source(source_url) as prepared:
            build_dataset(prepared.path, datasets_root, temp_name, True, build_kwargs)
            write_source_summary(temp_dir, prepared.source)
            swap_dataset_dirs(dataset_dir, temp_dir, backup_dir)
            cleanup_dataset_dir(datasets_root, backup_name)
            refreshed = sync_source_and_enrich(catalog, name, prepared.source, project_config)
            LOGGER.info("Refreshed dataset %s with ID %s.", refreshed["name"], refreshed["id"])
            return refreshed
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


def cleanup_dataset_dir(datasets_dir: Path, name: str) -> None:
    target = datasets_dir / name
    if not target.exists():
        return
    try:
        starlet.delete_dataset(str(datasets_dir), name, missing_ok=True)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)


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
            updated.setdefault("type", source_attr.get("type"))
            updated.setdefault("description", source_attr.get("description"))
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


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


if __name__ == "__main__":
    main()
