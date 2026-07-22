import json
import multiprocessing
import sqlite3
import time
from pathlib import Path

import pytest

from ucrstar.app import create_app
from ucrstar.assistant_tools import (
    GeoDataFrameQueryRunner,
    ViewportSummarizer,
    hybrid_rank,
    normalize_dataframe_query,
    validate_action,
)
from ucrstar.assistant_style import build_assistant_style
from ucrstar.chat import compact_dataset, parse_assistant_plan


class PlanningLLM:
    enabled = True
    provider = "ollama"
    chat_model = "planner"
    embedding_model = "unit-embed"
    embedding_key = "ollama:unit-embed"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return self.responses.pop(0)

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


class FakeGeocoder:
    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int = 5) -> list[dict]:
        self.queries.append((query, limit))
        return self.results


class RecordingDataFrameQueryRunner:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[tuple[dict, list[float], dict]] = []

    def run(self, dataset: dict, bounds: list[float], query: dict) -> dict:
        self.calls.append((dataset, bounds, query))
        return self.result


class FakeBatch:
    def __init__(self, records: list[dict]) -> None:
        self.records = records

    def to_dict(self, *, orient: str):
        assert orient == "records"
        return self.records


def assistant_config(**chat_overrides) -> dict:
    return {
        "llm": {
            "default": "ollama",
            "semantic_search": True,
            "chat": chat_overrides,
            "providers": {
                "ollama": {
                    "enabled": True,
                    "base_url": "http://unused.test",
                    "chat_model": "planner",
                    "embedding_model": "unit-embed",
                }
            },
        }
    }


def assistant_app(
    tmp_path: Path,
    llm: PlanningLLM,
    *,
    geocoder=None,
    chat_config=None,
    dataframe_query_runner=None,
):
    config = {
        "TESTING": True,
        "DATASETS_DIR": tmp_path / "datasets",
        "DATABASE": tmp_path / "instance" / "test.sqlite",
        "UCRSTAR2_CONFIG": assistant_config(**(chat_config or {})),
        "LLM_CLIENT": llm,
        "GEOCODER": geocoder or FakeGeocoder([]),
    }
    if dataframe_query_runner is not None:
        config["DATAFRAME_QUERY_RUNNER"] = dataframe_query_runner
    return create_app(config)


def publish_dataset(app, name: str, description: str = "") -> dict:
    catalog = app.extensions["ucrstar_catalog"]
    dataset = catalog.register_source(
        name,
        {
            "type": "local",
            "url": f"/tmp/{name}.geojson",
            "accessed_at": "2026-01-01T00:00:00+00:00",
            "modified_at": None,
            "metadata": {},
        },
        description=description,
    )
    catalog.update_state(dataset["id"], "published")
    return catalog.get(dataset["id"])


def configure_vector_dataset(app, dataset_id: str) -> dict:
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute(
            """
            UPDATE datasets
            SET geometry_types = ?, schema_json = ?, visualization_type = ?,
                visualization_url = ?, num_features = ?, summary_json = ?
            WHERE id = ?
            """,
            (
                json.dumps(["Polygon"]),
                json.dumps([{"name": "kind", "type": "string"}]),
                "VectorTile",
                f"/datasets/{dataset_id}/tiles/{{z}}/{{x}}/{{y}}.mvt",
                100,
                json.dumps(
                    {
                        "attributes": [
                            {
                                "name": "kind",
                                "approx_distinct": 2,
                                "top_k": [
                                    {"value": "park", "count": 10},
                                    {"value": "other", "count": 90},
                                ],
                            }
                        ]
                    }
                ),
                dataset_id,
            ),
        )
    return app.extensions["ucrstar_catalog"].get(dataset_id)


def configure_point_dataset(app, dataset_id: str) -> dict:
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute(
            """
            UPDATE datasets
            SET geometry_types = ?, schema_json = ?, visualization_type = ?,
                visualization_url = ?, num_features = ?, summary_json = ?
            WHERE id = ?
            """,
            (
                json.dumps(["Point"]),
                json.dumps(
                    [
                        {"name": "category", "type": "string"},
                        {"name": "name", "type": "string"},
                    ]
                ),
                "VectorTile",
                f"/datasets/{dataset_id}/tiles/{{z}}/{{x}}/{{y}}.mvt",
                25,
                json.dumps(
                    {
                        "attributes": [
                            {
                                "name": "category",
                                "approx_distinct": 3,
                                "top_k": [
                                    {"value": "restaurant", "count": 10},
                                    {"value": "coffee", "count": 8},
                                    {"value": "other", "count": 7},
                                ],
                            }
                        ]
                    }
                ),
                dataset_id,
            ),
        )
    return app.extensions["ucrstar_catalog"].get(dataset_id)


def configure_large_schema_dataset(app, dataset_id: str) -> dict:
    schema = [
        {"name": f"FIELD_{index:03d}", "type": "String"}
        for index in range(125)
    ]
    schema.extend(
        [
            {
                "name": "REBUILD_PROGRESS",
                "type": "String",
                "description": "Overall rebuild progress status.",
            },
            {
                "name": "REBUILD_PROGRESS_NUM",
                "type": "Integer",
                "description": "Numeric code for rebuild progress.",
            },
        ]
    )
    summary = {
        "attributes": [
            {
                "name": "REBUILD_PROGRESS_NUM",
                "min": 0,
                "max": 5,
                "approx_distinct": 6,
                "top_k": [
                    {"value": value, "count": 10}
                    for value in range(6)
                ],
            }
        ]
    }
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute(
            "UPDATE datasets SET schema_json = ?, summary_json = ? WHERE id = ?",
            (json.dumps(schema), json.dumps(summary), dataset_id),
        )
    return app.extensions["ucrstar_catalog"].get(dataset_id)


def plan(tool_calls: list[dict], message: str = "Working on it.") -> str:
    return json.dumps({"message": message, "tool_calls": tool_calls})


def final_message(message: str) -> str:
    return json.dumps({"message": message})


def test_ambiguous_search_returns_minimal_dataset_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("starlet.list_datasets", lambda root: [])
    llm = PlanningLLM(
        [
            plan([{"name": "search_datasets", "arguments": {"query": "wildfire"}}]),
            final_message("I found two wildfire datasets."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    first = publish_dataset(app, "wildfire_perimeters", "Mapped fire perimeters")
    second = publish_dataset(app, "wildfire_risk", "Modeled fire risk")

    response = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Find wildfire data"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"]["content"] == "I found two wildfire datasets."
    assert body["actions"] == [
        {
            "type": "show_datasets",
            "query": "wildfire",
            "datasets": [
                {
                    "id": first["id"],
                    "name": "wildfire_perimeters",
                    "description": "Mapped fire perimeters",
                    "geometry_types": [],
                    "num_features": None,
                    "size_bytes": 0,
                    "mbr": None,
                    "dataset_state": "published",
                },
                {
                    "id": second["id"],
                    "name": "wildfire_risk",
                    "description": "Modeled fire risk",
                    "geometry_types": [],
                    "num_features": None,
                    "size_bytes": 0,
                    "mbr": None,
                    "dataset_state": "published",
                },
            ],
        }
    ]


def test_compact_dataset_promotes_relevant_fields_beyond_schema_limit(tmp_path: Path) -> None:
    app = assistant_app(tmp_path, PlanningLLM([]))
    dataset = publish_dataset(app, "parcels")
    dataset = configure_large_schema_dataset(app, dataset["id"])

    compact = compact_dataset(
        dataset,
        attribute_query="Color code by REBUILD_PROGRESS_NUM",
    )

    assert compact["schema_field_count"] == 127
    assert compact["schema_truncated"] is True
    assert compact["attribute_names_complete"] is True
    assert "REBUILD_PROGRESS_NUM" in compact["attribute_names"]
    assert compact["relevant_attributes"][0]["name"] == "REBUILD_PROGRESS_NUM"
    target = next(
        field for field in compact["schema"] if field["name"] == "REBUILD_PROGRESS_NUM"
    )
    assert target["min"] == 0
    assert target["max"] == 5
    assert target["approx_distinct"] == 6


def test_chat_context_resolves_late_schema_field_from_recent_history(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            final_message("Let me check that field."),
            final_message("REBUILD_PROGRESS_NUM is present in the server schema."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "parcels")
    dataset = configure_large_schema_dataset(app, dataset["id"])
    client = app.test_client()

    first = client.post(
        "/llm/chat.json",
        json={
            "message": "Use REBUILD_PROGRESS_NUM",
            "context": {"dataset_id": dataset["id"]},
        },
    ).get_json()
    response = client.post(
        "/llm/chat.json",
        json={
            "session_id": first["session_id"],
            "message": "It is literally available; I can see it",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    second_context = llm.calls[1][-1]["content"]
    assert '"schema_field_count":127' in second_context
    assert '"schema_truncated":true' in second_context
    assert '"name":"REBUILD_PROGRESS_NUM"' in second_context
    assert '"attribute_names_complete":true' in second_context


def test_find_attributes_tool_searches_complete_schema(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan(
                [
                    {
                        "name": "find_attributes",
                        "arguments": {"query": "rebuild progress"},
                    }
                ]
            ),
            final_message(
                "The closest fields are REBUILD_PROGRESS and REBUILD_PROGRESS_NUM."
            ),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "parcels")
    dataset = configure_large_schema_dataset(app, dataset["id"])

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Find the closest attribute to rebuild progress",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["actions"] == []
    assert body["message"]["content"] == (
        "The closest fields are REBUILD_PROGRESS and REBUILD_PROGRESS_NUM."
    )
    tool_prompt = llm.calls[1][-1]["content"]
    assert '"name":"find_attributes"' in tool_prompt
    assert '"schema_field_count":127' in tool_prompt
    assert '"name":"REBUILD_PROGRESS"' in tool_prompt
    assert '"name":"REBUILD_PROGRESS_NUM"' in tool_prompt
    assert "source" not in json.dumps(body["actions"])


def test_exact_search_match_selects_dataset_and_persists_tool_trace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("starlet.list_datasets", lambda root: [])
    llm = PlanningLLM(
        [
            plan([{"name": "search_datasets", "arguments": {"query": "counties"}}]),
            final_message("I found and selected the counties dataset."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "counties", "County boundaries")

    body = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Open the counties dataset"},
    ).get_json()

    assert body["actions"] == [{"type": "select_dataset", "dataset_id": dataset["id"]}]
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        row = connection.execute(
            """
            SELECT tool_calls_json, actions_json
            FROM chat_messages
            WHERE role = 'assistant'
            """
        ).fetchone()
    trace = json.loads(row[0])
    assert trace[0]["name"] == "search_datasets"
    assert trace[0]["status"] == "complete"
    assert trace[0]["result"]["selected_dataset_id"] == dataset["id"]
    assert json.loads(row[1]) == body["actions"]


def test_followup_can_select_dataset_from_server_recorded_search_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("starlet.list_datasets", lambda root: [])
    llm = PlanningLLM([])
    app = assistant_app(tmp_path, llm)
    publish_dataset(app, "fire_perimeters")
    second = publish_dataset(app, "fire_risk")
    llm.responses.extend(
        [
            plan([{"name": "search_datasets", "arguments": {"query": "fire"}}]),
            final_message("I found two fire datasets."),
            plan([{"name": "select_dataset", "arguments": {"dataset_id": second["id"]}}]),
            final_message("I selected the second result."),
        ]
    )
    client = app.test_client()

    first = client.post("/llm/chat.json", json={"message": "Find fire data"}).get_json()
    second_response = client.post(
        "/llm/chat.json",
        json={
            "session_id": first["session_id"],
            "message": "Open the second one",
        },
    ).get_json()

    assert second_response["actions"] == [
        {"type": "select_dataset", "dataset_id": second["id"]}
    ]
    assert "Server-recorded results from this turn" in llm.calls[2][2]["content"]
    assert second["id"] in llm.calls[2][2]["content"]


def test_combined_region_and_basemap_request_returns_ordered_validated_actions(
    tmp_path: Path,
) -> None:
    geocoder = FakeGeocoder(
        [
            {
                "label": "California, United States",
                "bounds": [-124.48, 32.53, -114.13, 42.01],
                "center": [-119.5, 37.2],
            }
        ]
    )
    llm = PlanningLLM(
        [
            plan(
                [
                    {"name": "geocode_region", "arguments": {"query": "California"}},
                    {"name": "change_basemap", "arguments": {"basemap": "satellite"}},
                ]
            ),
            final_message("I located California and prepared the satellite view."),
        ]
    )
    app = assistant_app(tmp_path, llm, geocoder=geocoder)

    response = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Move to California and use satellite imagery"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["actions"] == [
        {
            "type": "fit_bounds",
            "query": "California",
            "label": "California, United States",
            "bounds": [-124.48, 32.53, -114.13, 42.01],
        },
        {"type": "change_basemap", "basemap": "satellite"},
    ]
    assert geocoder.queries == [("California", 5)]


def test_viewport_summary_queries_server_data_and_is_used_for_answer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    llm = PlanningLLM(
        [
            plan([{"name": "summarize_viewport", "arguments": {}}]),
            final_message("Two visible records have populations from 10 to 30."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "places", "Places and population")
    query_calls = []

    def fake_query(dataset_path, bounds, *, batch_size):
        query_calls.append((dataset_path, bounds, batch_size))
        return [
            FakeBatch(
                [
                    {"name": "Alpha", "population": 10, "geometry": object()},
                    {"name": "Beta", "population": 30, "geometry": object()},
                ]
            )
        ]

    monkeypatch.setattr("starlet.query_dataset", fake_query)
    bounds = [-118.0, 33.0, -117.0, 34.0]

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Explain what is visible",
            "context": {
                "dataset_id": dataset["id"],
                "viewport": {"bounds": bounds, "center": [-117.5, 33.5], "zoom": 9},
            },
        },
    )

    assert response.status_code == 200
    assert response.get_json()["message"]["content"] == (
        "Two visible records have populations from 10 to 30."
    )
    assert response.get_json()["actions"] == []
    assert query_calls == [
        (tmp_path / "datasets" / "places", tuple(bounds), 1000)
    ]
    synthesis_prompt = llm.calls[1][-1]["content"]
    assert '"feature_count":2' in synthesis_prompt
    assert '"min":10.0' in synthesis_prompt
    assert '"max":30.0' in synthesis_prompt
    assert '"geometry":' not in synthesis_prompt


def test_dataframe_query_tool_returns_a_small_grounded_viewport_answer(
    tmp_path: Path,
) -> None:
    runner = RecordingDataFrameQueryRunner(
        {
            "dataset_id": "dataset-id",
            "scope": "current_viewport",
            "operation": "records",
            "matched_features": 1,
            "columns": ["kind"],
            "rows": [{"kind": "park"}],
            "truncated": False,
        }
    )
    llm = PlanningLLM(
        [
            plan(
                [
                    {
                        "name": "query_dataframe",
                        "arguments": {
                            "where": [
                                {
                                    "attribute": "kind",
                                    "operator": "eq",
                                    "value": "park",
                                }
                            ],
                            "operation": "records",
                            "select": ["kind"],
                            "limit": 5,
                        },
                    }
                ]
            ),
            final_message("The visible feature is a park."),
        ]
    )
    app = assistant_app(tmp_path, llm, dataframe_query_runner=runner)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )
    bounds = [-118.0, 33.0, -117.0, 34.0]

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Which visible feature is a park?",
            "context": {
                "dataset_id": dataset["id"],
                "viewport": {"bounds": bounds, "center": [-117.5, 33.5], "zoom": 9},
            },
        },
    )

    assert response.status_code == 200
    assert response.get_json()["message"]["content"] == "The visible feature is a park."
    assert response.get_json()["actions"] == []
    assert len(runner.calls) == 1
    _, called_bounds, called_query = runner.calls[0]
    assert called_bounds == bounds
    assert called_query == {
        "where": [
            {
                "attribute": "kind",
                "operator": "eq",
                "value": "park",
                "case_sensitive": True,
            }
        ],
        "combine": "and",
        "operation": "records",
        "limit": 5,
        "select": ["kind"],
    }
    assert '"rows":[{"kind":"park"}]' in llm.calls[1][-1]["content"]


def test_dataframe_query_tool_requires_viewport_and_schema_fields(tmp_path: Path) -> None:
    runner = RecordingDataFrameQueryRunner({})
    llm = PlanningLLM(
        [
            plan(
                [
                    {
                        "name": "query_dataframe",
                        "arguments": {
                            "operation": "records",
                            "select": ["missing"],
                        },
                    }
                ]
            ),
            final_message("I need a valid viewport and field."),
        ]
    )
    app = assistant_app(tmp_path, llm, dataframe_query_runner=runner)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Find the missing property",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["actions"] == []
    assert runner.calls == []
    assert "current viewport is unavailable" in llm.calls[1][-1]["content"]


def test_false_geometry_refusal_is_corrected_to_dataframe_bounds_query(
    tmp_path: Path,
) -> None:
    runner = RecordingDataFrameQueryRunner(
        {
            "dataset_id": "dataset-id",
            "scope": "current_viewport",
            "operation": "bounds",
            "matched_features": 1,
            "crs": "EPSG:4326",
            "geometry_bounds": [-118.2, 33.9, -118.1, 34.0],
        }
    )
    llm = PlanningLLM(
        [
            final_message(
                "The query_dataframe tool can only return attributes; I cannot compute "
                "the MBR from geometry."
            ),
            plan(
                [
                    {
                        "name": "query_dataframe",
                        "arguments": {
                            "where": [
                                {
                                    "attribute": "kind",
                                    "operator": "eq",
                                    "value": "park",
                                }
                            ],
                            "operation": "bounds",
                        },
                    }
                ]
            ),
            final_message("The feature MBR is -118.2, 33.9, -118.1, 34.0."),
        ]
    )
    app = assistant_app(tmp_path, llm, dataframe_query_runner=runner)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Retrieve the MBR of the visible park geometries",
            "context": {
                "dataset_id": dataset["id"],
                "viewport": {
                    "bounds": [-119, 33, -117, 35],
                    "center": [-118, 34],
                    "zoom": 10,
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.get_json()["message"]["content"].startswith("The feature MBR")
    assert response.get_json()["actions"] == []
    assert len(runner.calls) == 1
    assert runner.calls[0][2]["operation"] == "bounds"
    assert runner.calls[0][2]["where"][0]["attribute"] == "kind"
    assert "query_dataframe supports geometry-derived queries" in llm.calls[1][-1][
        "content"
    ]


def test_focus_feature_returns_verified_bounds_and_attribute_highlight(
    tmp_path: Path,
) -> None:
    runner = RecordingDataFrameQueryRunner(
        {
            "dataset_id": "dataset-id",
            "scope": "current_viewport",
            "operation": "bounds",
            "matched_features": 1,
            "geometry_bounds": [-118.0971, 34.1668, -118.0945, 34.1691],
            "crs": "EPSG:4326",
        }
    )
    llm = PlanningLLM(
        [
            plan(
                [
                    {
                        "name": "focus_feature",
                        "arguments": {
                            "attribute": "kind",
                            "value": "park",
                            "color": "#0000ff",
                            "max_zoom": 20,
                        },
                    }
                ]
            ),
            final_message("I focused and highlighted the matching park."),
        ]
    )
    app = assistant_app(tmp_path, llm, dataframe_query_runner=runner)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Highlight the identified park in blue and zoom to it",
            "context": {
                "dataset_id": dataset["id"],
                "viewport": {
                    "bounds": [-119, 33, -117, 35],
                    "center": [-118, 34],
                    "zoom": 10,
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.get_json()["actions"] == [
        {
            "type": "fit_bounds",
            "query": "kind=park",
            "label": "matching feature",
            "bounds": [-118.0971, 34.1668, -118.0945, 34.1691],
            "max_zoom": 20.0,
        },
        {
            "type": "highlight_feature",
            "dataset_id": dataset["id"],
            "attribute": "kind",
            "value": "park",
            "color": "#0000ff",
        },
    ]
    assert runner.calls[0][2] == {
        "where": [
            {
                "attribute": "kind",
                "operator": "eq",
                "value": "park",
                "case_sensitive": True,
            }
        ],
        "combine": "and",
        "operation": "bounds",
        "limit": 2,
    }


def test_viewport_summary_is_bounded_and_marks_truncation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    llm = PlanningLLM([])
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "many_places")
    monkeypatch.setattr(
        "starlet.query_dataset",
        lambda *args, **kwargs: [
            FakeBatch(
                [
                    {"name": "A", "value": 1},
                    {"name": "B", "value": 2},
                    {"name": "C", "value": 3},
                ]
            )
        ],
    )
    summarizer = ViewportSummarizer(
        app.extensions["ucrstar_catalog"],
        max_features=2,
        sample_size=1,
        max_attributes=5,
    )

    summary = summarizer.summarize(dataset["id"], [-1, -1, 1, 1])

    assert summary["feature_count"] is None
    assert summary["scanned_features"] == 2
    assert summary["truncated"] is True
    assert summary["sample_records"] == [{"name": "A", "value": 1}]


def test_geodataframe_runner_filters_records_in_an_isolated_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pandas = pytest.importorskip("pandas")
    frame = pandas.DataFrame(
        [
            {"record_id": 1, "kind": "Park", "name": "North"},
            {"record_id": 2, "kind": "school", "name": "Central"},
            {"record_id": 3, "kind": "park", "name": "South"},
        ]
    )
    monkeypatch.setattr(
        "starlet.query_dataset",
        lambda *args, **kwargs: [frame],
    )
    dataset = {
        "id": "dataset-id",
        "name": "places",
        "schema": [
            {"name": "record_id", "type": "integer"},
            {"name": "kind", "type": "string"},
            {"name": "name", "type": "string"},
        ],
    }
    (tmp_path / "places").mkdir()
    query = normalize_dataframe_query(
        {
            "where": [
                {
                    "attribute": "kind",
                    "operator": "eq",
                    "value": "park",
                    "case_sensitive": False,
                }
            ],
            "operation": "records",
            "select": ["record_id", "name"],
            "limit": 10,
        },
        dataset,
    )
    runner = GeoDataFrameQueryRunner(
        tmp_path,
        timeout_seconds=2,
        process_start_method="fork",
    )

    result = runner.run(dataset, [-1, -1, 1, 1], query)

    assert result["matched_features"] == 2
    assert result["rows"] == [
        {"record_id": 1, "name": "North"},
        {"record_id": 3, "name": "South"},
    ]
    assert result["truncated"] is False


def test_geodataframe_runner_returns_bounded_geometry_facts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    geopandas = pytest.importorskip("geopandas")
    geometry_module = pytest.importorskip("shapely.geometry")
    frame = geopandas.GeoDataFrame(
        [
            {"record_id": 1, "kind": "park"},
            {"record_id": 2, "kind": "school"},
            {"record_id": 3, "kind": "park"},
        ],
        geometry=[
            geometry_module.Point(0, 1),
            geometry_module.Point(50, 50),
            geometry_module.Point(3, 4),
        ],
        crs="EPSG:4326",
    )
    monkeypatch.setattr("starlet.query_dataset", lambda *args, **kwargs: [frame])
    dataset = {
        "id": "dataset-id",
        "name": "places",
        "schema": [
            {"name": "record_id", "type": "integer"},
            {"name": "kind", "type": "string"},
        ],
    }
    (tmp_path / "places").mkdir()
    runner = GeoDataFrameQueryRunner(
        tmp_path,
        timeout_seconds=2,
        process_start_method="fork",
    )
    where = [{"attribute": "kind", "operator": "eq", "value": "park"}]

    bounds_result = runner.run(
        dataset,
        [-180, -90, 180, 90],
        normalize_dataframe_query(
            {"where": where, "operation": "bounds"},
            dataset,
        ),
    )
    records_result = runner.run(
        dataset,
        [-180, -90, 180, 90],
        normalize_dataframe_query(
            {
                "where": where,
                "operation": "geometry_records",
                "select": ["record_id"],
                "geometry_fields": ["type", "bounds", "centroid"],
                "limit": 5,
            },
            dataset,
        ),
    )

    assert bounds_result["geometry_bounds"] == [0.0, 1.0, 3.0, 4.0]
    assert bounds_result["crs"] == "EPSG:4326"
    assert records_result["rows"] == [
        {
            "record_id": 1,
            "geometry": {
                "type": "Point",
                "bounds": [0.0, 1.0, 0.0, 1.0],
                "centroid": [0.0, 1.0],
            },
        },
        {
            "record_id": 3,
            "geometry": {
                "type": "Point",
                "bounds": [3.0, 4.0, 3.0, 4.0],
                "centroid": [3.0, 4.0],
            },
        },
    ]


def test_geodataframe_runner_terminates_a_timed_out_process(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def slow_query(*args, **kwargs):
        time.sleep(10)
        return []

    monkeypatch.setattr("starlet.query_dataset", slow_query)
    dataset = {"id": "dataset-id", "name": "places", "schema": []}
    (tmp_path / "places").mkdir()
    query = normalize_dataframe_query(
        {"operation": "count"},
        dataset,
    )
    runner = GeoDataFrameQueryRunner(
        tmp_path,
        timeout_seconds=0.1,
        process_start_method="fork",
    )
    started = time.monotonic()

    with pytest.raises(ValueError, match="exceeded the 0.1-second time limit"):
        runner.run(dataset, [-1, -1, 1, 1], query)

    assert time.monotonic() - started < 2
    assert not any(
        process.name == "ucrstar-dataframe-query"
        for process in multiprocessing.active_children()
    )


def test_geodataframe_runner_rejects_oversized_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    pandas = pytest.importorskip("pandas")
    monkeypatch.setattr(
        "starlet.query_dataset",
        lambda *args, **kwargs: [pandas.DataFrame([{"description": "x" * 5_000}])],
    )
    dataset = {
        "id": "dataset-id",
        "name": "places",
        "schema": [{"name": "description", "type": "string"}],
    }
    (tmp_path / "places").mkdir()
    query = normalize_dataframe_query(
        {"operation": "records", "select": ["description"], "limit": 1},
        dataset,
    )
    runner = GeoDataFrameQueryRunner(
        tmp_path,
        timeout_seconds=2,
        max_result_bytes=1_000,
        process_start_method="fork",
    )

    with pytest.raises(ValueError, match="exceeded the response size limit"):
        runner.run(dataset, [-1, -1, 1, 1], query)


def test_dataframe_query_rejects_code_and_unknown_attributes() -> None:
    dataset = {
        "schema": [{"name": "kind", "type": "string"}],
    }
    with pytest.raises(ValueError, match="Unsupported dataframe query options"):
        normalize_dataframe_query(
            {
                "expression": "__import__('os').system('echo unsafe')",
                "operation": "records",
                "select": ["kind"],
            },
            dataset,
        )
    with pytest.raises(ValueError, match='Attribute "missing"'):
        normalize_dataframe_query(
            {"operation": "records", "select": ["missing"]},
            dataset,
        )
    with pytest.raises(ValueError, match="geometry_fields containing"):
        normalize_dataframe_query(
            {
                "operation": "geometry_records",
                "geometry_fields": ["raw_coordinates"],
            },
            dataset,
        )


def test_invalid_tool_arguments_return_no_action_and_an_error_trace(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan([{"name": "change_basemap", "arguments": {"basemap": "terrain"}}]),
            final_message("Terrain is not an available basemap."),
        ]
    )
    app = assistant_app(tmp_path, llm)

    body = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Use the terrain basemap"},
    ).get_json()

    assert body["actions"] == []
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        raw_trace = connection.execute(
            "SELECT tool_calls_json FROM chat_messages WHERE role = 'assistant'"
        ).fetchone()[0]
    trace = json.loads(raw_trace)
    assert trace[0]["status"] == "error"
    assert trace[0]["error"] == "Basemap must be street or satellite"


def test_hybrid_rank_includes_good_semantic_match_and_rejects_distant_match() -> None:
    semantic = [
        {"id": "near", "name": "roads", "search_score": 0.2},
        {"id": "far", "name": "weather", "search_score": 1.1},
    ]

    ranked = hybrid_rank("transportation", [], semantic, semantic_max_distance=0.8)

    assert [item["dataset"]["id"] for item in ranked] == ["near"]


def test_action_validation_rejects_unverified_values() -> None:
    try:
        validate_action({"type": "fit_bounds", "bounds": [0, 1]})
    except ValueError as exc:
        assert str(exc) == "Invalid fit_bounds action"
    else:
        raise AssertionError("invalid bounds were accepted")

    try:
        validate_action({"type": "change_basemap", "basemap": "terrain"})
    except ValueError as exc:
        assert str(exc) == "Invalid change_basemap action"
    else:
        raise AssertionError("invalid basemap was accepted")


def test_frontend_dispatches_each_supported_assistant_action() -> None:
    javascript = (
        Path(__file__).parents[1] / "src" / "ucrstar" / "static" / "index.js"
    ).read_text()

    assert "action.type === 'show_datasets'" in javascript
    assert "renderSearchResults(lastSearchResults)" in javascript
    assert "await selectDataset(action.dataset_id)" in javascript
    assert "action.type === 'fit_bounds' && validActionBounds(action.bounds)" in javascript
    assert "map.fitBounds(" in javascript
    assert "await waitForMapLoad();\n        map.fitBounds(" not in javascript
    assert "['==', ['get',action.attribute], action.value]" in javascript
    assert "thinking = null" in javascript
    assert "action.type === 'change_basemap'" in javascript
    assert "updateBasemapMode()" in javascript


def test_assistant_style_is_source_bound_and_adds_point_fallback() -> None:
    dataset = {
        "id": "counties-id",
        "name": "counties",
        "geometry_types": ["Polygon"],
        "num_features": 100,
        "schema": [{"name": "kind", "type": "string"}],
        "summary_json": {
            "attributes": [
                {
                    "name": "kind",
                    "top_k": [{"value": "park", "count": 10}],
                }
            ]
        },
        "visualization": {
            "type": "VectorTile",
            "url": "/datasets/counties-id/tiles/{z}/{x}/{y}.mvt",
            "source_layer": "layer0",
        },
    }
    requested = {
        "version": 8,
        "name": "Park emphasis",
        "sources": {
            "external": {
                "type": "vector",
                "tiles": ["https://untrusted.example/{z}/{x}/{y}.mvt"],
            }
        },
        "layers": [
            {
                "id": "park-fill",
                "type": "fill",
                "source": "external",
                "paint": {
                    "fill-color": [
                        "match",
                        ["get", "kind"],
                        "park",
                        "#22c55e",
                        "#d1d5db",
                    ],
                    "fill-opacity": 0.75,
                },
            }
        ],
    }

    style = build_assistant_style(requested, dataset)

    assert style["sources"] == {
        "dataset": {
            "type": "vector",
            "tiles": ["/datasets/counties-id/tiles/{z}/{x}/{y}.mvt"],
        }
    }
    assert style["layers"][0]["source"] == "dataset"
    assert style["layers"][0]["paint"]["fill-color"] == requested["layers"][0]["paint"]["fill-color"]
    fallback = next(layer for layer in style["layers"] if layer["type"] == "circle")
    assert fallback["filter"] == ["==", ["geometry-type"], "Point"]
    assert fallback["paint"]["circle-color"] == requested["layers"][0]["paint"]["fill-color"]
    assert style["metadata"]["ucrstar:assistant"]["point_fallback"] is True
    assert "zoom in" in style["metadata"]["ucrstar:assistant"]["sampling_note"].lower()


def test_assistant_style_rejects_unknown_attributes() -> None:
    dataset = {
        "id": "places-id",
        "name": "places",
        "geometry_types": ["Point"],
        "schema": [{"name": "name", "type": "string"}],
        "visualization": {"type": "GeoJSON", "url": "/places.geojson"},
    }
    style = {
        "version": 8,
        "layers": [
            {
                "id": "points",
                "type": "circle",
                "paint": {"circle-color": ["get", "invented_field"]},
            }
        ],
    }

    with pytest.raises(ValueError, match="unknown dataset attribute"):
        build_assistant_style(style, dataset)


def test_assistant_style_normalizes_common_llm_maplibre_syntax() -> None:
    dataset = {
        "id": "cemetery-id",
        "name": "cemetery",
        "geometry_types": ["Point", "LineString", "Polygon"],
        "schema": [{"name": "tagsMap", "type": "string"}],
        "visualization": {
            "type": "VectorTile",
            "url": "/datasets/cemetery-id/tiles/{z}/{x}/{y}.mvt",
            "source_layer": "layer0",
        },
    }
    requested = {
        "version": 8,
        "layers": [
            {
                "id": "cemetery-points",
                "type": "circle",
                "filter": ["in", "$type", "Point", "MultiPoint"],
                "paint": {
                    "circle-color": [
                        "case",
                        ["includes", "'religion', 'muslim'", ["get", "tagsMap"]],
                        "#008000",
                        "#808080",
                    ]
                },
            }
        ],
    }

    style = build_assistant_style(requested, dataset)

    layer = style["layers"][0]
    assert layer["filter"] == ["==", ["geometry-type"], "Point"]
    assert layer["paint"]["circle-color"][1][0] == "in"
    assert requested["layers"][0]["paint"]["circle-color"][1][0] == "includes"


def test_assistant_style_normalizes_variadic_maplibre_filters() -> None:
    dataset = {
        "id": "areas-id",
        "name": "areas",
        "geometry_types": ["MultiPolygon", "Polygon"],
        "schema": [{"name": "zip", "type": "integer"}],
        "visualization": {
            "type": "VectorTile",
            "url": "/datasets/areas-id/tiles/{z}/{x}/{y}.mvt",
            "source_layer": "layer0",
        },
    }
    requested = {
        "version": 8,
        "layers": [
            {
                "id": "areas-fill",
                "type": "fill",
                "filter": ["in", ["geometry-type"], "Polygon", "MultiPolygon"],
                "paint": {"fill-color": "#ff0000"},
            },
            {
                "id": "selected-zips",
                "type": "line",
                "filter": ["in", ["get", "zip"], 90001, 90002, 90003],
                "paint": {"line-color": "#222222"},
            },
        ],
    }

    style = build_assistant_style(requested, dataset)

    assert style["layers"][0]["filter"] == ["==", ["geometry-type"], "Polygon"]
    assert style["layers"][1]["filter"] == [
        "in",
        ["get", "zip"],
        ["literal", [90001, 90002, 90003]],
    ]


def test_assistant_style_rejects_glyph_dependent_labels_and_bad_expression_arity() -> None:
    dataset = {
        "id": "areas-id",
        "name": "areas",
        "geometry_types": ["Polygon"],
        "schema": [
            {"name": "zip", "type": "integer"},
            {"name": "label", "type": "string"},
        ],
        "visualization": {"type": "GeoJSON", "url": "/areas.geojson"},
    }
    label_style = {
        "version": 8,
        "layers": [
            {
                "id": "labels",
                "type": "symbol",
                "layout": {"text-field": ["get", "label"], "text-size": 12},
                "paint": {"text-color": "#222222"},
            }
        ],
    }
    bad_expression_style = {
        "version": 8,
        "layers": [
            {
                "id": "areas",
                "type": "fill",
                "paint": {
                    "fill-color": [
                        "case",
                        ["in", ["get", "zip"], ["literal", [90001]], "extra"],
                        "#ff0000",
                        "#808080",
                    ]
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="set_labels canvas overlay"):
        build_assistant_style(label_style, dataset)
    with pytest.raises(ValueError, match='operator "in" expects 2 arguments'):
        build_assistant_style(bad_expression_style, dataset)


def test_style_endpoint_returns_only_server_validated_layers(tmp_path: Path) -> None:
    llm = PlanningLLM([])
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "areas")
    dataset = configure_vector_dataset(app, dataset["id"])
    stored_style = {
        "version": 8,
        "layers": [
            {
                "id": "areas-fill",
                "type": "fill",
                "filter": ["in", ["geometry-type"], "Polygon", "MultiPolygon"],
                "paint": {"fill-color": "#ff0000"},
            },
            {
                "id": "areas-labels",
                "type": "symbol",
                "layout": {"text-field": ["get", "kind"]},
                "paint": {"text-color": "#222222"},
            },
        ],
    }
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute(
            "UPDATE datasets SET style_json = ? WHERE id = ?",
            (json.dumps(stored_style), dataset["id"]),
        )

    response = app.test_client().get(f"/datasets/{dataset['id']}/style.json")

    assert response.status_code == 200
    style = response.get_json()
    assert [layer["id"] for layer in style["layers"]] == ["areas-fill"]
    assert style["layers"][0]["filter"] == ["==", ["geometry-type"], "Polygon"]
    assert "set_labels canvas overlay" in (
        style["metadata"]["ucrstar:style_warnings"][0]
    )


def test_malformed_gemini_tool_plan_is_repaired_without_showing_json() -> None:
    valid = json.dumps(
        {
            "tool_calls": [
                {
                    "name": "apply_style",
                    "arguments": {"style": {"version": 8, "layers": []}},
                }
            ],
            "message": "I applied the requested style.",
        }
    )
    malformed = valid.replace('}}}], "message"', '}}], "message"', 1)
    with pytest.raises(json.JSONDecodeError):
        json.loads(malformed)

    parsed = parse_assistant_plan(malformed, max_tool_calls=5)

    assert parsed["message"] == "I applied the requested style."
    assert parsed["tool_calls"] == [
        {
            "name": "apply_style",
            "arguments": {"style": {"version": 8, "layers": []}},
        }
    ]


def test_irreparable_structured_plan_is_retried_and_not_exposed(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            '{"message":"unfinished',
            final_message("I could not determine a safe map action, so I made no changes."),
        ]
    )
    app = assistant_app(tmp_path, llm)

    response = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Change the map"},
    )

    assert response.status_code == 200
    assert response.get_json()["message"]["content"] == (
        "I could not determine a safe map action, so I made no changes."
    )
    assert len(llm.calls) == 2
    assert "malformed JSON" in llm.calls[1][-1]["content"]


def test_search_then_style_uses_iterative_tool_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("starlet.list_datasets", lambda root: [])
    llm = PlanningLLM([])
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "counties", "County boundaries")
    dataset = configure_vector_dataset(app, dataset["id"])
    requested_style = {
        "version": 8,
        "name": "Dark counties",
        "layers": [
            {
                "id": "county-fill",
                "type": "fill",
                "paint": {"fill-color": "#334155", "fill-opacity": 0.8},
            },
            {
                "id": "county-outline",
                "type": "line",
                "paint": {"line-color": "#f8fafc", "line-width": 2},
            },
        ],
    }
    llm.responses.extend(
        [
            plan([{"name": "search_datasets", "arguments": {"query": "counties"}}]),
            plan([{"name": "apply_style", "arguments": {"style": requested_style}}]),
            final_message("I selected the counties and applied a dark outlined style."),
        ]
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Open counties with a dark fill and bright outline"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert [action["type"] for action in body["actions"]] == [
        "select_dataset",
        "apply_style",
    ]
    assert body["actions"][0]["dataset_id"] == dataset["id"]
    applied = body["actions"][1]
    assert applied["dataset_id"] == dataset["id"]
    assert any(layer["id"] == "ucrstar-point-fallback" for layer in applied["style"]["layers"])
    assert dataset["id"] in llm.calls[1][-1]["content"]
    assert "small_geometry_point_fallback" in llm.calls[1][-1]["content"]


def test_search_then_repaired_categorical_style_executes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("starlet.list_datasets", lambda root: [])
    style = {
        "version": 8,
        "name": "Cemetery religions",
        "layers": [
            {
                "id": "cemetery-points",
                "type": "circle",
                "filter": ["==", "$type", "Point"],
                "paint": {
                    "circle-color": [
                        "case",
                        ["includes", "'religion', 'muslim'", ["get", "tagsMap"]],
                        "#008000",
                        ["includes", "'religion', 'christian'", ["get", "tagsMap"]],
                        "#ff0000",
                        ["includes", "'religion', 'jewish'", ["get", "tagsMap"]],
                        "#0000ff",
                        "#808080",
                    ],
                    "circle-radius": 3,
                },
            }
        ],
    }
    valid_style_plan = json.dumps(
        {
            "tool_calls": [{"name": "apply_style", "arguments": {"style": style}}],
            "message": "Applying the requested religion colors.",
        }
    )
    malformed_style_plan = valid_style_plan.replace(
        '}}}], "message"',
        '}}], "message"',
        1,
    )
    llm = PlanningLLM(
        [
            plan([{"name": "search_datasets", "arguments": {"query": "cemetery"}}]),
            malformed_style_plan,
            final_message("I selected the cemetery data and applied the religion colors."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "cemetery", "Global cemetery features")
    dataset = configure_vector_dataset(app, dataset["id"])
    with sqlite3.connect(app.config["DATABASE"]) as connection:
        connection.execute(
            "UPDATE datasets SET schema_json = ? WHERE id = ?",
            (json.dumps([{"name": "tagsMap", "type": "string"}]), dataset["id"]),
        )

    response = app.test_client().post(
        "/llm/chat.json",
        json={"message": "Show cemetery data with colors for major religions"},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"]["content"] == (
        "I selected the cemetery data and applied the religion colors."
    )
    assert [action["type"] for action in body["actions"]] == [
        "select_dataset",
        "apply_style",
    ]
    expression = body["actions"][1]["style"]["layers"][0]["paint"]["circle-color"]
    assert [expression[index][0] for index in (1, 3, 5)] == ["in", "in", "in"]


def test_invalid_style_is_corrected_server_side_before_action_is_returned(tmp_path: Path) -> None:
    invalid_style = {
        "version": 8,
        "layers": [
            {
                "id": "labels",
                "type": "symbol",
                "layout": {"text-field": ["get", "kind"]},
                "paint": {"text-color": "#222222"},
            }
        ],
    }
    corrected_style = {
        "version": 8,
        "layers": [
            {
                "id": "areas",
                "type": "fill",
                "paint": {"fill-color": "#ff0000", "fill-opacity": 0.7},
            }
        ],
    }
    llm = PlanningLLM(
        [
            plan([{"name": "apply_style", "arguments": {"style": invalid_style}}]),
            plan([{"name": "apply_style", "arguments": {"style": corrected_style}}]),
            final_message("I applied the validated red style without labels."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "areas")
    dataset = configure_vector_dataset(app, dataset["id"])

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Make the areas red",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"]["content"] == (
        "I applied the validated red style without labels."
    )
    assert [action["type"] for action in body["actions"]] == ["apply_style"]
    assert [layer["id"] for layer in body["actions"][0]["style"]["layers"]] == [
        "areas",
        "ucrstar-point-fallback",
    ]
    correction_prompt = llm.calls[1][-1]["content"]
    assert '"status":"error"' in correction_prompt
    assert "set_labels canvas overlay" in correction_prompt


def test_unresolved_invalid_style_cannot_produce_a_false_success_message(tmp_path: Path) -> None:
    invalid_style = {
        "version": 8,
        "layers": [
            {
                "id": "labels",
                "type": "symbol",
                "layout": {"text-field": ["get", "kind"]},
                "paint": {"text-color": "#222222"},
            }
        ],
    }
    llm = PlanningLLM(
        [
            plan([{"name": "apply_style", "arguments": {"style": invalid_style}}]),
            final_message("The style was applied successfully."),
        ]
    )
    app = assistant_app(tmp_path, llm, chat_config={"max_tool_rounds": 1})
    dataset = publish_dataset(app, "areas")
    dataset = configure_vector_dataset(app, dataset["id"])

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Label the areas",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["actions"] == []
    assert "couldn't apply the requested style" in body["message"]["content"]
    assert "No invalid style was sent to the map" in body["message"]["content"]
    assert "applied successfully" not in body["message"]["content"]


def test_labels_are_validated_and_returned_as_a_canvas_overlay_action(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan(
                [
                    {
                        "name": "set_labels",
                        "arguments": {
                            "attribute": "kind",
                            "size": 16,
                            "color": "#223344",
                            "background": "light",
                            "min_zoom": 8,
                            "allow_overlap": True,
                        },
                    }
                ]
            ),
            final_message("I centered the kind label on each visible polygon."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Label each polygon with its kind and center it",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["actions"] == [
        {
            "type": "set_labels",
            "dataset_id": dataset["id"],
            "attribute": "kind",
            "size": 16.0,
            "color": "#223344",
            "background": "light",
            "min_zoom": 8.0,
            "max_zoom": 24.0,
            "allow_overlap": True,
            "placement": "center",
        }
    ]
    continuation = llm.calls[1][-1]["content"]
    assert '"labels":{"attribute":"kind"' in continuation


def test_false_glyph_refusal_is_corrected_to_the_canvas_label_tool(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            final_message(
                "I cannot display labels because there is no server-approved glyph source."
            ),
            plan([{"name": "set_labels", "arguments": {"attribute": "kind"}}]),
            final_message("I added the requested labels."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Add kind labels to the polygons",
            "context": {
                "dataset_id": dataset["id"],
                "style": {
                    "version": 8,
                    "metadata": {
                        "ucrstar:style_warnings": [
                            "Dropped a text label layer because there is no glyph source"
                        ]
                    },
                    "layers": [],
                },
            },
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["message"]["content"] == "I added the requested labels."
    assert [action["type"] for action in body["actions"]] == ["set_labels"]
    assert len(llm.calls) == 3
    initial_context = next(
        message["content"]
        for message in llm.calls[0]
        if message["role"] == "user"
        and message["content"].startswith("Current application context")
    )
    assert "ucrstar:style_warnings" not in initial_context
    assert '"text_labels":{"available":true,"tool":"set_labels"' in initial_context
    correction = llm.calls[1][-1]["content"]
    assert "text labels are available through the set_labels" in correction


def test_unicode_icons_are_validated_for_point_dataset_categories(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan(
                [
                    {
                        "name": "set_point_icons",
                        "arguments": {
                            "attribute": "category",
                            "icons": {"restaurant": "🍴", "coffee": "☕"},
                            "default_icon": "📍",
                            "size": 28,
                            "min_zoom": 5,
                        },
                    }
                ]
            ),
            final_message("I added category-specific icons to the visible POIs."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = configure_point_dataset(
        app,
        publish_dataset(app, "points_of_interest")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Use restaurant and coffee icons",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["actions"] == [
        {
            "type": "set_point_icons",
            "dataset_id": dataset["id"],
            "attribute": "category",
            "icons": {"restaurant": "🍴", "coffee": "☕"},
            "default_icon": "📍",
            "size": 28.0,
            "min_zoom": 5.0,
            "max_zoom": 24.0,
            "allow_overlap": False,
        }
    ]


def test_label_and_icon_tools_reject_unsupported_dataset_requests(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan([{"name": "set_labels", "arguments": {"attribute": "missing"}}]),
            plan(
                [
                    {
                        "name": "set_point_icons",
                        "arguments": {"default_icon": "📍"},
                    }
                ]
            ),
            final_message("Those display requests are not valid for this dataset."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = configure_vector_dataset(
        app,
        publish_dataset(app, "areas")["id"],
    )

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Label missing and use pins",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["actions"] == []
    assert '"status":"error"' in llm.calls[1][-1]["content"]
    assert "not present in the dataset schema" in llm.calls[1][-1]["content"]
    assert '"status":"error"' in llm.calls[2][-1]["content"]
    assert "only for point datasets" in llm.calls[2][-1]["content"]


def test_overlay_action_validation_rejects_invalid_ranges_and_symbols() -> None:
    with pytest.raises(ValueError, match="min_zoom must be less"):
        validate_action(
            {
                "type": "set_labels",
                "dataset_id": "dataset",
                "attribute": "name",
                "min_zoom": 10,
                "max_zoom": 5,
            }
        )
    with pytest.raises(ValueError, match="non-empty Unicode symbol"):
        validate_action(
            {
                "type": "set_point_icons",
                "dataset_id": "dataset",
                "attribute": None,
                "default_icon": "\n",
            }
        )


def test_highlight_uses_selected_feature_id_from_browser_context(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan([{"name": "highlight_feature", "arguments": {"color": "#ffcc00"}}]),
            final_message("I highlighted the selected feature."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "parcels")
    dataset = configure_vector_dataset(app, dataset["id"])

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Highlight this feature in yellow",
            "context": {
                "dataset_id": dataset["id"],
                "selected_feature_id": 42,
            },
        },
    )

    assert response.status_code == 200
    assert response.get_json()["actions"] == [
        {
            "type": "highlight_feature",
            "dataset_id": dataset["id"],
            "feature_id": 42,
            "color": "#ffcc00",
        }
    ]


def test_reset_style_returns_a_small_server_default_action(tmp_path: Path) -> None:
    llm = PlanningLLM(
        [
            plan([{"name": "reset_style", "arguments": {}}]),
            final_message("I restored the server-default style."),
        ]
    )
    app = assistant_app(tmp_path, llm)
    dataset = publish_dataset(app, "parcels")

    response = app.test_client().post(
        "/llm/chat.json",
        json={
            "message": "Reset the style",
            "context": {"dataset_id": dataset["id"]},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["actions"] == [
        {"type": "reset_style", "dataset_id": dataset["id"]}
    ]


def test_frontend_preserves_advanced_styles_and_id_highlights() -> None:
    javascript = (
        Path(__file__).parents[1] / "src" / "ucrstar" / "static" / "index.js"
    ).read_text()
    stylesheet = (
        Path(__file__).parents[1] / "src" / "ucrstar" / "static" / "index.css"
    ).read_text()

    assert "addDatasetSourceAndStyleLayers" in javascript
    assert "['fill','line','circle','symbol','heatmap','fill-extrusion']" in javascript
    assert "action.type === 'apply_style'" in javascript
    assert "action.type === 'reset_style'" in javascript
    assert "action.type === 'highlight_feature'" in javascript
    assert "['get','_id']" in javascript
    assert "Number.isSafeInteger(action.feature_id)" in javascript
    assert "action.type === 'set_labels'" in javascript
    assert "action.type === 'set_point_icons'" in javascript
    assert '"Apple Color Emoji"' in javascript
    assert "properties._id != null ? properties._id : f.id" in javascript
    assert "ringCentroid(ring)" in javascript
    assert "context.point_icons = clone(_pointIconConfig)" in javascript
    assert "updateLabelStyleLayer" not in javascript
    assert "assistantMessageText(data.message.content)" in javascript
    assert '/"tool_calls"\\s*:/' in javascript
    assert "showFeaturePopup(properties, e.lngLat, sampleGeojsonUrl, fallback._id)" in javascript
    assert "isolation: isolate" in stylesheet
    assert "#label-canvas" in stylesheet
    assert "z-index: 1" in stylesheet
    assert ".maplibregl-popup {\n  z-index: 20;\n}" in stylesheet
