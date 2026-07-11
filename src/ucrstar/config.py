from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path("ucrstar.config.json")


DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "enabled": False,
        "provider": "ollama",
        "max_description_chars": 250,
        "semantic_search": True,
        "search_limit": 20,
        "fallback_on_error": True,
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
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as file:
            config = deep_merge(config, json.load(file))
    return expand_env(config)


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
