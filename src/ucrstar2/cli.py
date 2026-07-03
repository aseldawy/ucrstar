from __future__ import annotations

import argparse
import sys
from pathlib import Path

import starlet

if __package__:
    from .app import create_app
    from .catalog import DatasetCatalog
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ucrstar2.app import create_app
    from ucrstar2.catalog import DatasetCatalog


def main() -> None:
    parser = argparse.ArgumentParser(prog="ucrstar2")
    parser.add_argument("--datasets-dir", default="datasets")
    parser.add_argument("--database", default="instance/ucrstar2.sqlite")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--debug", action="store_true")

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

    subparsers.add_parser("sync-datasets")

    args = parser.parse_args()
    config = {"DATASETS_DIR": args.datasets_dir, "DATABASE": args.database}
    catalog = DatasetCatalog(args.database, args.datasets_dir)

    if args.command == "sync-datasets":
        rows = catalog.sync()
        print(f"Synced {len(rows)} dataset(s).")
        return
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

        starlet.add_dataset(
            args.input_path,
            args.datasets_dir,
            name=dataset_name,
            overwrite=args.overwrite,
            **build_kwargs,
        )
        catalog.sync()
        dataset = catalog.get(dataset_name)
        if dataset is None:
            raise RuntimeError(f"Dataset was built but not found in catalog: {dataset_name}")
        print(f"Added dataset {dataset['name']} with ID {dataset['id']}.")
        return
    if args.command == "serve":
        app = create_app(config)
        app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
