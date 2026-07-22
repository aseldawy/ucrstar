import json
import sqlite3
from pathlib import Path

from ucrstar.app import create_app
from ucrstar.assistant_tools import ViewportSummarizer, hybrid_rank, validate_action


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


class FakeBatch:
    def __init__(self, records: list[dict]) -> None:
        self.records = records

    def to_dict(self, *, orient: str):
        assert orient == "records"
        return self.records


def assistant_config(**chat_overrides) -> dict:
    return {
        "llm": {
            "enabled": True,
            "provider": "ollama",
            "semantic_search": True,
            "chat": chat_overrides,
            "providers": {
                "ollama": {
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
):
    return create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
            "UCRSTAR2_CONFIG": assistant_config(**(chat_config or {})),
            "LLM_CLIENT": llm,
            "GEOCODER": geocoder or FakeGeocoder([]),
        }
    )


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
    assert "geometry" not in synthesis_prompt


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
    assert "action.type === 'change_basemap'" in javascript
    assert "updateBasemapMode()" in javascript
