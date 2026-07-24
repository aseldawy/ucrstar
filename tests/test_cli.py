import json
import logging
import tempfile
from pathlib import Path
import urllib.error

from ucrstar import cli
from ucrstar.esri_hub import HubDataset
from ucrstar.sources import PreparedSource


def test_serve_prints_url_to_stdout(tmp_path: Path, monkeypatch, capsys) -> None:
    calls = {}

    class FakeApp:
        def run(self, **kwargs):
            calls["run"] = kwargs

    monkeypatch.setattr(cli, "create_app", lambda config: FakeApp())
    monkeypatch.setattr(
        "sys.argv",
        [
            "ucrstar",
            "--datasets-dir",
            str(tmp_path / "datasets"),
            "--database",
            str(tmp_path / "instance" / "catalog.sqlite"),
            "--config",
            str(tmp_path / "missing-config.json"),
            "--log-output",
            "file",
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "8123",
        ],
    )

    cli.main()

    assert "Serving UCR Star at http://127.0.0.1:8123/" in capsys.readouterr().out
    assert calls["run"]["host"] == "0.0.0.0"
    assert calls["run"]["port"] == 8123


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


def test_add_dataset_passes_csv_indexes_to_starlet(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.csv"
    input_path.write_text("x,y,wkt\n1,2,POINT(1 2)\n", encoding="utf-8")

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["datasets_arg"] = datasets_arg
        calls["kwargs"] = kwargs
        (datasets_dir / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "get_config", lambda: {"mvt": {"zoom": 10}})
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
            "geometry": [{"geom_types": {"Point": 1}, "total_points": 1}],
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
            "--csv-x-index",
            "0",
            "--csv-y-index",
            "1",
            "--csv-wkt-index",
            "2",
        ],
    )

    cli.main()

    assert calls["input_arg"] == str(input_path)
    assert calls["datasets_arg"] == str(datasets_dir)
    assert calls["kwargs"]["csv_x_index"] == 0
    assert calls["kwargs"]["csv_y_index"] == 1
    assert calls["kwargs"]["csv_wkt_index"] == 2


def test_add_dataset_keeps_catalog_row_when_remote_url_is_unreachable(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    caplog.set_level(logging.INFO)

    def fake_prepare_input_source(value):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(cli, "prepare_input_source", fake_prepare_input_source)
    monkeypatch.setattr(cli.starlet, "get_config", lambda: {"mvt": {"zoom": 10}})
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
            "https://example.com/data/roads.geojson",
            "--name",
            "roads",
        ],
    )

    cli.main()

    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    assert dataset is not None
    assert dataset["dataset_state"] == "error"
    assert "Could not reach remote URL" in caplog.text or "could not be reached" in caplog.text


def test_add_dataset_remembers_starlet_config_zoom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["kwargs"] = kwargs
        (datasets_dir / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "get_config", lambda: {"mvt": {"zoom": 19}})
    monkeypatch.setattr(cli.starlet, "list_datasets", lambda root: ["roads"])
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": "roads",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 10 * 1024 * 1024,
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
        ],
    )

    cli.main()

    assert calls["kwargs"]["zoom"] == 19
    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    assert dataset["metadata_json"]["max_zoom"] == 19
    assert dataset["visualization"]["max_zoom"] == 19


def test_add_dataset_allows_nested_dataset_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["kwargs"] = kwargs
        dataset_dir = datasets_dir / "osm21" / "roads"
        dataset_dir.mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "get_config", lambda: {"mvt": {"zoom": 10}})
    monkeypatch.setattr(cli.starlet, "list_datasets", lambda root: ["osm21/roads"])
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": "roads",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 1024,
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
            "osm21/roads",
        ],
    )

    cli.main()

    assert calls["kwargs"]["name"] == "osm21/roads"
    assert (datasets_dir / "osm21").is_dir()
    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("osm21/roads")
    assert dataset["name"] == "osm21/roads"
    assert dataset["dataset_state"] == "published"


def test_add_dataset_saves_local_schema_doc_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    input_path = tmp_path / "source.geojson"
    schema_doc = tmp_path / "schema-doc.json"
    input_path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    schema_doc.write_text(
        json.dumps(
            {
                "description": "Road centerlines maintained by the city.",
                "attributes": [
                    {
                        "name": "ROAD_NAME",
                        "description": "Official street name.",
                    },
                    {
                        "name": "LANES",
                        "type": "integer",
                        "description": "Number of through lanes.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        (datasets_dir / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "get_config", lambda: {"mvt": {"zoom": 10}})
    monkeypatch.setattr(cli.starlet, "list_datasets", lambda root: ["roads"])
    monkeypatch.setattr(
        cli.starlet,
        "get_dataset_metadata",
        lambda dataset: {
            "name": "roads",
            "path": str(dataset),
            "exists": True,
            "size_bytes": 1024,
            "bbox": [0, 1, 2, 3],
            "has_mvt": True,
        },
    )
    def fake_get_dataset_summary(dataset):
        summary_path = Path(dataset) / "summary.json"
        if summary_path.exists():
            return json.loads(summary_path.read_text(encoding="utf-8"))
        return {
            "description": None,
            "geometry": [{"geom_types": {"LineString": 2}, "total_points": 12}],
            "attributes": [
                {"name": "ROAD_NAME", "type": "string", "description": None},
                {"name": "LANES", "type": None, "description": None},
            ],
        }

    monkeypatch.setattr(cli.starlet, "get_dataset_summary", fake_get_dataset_summary)
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
            "--schema-doc",
            str(schema_doc),
        ],
    )

    cli.main()

    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    schema = {field["name"]: field for field in dataset["schema"]}
    assert dataset["description"] == "Road centerlines maintained by the city."
    assert dataset["source"]["metadata"]["description"] == "Road centerlines maintained by the city."
    assert schema["ROAD_NAME"]["description"] == "Official street name."
    assert schema["LANES"]["type"] == "integer"
    assert schema["LANES"]["description"] == "Number of through lanes."


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
        lambda value, *, probe_remote=True: {
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


def test_add_dataset_create_only_does_not_probe_remote_url(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"

    def fail_source_reference(value, *, probe_remote=True):
        assert value == url
        if probe_remote:
            raise AssertionError("create-only should not probe remote URLs")
        return {
            "type": "remote_file",
            "url": value,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {
                "url": value,
                "path": "/data/roads.geojson",
                "netloc": "example.com",
                "filename": "roads.geojson",
                "content_type": None,
                "content_length": None,
            },
        }

    monkeypatch.setattr(cli, "source_reference", fail_source_reference)
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
    assert dataset["source"]["url"] == url
    assert dataset["source"]["metadata"]["filename"] == "roads.geojson"


def test_add_dataset_can_disable_generated_downloads(
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
            "modified_at": None,
            "metadata": {"filename": "roads.geojson"},
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
            "--no-download",
        ],
    )

    cli.main()

    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("roads")
    assert dataset["downloads_enabled"] is False
    assert dataset["source"]["url"] == url


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
    assert [dataset["name"] for dataset in datasets] == ["egis-lacounty/Address_Points"]
    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("egis-lacounty/Address_Points")
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


def test_add_datasets_create_only_registers_ezesri_catalog_layers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    calls = {}
    catalog_url = "https://www.ezesri.com/catalog.json"

    monkeypatch.setattr(
        cli,
        "fetch_json",
        lambda url: {
            "generated": "2026-02-01T19:11:17.738739",
            "services": [
                {
                    "id": "d3a78deedc0749eeb3ed9069773d5551",
                    "title": "German State Boundaries",
                    "place": "Deutschland, DE",
                    "category": "Boundaries",
                    "categoryKey": "boundaries",
                    "url": "https://services2.arcgis.com/example/arcgis/rest/services/Germany/FeatureServer",
                    "description": "<p>Federal state boundaries</p>",
                    "owner": "esri_DE_content",
                    "numViews": 100,
                    "tags": ["boundaries"],
                    "layers": [{"id": 0, "name": "States", "type": "esriGeometryPolygon"}],
                    "layerCount": 1,
                    "capabilities": "Query,Extract",
                    "maxRecordCount": 1000,
                },
                {
                    "id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "title": "Public Safety",
                    "place": "Example County",
                    "category": "Emergency",
                    "categoryKey": "emergency",
                    "url": "https://services.example.com/arcgis/rest/services/PublicSafety/MapServer",
                    "description": "Public safety layers",
                    "owner": "county",
                    "numViews": 10,
                    "tags": ["safety"],
                    "layers": [
                        {"id": 2, "name": "Stations", "type": "esriGeometryPoint"},
                        {"id": 3, "name": "Districts", "type": "esriGeometryPolygon"},
                    ],
                    "layerCount": 2,
                    "capabilities": "Query",
                    "maxRecordCount": 2000,
                },
                {
                    "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "title": "Tiles Only",
                    "url": "https://services.example.com/arcgis/rest/services/TilesOnly/FeatureServer",
                    "layers": [{"id": 0, "name": "Tiles", "type": "esriGeometryPolygon"}],
                    "layerCount": 1,
                    "capabilities": "Tiles",
                },
            ],
        },
    )

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
            "add-datasets",
            catalog_url,
            "--create-only",
        ],
    )

    cli.main()

    datasets = cli.DatasetCatalog(db_path, datasets_dir).list({"state": "created"})
    assert [dataset["name"] for dataset in datasets] == [
        "ezesri/German_State_Boundaries",
        "ezesri/Public_Safety_-_Districts",
        "ezesri/Public_Safety_-_Stations",
    ]
    assert calls == {}

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    states = catalog.get("ezesri/German_State_Boundaries")
    assert states["description"] == "Federal state boundaries"
    assert states["source"]["type"] == "ezesri_directory"
    assert states["source"]["url"] == "https://services2.arcgis.com/example/arcgis/rest/services/Germany/FeatureServer/0"
    assert states["source"]["metadata"]["canonical_id"] == "arcgis-item:d3a78deedc0749eeb3ed9069773d5551:0"
    assert states["source"]["metadata"]["repository"]["catalog_url"] == catalog_url
    assert states["source"]["metadata"]["repository"]["generated_at"] == "2026-02-01T19:11:17.738739"
    assert states["source"]["metadata"]["directory_service"]["owner"] == "esri_DE_content"


def test_refresh_repository_adds_new_and_removes_missing_datasets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    catalog_url = "https://www.ezesri.com/catalog.json"
    calls = {}

    repository = cli.DatasetCatalog(db_path, datasets_dir).upsert_repository(
        "ezesri",
        catalog_url,
        description="ezesri directory",
        repository_type="ezesri_directory",
    )
    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    catalog.register_source(
        "Obsolete",
        {
            "type": "ezesri_directory",
            "url": "https://services.example.com/arcgis/rest/services/Old/FeatureServer/0",
            "accessed_at": "2026-01-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {"title": "Obsolete", "canonical_id": "old"},
        },
        repository_id=repository["id"],
    )

    monkeypatch.setattr(
        cli,
        "fetch_json",
        lambda url: {
            "generated": "2026-02-01T19:11:17.738739",
            "services": [
                {
                    "id": "d3a78deedc0749eeb3ed9069773d5551",
                    "title": "German State Boundaries",
                    "url": "https://services2.arcgis.com/example/arcgis/rest/services/Germany/FeatureServer",
                    "description": "Federal state boundaries",
                    "layers": [{"id": 0, "name": "States", "type": "esriGeometryPolygon"}],
                    "layerCount": 1,
                    "capabilities": "Query",
                }
            ],
        },
    )

    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        calls["deleted"] = name_arg
        return True

    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)
    monkeypatch.setattr(cli.starlet, "list_datasets", lambda root: [])
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
            "refresh-repositories",
            "ezesri",
            "--create-only",
        ],
    )

    cli.main()

    refreshed = cli.DatasetCatalog(db_path, datasets_dir)
    assert refreshed.get("Obsolete") is None
    assert refreshed.get("ezesri/German_State_Boundaries")["repository_id"] == repository["id"]
    assert calls["deleted"] == "Obsolete"


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


def test_process_dataset_downloads_multiple_remote_sources_to_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    urls = "https://example.com/data/roads.geojson\nhttps://example.com/data/parks.geojson"

    def fake_prepare_input_source(value):
        tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-test-")
        source_dir = Path(tempdir.name)
        (source_dir / "roads.geojson").write_text("roads", encoding="utf-8")
        (source_dir / "parks.geojson").write_text("parks", encoding="utf-8")
        return PreparedSource(
            path=source_dir,
            source={
                "type": "multi_remote_file",
                "url": value,
                "accessed_at": "2026-07-01T00:00:00+00:00",
                "modified_at": "2026-06-30T06:24:35+00:00",
                "metadata": {
                    "urls": value.splitlines(),
                    "downloaded_path": str(source_dir),
                    "files": [
                        {"url": value.splitlines()[0], "filename": "roads.geojson"},
                        {"url": value.splitlines()[1], "filename": "parks.geojson"},
                    ],
                },
            },
            _tempdir=tempdir,
        )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        source_path = Path(input_arg)
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        assert source_path.is_dir()
        assert sorted(path.name for path in source_path.iterdir()) == [
            "parks.geojson",
            "roads.geojson",
        ]
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
            "description": "Roads and parks",
            "geometry": [{"geom_types": {"LineString": 2}, "total_points": 12}],
            "attributes": [],
        },
    )

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    catalog.register_source(
        "osm",
        {
            "type": "multi_remote_file",
            "url": urls,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": "2026-06-29T00:00:00+00:00",
            "metadata": {"file_count": 2},
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
            "osm",
        ],
    )

    cli.main()

    dataset = catalog.get("osm")
    assert calls["name"] == "osm"
    assert Path(calls["input_arg"]).name.startswith("ucrstar-test-")
    assert sorted(path.name for path in datasets_dir.joinpath("osm", "download").iterdir()) == [
        "parks.geojson",
        "roads.geojson",
    ]
    assert dataset["dataset_state"] == "published"
    assert dataset["source"]["type"] == "multi_remote_file"
    assert dataset["source"]["url"] == urls
    assert dataset["source"]["modified_at"] == "2026-06-30T06:24:35+00:00"


def test_process_dataset_preserves_cached_download_for_nested_dataset_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / "NE" / "lakes"
    download_dir = dataset_dir / "download"
    download_dir.mkdir(parents=True)
    cached_file = download_dir / "lakes.geojson"
    cached_file.write_text("cached", encoding="utf-8")
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/lakes.geojson"

    monkeypatch.setattr(cli, "prepare_input_source", lambda value: (_ for _ in ()).throw(AssertionError("cached download should be reused")))
    monkeypatch.setattr(cli, "current_source_state", lambda source: source)
    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: ["NE/lakes"] if dataset_dir.exists() else [],
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
            "description": "Lakes",
            "geometry": [{"geom_types": {"Polygon": 1}, "total_points": 4}],
            "attributes": [],
        },
    )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        target = Path(datasets_arg) / Path(*kwargs["name"].split("/"))
        if target.exists():
            cli.shutil.rmtree(target)
        target.mkdir(parents=True)
        return None, None, None

    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        target = Path(datasets_arg) / Path(*name_arg.split("/"))
        if target.exists():
            cli.shutil.rmtree(target)
            return True
        return False

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "remote_file",
            "url": url,
            "accessed_at": "2026-01-01T00:00:00+00:00",
            "modified_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"filename": "lakes.geojson"},
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
            "process-dataset",
            "NE/lakes",
        ],
    )

    cli.main()

    assert calls["name"].startswith("NE/lakes__build_")
    assert Path(calls["input_arg"]) == download_dir
    assert (dataset_dir / "download" / "lakes.geojson").read_text(encoding="utf-8") == "cached"
    assert catalog.get("NE/lakes")["source"]["url"] == url


def test_process_dataset_uses_cached_download_when_remote_url_fails(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / "roads"
    download_dir = dataset_dir / "download"
    download_dir.mkdir(parents=True)
    cached_file = download_dir / "roads.geojson"
    cached_file.write_text("cached", encoding="utf-8")
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"
    caplog.set_level(logging.WARNING)

    monkeypatch.setattr(cli, "current_source_state", lambda source: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    monkeypatch.setattr(cli, "prepare_input_source", lambda value: (_ for _ in ()).throw(AssertionError("cached fallback should avoid downloading")))
    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: ["roads"] if dataset_dir.exists() else [],
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
            "geometry": [{"geom_types": {"LineString": 1}, "total_points": 2}],
            "attributes": [],
        },
    )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        target = Path(datasets_arg) / kwargs["name"]
        if target.exists():
            cli.shutil.rmtree(target)
        target.mkdir(parents=True)
        return None, None, None

    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        target = Path(datasets_arg) / name_arg
        if target.exists():
            cli.shutil.rmtree(target)
            return True
        return False

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "remote_file",
            "url": url,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {"filename": "roads.geojson"},
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
            "process-dataset",
            "roads",
        ],
    )

    cli.main()

    assert calls["name"].startswith("roads__build_")
    assert Path(calls["input_arg"]) == download_dir
    assert "using cached source copy" in caplog.text.lower()
    assert catalog.get("roads")["dataset_state"] == "published"


def test_process_dataset_reports_error_when_remote_url_fails_without_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / "roads"
    dataset_dir.mkdir(parents=True)
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"

    monkeypatch.setattr(cli, "current_source_state", lambda source: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: ["roads"] if dataset_dir.exists() else [],
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
            "geometry": [{"geom_types": {"LineString": 1}, "total_points": 2}],
            "attributes": [],
        },
    )

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "remote_file",
            "url": url,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {"filename": "roads.geojson"},
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
            "process-dataset",
            "roads",
        ],
    )

    try:
        cli.main()
    except urllib.error.URLError:
        pass
    else:
        raise AssertionError("process-dataset should fail when neither remote nor cache is available")

    assert catalog.get("roads")["dataset_state"] == "error"


def test_process_dataset_uses_nested_cached_download_when_remote_url_fails(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    dataset_dir = datasets_dir / "roads"
    nested_dir = dataset_dir / "download" / "archive"
    nested_dir.mkdir(parents=True)
    cached_file = nested_dir / "roads.geojson"
    cached_file.write_text("cached", encoding="utf-8")
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"
    caplog.set_level(logging.WARNING)

    monkeypatch.setattr(cli, "current_source_state", lambda source: source)
    monkeypatch.setattr(cli, "prepare_input_source", lambda value: (_ for _ in ()).throw(urllib.error.HTTPError(value, 500, "Internal Server Error", None, None)))
    monkeypatch.setattr(
        cli.starlet,
        "list_datasets",
        lambda root: ["roads"] if dataset_dir.exists() else [],
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
            "geometry": [{"geom_types": {"LineString": 1}, "total_points": 2}],
            "attributes": [],
        },
    )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        target = Path(datasets_arg) / kwargs["name"]
        if target.exists():
            cli.shutil.rmtree(target)
        target.mkdir(parents=True)
        return None, None, None

    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        target = Path(datasets_arg) / name_arg
        if target.exists():
            cli.shutil.rmtree(target)
            return True
        return False

    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "remote_file",
            "url": url,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": "2026-07-01T00:00:00+00:00",
            "metadata": {"filename": "missing-top-level.geojson"},
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
            "process-dataset",
            "roads",
        ],
    )

    cli.main()

    assert Path(calls["input_arg"]) == nested_dir.parent
    assert "using cached source copy" in caplog.text.lower()
    assert catalog.get("roads")["dataset_state"] == "published"


def test_add_dataset_accepts_multiple_input_urls(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    db_path = tmp_path / "instance" / "catalog.sqlite"
    roads_url = "https://example.com/data/roads.geojson"
    parks_url = "https://example.com/data/parks.geojson"
    combined = f"{roads_url}\n{parks_url}"

    def fake_source_reference(value):
        calls["registered_source_value"] = value
        return {
            "type": "multi_remote_file",
            "url": value,
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": "2026-06-30T06:24:35+00:00",
            "metadata": {"urls": value.splitlines(), "file_count": 2},
        }

    def fake_prepare_input_source(value):
        calls["prepared_source_value"] = value
        tempdir = tempfile.TemporaryDirectory(prefix="ucrstar-test-")
        source_dir = Path(tempdir.name)
        (source_dir / "roads.geojson").write_text("roads", encoding="utf-8")
        (source_dir / "parks.geojson").write_text("parks", encoding="utf-8")
        return PreparedSource(
            path=source_dir,
            source={
                "type": "multi_remote_file",
                "url": value,
                "accessed_at": "2026-07-01T00:00:00+00:00",
                "modified_at": "2026-06-30T06:24:35+00:00",
                "metadata": {"urls": value.splitlines(), "downloaded_path": str(source_dir)},
            },
            _tempdir=tempdir,
        )

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        assert Path(input_arg).is_dir()
        (Path(datasets_arg) / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    monkeypatch.setattr(cli, "source_reference", fake_source_reference)
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
            "description": "OSM layers",
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
            roads_url,
            parks_url,
            "--name",
            "osm",
        ],
    )

    cli.main()

    dataset = cli.DatasetCatalog(db_path, datasets_dir).get("osm")
    assert calls["registered_source_value"] == combined
    assert calls["prepared_source_value"] == combined
    assert calls["name"] == "osm"
    assert dataset["source"]["url"] == combined
    assert dataset["dataset_state"] == "published"


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


def test_refresh_reuses_current_download_cache_and_preserves_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = {}
    datasets_dir = tmp_path / "datasets"
    roads_dir = datasets_dir / "roads"
    download_dir = roads_dir / "download"
    download_dir.mkdir(parents=True)
    cached_file = download_dir / "roads.geojson"
    cached_file.write_text("cached", encoding="utf-8")
    db_path = tmp_path / "instance" / "catalog.sqlite"
    url = "https://example.com/data/roads.geojson"

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

    def fail_prepare_input_source(value):
        raise AssertionError("current cached download should avoid redownloading")

    def fake_add_dataset(input_arg, datasets_arg, **kwargs):
        calls["input_arg"] = input_arg
        calls["name"] = kwargs["name"]
        assert Path(input_arg) == cached_file
        (Path(datasets_arg) / kwargs["name"]).mkdir(parents=True)
        return None, None, None

    def fake_delete_dataset(datasets_arg, name_arg, **kwargs):
        target = Path(datasets_arg) / name_arg
        if target.exists():
            cli.shutil.rmtree(target)
            return True
        return False

    monkeypatch.setattr(cli, "prepare_input_source", fail_prepare_input_source)
    monkeypatch.setattr(cli, "current_source_state", lambda source: source)
    monkeypatch.setattr(cli.starlet, "add_dataset", fake_add_dataset)
    monkeypatch.setattr(cli.starlet, "delete_dataset", fake_delete_dataset)

    catalog = cli.DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "remote_file",
            "url": url,
            "accessed_at": "2026-01-01T00:00:00+00:00",
            "modified_at": "2026-01-01T00:00:00+00:00",
            "metadata": {"filename": "roads.geojson"},
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
            "--force",
        ],
    )

    cli.main()

    assert calls["name"].startswith("roads__refresh_")
    assert (roads_dir / "download" / "roads.geojson").read_text(encoding="utf-8") == "cached"
    assert catalog.get("roads")["source"]["url"] == url
