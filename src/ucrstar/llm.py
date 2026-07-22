from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)


DEFAULT_STYLE = {
    "source_layer": "layer0",
    "layers": {
        "fill": {
            "fill-color": "#2a9d8f",
            "fill-opacity": 0.25,
        },
        "line": {
            "line-color": "#0f6b99",
            "line-width": ["interpolate", ["linear"], ["zoom"], 2, 0.7, 9, 2.5],
            "line-opacity": 0.88,
        },
        "circle": {
            "circle-color": "#d1495b",
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 2, 10, 5],
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": 0.8,
        },
    },
}


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool
    provider: str
    max_description_chars: int
    semantic_search: bool
    search_limit: int
    fallback_on_error: bool
    provider_config: dict[str, Any]


def llm_from_config(config: dict[str, Any]) -> "LLMClient":
    llm = config.get("llm") or {}
    provider = llm.get("provider", "integrated")
    providers = llm.get("providers") or {}
    settings = LLMConfig(
        enabled=bool(llm.get("enabled", False)),
        provider=provider,
        max_description_chars=int(llm.get("max_description_chars", 250)),
        semantic_search=bool(llm.get("semantic_search", True)),
        search_limit=int(llm.get("search_limit", 20)),
        fallback_on_error=bool(llm.get("fallback_on_error", True)),
        provider_config=providers.get(provider, {}),
    )

    if not settings.enabled:
        return NullLLM(settings)
    if provider == "openai":
        LOGGER.info("Using OpenAI LLM provider")
        return OpenAIClient(settings)
    if provider == "gemini":
        LOGGER.info("Using Gemini LLM provider")
        return GeminiClient(settings)
    if provider == "ollama":
        LOGGER.info("Using Ollama LLM provider")
        return OllamaClient(settings)
    if provider == "integrated":
        LOGGER.info("Using integrated LLM provider")
        return IntegratedClient(settings)
    LOGGER.warning("Unknown LLM provider '%s'; using null provider", provider)
    return NullLLM(settings)


class LLMClient:
    def __init__(self, settings: LLMConfig) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def provider(self) -> str:
        return self.settings.provider

    @property
    def chat_model(self) -> str:
        return self.settings.provider_config.get("chat_model", "unknown-chat")

    @property
    def embedding_model(self) -> str:
        return self.settings.provider_config.get("embedding_model", "unknown-embedding")

    @property
    def embedding_key(self) -> str:
        return f"{self.provider}:{self.embedding_model}"

    def enrich_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {}
        prompt = (
            "Return compact JSON that improves this geospatial dataset catalog entry. "
            f"Dataset descriptions must be at most {self.settings.max_description_chars} characters. "
            "Use keys: description, attributes, style. attributes is an object keyed by field name "
            "with short human-readable descriptions. style must be a MapLibre Style Specification "
            "v8 document with a sources object and layers array. Use MapLibre get/match/interpolate "
            "expressions so the styled attributes and category or range meanings remain machine-readable. "
            "Never classify features by cosmetic storage fields whose names or values describe colors, "
            "RGB, or hex codes. Use a meaningful category such as a name, type, class, or status and use "
            "colors only as expression outputs. For categorical match expressions, include every category "
            "supported by the supplied statistics and use an explicit fallback color for all other values. "
            "Only choose a categorical style when its explicit categories account for at least 80 percent "
            "of all dataset records according to the supplied counts. Otherwise use a constant color. "
            "When source_style contains an Esri renderer, translate its fields, values, labels, and colors "
            "into equivalent MapLibre expressions and include ucrstar:legend metadata when useful. Dataset:\n"
            + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        )
        result = self.complete_json(prompt)
        if not isinstance(result, dict):
            return {}
        if result.get("description"):
            result["description"] = trim_text(
                str(result["description"]),
                self.settings.max_description_chars,
            )
        return result

    def embed(self, text: str) -> list[float]:
        return deterministic_embedding(text)

    def chat(self, messages: list[dict[str, str]]) -> str:
        """Return an assistant response for a normalized chat message list."""
        raise NotImplementedError

    def complete_json(self, prompt: str) -> dict[str, Any]:
        raise NotImplementedError


class NullLLM(LLMClient):
    @property
    def enabled(self) -> bool:
        return False

    def chat(self, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("LLM support is not available")

    def complete_json(self, prompt: str) -> dict[str, Any]:
        return {}


class IntegratedClient(LLMClient):
    def __init__(self, settings: LLMConfig) -> None:
        super().__init__(settings)
        self._llama = None
        self._llama_load_error = None
        if settings.provider_config.get("backend") == "llama-cpp":
            try:
                from llama_cpp import Llama  # type: ignore

                model_path = resolve_integrated_model(settings.provider_config)
                LOGGER.info("Loading integrated llama-cpp model from %s", model_path)
                self._llama = Llama(model_path=str(model_path), verbose=False)
            except Exception as exc:
                LOGGER.exception("Failed to load integrated llama-cpp model")
                self._llama_load_error = exc
                self._llama = None

    def complete_json(self, prompt: str) -> dict[str, Any]:
        if self._llama is not None:
            LOGGER.info("Generating enrichment with integrated llama-cpp model")
            response = self._llama(prompt, max_tokens=800)
            text = response["choices"][0]["text"]
            return parse_json_object(text)
        if self._llama_load_error is not None:
            LOGGER.info("Falling back to built-in integrated enrichment")
        return builtin_enrichment(prompt, self.settings.max_description_chars)

    def chat(self, messages: list[dict[str, str]]) -> str:
        if self._llama is None:
            if self._llama_load_error is not None:
                raise RuntimeError("The integrated chat model could not be loaded") from self._llama_load_error
            raise RuntimeError("The configured integrated backend does not support chat")
        LOGGER.info("Generating chat response with integrated llama-cpp model")
        response = self._llama.create_chat_completion(
            messages=messages,
            max_tokens=int(self.settings.provider_config.get("chat_max_tokens", 1000)),
            temperature=float(self.settings.provider_config.get("temperature", 0.2)),
        )
        content = response["choices"][0]["message"]["content"]
        return str(content or "").strip()

    def embed(self, text: str) -> list[float]:
        dimensions = int(self.settings.provider_config.get("embedding_dimensions", 128))
        return deterministic_embedding(text, dimensions=dimensions)


class OpenAIClient(LLMClient):
    def chat(self, messages: list[dict[str, str]]) -> str:
        LOGGER.info("Calling OpenAI chat model %s", self.chat_model)
        data = self._post(
            "/chat/completions",
            {
                "model": self.chat_model,
                "messages": messages,
                "temperature": float(self.settings.provider_config.get("temperature", 0.2)),
            },
        )
        return str(data["choices"][0]["message"]["content"] or "").strip()

    def complete_json(self, prompt: str) -> dict[str, Any]:
        LOGGER.info("Calling OpenAI chat model %s", self.chat_model)
        body = {
            "model": self.chat_model,
            "messages": [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        data = self._post("/chat/completions", body)
        return parse_json_object(data["choices"][0]["message"]["content"])

    def embed(self, text: str) -> list[float]:
        LOGGER.info("Calling OpenAI embedding model %s", self.embedding_model)
        body = {"model": self.embedding_model, "input": text}
        data = self._post("/embeddings", body)
        return [float(v) for v in data["data"][0]["embedding"]]

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        api_key = self.settings.provider_config.get("api_key")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        base_url = self.settings.provider_config.get("base_url", "https://api.openai.com/v1")
        return post_json(base_url.rstrip("/") + path, body, headers)


class GeminiClient(LLMClient):
    def chat(self, messages: list[dict[str, str]]) -> str:
        LOGGER.info("Calling Gemini chat model %s", self.chat_model)
        system_parts = [
            {"text": message["content"]}
            for message in messages
            if message.get("role") == "system"
        ]
        contents = [
            {
                "role": "model" if message.get("role") == "assistant" else "user",
                "parts": [{"text": message["content"]}],
            }
            for message in messages
            if message.get("role") in {"user", "assistant"}
        ]
        body: dict[str, Any] = {"contents": contents}
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}
        data = self._post(f"/models/{self.chat_model}:generateContent", body)
        return str(data["candidates"][0]["content"]["parts"][0]["text"] or "").strip()

    def complete_json(self, prompt: str) -> dict[str, Any]:
        LOGGER.info("Calling Gemini chat model %s", self.chat_model)
        model = self.chat_model
        data = self._post(
            f"/models/{model}:generateContent",
            {"contents": [{"parts": [{"text": prompt}]}]},
        )
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return parse_json_object(text)

    def embed(self, text: str) -> list[float]:
        LOGGER.info("Calling Gemini embedding model %s", self.embedding_model)
        model = self.embedding_model
        content_text = text
        if model == "gemini-embedding-2":
            content_text = f"title: dataset catalog entry | text: {text}"
        data = self._post(
            f"/models/{model}:embedContent",
            {"content": {"parts": [{"text": content_text}]}},
        )
        if "embedding" in data:
            return [float(v) for v in data["embedding"]["values"]]
        embeddings = data.get("embeddings") or []
        if embeddings:
            return [float(v) for v in embeddings[0]["values"]]
        raise RuntimeError(f"Gemini embedding response did not include embedding values: {data}")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        base_url = self.settings.provider_config.get(
            "base_url",
            "https://generativelanguage.googleapis.com/v1beta",
        )
        api_key = self.settings.provider_config.get("api_key", "")
        return post_json(
            base_url.rstrip("/") + path,
            body,
            {"Content-Type": "application/json", "x-goog-api-key": api_key},
        )


class OllamaClient(LLMClient):
    def chat(self, messages: list[dict[str, str]]) -> str:
        LOGGER.info("Calling Ollama chat model %s", self.chat_model)
        data = self._post(
            "/api/chat",
            {
                "model": self.chat_model,
                "stream": False,
                "messages": messages,
                "options": {
                    "temperature": float(self.settings.provider_config.get("temperature", 0.2))
                },
            },
        )
        return str(data["message"]["content"] or "").strip()

    def complete_json(self, prompt: str) -> dict[str, Any]:
        LOGGER.info("Calling Ollama chat model %s", self.chat_model)
        data = self._post(
            "/api/chat",
            {
                "model": self.chat_model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": "Return only valid JSON."},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        return parse_json_object(data["message"]["content"])

    def embed(self, text: str) -> list[float]:
        LOGGER.info("Calling Ollama embedding model %s", self.embedding_model)
        try:
            data = self._post("/api/embed", {"model": self.embedding_model, "input": text})
            embeddings = data.get("embeddings") or []
            if embeddings:
                return [float(v) for v in embeddings[0]]
        except (KeyError, urllib.error.URLError):
            pass
        data = self._post("/api/embeddings", {"model": self.embedding_model, "prompt": text})
        return [float(v) for v in data["embedding"]]

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        base_url = self.settings.provider_config.get("base_url", "http://127.0.0.1:11434")
        return post_json(base_url.rstrip("/") + path, body, {"Content-Type": "application/json"})


def post_json(url: str, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120, context=ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Provider HTTP {exc.code}: {body_text}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Provider request failed: {exc.reason}") from exc


def ssl_context() -> ssl.SSLContext:
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if match is None:
            return {}
        value = json.loads(match.group(0))
    return value if isinstance(value, dict) else {}


def trim_text(text: str, limit: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def deterministic_embedding(text: str, dimensions: int = 128) -> list[float]:
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    vector = [0.0] * dimensions
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        sign = -1.0 if digest[4] % 2 else 1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def fallback_style(geometry_types: list[str] | None = None) -> dict[str, Any]:
    return json.loads(json.dumps(DEFAULT_STYLE))


def resolve_integrated_model(config: dict[str, Any]) -> Path:
    explicit_path = config.get("model_path")
    if explicit_path:
        return Path(explicit_path)

    model_id = config.get("model_id")
    if not model_id:
        raise ValueError("integrated llama-cpp backend requires model_id or model_path")

    model_dir = Path(config.get("model_dir", "models"))
    model_file = config.get("model_file") or discover_gguf_file(model_id)
    target_dir = model_dir / safe_model_dir_name(model_id)
    target_path = target_dir / model_file
    if target_path.exists():
        LOGGER.info("Using cached integrated model %s", target_path)
        return target_path

    target_dir.mkdir(parents=True, exist_ok=True)
    download_url = config.get("download_url") or hf_resolve_url(model_id, model_file)
    LOGGER.info("Downloading integrated model %s to %s", download_url, target_path)
    download_file(download_url, target_path)
    return target_path


def safe_model_dir_name(model_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", model_id).strip("_")


def discover_gguf_file(model_id: str) -> str:
    metadata_url = f"https://huggingface.co/api/models/{model_id}"
    with urllib.request.urlopen(metadata_url, timeout=60) as response:
        metadata = json.loads(response.read().decode("utf-8"))
    files = [
        item.get("rfilename", "")
        for item in metadata.get("siblings", [])
        if item.get("rfilename", "").lower().endswith(".gguf")
    ]
    if not files:
        raise ValueError(f"No GGUF files found for model_id: {model_id}")
    preferred = sorted(
        files,
        key=lambda name: (
            "q4_k_m" not in name.lower(),
            "q4" not in name.lower(),
            len(name),
            name,
        ),
    )
    return preferred[0]


def hf_resolve_url(model_id: str, model_file: str) -> str:
    return f"https://huggingface.co/{model_id}/resolve/main/{model_file}"


def download_file(url: str, target_path: Path) -> None:
    partial_path = target_path.with_suffix(target_path.suffix + ".part")
    with urllib.request.urlopen(url, timeout=600) as response:
        with partial_path.open("wb") as file:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                file.write(chunk)
    partial_path.replace(target_path)


def builtin_enrichment(prompt: str, max_description_chars: int) -> dict[str, Any]:
    payload = extract_dataset_payload(prompt)
    if not payload:
        return {}

    name = str(payload.get("name") or "dataset").replace("_", " ").replace("-", " ")
    geometry_types = payload.get("geometry_types") or []
    feature_count = payload.get("num_features")
    description_parts = [name.title()]
    if geometry_types:
        description_parts.append("with " + ", ".join(geometry_types).lower() + " geometries")
    if feature_count:
        description_parts.append(f"containing about {int(feature_count):,} features")
    description = trim_text(" ".join(description_parts) + ".", max_description_chars)

    attributes: dict[str, str] = {}
    for field in payload.get("schema") or []:
        field_name = field.get("name")
        field_type = field.get("type") or "value"
        if not field_name or field_type == "geometry":
            continue
        readable = str(field_name).replace("_", " ").replace("-", " ")
        attributes[field_name] = trim_text(f"{readable.title()} attribute ({field_type}).", 160)

    source_style = payload.get("source_style") or {}
    translated_style = (
        esri_renderer_style(source_style.get("renderer"), geometry_types)
        if source_style.get("format") == "esri-renderer"
        else None
    )
    return {
        "description": description,
        "attributes": attributes,
        "style": translated_style or style_for_geometry_types(geometry_types),
    }


def extract_dataset_payload(prompt: str) -> dict[str, Any]:
    marker = "Dataset:\n"
    _, found, payload = prompt.partition(marker)
    if not found:
        return {}
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def style_for_geometry_types(geometry_types: list[str]) -> dict[str, Any]:
    style = fallback_style(geometry_types)
    lower_types = {value.lower() for value in geometry_types}
    if any("polygon" in value for value in lower_types):
        style["layers"]["fill"].update({"fill-color": "#4c8f6a", "fill-opacity": 0.32})
        style["layers"]["line"].update({"line-color": "#265c42"})
    elif any("line" in value for value in lower_types):
        style["layers"]["line"].update({"line-color": "#226f9f", "line-width": 2.0})
    elif any("point" in value for value in lower_types):
        style["layers"]["circle"].update({"circle-color": "#b84a62", "circle-radius": 4.5})
    return style


def esri_renderer_style(
    renderer: Any, geometry_types: list[str]
) -> dict[str, Any] | None:
    """Translate common Esri renderers into machine-readable MapLibre expressions."""
    if not isinstance(renderer, dict):
        return None
    style = style_for_geometry_types(geometry_types)
    layer_type, color_property = primary_color_property(geometry_types)
    renderer_type = str(renderer.get("type") or "").lower()
    field = renderer.get("field1") or renderer.get("field")

    if renderer_type == "uniquevalue" and field:
        entries = renderer.get("uniqueValueInfos") or []
        expression: list[Any] = ["match", ["get", field]]
        labels: dict[str, str] = {}
        for entry in entries:
            if not isinstance(entry, dict) or "value" not in entry:
                continue
            value = entry["value"]
            expression.extend([value, esri_symbol_color(entry.get("symbol"), "#808080")])
            labels[str(value)] = str(entry.get("label") or value)
        expression.append(esri_symbol_color(renderer.get("defaultSymbol"), "#bdbdbd"))
        style["layers"][layer_type][color_property] = expression
        style["metadata"] = {
            "ucrstar:legend": {
                "type": "categorical",
                "property": field,
                "labels": labels,
            }
        }
        return style

    if renderer_type == "classbreaks" and field:
        entries = renderer.get("classBreakInfos") or []
        default_color = esri_symbol_color(renderer.get("defaultSymbol"), "#d9d9d9")
        stops = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("classMaxValue") is None:
                continue
            maximum = entry["classMaxValue"]
            color = esri_symbol_color(entry.get("symbol"), default_color)
            stops.append({"value": maximum, "label": entry.get("label"), "color": color})
        expression: list[Any] = [
            "step",
            ["to-number", ["get", field]],
            stops[0]["color"] if stops else default_color,
        ]
        for index in range(1, len(stops)):
            expression.extend([stops[index - 1]["value"], stops[index]["color"]])
        style["layers"][layer_type][color_property] = expression
        style["metadata"] = {
            "ucrstar:legend": {
                "type": "gradient",
                "property": field,
                "stops": stops,
            }
        }
        return style

    if renderer_type == "simple":
        style["layers"][layer_type][color_property] = esri_symbol_color(
            renderer.get("symbol"),
            style["layers"][layer_type].get(color_property, "#808080"),
        )
        return style
    return None


def primary_color_property(geometry_types: list[str]) -> tuple[str, str]:
    values = " ".join(geometry_types).lower()
    if "polygon" in values:
        return "fill", "fill-color"
    if "line" in values:
        return "line", "line-color"
    return "circle", "circle-color"


def esri_symbol_color(symbol: Any, default: str) -> str:
    if not isinstance(symbol, dict):
        return default
    color = symbol.get("color")
    if not isinstance(color, list) or len(color) < 3:
        outline = symbol.get("outline") or {}
        color = outline.get("color")
    if not isinstance(color, list) or len(color) < 3:
        return default
    red, green, blue = (max(0, min(255, int(value))) for value in color[:3])
    if len(color) > 3 and int(color[3]) < 255:
        return f"rgba({red},{green},{blue},{max(0, min(255, int(color[3]))) / 255:.3f})"
    return f"#{red:02x}{green:02x}{blue:02x}"
