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


def test_catalog_sync_accepts_nested_dataset_names(tmp_path: Path, monkeypatch) -> None:
    datasets_dir = tmp_path / "datasets"
    (datasets_dir / "osm21" / "roads").mkdir(parents=True)
    db_path = tmp_path / "catalog.sqlite"
    seen_paths = []

    monkeypatch.setattr("starlet.list_datasets", lambda root: ["osm21/roads"])
    monkeypatch.setattr(
        "starlet.get_dataset_metadata",
        lambda dataset: seen_paths.append(Path(dataset))
        or {
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
            "description": "OSM roads",
            "geometry": [{"geom_types": {"LineString": 4}, "total_points": 20}],
            "attributes": [],
        },
    )

    catalog = DatasetCatalog(db_path, datasets_dir)
    synced = catalog.sync()[0]

    assert synced["name"] == "osm21/roads"
    assert catalog.get("osm21/roads")["description"] == "OSM roads"
    assert seen_paths == [datasets_dir / "osm21" / "roads"]


def test_catalog_sync_merges_metadata_without_deleting_existing_keys(
    tmp_path: Path, monkeypatch
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
            "size_bytes": 10 * 1024 * 1024,
            "bbox": [0, 1, 2, 3],
            "has_mvt": True,
            "zoom_levels": [],
            "generated_at": "new",
        },
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "description": "Road network",
            "geometry": [{"geom_types": {"LineString": 4}, "total_points": 20}],
            "attributes": [],
        },
    )

    catalog = DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_metadata(dataset["id"], {"max_zoom": 19, "generated_at": "old"})

    synced = catalog.sync()[0]
    detail = catalog.get("roads")

    assert synced["metadata_json"]["max_zoom"] == 19
    assert synced["metadata_json"]["generated_at"] == "new"
    assert detail["visualization"]["max_zoom"] == 19


def test_catalog_omits_vector_zoom_when_starlet_metadata_has_none(
    tmp_path: Path, monkeypatch
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
            "size_bytes": 10 * 1024 * 1024,
            "bbox": [0, 1, 2, 3],
            "has_mvt": True,
            "zoom_levels": [],
        },
    )
    monkeypatch.setattr(
        "starlet.get_dataset_summary",
        lambda dataset: {
            "description": "Road network",
            "geometry": [{"geom_types": {"LineString": 4}, "total_points": 20}],
            "attributes": [],
        },
    )

    detail = DatasetCatalog(db_path, datasets_dir).sync()[0]
    catalog = DatasetCatalog(db_path, datasets_dir)
    visualization = catalog.get(detail["id"])["visualization"]
    source = catalog.style(detail["id"])["sources"]["dataset"]

    assert "min_zoom" not in visualization
    assert "max_zoom" not in visualization
    assert "minzoom" not in source
    assert "maxzoom" not in source


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

    seen_payload = {}

    class FakeLLM:
        enabled = True
        embedding_key = "fake:test-embed"

        def enrich_dataset(self, payload):
            seen_payload.update(payload)
            return {
                "description": "Road centerline dataset.",
                "attributes": {"road_name": "Street name."},
                "style": {"layers": {"line": {"line-color": "#111111"}}},
            }

        def embed(self, text):
            return [1.0, 0.0]

    catalog = DatasetCatalog(db_path, datasets_dir)
    dataset = catalog.sync()[0]
    catalog.update_source(
        dataset["id"],
        {
            "type": "esri_hub",
            "url": "https://example.com/item",
            "metadata": {
                "hub": {
                    "layer": {
                        "drawingInfo": {
                            "renderer": {"type": "uniqueValue", "field1": "road_name"}
                        }
                    }
                }
            },
        },
    )
    enriched = catalog.enrich(dataset["id"], FakeLLM())

    assert enriched["description"] == "Road centerline dataset."
    assert enriched["schema"][0]["description"] == "Street name."
    assert enriched["style"]["version"] == 8
    assert enriched["style"]["sources"]["dataset"]["type"] == "geojson"
    style = catalog.style(dataset["id"])
    assert style["version"] == 8
    line_layer = next(layer for layer in style["layers"] if layer["id"] == "lines")
    assert line_layer["paint"]["line-color"] == "#111111"
    assert seen_payload["source_style"] == {
        "format": "esri-renderer",
        "renderer": {"type": "uniqueValue", "field1": "road_name"},
    }
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

    assert normalized["version"] == 8
    assert normalized["sources"]["dataset"]["type"] == "vector"
    layers = {layer["id"]: layer for layer in normalized["layers"]}
    assert layers["fill"]["source-layer"] == "layer0"
    assert layers["fill"]["paint"] == {
        "fill-color": "#add8e6",
        "fill-opacity": 0.5,
    }
    assert layers["lines"]["paint"]["line-color"] == "#808080"
    assert layers["lines"]["paint"]["line-width"] == 2
    assert "paint" not in layers["lines"]["paint"]
    assert layers["points"]["paint"]["circle-color"] == "#808080"
    assert layers["points"]["paint"]["circle-radius"] == 5


def test_normalize_style_replaces_cosmetic_color_category_with_semantic_field() -> None:
    style = {
        "version": 8,
        "layers": [
            {
                "id": "areas",
                "type": "fill",
                "paint": {
                    "fill-color": [
                        "match",
                        ["get", "color_egis"],
                        "Blue - RGB 0,0,255",
                        "rgb(0,0,255)",
                        "Red - RGB 255,0,0",
                        "rgb(255,0,0)",
                        "#ccc",
                    ]
                },
            }
        ],
    }
    dataset = {
        "name": "service_areas",
        "num_features": 12,
        "schema": [
            {"name": "dcfsoffice", "description": "Name of the assigned office."},
            {"name": "color_egis", "description": "Assigned display color."},
        ],
        "summary_json": {
            "attributes": [
                {
                    "name": "dcfsoffice",
                    "role": "categorical_text",
                    "approx_distinct": 3,
                    "top_k": [
                        {"value": "North", "count": 5},
                        {"value": "Central", "count": 4},
                        {"value": "South", "count": 3},
                    ],
                },
                {
                    "name": "color_egis",
                    "role": "categorical_text",
                    "approx_distinct": 2,
                    "top_k": [
                        {"value": "Blue - RGB 0,0,255", "count": 7},
                        {"value": "Red - RGB 255,0,0", "count": 5},
                    ],
                },
            ]
        },
        "visualization": {"type": "GeoJSON", "url": "/areas.geojson"},
    }

    normalized = normalize_style(style, ["Polygon"], dataset)

    expression = normalized["layers"][0]["paint"]["fill-color"]
    assert expression[0:2] == ["match", ["get", "dcfsoffice"]]
    assert expression[2::2][:-1] == ["North", "Central", "South"]
    assert normalized["metadata"]["ucrstar:legend"] == {
        "type": "categorical",
        "property": "dcfsoffice",
        "labels": {"North": "North", "Central": "Central", "South": "South"},
    }


def test_normalize_style_rejects_categorical_style_below_80_percent_coverage() -> None:
    style = {
        "version": 8,
        "metadata": {
            "ucrstar:legend": {"type": "categorical", "property": "religion"}
        },
        "layers": [
            {
                "id": "places",
                "type": "circle",
                "paint": {
                    "circle-color": [
                        "match",
                        ["get", "religion"],
                        "christian",
                        "#00aa00",
                        "muslim",
                        "#0000aa",
                        "#808080",
                    ]
                },
            }
        ],
    }
    dataset = {
        "name": "places",
        "num_features": 100,
        "summary_json": {
            "attributes": [
                {
                    "name": "tagsMap",
                    "top_k": [
                        {
                            "value": "[('religion', 'christian'), ('name', 'A')]",
                            "count": 35,
                        },
                        {
                            "value": "[('religion', 'muslim'), ('name', 'B')]",
                            "count": 20,
                        },
                        {"value": "[('name', 'C')]", "count": 45},
                    ],
                }
            ]
        },
        "visualization": {"type": "GeoJSON", "url": "/places.geojson"},
    }

    normalized = normalize_style(style, ["Point"], dataset)

    assert normalized["layers"][0]["paint"]["circle-color"] == "#d1495b"
    assert "ucrstar:legend" not in normalized["metadata"]


def test_normalize_style_keeps_categorical_style_at_80_percent_coverage() -> None:
    style = {
        "version": 8,
        "layers": [
            {
                "id": "areas",
                "type": "fill",
                "paint": {
                    "fill-color": [
                        "match",
                        ["get", "zone"],
                        "residential",
                        "#00aa00",
                        "commercial",
                        "#0000aa",
                        "#808080",
                    ]
                },
            }
        ],
    }
    dataset = {
        "name": "zones",
        "num_features": 100,
        "summary_json": {
            "attributes": [
                {
                    "name": "zone",
                    "top_k": [
                        {"value": "residential", "count": 60},
                        {"value": "commercial", "count": 20},
                        {"value": "other", "count": 20},
                    ],
                }
            ]
        },
        "visualization": {"type": "GeoJSON", "url": "/zones.geojson"},
    }

    normalized = normalize_style(style, ["Polygon"], dataset)

    assert normalized["layers"][0]["paint"]["fill-color"] == style["layers"][0]["paint"]["fill-color"]


def test_normalize_style_rejects_nested_categorical_style_below_leaf_coverage() -> None:
    style = {
        "version": 8,
        "metadata": {
            "ucrstar:legend": {"type": "categorical", "property": "religion"}
        },
        "layers": [
            {
                "id": "places",
                "type": "circle",
                "paint": {
                    "circle-color": [
                        "match",
                        ["get", "religion"],
                        "christian",
                        [
                            "match",
                            ["get", "denomination"],
                            "catholic",
                            "#7fc97f",
                            "orthodox",
                            "#33a02c",
                            "#4daf4a",
                        ],
                        "muslim",
                        [
                            "match",
                            ["get", "denomination"],
                            "sunni",
                            "#80b1d3",
                            "#377eb8",
                        ],
                        "jewish",
                        "#ffd700",
                        "#808080",
                    ]
                },
            }
        ],
    }
    dataset = {
        "name": "cemeteries",
        "num_features": 100,
        "summary_json": {
            "attributes": [
                {
                    "name": "tagsMap",
                    "top_k": [
                        {
                            "value": "[('religion', 'christian'), ('denomination', 'catholic')]",
                            "count": 10,
                        },
                        {
                            "value": "[('religion', 'christian'), ('denomination', 'orthodox')]",
                            "count": 5,
                        },
                        {
                            "value": "[('religion', 'christian'), ('denomination', 'protestant')]",
                            "count": 70,
                        },
                        {"value": "[('religion', 'jewish')]", "count": 3},
                        {"value": "[('amenity', 'grave_yard')]", "count": 12},
                    ],
                }
            ]
        },
        "visualization": {"type": "GeoJSON", "url": "/cemeteries.geojson"},
    }

    normalized = normalize_style(style, ["Point"], dataset)

    assert normalized["layers"][0]["paint"]["circle-color"] == "#d1495b"
    assert "ucrstar:legend" not in normalized["metadata"]


def test_normalize_style_keeps_nested_categorical_style_at_leaf_coverage() -> None:
    style = {
        "version": 8,
        "layers": [
            {
                "id": "places",
                "type": "circle",
                "paint": {
                    "circle-color": [
                        "match",
                        ["get", "religion"],
                        "christian",
                        [
                            "match",
                            ["get", "denomination"],
                            "catholic",
                            "#7fc97f",
                            "orthodox",
                            "#33a02c",
                            "#4daf4a",
                        ],
                        "jewish",
                        "#ffd700",
                        "#808080",
                    ]
                },
            }
        ],
    }
    dataset = {
        "name": "cemeteries",
        "num_features": 100,
        "summary_json": {
            "attributes": [
                {
                    "name": "tagsMap",
                    "top_k": [
                        {
                            "value": "[('religion', 'christian'), ('denomination', 'catholic')]",
                            "count": 50,
                        },
                        {
                            "value": "[('religion', 'christian'), ('denomination', 'orthodox')]",
                            "count": 20,
                        },
                        {"value": "[('religion', 'jewish')]", "count": 10},
                        {"value": "[('religion', 'christian')]", "count": 20},
                    ],
                }
            ]
        },
        "visualization": {"type": "GeoJSON", "url": "/cemeteries.geojson"},
    }

    normalized = normalize_style(style, ["Point"], dataset)

    assert normalized["layers"][0]["paint"]["circle-color"] == style["layers"][0]["paint"]["circle-color"]


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
