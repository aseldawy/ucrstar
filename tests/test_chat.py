import json
import sqlite3
import uuid
from pathlib import Path

from ucrstar.app import create_app
from ucrstar.chat import LLMRegistry


class FakeChatLLM:
    enabled = True
    provider = "ollama"
    chat_model = "unit-chat"
    embedding_model = "unit-embed"
    embedding_key = "ollama:unit-embed"

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]]) -> str:
        self.calls.append(messages)
        return f"Assistant response {len(self.calls)}"


def enabled_config() -> dict:
    return {
        "llm": {
            "default": "ollama",
            "semantic_search": True,
            "providers": {
                "ollama": {
                    "enabled": True,
                    "base_url": "http://unused.test",
                    "chat_model": "unit-chat",
                    "embedding_model": "unit-embed",
                }
            },
        }
    }


def chat_app(tmp_path: Path, llm: FakeChatLLM):
    return create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
            "UCRSTAR2_CONFIG": enabled_config(),
            "LLM_CLIENT": llm,
        }
    )


def test_capabilities_disable_chat_when_llm_is_disabled(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
            "UCRSTAR2_CONFIG": {"llm": {"providers": {}}},
        }
    )

    response = app.test_client().get("/llm/capabilities.json")

    assert response.status_code == 200
    assert response.get_json() == {
        "available": False,
        "status": "disabled",
        "reason": "No LLM providers are enabled.",
        "default_model": None,
        "models": [],
        "capabilities": {
            "chat": False,
            "server_history": False,
            "semantic_search": False,
            "viewport_summary": False,
            "viewport_dataframe_query": False,
            "viewport_geometry_query": False,
            "query_result_focus": False,
            "style_generation": False,
            "feature_highlight": False,
            "text_labels": False,
            "unicode_point_icons": False,
        },
        "action_types": [
            "show_datasets",
            "select_dataset",
            "fit_bounds",
            "change_basemap",
            "apply_style",
            "reset_style",
            "highlight_feature",
            "clear_highlight",
            "set_labels",
            "clear_labels",
            "set_point_icons",
            "clear_point_icons",
        ],
    }


def test_capabilities_publish_only_the_server_configured_model(tmp_path: Path) -> None:
    app = chat_app(tmp_path, FakeChatLLM())

    body = app.test_client().get("/llm/capabilities.json").get_json()

    assert body["available"] is True
    assert body["default_model"] == "ollama:unit-chat"
    assert body["models"] == [
        {"id": "ollama:unit-chat", "label": "unit-chat", "provider": "ollama"}
    ]
    assert "base_url" not in json.dumps(body)


def test_capabilities_publish_all_valid_enabled_providers_and_configured_default(
    tmp_path: Path,
) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
            "UCRSTAR2_CONFIG": {
                "llm": {
                    "default": "gemini",
                    "providers": {
                        "openai": {
                            "enabled": False,
                            "chat_model": "hidden-model",
                        },
                        "gemini": {
                            "enabled": True,
                            "api_key": "unit-key",
                            "chat_model": "gemini-unit",
                            "embedding_model": "gemini-embed",
                        },
                        "ollama": {
                            "enabled": True,
                            "base_url": "http://unused.test",
                            "chat_model": "ollama-unit",
                            "embedding_model": "ollama-embed",
                        },
                    },
                }
            },
        }
    )

    body = app.test_client().get("/llm/capabilities.json").get_json()

    assert body["available"] is True
    assert body["default_model"] == "gemini:gemini-unit"
    assert body["models"] == [
        {
            "id": "gemini:gemini-unit",
            "label": "gemini-unit",
            "provider": "gemini",
        },
        {
            "id": "ollama:ollama-unit",
            "label": "ollama-unit",
            "provider": "ollama",
        },
    ]


def test_registry_routes_and_caches_clients_by_selected_provider(monkeypatch) -> None:
    config = {
        "llm": {
            "default": "gemini",
            "providers": {
                "gemini": {
                    "enabled": True,
                    "api_key": "unit-key",
                    "chat_model": "gemini-unit",
                },
                "ollama": {
                    "enabled": True,
                    "base_url": "http://unused.test",
                    "chat_model": "ollama-unit",
                },
            },
        }
    }
    created = []

    class RoutedClient:
        enabled = True

        def __init__(self, provider: str) -> None:
            self.provider = provider

    def fake_llm_from_config(received_config, provider=None):
        assert received_config is config
        created.append(provider)
        return RoutedClient(provider)

    monkeypatch.setattr("ucrstar.chat.llm_from_config", fake_llm_from_config)
    registry = LLMRegistry(config)

    gemini = registry.client("gemini:gemini-unit")
    ollama = registry.client("ollama:ollama-unit")

    assert gemini.provider == "gemini"
    assert ollama.provider == "ollama"
    assert registry.client("gemini:gemini-unit") is gemini
    assert created == ["gemini", "ollama"]


def test_integrated_enrichment_only_backend_does_not_advertise_chat(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
            "UCRSTAR2_CONFIG": {
                "llm": {
                    "default": "integrated",
                    "providers": {
                        "integrated": {
                            "enabled": True,
                            "backend": "builtin",
                            "chat_model": "builtin-heuristic",
                            "embedding_model": "builtin-hash",
                        }
                    },
                }
            },
        }
    )

    body = app.test_client().get("/llm/capabilities.json").get_json()

    assert body["available"] is False
    assert body["status"] == "misconfigured"
    assert "enrichment but not chat" in body["reason"]


def test_first_chat_request_creates_session_and_followup_uses_history(tmp_path: Path) -> None:
    llm = FakeChatLLM()
    app = chat_app(tmp_path, llm)
    client = app.test_client()

    first_response = client.post(
        "/llm/chat.json",
        json={
            "message": "What am I looking at?",
            "context": {
                "viewport": {
                    "bounds": [-118.0, 33.0, -117.0, 34.0],
                    "center": [-117.5, 33.5],
                    "zoom": 9,
                },
                "basemap": "street",
            },
        },
    )

    assert first_response.status_code == 200
    first = first_response.get_json()
    uuid.UUID(first["session_id"])
    assert first["message"]["content"] == "Assistant response 1"
    assert first["actions"] == []
    assert "Current application context" in llm.calls[0][-1]["content"]
    assert '"bounds":[-118.0,33.0,-117.0,34.0]' in llm.calls[0][-1]["content"]

    second_response = client.post(
        "/llm/chat.json",
        json={
            "session_id": first["session_id"],
            "model_id": "ollama:unit-chat",
            "message": "Please explain that more simply.",
            "context": {"basemap": "satellite"},
        },
    )

    assert second_response.status_code == 200
    second = second_response.get_json()
    assert second["session_id"] == first["session_id"]
    assert second["message"]["content"] == "Assistant response 2"
    assert [message["role"] for message in llm.calls[1]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert llm.calls[1][1]["content"] == "What am I looking at?"
    assert llm.calls[1][2]["content"] == "Assistant response 1"

    with sqlite3.connect(app.config["DATABASE"]) as connection:
        session = connection.execute(
            "SELECT id, model_id FROM chat_sessions"
        ).fetchone()
        messages = connection.execute(
            "SELECT role, content, context_json FROM chat_messages ORDER BY sequence"
        ).fetchall()
    assert session == (first["session_id"], "ollama:unit-chat")
    assert [message[0] for message in messages] == ["user", "assistant", "user", "assistant"]
    assert json.loads(messages[0][2])["viewport"]["zoom"] == 9.0


def test_missing_saved_session_returns_not_found_without_calling_provider(tmp_path: Path) -> None:
    llm = FakeChatLLM()
    client = chat_app(tmp_path, llm).test_client()

    response = client.post(
        "/llm/chat.json",
        json={
            "session_id": str(uuid.uuid4()),
            "model_id": "ollama:unit-chat",
            "message": "Hello",
        },
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Chat session not found"
    assert llm.calls == []


def test_chat_rejects_invalid_context_and_unconfigured_models(tmp_path: Path) -> None:
    llm = FakeChatLLM()
    client = chat_app(tmp_path, llm).test_client()

    invalid_context = client.post(
        "/llm/chat.json",
        json={
            "message": "Hello",
            "model_id": "ollama:unit-chat",
            "context": {"viewport": {"bounds": [1, 2]}},
        },
    )
    invalid_model = client.post(
        "/llm/chat.json",
        json={"message": "Hello", "model_id": "unconfigured:model"},
    )

    assert invalid_context.status_code == 400
    assert invalid_context.get_json()["error"] == "context.viewport.bounds is invalid"
    assert invalid_model.status_code == 400
    assert "not configured" in invalid_model.get_json()["error"]
    assert llm.calls == []


def test_disabled_chat_endpoint_returns_service_unavailable(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "DATASETS_DIR": tmp_path / "datasets",
            "DATABASE": tmp_path / "instance" / "test.sqlite",
            "UCRSTAR2_CONFIG": {"llm": {"providers": {}}},
        }
    )

    response = app.test_client().post("/llm/chat.json", json={"message": "Hello"})

    assert response.status_code == 503
    assert response.get_json()["reason"] == "No LLM providers are enabled."


def test_frontend_uses_server_chat_instead_of_direct_ollama() -> None:
    javascript = Path("src/ucrstar/static/index.js").read_text(encoding="utf-8")
    html = Path("src/ucrstar/static/index.html").read_text(encoding="utf-8")

    assert "/llm/capabilities.json" in javascript
    assert "/llm/chat.json" in javascript
    assert "ucrstar-ai-model-id" in javascript
    assert "saveModel(modelSelect.value)" in javascript
    assert "localhost:11434" not in javascript
    assert "llama3.2" not in html
    assert "id=\"aiFab\" title=\"AI Assistant\" disabled" in html
