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
