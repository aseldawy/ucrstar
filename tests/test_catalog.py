from pathlib import Path

from ucrstar.catalog import DatasetCatalog, normalize_schema_type, normalize_style


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
    repositories = catalog.list_repositories()
    assert repositories[0]["short_name"] == "default"
    assert repositories[0]["total_datasets"] == 1
    assert catalog.get("roads")["repository_id"] == repositories[0]["id"]


def test_catalog_tracks_repository_and_filters_datasets(tmp_path: Path) -> None:
    catalog = DatasetCatalog(tmp_path / "catalog.sqlite", tmp_path / "datasets")
    repository = catalog.upsert_repository(
        "lacounty",
        "https://egis-lacounty.hub.arcgis.com",
        description="LA County GIS data",
        repository_type="esri_hub",
    )
    catalog.register_source(
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
    catalog.register_source(
        "local_roads",
        {
            "type": "local",
            "url": str(tmp_path / "roads.geojson"),
            "accessed_at": "2026-07-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {},
        },
    )

    assert [dataset["name"] for dataset in catalog.list({"state": "all", "repository": "lacounty"})] == ["addresses"]
    counts = {repo["short_name"]: repo["total_datasets"] for repo in catalog.list_repositories()}
    assert counts["default"] == 1
    assert counts["lacounty"] == 1


def test_catalog_enriches_style_and_embedding(tmp_path: Path, monkeypatch) -> None:
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
            "description": None,
            "geometry": [{"geom_types": {"LineString": 4}, "total_points": 20}],
            "attributes": [
                {
                    "name": "road_name",
                    "role": "text",
                    "top_k": [["Main St", 3]],
                }
            ],
        },
    )

    class FakeLLM:
        enabled = True
        embedding_key = "fake:test-embed"

        def enrich_dataset(self, payload):
            return {
                "description": "Road centerline dataset.",
                "attributes": {"road_name": "Street name."},
                "style": {"layers": {"line": {"line-color": "#111111"}}},
            }

        def embed(self, text):
            return [1.0, 0.0]

    catalog = DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    enriched = catalog.enrich(dataset["id"], FakeLLM())

    assert enriched["description"] == "Road centerline dataset."
    assert enriched["schema"][0]["description"] == "Street name."
    assert catalog.style(dataset["id"])["layers"]["line"]["line-color"] == "#111111"
    assert catalog.semantic_search("streets", FakeLLM(), {}, limit=5)[0]["id"] == dataset["id"]
    deleted = catalog.delete(dataset["id"])

    assert deleted["name"] == "roads"
    assert catalog.get(dataset["id"]) is None
    assert catalog.semantic_search("streets", FakeLLM(), {}, limit=5) == []


def test_normalize_style_flattens_nested_paint_and_ignores_source_layer() -> None:
    style = {
        "source_layer": "cemetery",
        "layers": {
            "fill": {
                "fill-color": "#2a9d8f",
                "paint": {"fill-color": "#add8e6", "fill-opacity": 0.5},
            },
            "line": {
                "line-color": "#0f6b99",
                "paint": {
                    "line-color": "#808080",
                    "line-width": 2,
                    "paint": {"line-color": "#000000"},
                },
            },
            "circle": {
                "circle-color": "#d1495b",
                "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 2, 10, 5],
                "paint": {"circle-color": "#808080", "circle-radius": 5},
            },
        },
    }

    normalized = normalize_style(style, ["Point"])

    assert normalized["source_layer"] == "layer0"
    assert normalized["layers"]["fill"] == {
        "fill-color": "#add8e6",
        "fill-opacity": 0.5,
    }
    assert normalized["layers"]["line"]["line-color"] == "#808080"
    assert normalized["layers"]["line"]["line-width"] == 2
    assert "paint" not in normalized["layers"]["line"]
    assert normalized["layers"]["circle"]["circle-color"] == "#808080"
    assert normalized["layers"]["circle"]["circle-radius"] == 5


def test_normalize_schema_type_simplifies_esri_field_types() -> None:
    assert normalize_schema_type("esriFieldTypeDouble") == "Double"
    assert normalize_schema_type("esriFieldTypeString") == "String"
    assert normalize_schema_type("esriFieldTypeSmallInteger") == "Integer"
    assert normalize_schema_type("text") == "text"


def test_catalog_logs_llm_enrichment_failures(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
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
            "description": "Roads",
            "geometry": [{"geom_types": {"LineString": 4}, "total_points": 20}],
            "attributes": [],
        },
    )

    class FailingLLM:
        enabled = True
        provider = "gemini"
        chat_model = "gemini-test"
        embedding_key = "gemini:embed-test"

        class settings:
            fallback_on_error = True

        def enrich_dataset(self, payload):
            raise RuntimeError("provider rejected request")

        def embed(self, text):
            return [1.0]

    catalog = DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    enriched = catalog.enrich(dataset["id"], FailingLLM())

    assert enriched["id"] == dataset["id"]
    assert "LLM enrichment failed with provider=gemini chat_model=gemini-test" in caplog.text
    assert "provider rejected request" in caplog.text
