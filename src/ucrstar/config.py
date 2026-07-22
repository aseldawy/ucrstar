from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import tomllib
from typing import Any


DEFAULT_CONFIG_PATH = Path("ucrstar.config.json")


DEFAULT_CONFIG: dict[str, Any] = {
    "runtime": {
        "temp_dir": None,
    },
    "llm": {
        "enabled": False,
        "provider": "ollama",
        "max_description_chars": 250,
        "semantic_search": True,
        "search_limit": 20,
        "fallback_on_error": True,
        "chat": {
            "max_message_chars": 8000,
            "max_history_messages": 20,
            "max_context_chars": 80000,
            "max_style_chars": 40000,
            "max_tool_calls": 5,
            "max_tool_rounds": 3,
            "tool_search_limit": 10,
            "semantic_max_distance": 0.8,
            "style_max_layers": 40,
            "style_max_nodes": 5000,
            "viewport_max_features": 5000,
            "viewport_sample_size": 5,
            "viewport_max_attributes": 20,
            "viewport_top_values": 5,
            "dataframe_query_timeout_seconds": 5,
            "dataframe_query_max_result_bytes": 32000,
            "dataframe_query_max_scanned_features": 50000,
            "dataframe_query_batch_size": 1000,
        },
        "geocoding": {
            "base_url": "https://nominatim.openstreetmap.org/search",
            "user_agent": "ucrstar/0.1 (geospatial dataset assistant)",
            "timeout_seconds": 10,
        },
        "providers": {
            "openai": {
                "api_key": "${OPENAI_API_KEY}",
                "base_url": "https://api.openai.com/v1",
                "chat_model": "gpt-4o-mini",
                "embedding_model": "text-embedding-3-small",
            },
            "gemini": {
                "api_key": "${GEMINI_API_KEY}",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "chat_model": "gemini-1.5-flash",
                "embedding_model": "gemini-embedding-2",
            },
            "ollama": {
                "base_url": "http://127.0.0.1:11434",
                "chat_model": "llama3.1",
                "embedding_model": "nomic-embed-text",
            },
            "integrated": {
                "backend": "llama-cpp",
                "model_dir": "models",
                "model_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                "model_file": "",
                "chat_model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
                "embedding_model": "builtin-hash",
                "embedding_dimensions": 128,
            },
        },
    },
    "logging": {
        "output": "file",
        "dir": "log",
        "level": "INFO",
    },
}


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config_path = Path(path or os.environ.get("UCRSTAR2_CONFIG", DEFAULT_CONFIG_PATH))
    pyproject_path = Path("pyproject.toml")
    if config_path != pyproject_path and pyproject_path.exists():
        config = deep_merge(config, read_config_file(pyproject_path))
    if config_path.exists():
        config = deep_merge(config, read_config_file(config_path))
    return expand_env(config)


def read_config_file(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".toml":
        with path.open("rb") as file:
            loaded = tomllib.load(file)
        return ((loaded.get("tool") or {}).get("ucrstar") or {}) if path.name == "pyproject.toml" else loaded
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def configure_runtime(config: dict[str, Any]) -> None:
    runtime = config.get("runtime") or {}
    temp_dir = runtime.get("temp_dir")
    if not temp_dir:
        return
    path = Path(temp_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(path)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            base[key] = deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value
