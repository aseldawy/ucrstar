from pathlib import Path

from ucrstar2.catalog import DatasetCatalog


def test_catalog_sync_keeps_stable_id(tmp_path: Path, monkeypatch) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "roads").mkdir(parents=True)
    db_path = tmp_path / "catalog.sqlite"

    monkeypatch.setattr("starlet.list_datasets", lambda root: ["roads"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
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
        "starlet.get_dataset_summary",
        lambda dataset: {
            "description": "Road network",
            "geometry": [
                {
                    "name": "geometry",
                    "geom_types": {"LineString": 4},
                    "mbr": [0, 1, 2, 3],
                    "total_points": 20,
                }
            ],
            "attributes": [{"name": "name", "role": "text"}],
        },
    )

    catalog = DatasetCatalog(db_path, datasets_dir)
    first = catalog.sync()[0]
    second = catalog.sync()[0]

    assert first["id"] == second["id"]
    assert catalog.get(first["id"])["name"] == "roads"
    assert catalog.get("roads")["num_features"] == 4
