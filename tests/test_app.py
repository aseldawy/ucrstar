from pathlib import Path

from ucrstar2.app import create_app


def test_datasets_endpoint_uses_catalog(tmp_path: Path, monkeypatch) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "counties").mkdir(parents=True)

    monkeypatch.setattr("starlet.list_datasets", lambda root: ["counties"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
        lambda dataset: {
            "name": "counties",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 100,
            "bbox": [-1, -2, 3, 4],
            "has_mvt": False,
        },
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "description": "County boundaries",
            "geometry": [{"geom_types": {"Polygon": 2}, "total_points": 12}],
            "attributes": [{"name": "geoid", "role": "text"}],
        },
    )

    db_path = tmp_path / "instance" / "test.sqlite"
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": db_path,
        }
    )

    assert not db_path.exists()
    response = app.test_client().get("/datasets.json?geometry_type=Polygon")

    assert response.status_code == 200
    assert db_path.exists()
    body = response.get_json()
    assert body["datasets"][0]["name"] == "counties"
    assert body["datasets"][0]["geometry_types"] == ["Polygon"]


def test_dataset_tiles_endpoint_uses_nested_dataset_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "counties").mkdir(parents=True)

    monkeypatch.setattr("starlet.list_datasets", lambda root: ["counties"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
        lambda dataset: {
            "name": "counties",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 100,
            "bbox": [-1, -2, 3, 4],
            "has_mvt": True,
        },
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "description": "County boundaries",
            "geometry": [{"geom_types": {"Polygon": 2}, "total_points": 12}],
            "attributes": [],
        },
    )
    monkeypatch.setattr("starlet.get_tile", lambda dataset, z, x, y: b"tile")

    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )

    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]
    detail = client.get(f"/datasets/{dataset['id']}.json").get_json()
    response = client.get(f"/datasets/{dataset['id']}/tiles/1/2/3.mvt")

    assert detail["visualization"]["url"] == (
        f"/datasets/{dataset['id']}/tiles" + "/{z}/{x}/{y}.mvt"
    )
    assert response.status_code == 200
    assert response.data == b"tile"
    assert response.mimetype == "application/vnd.mapbox-vector-tile"
