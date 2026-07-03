from pathlib import Path

from ucrstar2 import cli


def test_add_dataset_builds_and_catalogs_dataset(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"

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
    assert "Added dataset roads with ID" in capsys.readouterr().out
