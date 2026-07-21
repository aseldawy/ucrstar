from pathlib import Path

import numpy as np

from ucrstar.app import create_app


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
    assert body["datasets"][0]["dataset_state"] == "published"


def test_datasets_endpoint_defaults_to_published_state(tmp_path: Path) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "test.sqlite"
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": db_path,
        }
    )
    catalog = app.config["DATABASE"]
    from ucrstar.catalog import DatasetCatalog

    DatasetCatalog(catalog, datasets_dir).register_source(
        "queued",
        {
            "type": "local",
            "url": str(tmp_path / "queued.geojson"),
            "accessed_at": "2026-01-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {},
        },
        overwrite=True,
    )

    client = app.test_client()
    default_body = client.get("/datasets.json").get_json()
    created_body = client.get("/datasets.json?state=created").get_json()

    assert default_body["datasets"] == []
    assert created_body["datasets"][0]["name"] == "queued"
    assert created_body["datasets"][0]["dataset_state"] == "created"


def test_repository_endpoints_list_repositories_and_datasets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("starlet.list_datasets", lambda root: [])
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "test.sqlite"
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": db_path,
        }
    )

    from ucrstar.catalog import DatasetCatalog

    catalog = DatasetCatalog(db_path, datasets_dir)
    repository = catalog.upsert_repository(
        "lacounty",
        "https://egis-lacounty.hub.arcgis.com",
        description="LA County GIS data",
        repository_type="esri_hub",
    )
    dataset = catalog.register_source(
        "addresses",
        {
            "type": "esri_hub",
            "url": "https://www.arcgis.com/home/item.html?id=11111111111111111111111111111111",
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {},
        },
        repository_id=repository["id"],
    )
    catalog.update_state(dataset["id"], "published")

    client = app.test_client()
    repositories = client.get("/repositories.json").get_json()["repositories"]
    datasets = client.get("/repositories/lacounty/datasets.json").get_json()["datasets"]
    filtered = client.get("/datasets.json?repository=lacounty").get_json()["datasets"]

    lacounty = [repo for repo in repositories if repo["short_name"] == "lacounty"][0]
    assert lacounty["total_datasets"] == 1
    assert datasets[0]["name"] == "addresses"
    assert filtered[0]["repository_id"] == repository["id"]


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
            "size_bytes": 10 * 1024 * 1024,
            "bbox": [-1, -2, 3, 4],
            "has_mvt": False,
            "has_pmtiles": True,
            "mvt_tile_count": 12,
            "max_zoom": 19,
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

    assert detail["visualization"]["type"] == "VectorTile"
    assert detail["visualization"]["url"] == (
        f"/datasets/{dataset['id']}/tiles" + "/{z}/{x}/{y}.mvt"
    )
    assert detail["visualization"]["tiles"] == [detail["visualization"]["url"]]
    assert detail["visualization"]["source_layer"] == "layer0"
    assert detail["visualization"]["max_zoom"] == 19
    assert response.status_code == 200
    assert response.data == b"tile"
    assert response.mimetype == "application/vnd.mapbox-vector-tile"


def test_vector_style_endpoint_uses_dataset_max_zoom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "roads").mkdir(parents=True)

    monkeypatch.setattr("starlet.list_datasets", lambda root: ["roads"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
        lambda dataset: {
            "name": "roads",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 10 * 1024 * 1024,
            "bbox": [0, 0, 1, 1],
            "has_mvt": True,
            "max_zoom": 19,
        },
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "description": "Road centerlines",
            "geometry": [{"geom_types": {"LineString": 2}, "total_points": 12}],
            "attributes": [],
        },
    )

    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )

    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]
    style = client.get(f"/datasets/{dataset['id']}/style.json").get_json()

    assert style["sources"]["dataset"]["maxzoom"] == 19


def test_dataset_style_endpoint(tmp_path: Path, monkeypatch) -> None:
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

    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )
    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]

    response = client.get(f"/datasets/{dataset['id']}/style.json")

    assert response.status_code == 200
    style = response.get_json()
    assert style["version"] == 8
    assert style["sources"]["dataset"] == {
        "type": "geojson",
        "data": f"/datasets/{dataset['id']}/download.geojson",
    }
    assert next(layer for layer in style["layers"] if layer["id"] == "fill")["paint"]["fill-color"]


def test_small_dataset_details_select_geojson_visualization(tmp_path: Path, monkeypatch) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "places").mkdir(parents=True)
    monkeypatch.setattr("starlet.list_datasets", lambda root: ["places"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
        lambda dataset: {"size_bytes": 1024, "bbox": [0, 0, 1, 1], "has_mvt": True},
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "geometry": [{"geom_types": {"Point": 3}, "total_points": 3}],
            "attributes": [],
        },
    )
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "db.sqlite",
        }
    )
    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]
    detail = client.get(f"/datasets/{dataset['id']}.json").get_json()

    assert detail["visualization"] == {
        "type": "GeoJSON",
        "format": "geojson",
        "url": f"/datasets/{dataset['id']}/download.geojson",
        "download_url": f"/datasets/{dataset['id']}/download.geojson",
    }


def test_one_megabyte_dataset_uses_vector_tiles(tmp_path: Path, monkeypatch) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "boundary").mkdir(parents=True)
    monkeypatch.setattr("starlet.list_datasets", lambda root: ["boundary"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
        lambda dataset: {
            "size_bytes": 1_000_000,
            "bbox": [0, 0, 1, 1],
            "has_mvt": False,
            "has_pmtiles": True,
            "mvt_tile_count": 5,
        },
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "geometry": [{"geom_types": {"Polygon": 3}, "total_points": 12}],
            "attributes": [],
        },
    )
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "db.sqlite",
        }
    )
    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]
    detail = client.get(f"/datasets/{dataset['id']}.json").get_json()

    assert detail["visualization"]["type"] == "VectorTile"


def test_frontend_index_is_served(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DEBUG": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"maplibre-gl" in response.data
    assert b"/static/index.css" in response.data
    assert b"/static/index.js" in response.data


def test_frontend_index_is_debug_only(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DEBUG": False,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )

    response = app.test_client().get("/")

    assert response.status_code == 404


def test_static_fallback_is_debug_only(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DEBUG": False,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )

    response = app.test_client().get("/index.html")

    assert response.status_code == 404


def test_histogram_png_endpoint(tmp_path: Path, monkeypatch) -> None:
    datasets_dir = tmp_path / "datasets"
    histogram_dir = datasets_dir / "counties" / "histograms"
    histogram_dir.mkdir(parents=True)
    np.save(histogram_dir / "global.npy", np.arange(64, dtype=np.float64).reshape(8, 8))

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

    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )
    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]

    response = client.get(f"/datasets/{dataset['id']}/histogram.png?size=64")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_sample_geojson_returns_clean_feature(tmp_path: Path, monkeypatch) -> None:
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
    monkeypatch.setattr(
        "starlet.get_sample_record",
        lambda dataset, geometry: {
            "name": "Riverside",
            "geoid": "06065",
            "geometry": {
                "type": "Point",
                "coordinates": [-117.4, 33.9],
            },
        },
    )

    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": datasets_dir,
            "DATABASE": tmp_path / "instance" / "test.sqlite",
        }
    )
    client = app.test_client()
    dataset = client.get("/datasets.json").get_json()["datasets"][0]

    response = client.get(f"/datasets/{dataset['id']}/sample.geojson?MBR=-118,33,-117,34")

    assert response.status_code == 200
    feature = response.get_json()
    assert set(feature) == {"type", "geometry", "properties"}
    assert feature["type"] == "Feature"
    assert feature["geometry"] == {"type": "Point", "coordinates": [-117.4, 33.9]}
    assert feature["properties"] == {"name": "Riverside", "geoid": "06065"}
