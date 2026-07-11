import logging
import tempfile
from pathlib import Path

from ucrstar import cli
from ucrstar.esri_hub import HubDataset
from ucrstar.sources import PreparedSource


def test_add_dataset_builds_and_catalogs_dataset(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
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
            "ucrstar",
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
    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    assert dataset["source"]["type"] == "local"
    assert dataset["source"]["url"] == str(input_path.resolve())
    assert dataset["dataset_state"] == "published"
    assert "Added dataset roads with ID" in caplog.text


def test_add_dataset_create_only_registers_source_without_building(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    caplog.set_level(logging.INFO)

    def fake_add_dataset(*args, **kwargs):
        calls["add_dataset"] = True

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
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
            "--create-only",
        ],
    )

    cli.main()

    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    assert dataset["dataset_state"] == "created"
    assert dataset["source"]["url"] == str(input_path.resolve())
    assert calls == {}
    assert "Registered dataset 'roads' in created state" in caplog.text


def test_add_dataset_create_only_registers_remote_source_timestamp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"

    monkeypatch.setattr(
        cli,
        "source_reference",
        lambda value: {
            "type": "remote_file",
            "url": value,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": "2026-06-30T06:24:35+00:00",
            "metadata": {
                "path": "/data/roads.geojson",
                "filename": "roads.geojson",
                "content_type": "application/geo+json",
            },
        },
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "add-dataset",
            url,
            "--create-only",
        ],
    )

    cli.main()

    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    assert dataset["dataset_state"] == "created"
    assert dataset["source"]["type"] == "remote_file"
    assert dataset["source"]["url"] == url
    assert dataset["source"]["modified_at"] == "2026-06-30T06:24:35+00:00"
    assert dataset["source"]["metadata"]["content_type"] == "application/geo+json"


def test_add_dataset_skips_existing_local_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    def fail_add_dataset(*args, **kwargs):
        raise AssertionError("starlet.add_dataset should not be called for duplicate local source")

    monkeypatch.setattr(cli.starlet, "add_dataset", fail_add_dataset)

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    catalog.register_source(
        "roads",
        cli.registration_source(str(input_path)),
        overwrite=True,
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "add-dataset",
            str(input_path),
            "--name",
            "roads_copy",
        ],
    )

    cli.main()

    assert cli.DatasetCatalog(db_path, datasets_dir).list({"state": "all"})[0]["name"] == "roads"


def test_add_dataset_skips_existing_remote_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"

    monkeypatch.setattr(
        cli,
        "source_reference",
        lambda value: {
            "type": "remote_file",
            "url": value,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": "2026-06-30T06:24:35+00:00",
            "metadata": {
                "path": "/data/roads.geojson",
                "filename": "roads.geojson",
                "content_type": "application/geo+json",
            },
        },
    )

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    catalog.register_source(
        "roads",
        cli.registration_source(url),
        overwrite=True,
    )

    def fail_add_dataset(*args, **kwargs):
        raise AssertionError("starlet.add_dataset should not be called for duplicate remote source")

    monkeypatch.setattr(cli.starlet, "add_dataset", fail_add_dataset)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "add-dataset",
            url,
        ],
    )

    cli.main()

    assert cli.DatasetCatalog(db_path, datasets_dir).list({"state": "all"})[0]["name"] == "roads"


def test_add_esri_hub_repository_skips_existing_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    calls = {"add_dataset": 0}

    class FakeHubClient:
        site_url = "https://egis-lacounty.hub.arcgis.com"
        search_base_url = "https://egis-lacounty.hub.arcgis.com/api/search/v1"
        download_base_url = "https://egis-lacounty.hub.arcgis.com/api/download/v1"

        def __init__(self, site_url):
            calls["site_url"] = site_url

        def iter_datasets(self, **kwargs):
            calls["iter_kwargs"] = kwargs
            return iter(
                [
                    HubDataset(
                        {
                            "id": "11111111111111111111111111111111_0",
                            "properties": {
                                "title": "Address Points",
                                "type": "Feature Layer",
                                "url": "https://services.example.com/FeatureServer",
                                "properties": {"downloads": {"formats": [{"key": "geojson"}]}},
                            },
                        }
                    )
                ]
            )

        def metadata(self, record_id):
            return {
                "record": {"id": record_id},
                "properties": {"title": "Address Points"},
                "arcgis_item": {
                    "id": "11111111111111111111111111111111",
                    "title": "Address Points",
                    "description": "<p>Address point layer</p>",
                    "modified": 1782800675000,
                },
                "layer": {"geometryType": "esriGeometryPoint"},
                "download_links": [{"format": "geojson", "url": "https://example.com/download"}],
            }

    monkeypatch.setattr(cli, "EsriHubClient", FakeHubClient)
    monkeypatch.setattr(cli.starlet, "add_dataset", lambda *args, **kwargs: calls.__setitem__("add_dataset", calls["add_dataset"] + 1))

    existing_source = cli.esri_hub_source(
        FakeHubClient("https://egis-lacounty.hub.arcgis.com/search"),
        HubDataset(
            {
                "id": "11111111111111111111111111111111_0",
                "properties": {
                    "title": "Address Points",
                    "type": "Feature Layer",
                    "url": "https://services.example.com/FeatureServer",
                    "properties": {"downloads": {"formats": [{"key": "geojson"}]}},
                },
            }
        ),
        FakeHubClient("https://egis-lacounty.hub.arcgis.com/search").metadata("11111111111111111111111111111111_0"),
    )
    cli.DatasetCatalog(db_path, datasets_dir).register_source("Address_Points", existing_source, overwrite=True)

    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "add-dataset",
            "https://egis-lacounty.hub.arcgis.com/search",
            "--create-only",
        ],
    )

    cli.main()

    assert calls["add_dataset"] == 0
    assert cli.DatasetCatalog(db_path, datasets_dir).list({"state": "all"})[0]["name"] == "Address_Points"


def test_add_dataset_create_only_registers_esri_hub_repository(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    calls = {}

    class FakeHubClient:
        site_url = "https://egis-lacounty.hub.arcgis.com"
        search_base_url = "https://egis-lacounty.hub.arcgis.com/api/search/v1"
        download_base_url = "https://egis-lacounty.hub.arcgis.com/api/download/v1"

        def __init__(self, site_url):
            calls["site_url"] = site_url

        def iter_datasets(self, **kwargs):
            calls["iter_kwargs"] = kwargs
            return iter(
                [
                    HubDataset(
                        {
                            "id": "11111111111111111111111111111111_0",
                            "properties": {
                                "title": "Address Points",
                                "type": "Feature Layer",
                                "url": "https://services.example.com/FeatureServer",
                                "properties": {"downloads": {"formats": [{"key": "geojson"}]}},
                            },
                        }
                    ),
                    HubDataset(
                        {
                            "id": "22222222222222222222222222222222",
                            "properties": {"title": "PDF Map", "type": "PDF"},
                        }
                    ),
                ]
            )

        def metadata(self, record_id):
            if record_id == "22222222222222222222222222222222":
                return {
                    "record": {"id": record_id},
                    "properties": {"title": "PDF Map"},
                    "arcgis_item": {
                        "id": "22222222222222222222222222222222",
                        "title": "PDF Map",
                        "type": "PDF",
                    },
                    "download_links": [],
                }
            return {
                "record": {"id": record_id},
                "properties": {"title": "Address Points"},
                "arcgis_item": {
                    "id": "11111111111111111111111111111111",
                    "title": "Address Points",
                    "description": "<p>Address point layer</p>",
                    "modified": 1782800675000,
                },
                "layer": {"geometryType": "esriGeometryPoint"},
                "download_links": [{"format": "geojson", "url": "https://example.com/download"}],
            }

    def fake_add_dataset(*args, **kwargs):
        calls["add_dataset"] = True

    monkeypatch.setattr(cli, "EsriHubClient", FakeHubClient)
    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "add-dataset",
            "https://egis-lacounty.hub.arcgis.com/search",
            "--create-only",
        ],
    )

    cli.main()

    datasets = cli.DatasetCatalog(db_path, datasets_dir).list({"state": "created"})
    assert [dataset["name"] for dataset in datasets] == ["Address_Points"]
    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("Address_Points")
    assert dataset is not None
    assert calls["site_url"] == "https://egis-lacounty.hub.arcgis.com/search"
    assert calls["iter_kwargs"] == {"page_size": 100}
    assert calls.get("add_dataset") is None
    assert dataset["description"] == "Address point layer"
    assert dataset["source"]["type"] == "esri_hub"
    assert dataset["source"]["url"] == "https://www.arcgis.com/home/item.html?id=11111111111111111111111111111111"
    assert dataset["source"]["modified_at"] == "2026-06-30T06:24:35+00:00"
    assert dataset["source"]["metadata"]["record_id"] == "11111111111111111111111111111111_0"
    assert dataset["source"]["metadata"]["repository"]["site_url"] == "https://egis-lacounty.hub.arcgis.com"
    assert dataset["source"]["metadata"]["hub"]["arcgis_item"]["title"] == "Address Points"


def test_process_dataset_processes_created_dataset(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    caplog.set_level(logging.INFO)

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        (Path(datasets_arg) / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: sorted(path.name for path in Path(root).iterdir() if path.is_dir()) if Path(root).exists() else [],
    )
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": Path(dataset).name,
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
    catalog.register_source(
        "roads",
        cli.registration_source(str(input_path)),
        overwrite=True,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "process-dataset",
            "--limit",
            "1",
        ],
    )

    cli.main()

    dataset = catalog.get("roads")
    assert calls["input_arg"] == str(input_path.resolve())
    assert calls["name"] == "roads"
    assert dataset["dataset_state"] == "published"
    assert "Published dataset roads with ID" in caplog.text


def test_process_dataset_downloads_remote_source_to_temporary_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"

    def fake_prepare_input_source(value):
        tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-test-")
        downloaded = Path(tempdir.name) / "roads.geojson"
        downloaded.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        return PreparedSource(
            path=downloaded,
            source={
                "type": "remote_file",
                "url": value,
                "accessed_at": "2026-07-01T00:00:00+00:00",
                "modified_at": "2026-06-30T06:24:35+00:00",
                "metadata": {"downloaded_path": str(downloaded)},
            },
            _tempdir=tempdir,
        )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        assert Path(input_arg).exists()
        assert Path(input_arg).parent.name.startswith("ucrstar-test-")
        (Path(datasets_arg) / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli, "prepare_input_source", fake_prepare_input_source)
    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: sorted(path.name for path in Path(root).iterdir() if path.is_dir()) if Path(root).exists() else [],
    )
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": Path(dataset).name,
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
    catalog.register_source(
        "roads",
        {
            "type": "remote_file",
            "url": url,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": "2026-06-29T00:00:00+00:00",
            "metadata": {"filename": "roads.geojson"},
        },
        overwrite=True,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "process-dataset",
            "roads",
        ],
    )

    cli.main()

    dataset = catalog.get("roads")
    assert calls["name"] == "roads"
    assert calls["input_arg"].endswith("roads.geojson")
    assert dataset["dataset_state"] == "published"
    assert dataset["source"]["url"] == url
    assert dataset["source"]["modified_at"] == "2026-06-30T06:24:35+00:00"


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
            "ucrstar",
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


def test_refresh_rebuilds_newer_source_under_temporary_name(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = []
    datasets_dir = tmp_path / "datasets"
    roads_dir = datasets_dir / "roads"
    roads_dir.mkdir(parents=True)
    db_path = tmp_path / "instance" / "catalog.sqlite"
    source_path = tmp_path / "roads.geojson"
    source_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    caplog.set_level(logging.INFO)

    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: sorted(path.name for path in Path(root).iterdir() if path.is_dir()),
    )
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": Path(dataset).name,
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

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls.append(kwargs["name"])
        (Path(datasets_arg) / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        target = Path(datasets_arg) / name_arg
        if target.exists():
            target.rmdir()
            return True
        return False

    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "local",
            "url": str(source_path),
            "accessed_at": "2026-01-01T00:00:00+00:00",
            "modified_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"path": str(source_path)},
        },
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(datasets_dir),
            "--database",
            str(db_path),
            "--config",
            str(tmp_path / "missing-config.json"),
            "refresh",
            "roads",
        ],
    )

    cli.main()

    assert calls
    assert calls[0].startswith("roads__refresh_")
    refreshed = catalog.get("roads")
    assert refreshed["source"]["url"] == str(source_path.resolve())
    assert "Refreshed dataset roads with ID" in caplog.text
