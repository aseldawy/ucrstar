import logging
from pathlib import Path

from ucrstar2 import cli


def test_add_dataset_builds_and_catalogs_dataset(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    caplog.set_level(logging.INFO)

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["datasets_arg"] = datasets_arg
        calls["kwargs"] = kwargs
        (datasets_dir / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "list_datasets", lambda root: ["roads"])
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": "roads",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 10,
            "bbox": [0, 1, 2, 3],
            "has_mvt": True,
        },
    )
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_summary",
        lambda dataset: {
            "description": "Roads",
            "geometry": [{"geom_types": {"LineString": 2}, "total_points": 12}],
            "attributes": [],
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar2",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "add-dataset",
            str(input_path),
            "--name",
            "roads",
            "--overwrite",
            "--zoom",
            "8",
        ],
    )

    cli.main()

    assert calls["input_arg"] == str(input_path)
    assert calls["datasets_arg"] == str(datasets_dir)
    assert calls["kwargs"]["name"] == "roads"
    assert calls["kwargs"]["overwrite"] is True
    assert calls["kwargs"]["zoom"] == 8
    assert calls["kwargs"]["covering_bbox"] is True
    assert "Added dataset roads with ID" in caplog.text


def test_delete_dataset_removes_folder_and_catalog_entry(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "roads").mkdir(parents=True)
    db_path = tmp_path / "instance" / "catalog.sqlite"
    caplog.set_level(logging.INFO)

    monkeypatch.setattr(cli.starlet, "list_datasets", lambda root: ["roads"])
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": "roads",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 10,
            "bbox": [0, 1, 2, 3],
            "has_mvt": True,
        },
    )
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_summary",
        lambda dataset: {
            "description": "Roads",
            "geometry": [{"geom_types": {"LineString": 2}, "total_points": 12}],
            "attributes": [],
        },
    )

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]

    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        calls["datasets_arg"] = datasets_arg
        calls["name_arg"] = name_arg
        calls["kwargs"] = kwargs
        return True

    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar2",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "delete-dataset",
            dataset["id"],
        ],
    )

    cli.main()

    assert calls["datasets_arg"] == str(datasets_dir)
    assert calls["name_arg"] == "roads"
    assert calls["kwargs"]["missing_ok"] is False
    assert catalog.get(dataset["id"]) is None
    assert "Deleted dataset roads with ID" in caplog.text
