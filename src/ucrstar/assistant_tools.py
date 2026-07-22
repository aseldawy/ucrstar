from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import starlet

from .catalog import DatasetCatalog
from .llm import ssl_context


LOGGER = logging.getLogger(__name__)


TOOL_DESCRIPTIONS = """
Available tools (request them only through tool_calls):
- search_datasets: {"query": string, "auto_select"?: boolean}. Search the published
  catalog. Use this for dataset discovery or when the user names a dataset that is not
  already selected. The server decides whether a match is confident enough to select.
- select_dataset: {"dataset_id": string}. Select a known dataset ID supplied in context
  or earlier conversation. Never invent an ID.
- geocode_region: {"query": string}. Resolve a named place and move the map to its
  verified bounds. Never provide coordinates yourself.
- change_basemap: {"basemap": "street"|"satellite"}.
- summarize_viewport: {}. Query and summarize the selected dataset inside the current
  viewport. Use this before describing visible features, values, patterns, or anomalies.

Return only JSON with this shape:
{"message":"brief response","tool_calls":[{"name":"tool","arguments":{}}]}.
Use an empty tool_calls array for ordinary conversation. Do not return UI actions directly.
""".strip()


@dataclass(frozen=True)
class ToolOutcome:
    trace: dict[str, Any]
    actions: list[dict[str, Any]]


@dataclass
class NominatimGeocoder:
    base_url: str = "https://nominatim.openstreetmap.org/search"
    user_agent: str = "ucrstar/0.1 (geospatial dataset assistant)"
    timeout: float = 10.0
    _cache: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        cache_key = f"{query.strip().lower()}:{limit}"
        if cache_key in self._cache:
            return self._cache[cache_key]
        url = self.base_url + "?" + urllib.parse.urlencode(
            {"format": "jsonv2", "limit": max(1, min(limit, 10)), "q": query}
        )
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
        )
        with urllib.request.urlopen(
            request,
            timeout=self.timeout,
            context=ssl_context(),
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        results = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            bounds = nominatim_bounds(item)
            if bounds is None:
                continue
            results.append(
                {
                    "label": str(item.get("display_name") or query),
                    "bounds": bounds,
                    "center": [float(item["lon"]), float(item["lat"])],
                }
            )
        self._cache[cache_key] = results
        return results


def nominatim_bounds(item: dict[str, Any]) -> list[float] | None:
    raw = item.get("boundingbox")
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    try:
        south, north, west, east = (float(value) for value in raw)
    except (TypeError, ValueError):
        return None
    bounds = [west, south, east, north]
    return bounds if valid_bounds(bounds) else None


@dataclass
class ViewportSummarizer:
    catalog: DatasetCatalog
    max_features: int = 5_000
    sample_size: int = 5
    max_attributes: int = 20
    top_values: int = 5

    def summarize(self, dataset_ref: str, bounds: list[float]) -> dict[str, Any]:
        dataset = self.catalog.get(dataset_ref)
        if dataset is None or dataset.get("dataset_state") != "published":
            raise ValueError("The selected dataset is not available")
        if not valid_bounds(bounds):
            raise ValueError("The current viewport bounds are invalid")

        dataset_dir = Path(self.catalog.datasets_dir) / dataset["name"]
        batches = starlet.query_dataset(
            dataset_dir,
            tuple(bounds),
            batch_size=min(1_000, self.max_features + 1),
        )
        schema_names = [
            field.get("name")
            for field in (dataset.get("schema") or [])
            if isinstance(field, dict)
            and field.get("name")
            and str(field.get("type") or "").lower() != "geometry"
            and field.get("name") != "geometry"
        ][: self.max_attributes]
        samples: list[dict[str, Any]] = []
        statistics: dict[str, dict[str, Any]] = {}
        scanned = 0
        truncated = False

        for batch in batches:
            records = batch.to_dict(orient="records")
            for record in records:
                if scanned >= self.max_features:
                    truncated = True
                    break
                clean = clean_record(record)
                if not schema_names:
                    schema_names = list(clean)[: self.max_attributes]
                clean = {name: clean.get(name) for name in schema_names if name in clean}
                if len(samples) < self.sample_size:
                    samples.append(clean)
                update_statistics(statistics, clean)
                scanned += 1
            if truncated:
                break

        return {
            "dataset_id": dataset["id"],
            "dataset_name": dataset["name"],
            "bounds": [float(value) for value in bounds],
            "feature_count": None if truncated else scanned,
            "scanned_features": scanned,
            "truncated": truncated,
            "sample_records": samples,
            "attributes": finalize_statistics(
                statistics,
                top_values=self.top_values,
            ),
        }


def clean_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): json_scalar(value)
        for key, value in record.items()
        if key != "geometry" and is_json_scalar(value)
    }


def is_json_scalar(value: Any) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if hasattr(value, "item"):
        return is_json_scalar(value.item())
    if hasattr(value, "isoformat"):
        return True
    return False


def json_scalar(value: Any) -> Any:
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def update_statistics(statistics: dict[str, dict[str, Any]], record: dict[str, Any]) -> None:
    for name, value in record.items():
        stats = statistics.setdefault(
            name,
            {
                "non_null": 0,
                "null": 0,
                "numeric_count": 0,
                "minimum": None,
                "maximum": None,
                "sum": 0.0,
                "values": Counter(),
            },
        )
        if value is None:
            stats["null"] += 1
            continue
        stats["non_null"] += 1
        if isinstance(value, int | float) and not isinstance(value, bool):
            numeric = float(value)
            stats["numeric_count"] += 1
            stats["minimum"] = numeric if stats["minimum"] is None else min(stats["minimum"], numeric)
            stats["maximum"] = numeric if stats["maximum"] is None else max(stats["maximum"], numeric)
            stats["sum"] += numeric
        if len(stats["values"]) < 100 or value in stats["values"]:
            stats["values"][value] += 1


def finalize_statistics(
    statistics: dict[str, dict[str, Any]],
    *,
    top_values: int,
) -> list[dict[str, Any]]:
    result = []
    for name, stats in statistics.items():
        entry: dict[str, Any] = {
            "name": name,
            "non_null_count": stats["non_null"],
            "null_count": stats["null"],
        }
        if stats["non_null"] and stats["numeric_count"] == stats["non_null"]:
            entry.update(
                {
                    "type": "numeric",
                    "min": stats["minimum"],
                    "max": stats["maximum"],
                    "mean": stats["sum"] / stats["numeric_count"],
                }
            )
        else:
            entry.update(
                {
                    "type": "categorical",
                    "top_values": [
                        {"value": value, "count": count}
                        for value, count in stats["values"].most_common(top_values)
                    ],
                }
            )
        result.append(entry)
    return result


@dataclass
class AssistantTools:
    catalog: DatasetCatalog
    geocoder: Any
    viewport_summarizer: ViewportSummarizer
    search_limit: int = 10
    semantic_max_distance: float = 0.8

    def execute(
        self,
        call: dict[str, Any],
        context: dict[str, Any],
        *,
        llm: Any,
        semantic_search: bool,
    ) -> ToolOutcome:
        name = call.get("name")
        arguments = call.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return tool_error(str(name or "unknown"), {}, "Invalid tool call")
        try:
            if name == "search_datasets":
                result, actions = self.search_datasets(
                    arguments,
                    llm=llm,
                    semantic_search=semantic_search,
                )
            elif name == "select_dataset":
                result, actions = self.select_dataset(arguments)
            elif name == "geocode_region":
                result, actions = self.geocode_region(arguments)
            elif name == "change_basemap":
                result, actions = self.change_basemap(arguments)
            elif name == "summarize_viewport":
                result, actions = self.summarize_viewport(context)
            else:
                return tool_error(name, arguments, "Unsupported tool")
            validated_actions = [validate_action(action) for action in actions]
            return ToolOutcome(
                trace={
                    "name": name,
                    "arguments": arguments,
                    "status": "complete",
                    "result": result,
                },
                actions=validated_actions,
            )
        except ValueError as exc:
            return tool_error(name, arguments, str(exc))
        except Exception:
            LOGGER.exception("Assistant tool '%s' failed", name)
            return tool_error(name, arguments, "Tool execution failed")

    def search_datasets(
        self,
        arguments: dict[str, Any],
        *,
        llm: Any,
        semantic_search: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        query = required_text(arguments, "query", max_chars=500)
        auto_select = arguments.get("auto_select", True)
        if not isinstance(auto_select, bool):
            raise ValueError("auto_select must be a boolean")

        self.catalog.sync()
        lexical = self.catalog.list({"q": query, "state": "published"})
        semantic: list[dict[str, Any]] = []
        if semantic_search:
            try:
                semantic = self.catalog.semantic_search(
                    query,
                    llm,
                    {"state": "published"},
                    limit=self.search_limit,
                )
            except Exception:
                semantic = []
        ranked = hybrid_rank(
            query,
            lexical,
            semantic,
            semantic_max_distance=self.semantic_max_distance,
        )[: self.search_limit]
        datasets = [public_dataset_result(item["dataset"]) for item in ranked]

        selected = None
        if auto_select and ranked:
            exact = normalize_search_text(ranked[0]["dataset"].get("name")) == normalize_search_text(query)
            only_confident = len(ranked) == 1 and ranked[0]["score"] >= 0.9
            if exact or only_confident:
                selected = ranked[0]["dataset"]

        if selected is not None:
            actions = [{"type": "select_dataset", "dataset_id": selected["id"]}]
        else:
            actions = [{"type": "show_datasets", "query": query, "datasets": datasets}]
        return (
            {
                "query": query,
                "match_count": len(datasets),
                "selected_dataset_id": selected.get("id") if selected else None,
                "datasets": datasets,
            },
            actions,
        )

    def select_dataset(
        self,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset_id = required_text(arguments, "dataset_id", max_chars=256)
        dataset = self.catalog.get(dataset_id)
        if dataset is None or dataset.get("dataset_state") != "published":
            raise ValueError("Dataset not found")
        public = public_dataset_result(dataset)
        return public, [{"type": "select_dataset", "dataset_id": dataset["id"]}]

    def geocode_region(
        self,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        query = required_text(arguments, "query", max_chars=300)
        results = self.geocoder.search(query, limit=5)
        if not results:
            raise ValueError("Region not found")
        match = results[0]
        bounds = match.get("bounds")
        if not valid_bounds(bounds):
            raise ValueError("Geocoder returned invalid bounds")
        result = {
            "query": query,
            "label": str(match.get("label") or query),
            "bounds": [float(value) for value in bounds],
        }
        return result, [{"type": "fit_bounds", **result}]

    def change_basemap(
        self,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        basemap = arguments.get("basemap")
        if basemap not in {"street", "satellite"}:
            raise ValueError("Basemap must be street or satellite")
        result = {"basemap": basemap}
        return result, [{"type": "change_basemap", "basemap": basemap}]

    def summarize_viewport(
        self,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset_id = context.get("dataset_id")
        viewport = context.get("viewport") or {}
        bounds = viewport.get("bounds") if isinstance(viewport, dict) else None
        if not isinstance(dataset_id, str):
            raise ValueError("No dataset is selected")
        if not valid_bounds(bounds):
            raise ValueError("The current viewport is unavailable")
        return self.viewport_summarizer.summarize(dataset_id, bounds), []


def tool_error(name: str, arguments: dict[str, Any], message: str) -> ToolOutcome:
    return ToolOutcome(
        trace={
            "name": name,
            "arguments": arguments,
            "status": "error",
            "error": message[:500],
        },
        actions=[],
    )


def hybrid_rank(
    query: str,
    lexical: list[dict[str, Any]],
    semantic: list[dict[str, Any]],
    *,
    semantic_max_distance: float,
) -> list[dict[str, Any]]:
    ranked: dict[str, dict[str, Any]] = {}
    for index, dataset in enumerate(lexical):
        score = lexical_score(query, dataset)
        ranked[dataset["id"]] = {
            "dataset": dataset,
            "score": score,
            "lexical_rank": index,
        }
    for index, dataset in enumerate(semantic):
        distance = float(dataset.get("search_score", 1.0))
        if not math.isfinite(distance) or distance > semantic_max_distance:
            continue
        similarity = max(0.0, 1.0 - (distance / 2.0))
        entry = ranked.setdefault(
            dataset["id"],
            {"dataset": dataset, "score": 0.0},
        )
        entry["semantic_rank"] = index
        entry["semantic_distance"] = distance
        entry["score"] = max(entry["score"], similarity)
        if "lexical_rank" in entry:
            entry["score"] = min(1.0, entry["score"] + 0.1)
    return sorted(
        ranked.values(),
        key=lambda item: (-item["score"], normalize_search_text(item["dataset"].get("name"))),
    )


def lexical_score(query: str, dataset: dict[str, Any]) -> float:
    normalized_query = normalize_search_text(query)
    normalized_name = normalize_search_text(dataset.get("name"))
    if normalized_query == normalized_name:
        return 1.0
    if normalized_query and normalized_query in normalized_name:
        return 0.95
    terms = {term for term in normalized_query.split() if term}
    text = normalize_search_text(
        f"{dataset.get('name') or ''} {dataset.get('description') or ''}"
    )
    if not terms:
        return 0.0
    matched = sum(1 for term in terms if term in text)
    return 0.5 + (0.4 * matched / len(terms))


def normalize_search_text(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").replace("-", " ").split())


def public_dataset_result(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": dataset.get("id"),
        "name": dataset.get("name"),
        "description": dataset.get("description"),
        "geometry_types": dataset.get("geometry_types") or [],
        "num_features": dataset.get("num_features"),
        "size_bytes": dataset.get("size_bytes", 0),
        "mbr": dataset.get("mbr"),
        "dataset_state": dataset.get("dataset_state"),
    }


def validate_action(action: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise ValueError("Action must be an object")
    action_type = action.get("type")
    if action_type == "show_datasets":
        query = action.get("query")
        datasets = action.get("datasets")
        if not isinstance(query, str) or not isinstance(datasets, list) or len(datasets) > 20:
            raise ValueError("Invalid show_datasets action")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in datasets):
            raise ValueError("Invalid dataset search result")
        return {"type": action_type, "query": query[:500], "datasets": datasets}
    if action_type == "select_dataset":
        dataset_id = action.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("Invalid select_dataset action")
        return {"type": action_type, "dataset_id": dataset_id}
    if action_type == "fit_bounds":
        if not valid_bounds(action.get("bounds")):
            raise ValueError("Invalid fit_bounds action")
        return {
            "type": action_type,
            "query": str(action.get("query") or "")[:300],
            "label": str(action.get("label") or "")[:500],
            "bounds": [float(value) for value in action["bounds"]],
        }
    if action_type == "change_basemap":
        if action.get("basemap") not in {"street", "satellite"}:
            raise ValueError("Invalid change_basemap action")
        return {"type": action_type, "basemap": action["basemap"]}
    raise ValueError("Unsupported action")


def valid_bounds(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 4
        and all(
            isinstance(item, int | float)
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            for item in value
        )
    )


def required_text(arguments: dict[str, Any], key: str, *, max_chars: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    value = value.strip()
    if len(value) > max_chars:
        raise ValueError(f"{key} must be at most {max_chars} characters")
    return value
