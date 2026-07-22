import json
from pathlib import Path

from ucrstar import llm as llm_module
from ucrstar.llm import (
    GeminiClient,
    LLMConfig,
    OllamaClient,
    OpenAIClient,
    llm_from_config,
    resolve_integrated_model,
    ssl_context,
)


def test_integrated_builtin_enriches_without_external_server() -> None:
    llm = llm_from_config(
        {
            "llm": {
                "default": "integrated",
                "max_description_chars": 80,
                "providers": {
                    "integrated": {
                        "enabled": True,
                        "backend": "builtin",
                        "model_path": "",
                        "chat_model": "builtin-heuristic",
                        "embedding_model": "builtin-hash",
                        "embedding_dimensions": 16,
                    }
                },
            }
        }
    )

    enrichment = llm.enrich_dataset(
        {
            "name": "road_centerlines",
            "num_features": 12,
            "geometry_types": ["LineString"],
            "schema": [{"name": "road_name", "type": "text"}],
        }
    )
    embedding = llm.embed("roads and streets")

    assert enrichment["description"]
    assert len(enrichment["description"]) <= 80
    assert enrichment["attributes"]["road_name"]
    assert enrichment["style"]["layers"]["line"]["line-color"]
    assert len(embedding) == 16


def test_integrated_builtin_translates_esri_unique_value_renderer() -> None:
    llm = llm_from_config(
        {
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
        }
    )

    enrichment = llm.enrich_dataset(
        {
            "name": "zoning",
            "geometry_types": ["Polygon"],
            "schema": [{"name": "ZONE", "type": "text"}],
            "source_style": {
                "format": "esri-renderer",
                "renderer": {
                    "type": "uniqueValue",
                    "field1": "ZONE",
                    "uniqueValueInfos": [
                        {
                            "value": "R1",
                            "label": "Residential",
                            "symbol": {"color": [20, 120, 220, 255]},
                        },
                        {
                            "value": "C1",
                            "label": "Commercial",
                            "symbol": {"color": [230, 80, 40, 255]},
                        },
                    ],
                },
            },
        }
    )

    expression = enrichment["style"]["layers"]["fill"]["fill-color"]
    assert expression == [
        "match",
        ["get", "ZONE"],
        "R1",
        "#1478dc",
        "C1",
        "#e65028",
        "#bdbdbd",
    ]
    assert enrichment["style"]["metadata"]["ucrstar:legend"]["labels"] == {
        "R1": "Residential",
        "C1": "Commercial",
    }


def test_integrated_model_resolver_reuses_downloaded_model(tmp_path: Path) -> None:
    model_path = (
        tmp_path
        / "models"
        / "Qwen__Qwen2.5-0.5B-Instruct-GGUF"
        / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    )
    model_path.parent.mkdir(parents=True)
    model_path.write_bytes(b"model")

    resolved = resolve_integrated_model(
        {
            "model_dir": str(tmp_path / "models"),
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "model_file": "qwen2.5-0.5b-instruct-q4_k_m.gguf",
        }
    )

    assert resolved == model_path


def test_integrated_model_resolver_downloads_missing_model(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm_module,
        "discover_gguf_file",
        lambda model_id: "model-q4_k_m.gguf",
    )
    monkeypatch.setattr(
        llm_module,
        "download_file",
        lambda url, target_path: target_path.write_bytes(url.encode("utf-8")),
    )

    resolved = resolve_integrated_model(
        {
            "model_dir": str(tmp_path / "models"),
            "model_id": "org/model",
        }
    )

    assert resolved == tmp_path / "models" / "org__model" / "model-q4_k_m.gguf"
    assert resolved.read_text(encoding="utf-8") == (
        "https://huggingface.co/org/model/resolve/main/model-q4_k_m.gguf"
    )


def test_integrated_model_discovery_uses_ssl_context(monkeypatch) -> None:
    calls = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(
                {
                    "siblings": [
                        {"rfilename": "model-q8_0.gguf"},
                        {"rfilename": "model-q4_k_m.gguf"},
                    ]
                }
            ).encode("utf-8")

    context = object()
    monkeypatch.setattr(llm_module, "ssl_context", lambda: context)

    def fake_urlopen(url, timeout, context=None):
        calls["url"] = url
        calls["timeout"] = timeout
        calls["context"] = context
        return FakeResponse()

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)

    model_file = llm_module.discover_gguf_file("org/model")

    assert model_file == "model-q4_k_m.gguf"
    assert calls == {
        "url": "https://huggingface.co/api/models/org/model",
        "timeout": 60,
        "context": context,
    }


def test_integrated_model_download_uses_ssl_context(tmp_path: Path, monkeypatch) -> None:
    calls = {}

    class FakeResponse:
        chunks = [b"abc", b"def", b""]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            calls.setdefault("sizes", []).append(size)
            return self.chunks.pop(0)

    context = object()
    monkeypatch.setattr(llm_module, "ssl_context", lambda: context)

    def fake_urlopen(url, timeout, context=None):
        calls["url"] = url
        calls["timeout"] = timeout
        calls["context"] = context
        return FakeResponse()

    monkeypatch.setattr(llm_module.urllib.request, "urlopen", fake_urlopen)

    target_path = tmp_path / "model.gguf"
    llm_module.download_file("https://example.test/model.gguf", target_path)

    assert target_path.read_bytes() == b"abcdef"
    assert not target_path.with_suffix(".gguf.part").exists()
    assert calls["url"] == "https://example.test/model.gguf"
    assert calls["timeout"] == 600
    assert calls["context"] is context
    assert calls["sizes"] == [1024 * 1024, 1024 * 1024, 1024 * 1024]


def test_provider_http_uses_ssl_context() -> None:
    context = ssl_context()

    assert context.verify_mode.name == "CERT_REQUIRED"


def test_gemini_embedding_uses_current_model_and_header(monkeypatch) -> None:
    calls = {}

    def fake_post_json(url, body, headers):
        calls["url"] = url
        calls["body"] = body
        calls["headers"] = headers
        return {"embedding": {"values": [0.1, 0.2]}}

    monkeypatch.setattr("ucrstar.llm.post_json", fake_post_json)
    client = GeminiClient(
        LLMConfig(
            enabled=True,
            provider="gemini",
            max_description_chars=250,
            semantic_search=True,
            search_limit=20,
            fallback_on_error=True,
            provider_config={
                "api_key": "secret",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "chat_model": "gemini-2.5-flash",
                "embedding_model": "gemini-embedding-2",
            },
        )
    )

    vector = client.embed("cemetery dataset")

    assert vector == [0.1, 0.2]
    assert calls["url"].endswith("/models/gemini-embedding-2:embedContent")
    assert calls["headers"]["x-goog-api-key"] == "secret"
    assert "title: dataset catalog entry | text:" in calls["body"]["content"]["parts"][0]["text"]


def test_openai_chat_sends_normalized_history(monkeypatch) -> None:
    client = OpenAIClient(
        LLMConfig(
            enabled=True,
            provider="openai",
            max_description_chars=250,
            semantic_search=True,
            search_limit=20,
            fallback_on_error=True,
            provider_config={"chat_model": "test-chat", "embedding_model": "test-embed"},
        )
    )
    calls = {}

    def fake_post(path, body):
        calls["path"] = path
        calls["body"] = body
        return {"choices": [{"message": {"content": "  Hello from OpenAI  "}}]}

    monkeypatch.setattr(client, "_post", fake_post)
    messages = [
        {"role": "system", "content": "Be helpful"},
        {"role": "user", "content": "Hello"},
    ]

    response = client.chat(messages)

    assert response == "Hello from OpenAI"
    assert calls["path"] == "/chat/completions"
    assert calls["body"]["messages"] == messages


def test_gemini_chat_maps_assistant_role_and_system_instruction(monkeypatch) -> None:
    client = GeminiClient(
        LLMConfig(
            enabled=True,
            provider="gemini",
            max_description_chars=250,
            semantic_search=True,
            search_limit=20,
            fallback_on_error=True,
            provider_config={"chat_model": "test-chat", "embedding_model": "test-embed"},
        )
    )
    calls = {}

    def fake_post(path, body):
        calls["path"] = path
        calls["body"] = body
        return {
            "candidates": [
                {"content": {"parts": [{"text": "Gemini "}, {"text": "response"}]}}
            ]
        }

    monkeypatch.setattr(client, "_post", fake_post)

    response = client.chat(
        [
            {"role": "system", "content": "Be helpful"},
            {"role": "user", "content": "First"},
            {"role": "assistant", "content": "Earlier response"},
            {"role": "user", "content": "Second"},
        ]
    )

    assert response == "Gemini response"
    assert calls["path"] == "/models/test-chat:generateContent"
    assert calls["body"]["systemInstruction"] == {"parts": [{"text": "Be helpful"}]}
    assert [item["role"] for item in calls["body"]["contents"]] == ["user", "model", "user"]
    assert calls["body"]["generationConfig"] == {
        "responseMimeType": "application/json",
        "temperature": 0.2,
        "maxOutputTokens": 8192,
    }


def test_ollama_chat_uses_server_configured_model(monkeypatch) -> None:
    client = OllamaClient(
        LLMConfig(
            enabled=True,
            provider="ollama",
            max_description_chars=250,
            semantic_search=True,
            search_limit=20,
            fallback_on_error=True,
            provider_config={"chat_model": "test-chat", "embedding_model": "test-embed"},
        )
    )
    calls = {}

    def fake_post(path, body):
        calls["path"] = path
        calls["body"] = body
        return {"message": {"content": "Ollama response"}}

    monkeypatch.setattr(client, "_post", fake_post)
    messages = [{"role": "user", "content": "Hello"}]

    response = client.chat(messages)

    assert response == "Ollama response"
    assert calls["path"] == "/api/chat"
    assert calls["body"]["model"] == "test-chat"
    assert calls["body"]["messages"] == messages
