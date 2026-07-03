from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import starlet

LOGGER = logging.getLogger(__name__)

if __package__:
    from .app import create_app
    from .catalog import DatasetCatalog
    from .config import load_config
    from .llm import llm_from_config
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ucrstar2.app import create_app
    from ucrstar2.catalog import DatasetCatalog
    from ucrstar2.config import load_config
    from ucrstar2.llm import llm_from_config


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
    serve.add_argument("--debug", action="store_true")
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
    add_dataset.add_argument("--zoom", type=int, default=7)
    add_dataset.add_argument("--partition-size", type=int)
    add_dataset.add_argument("--threshold", type=int)
    add_dataset.add_argument(
        "--no-covering-bbox",
        action="store_true",
        help="Do not write per-row bounding boxes for faster spatial pruning.",
    )
    add_dataset.add_argument("--pmtiles", action="store_true")

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
        dataset_name = args.name or Path(args.input_path).stem
        build_kwargs = {
            "zoom": args.zoom,
            "covering_bbox": not args.no_covering_bbox,
            "pmtiles": args.pmtiles,
        }
        if args.partition_size is not None:
            build_kwargs["partition_size"] = args.partition_size
        if args.threshold is not None:
            build_kwargs["threshold"] = args.threshold

        LOGGER.info("Adding dataset '%s' from %s", dataset_name, args.input_path)
        LOGGER.info("Building Starlet dataset under %s", args.datasets_dir)
        starlet.add_dataset(
            args.input_path,
            args.datasets_dir,
            name=dataset_name,
            overwrite=args.overwrite,
            **build_kwargs,
        )
        LOGGER.info("Starlet build finished for dataset '%s'", dataset_name)
        LOGGER.info("Syncing catalog metadata")
        catalog.sync()
        dataset = catalog.get(dataset_name)
        if dataset is None:
            raise RuntimeError(f"Dataset was built but not found in catalog: {dataset_name}")
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
        LOGGER.info("Added dataset %s with ID %s.", dataset["name"], dataset["id"])
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
    if args.command == "serve":
        app = create_app(config)
        app.run(
            host=args.host,
            port=args.port,
            debug=args.debug,
            use_reloader=args.reload,
        )


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


if __name__ == "__main__":
    main()
