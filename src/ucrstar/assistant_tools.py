from __future__ import annotations

import json
import logging
import math
import multiprocessing
import re
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import starlet

from .assistant_style import build_assistant_style, style_attributes
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
- query_dataframe: {"where"?: [{"attribute": string, "operator":
  "eq"|"ne"|"lt"|"lte"|"gt"|"gte"|"in"|"not_in"|"contains"|
  "starts_with"|"ends_with"|"is_null"|"not_null", "value"?: scalar|array,
  "case_sensitive"?: boolean}], "combine"?: "and"|"or", "operation"?:
  "records"|"count"|"distinct"|"min"|"max"|"mean"|"sum", "select"?:
  [attribute], "attribute"?: string, "limit"?: integer}. Run a bounded GeoPandas
  dataframe query over the selected dataset inside the current viewport. Use it to answer
  small, specific questions about matching properties or values. Use find_attributes first
  when the field is uncertain. records requires select; distinct/min/max/mean/sum require
  attribute; count requires neither. This tool is read-only, runs in an isolated process
  with a hard five-second deadline, and fails instead of returning an oversized result. It
  accepts only this structured query form; never provide Python code or a pandas query
  expression.
- find_attributes: {"query": string, "dataset_id"?: string}. Search the complete schema
  of the selected dataset by field name and description. Use this before claiming that an
  attribute is absent, especially when dataset.schema_truncated is true, and when the user
  asks for the closest field to a phrase.
- apply_style: {"style": MapLibreStyleV8, "dataset_id"?: string}. Create a complete
  dataset style or modify the current style. Preserve current layers and properties that
  the user did not ask to change. The server binds the dataset source, so use source
  "dataset" and never add external sources, sprites, glyph URLs, or icon images. You may
  use expressions, filters, zoom-dependent paint/layout, heatmaps, and fill extrusions.
  Do not add symbol layers, text-field, or icon-image here. Use set_labels for text and
  set_point_icons for Unicode symbols instead. Reference only attributes present in
  dataset.schema, plus numeric "_id".
  Use MapLibre expression syntax: substring or array membership is ["in", needle,
  haystack], never an "includes" operator. Use ["geometry-type"] in filters rather than
  the legacy "$type" property. For case-insensitive text matching, combine "downcase",
  "to-string", and "in".
  For vector polygon or line datasets, include a circle layer for Point geometry because
  small features can be encoded as points at low zoom. Vector tiles may also be sampled;
  never claim that a style can reveal omitted features without zooming in.
- reset_style: {}. Restore the selected dataset's server-provided style.
- highlight_feature: {"feature_id"?: integer, "color"?: "#RRGGBB"}. Highlight a
  feature across tile boundaries using its numeric _id. Omit feature_id only when
  selected_feature_id is present in context.
- clear_highlight: {}. Remove the current feature highlight.
- set_labels: {"attribute": string, "dataset_id"?: string, "size"?: number,
  "color"?: "#RRGGBB", "background"?: "none"|"light"|"dark",
  "min_zoom"?: number, "max_zoom"?: number, "allow_overlap"?: boolean}.
  Draw the attribute as a centered label for each visible point, line, or polygon. This
  renderer does not require MapLibre glyphs. Use a real schema field (or numeric "_id").
- clear_labels: {"dataset_id"?: string}. Remove assistant-configured labels.
- set_point_icons: {"attribute"?: string, "icons"?: {"attribute value":"emoji"},
  "default_icon"?: "emoji", "dataset_id"?: string, "size"?: number,
  "min_zoom"?: number, "max_zoom"?: number, "allow_overlap"?: boolean}.
  Draw Unicode symbols on visible Point features. For category-specific icons, supply an
  exact-value mapping and normally a default_icon. To use one icon for all points, omit
  attribute and icons and provide default_icon. Use this for point datasets such as POIs
  or crimes; do not create MapLibre icon-image layers.
- clear_point_icons: {"dataset_id"?: string}. Remove assistant-configured point icons.

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


@dataclass
class GeoDataFrameQueryRunner:
    datasets_dir: Path
    timeout_seconds: float = 5.0
    max_result_bytes: int = 32_000
    max_scanned_features: int = 50_000
    batch_size: int = 1_000
    process_start_method: str = "spawn"

    def run(
        self,
        dataset: dict[str, Any],
        bounds: list[float],
        query: dict[str, Any],
    ) -> dict[str, Any]:
        dataset_name = dataset.get("name")
        if not isinstance(dataset_name, str) or not dataset_name:
            raise ValueError("The selected dataset has no queryable storage path")
        root = Path(self.datasets_dir).resolve()
        dataset_dir = (root / dataset_name).resolve()
        try:
            dataset_dir.relative_to(root)
            if dataset_dir == root:
                raise ValueError
        except ValueError as exc:
            raise ValueError("The selected dataset storage path is invalid") from exc

        timeout = min(5.0, max(0.05, float(self.timeout_seconds)))
        payload = {
            "dataset_dir": str(dataset_dir),
            "dataset_id": str(dataset.get("id") or ""),
            "dataset_name": dataset_name,
            "bounds": bounds,
            "query": query,
            "max_result_bytes": min(64_000, max(1_000, int(self.max_result_bytes))),
            "max_scanned_features": min(
                100_000, max(1, int(self.max_scanned_features))
            ),
            "batch_size": min(5_000, max(1, int(self.batch_size))),
        }
        process_context = multiprocessing.get_context(self.process_start_method)
        receiver, sender = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=_dataframe_query_worker,
            args=(sender, payload),
            name="ucrstar-dataframe-query",
            daemon=True,
        )
        try:
            process.start()
            sender.close()
            if not receiver.poll(timeout):
                _terminate_process(process)
                raise ValueError(
                    f"GeoPandas query exceeded the {timeout:g}-second time limit"
                )
            response = receiver.recv()
            process.join(0.25)
            if process.is_alive():
                _terminate_process(process)
            if not isinstance(response, dict) or response.get("status") not in {
                "complete",
                "error",
            }:
                raise ValueError("GeoPandas query worker returned an invalid response")
            if response["status"] == "error":
                raise ValueError(str(response.get("error") or "GeoPandas query failed")[:500])
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("GeoPandas query worker returned an invalid result")
            return result
        except (EOFError, OSError) as exc:
            if process.is_alive():
                _terminate_process(process)
            raise ValueError("GeoPandas query worker stopped without a result") from exc
        finally:
            receiver.close()
            try:
                sender.close()
            except OSError:
                pass
            if process.is_alive():
                _terminate_process(process)


def _terminate_process(process: Any) -> None:
    """Synchronously stop a query worker; never leave timed-out work running."""
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(0.5)
    if process.is_alive():
        process.kill()
        process.join()


def _dataframe_query_worker(sender: Any, payload: dict[str, Any]) -> None:
    try:
        result = execute_dataframe_query(payload)
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > payload["max_result_bytes"]:
            raise ValueError(
                "GeoPandas query result exceeded the response size limit"
            )
        sender.send({"status": "complete", "result": result})
    except Exception as exc:
        sender.send({"status": "error", "error": str(exc)[:500]})
    finally:
        sender.close()


def execute_dataframe_query(payload: dict[str, Any]) -> dict[str, Any]:
    import pandas as pd

    query = payload["query"]
    operation = query["operation"]
    limit = query["limit"]
    selected = query.get("select") or []
    aggregate_attribute = query.get("attribute")
    scanned = 0
    matched = 0
    rows: list[dict[str, Any]] = []
    distinct_values: dict[str, Any] = {}
    aggregate_value: Any = None
    numeric_sum = 0.0
    numeric_count = 0

    batches = starlet.query_dataset(
        payload["dataset_dir"],
        tuple(payload["bounds"]),
        batch_size=payload["batch_size"],
    )
    for frame in batches:
        scanned += len(frame)
        if scanned > payload["max_scanned_features"]:
            raise ValueError("GeoPandas query exceeded the feature scan limit")
        filtered = filter_dataframe(frame, query["where"], query["combine"], pd)
        matched += len(filtered)
        if operation == "records":
            for values in filtered[selected].itertuples(index=False, name=None):
                if len(rows) >= limit:
                    break
                rows.append(
                    {
                        column: dataframe_json_value(value)
                        for column, value in zip(selected, values)
                    }
                )
        elif operation == "distinct":
            for value in filtered[aggregate_attribute].dropna().tolist():
                clean = dataframe_json_value(value)
                key = json.dumps(clean, ensure_ascii=False, sort_keys=True, default=str)
                distinct_values.setdefault(key, clean)
                if len(distinct_values) > limit:
                    raise ValueError(
                        "GeoPandas distinct result exceeded the requested value limit"
                    )
        elif operation in {"min", "max"}:
            series = filtered[aggregate_attribute].dropna()
            if len(series):
                candidate = series.min() if operation == "min" else series.max()
                if aggregate_value is None:
                    aggregate_value = candidate
                elif operation == "min":
                    aggregate_value = min(aggregate_value, candidate)
                else:
                    aggregate_value = max(aggregate_value, candidate)
        elif operation in {"mean", "sum"}:
            numeric = pd.to_numeric(
                filtered[aggregate_attribute], errors="coerce"
            ).dropna()
            numeric_sum += float(numeric.sum())
            numeric_count += int(numeric.count())

    result: dict[str, Any] = {
        "dataset_id": payload["dataset_id"],
        "dataset_name": payload["dataset_name"],
        "scope": "current_viewport",
        "bounds": payload["bounds"],
        "operation": operation,
        "scanned_features": scanned,
        "matched_features": matched,
    }
    if operation == "records":
        result["columns"] = selected
        result["rows"] = rows
        result["truncated"] = matched > len(rows)
    elif operation == "count":
        result["value"] = matched
    elif operation == "distinct":
        result["attribute"] = aggregate_attribute
        result["values"] = list(distinct_values.values())
    elif operation in {"min", "max"}:
        result["attribute"] = aggregate_attribute
        result["value"] = dataframe_json_value(aggregate_value)
    elif operation == "sum":
        result["attribute"] = aggregate_attribute
        result["value"] = numeric_sum if numeric_count else None
    elif operation == "mean":
        result["attribute"] = aggregate_attribute
        result["value"] = numeric_sum / numeric_count if numeric_count else None
    return result


def filter_dataframe(frame: Any, conditions: list[dict[str, Any]], combine: str, pd: Any) -> Any:
    if not conditions:
        return frame
    mask = pd.Series(combine == "and", index=frame.index, dtype=bool)
    for condition in conditions:
        condition_mask = dataframe_condition_mask(frame, condition)
        mask = mask & condition_mask if combine == "and" else mask | condition_mask
    return frame.loc[mask]


def dataframe_condition_mask(frame: Any, condition: dict[str, Any]) -> Any:
    series = frame[condition["attribute"]]
    operator = condition["operator"]
    value = condition.get("value")
    case_sensitive = condition.get("case_sensitive", True)
    if operator == "is_null":
        return series.isna()
    if operator == "not_null":
        return series.notna()
    if not case_sensitive:
        series = series.astype("string").str.casefold()
        if isinstance(value, list):
            value = [str(item).casefold() for item in value]
        else:
            value = str(value).casefold()
    if operator == "eq":
        return series == value
    if operator == "ne":
        return series != value
    if operator == "lt":
        return series < value
    if operator == "lte":
        return series <= value
    if operator == "gt":
        return series > value
    if operator == "gte":
        return series >= value
    if operator == "in":
        return series.isin(value)
    if operator == "not_in":
        return ~series.isin(value)
    text = series.astype("string")
    if not case_sensitive:
        text = text.str.casefold()
    if operator == "contains":
        return text.str.contains(str(value), regex=False, na=False)
    if operator == "starts_with":
        return text.str.startswith(str(value), na=False)
    if operator == "ends_with":
        return text.str.endswith(str(value), na=False)
    raise ValueError("Unsupported dataframe query operator")


def dataframe_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        if bool(value != value):
            return None
    except (TypeError, ValueError):
        pass
    value = json_scalar(value)
    if isinstance(value, str | bool | int) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


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


def attribute_search_results(
    dataset: dict[str, Any],
    query: str,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rank fields from the complete schema and include their available statistics."""
    query_text = normalize_attribute_text(query)
    query_tokens = {token for token in query_text.split() if len(token) >= 3}
    summary_by_name = {
        str(item.get("name")): item
        for item in ((dataset.get("summary_json") or {}).get("attributes") or [])
        if isinstance(item, dict) and item.get("name")
    }
    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for index, field in enumerate(dataset.get("schema") or []):
        if not isinstance(field, dict) or not field.get("name"):
            continue
        name_text = normalize_attribute_text(str(field["name"]))
        description_text = normalize_attribute_text(str(field.get("description") or ""))
        searchable = f"{name_text} {description_text}".strip()
        field_tokens = set(searchable.split())
        overlap = len(query_tokens & field_tokens)
        score = float(overlap * 12)
        if name_text == query_text:
            score += 200
        elif query_text and query_text in name_text:
            score += 120
        elif len(name_text.replace(" ", "")) >= 4 and name_text in query_text:
            score += 90
        if query_tokens and query_tokens <= set(name_text.split()):
            score += 80
        if overlap == 0 and query_text not in name_text and name_text not in query_text:
            continue

        detail = compact_attribute(field, summary_by_name.get(str(field["name"])))
        ranked.append((score, -index, detail))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [detail for _, _, detail in ranked[: max(1, min(limit, 50))]]


def normalize_attribute_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


def compact_attribute(
    field: dict[str, Any],
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        key: field[key]
        for key in ("name", "type", "role", "description", "min", "max", "top_k")
        if key in field
    }
    summary = summary or {}
    for key in ("min", "max", "approx_distinct", "non_null_count", "null_count"):
        if key not in result and key in summary:
            result[key] = summary[key]
    if "top_k" not in result and isinstance(summary.get("top_k"), list):
        result["top_k"] = summary["top_k"][:20]
    elif isinstance(result.get("top_k"), list):
        result["top_k"] = result["top_k"][:20]
    return result


@dataclass
class AssistantTools:
    catalog: DatasetCatalog
    geocoder: Any
    viewport_summarizer: ViewportSummarizer
    dataframe_query_runner: Any
    search_limit: int = 10
    semantic_max_distance: float = 0.8
    style_max_chars: int = 40_000
    style_max_layers: int = 40
    style_max_nodes: int = 5_000

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
            elif name == "query_dataframe":
                result, actions = self.query_dataframe(arguments, context)
            elif name == "find_attributes":
                result, actions = self.find_attributes(arguments, context)
            elif name == "apply_style":
                result, actions = self.apply_style(arguments, context)
            elif name == "reset_style":
                result, actions = self.reset_style(context)
            elif name == "highlight_feature":
                result, actions = self.highlight_feature(arguments, context)
            elif name == "clear_highlight":
                result, actions = self.clear_highlight(context)
            elif name == "set_labels":
                result, actions = self.set_labels(arguments, context)
            elif name == "clear_labels":
                result, actions = self.clear_labels(arguments, context)
            elif name == "set_point_icons":
                result, actions = self.set_point_icons(arguments, context)
            elif name == "clear_point_icons":
                result, actions = self.clear_point_icons(arguments, context)
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

    def query_dataframe(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        viewport = context.get("viewport") or {}
        bounds = viewport.get("bounds") if isinstance(viewport, dict) else None
        if not valid_bounds(bounds):
            raise ValueError("The current viewport is unavailable")
        query = normalize_dataframe_query(arguments, dataset)
        result = self.dataframe_query_runner.run(
            dataset,
            [float(value) for value in bounds],
            query,
        )
        return result, []

    def find_attributes(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        query = required_text(arguments, "query", max_chars=500)
        dataset = self.context_dataset(arguments, context)
        matches = attribute_search_results(dataset, query, limit=20)
        return (
            {
                "dataset_id": dataset["id"],
                "dataset_name": dataset["name"],
                "query": query,
                "schema_field_count": len(dataset.get("schema") or []),
                "matches": matches,
            },
            [],
        )

    def apply_style(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        style = build_assistant_style(
            arguments.get("style"),
            dataset,
            max_chars=self.style_max_chars,
            max_layers=self.style_max_layers,
            max_nodes=self.style_max_nodes,
        )
        attributes = sorted(style_attributes(style))
        result = {
            "dataset_id": dataset["id"],
            "style_name": style.get("name"),
            "layer_count": len(style["layers"]),
            "attributes": attributes,
            "point_fallback": bool(
                ((style.get("metadata") or {}).get("ucrstar:assistant") or {}).get(
                    "point_fallback"
                )
            ),
            "sampling_note": (
                ((style.get("metadata") or {}).get("ucrstar:assistant") or {}).get(
                    "sampling_note"
                )
            ),
        }
        return result, [
            {
                "type": "apply_style",
                "dataset_id": dataset["id"],
                "style": style,
            }
        ]

    def reset_style(
        self,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset({}, context)
        result = {"dataset_id": dataset["id"]}
        return result, [{"type": "reset_style", **result}]

    def highlight_feature(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        if (dataset.get("visualization") or {}).get("type") != "VectorTile":
            raise ValueError("_id highlighting is available only for vector-tile datasets")
        feature_id = arguments.get("feature_id", context.get("selected_feature_id"))
        if (
            not isinstance(feature_id, int)
            or isinstance(feature_id, bool)
            or not -(2**53 - 1) <= feature_id <= 2**53 - 1
        ):
            raise ValueError("feature_id must be a JavaScript-safe integer")
        color = arguments.get("color", "#ffd54f")
        if not isinstance(color, str) or not is_hex_color(color):
            raise ValueError("highlight color must be a hexadecimal color")
        result = {
            "dataset_id": dataset["id"],
            "feature_id": feature_id,
            "color": color,
        }
        return result, [{"type": "highlight_feature", **result}]

    def clear_highlight(
        self,
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset({}, context)
        result = {"dataset_id": dataset["id"]}
        return result, [{"type": "clear_highlight", **result}]

    def set_labels(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        attribute = required_text(arguments, "attribute", max_chars=256)
        require_dataset_attribute(dataset, attribute)
        action = label_action(arguments, dataset["id"], attribute)
        return dict(action), [action]

    def clear_labels(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        result = {"dataset_id": dataset["id"]}
        return result, [{"type": "clear_labels", **result}]

    def set_point_icons(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        geometry_types = " ".join(
            str(value).lower() for value in (dataset.get("geometry_types") or [])
        )
        if "point" not in geometry_types:
            raise ValueError("Unicode icons are available only for point datasets")

        attribute = arguments.get("attribute")
        if attribute is not None:
            if not isinstance(attribute, str) or not attribute.strip():
                raise ValueError("attribute must be a non-empty string")
            attribute = attribute.strip()
            require_dataset_attribute(dataset, attribute)
        action = point_icon_action(arguments, dataset["id"], attribute)
        return dict(action), [action]

    def clear_point_icons(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        dataset = self.context_dataset(arguments, context)
        result = {"dataset_id": dataset["id"]}
        return result, [{"type": "clear_point_icons", **result}]

    def context_dataset(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        dataset_id = arguments.get("dataset_id", context.get("dataset_id"))
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("No dataset is selected")
        dataset = self.catalog.get(dataset_id)
        if dataset is None or dataset.get("dataset_state") != "published":
            raise ValueError("The selected dataset is not available")
        return dataset


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


DATAFRAME_QUERY_OPERATORS = {
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "ends_with",
    "is_null",
    "not_null",
}
DATAFRAME_QUERY_OPERATIONS = {"records", "count", "distinct", "min", "max", "mean", "sum"}


def normalize_dataframe_query(
    arguments: dict[str, Any],
    dataset: dict[str, Any],
) -> dict[str, Any]:
    unknown = set(arguments) - {
        "dataset_id",
        "where",
        "combine",
        "operation",
        "select",
        "attribute",
        "limit",
    }
    if unknown:
        raise ValueError(
            "Unsupported dataframe query options: " + ", ".join(sorted(unknown))
        )
    queryable_attributes = {
        str(field.get("name"))
        for field in (dataset.get("schema") or [])
        if isinstance(field, dict)
        and field.get("name")
        and str(field.get("role") or "").lower() != "geometry"
        and str(field.get("name")).lower() != "geometry"
    }
    queryable_attributes.add("_id")

    where = arguments.get("where", [])
    if not isinstance(where, list) or len(where) > 10:
        raise ValueError("where must be an array with at most 10 conditions")
    normalized_where = [
        normalize_dataframe_condition(condition, queryable_attributes)
        for condition in where
    ]
    combine = arguments.get("combine", "and")
    if combine not in {"and", "or"}:
        raise ValueError("combine must be 'and' or 'or'")
    operation = arguments.get("operation", "records")
    if operation not in DATAFRAME_QUERY_OPERATIONS:
        raise ValueError("Unsupported dataframe query operation")
    limit = arguments.get("limit", 10)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= (50 if operation == "distinct" else 20)
    ):
        raise ValueError("limit is outside the allowed range")

    result: dict[str, Any] = {
        "where": normalized_where,
        "combine": combine,
        "operation": operation,
        "limit": limit,
    }
    select = arguments.get("select")
    if operation == "records":
        if (
            not isinstance(select, list)
            or not select
            or len(select) > 10
            or any(not isinstance(name, str) or not name for name in select)
        ):
            raise ValueError("records queries require 1 to 10 selected attributes")
        normalized_select = list(dict.fromkeys(select))
        for attribute in normalized_select:
            require_queryable_attribute(attribute, queryable_attributes)
        result["select"] = normalized_select
    elif select is not None:
        raise ValueError("select is supported only for records queries")

    attribute = arguments.get("attribute")
    if operation in {"distinct", "min", "max", "mean", "sum"}:
        if not isinstance(attribute, str) or not attribute:
            raise ValueError(f"{operation} queries require attribute")
        require_queryable_attribute(attribute, queryable_attributes)
        result["attribute"] = attribute
    elif attribute is not None:
        raise ValueError("attribute is not used by this dataframe query operation")
    return result


def normalize_dataframe_condition(
    condition: Any,
    queryable_attributes: set[str],
) -> dict[str, Any]:
    if not isinstance(condition, dict):
        raise ValueError("Each dataframe query condition must be an object")
    unknown = set(condition) - {"attribute", "operator", "value", "case_sensitive"}
    if unknown:
        raise ValueError(
            "Unsupported dataframe condition options: " + ", ".join(sorted(unknown))
        )
    attribute = condition.get("attribute")
    if not isinstance(attribute, str) or not attribute:
        raise ValueError("Each dataframe query condition requires an attribute")
    require_queryable_attribute(attribute, queryable_attributes)
    operator = condition.get("operator")
    if operator not in DATAFRAME_QUERY_OPERATORS:
        raise ValueError("Unsupported dataframe query operator")
    case_sensitive = condition.get("case_sensitive", True)
    if not isinstance(case_sensitive, bool):
        raise ValueError("case_sensitive must be a boolean")
    normalized = {
        "attribute": attribute,
        "operator": operator,
        "case_sensitive": case_sensitive,
    }
    if operator in {"is_null", "not_null"}:
        if "value" in condition:
            raise ValueError(f"{operator} does not accept a value")
        return normalized
    if "value" not in condition:
        raise ValueError(f"{operator} requires a value")
    value = condition["value"]
    if operator in {"in", "not_in"}:
        if not isinstance(value, list) or not value or len(value) > 100:
            raise ValueError(f"{operator} requires an array of 1 to 100 scalar values")
        normalized["value"] = [dataframe_query_scalar(item) for item in value]
    else:
        normalized["value"] = dataframe_query_scalar(value)
    if operator in {"contains", "starts_with", "ends_with"} and not isinstance(
        normalized["value"], str
    ):
        raise ValueError(f"{operator} requires a string value")
    return normalized


def dataframe_query_scalar(value: Any) -> str | bool | int | float | None:
    if value is None or isinstance(value, str | bool | int):
        if isinstance(value, str) and len(value) > 1_000:
            raise ValueError("Dataframe query string values are limited to 1000 characters")
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("Dataframe query values must be finite JSON scalars")


def require_queryable_attribute(attribute: str, queryable_attributes: set[str]) -> None:
    if attribute not in queryable_attributes:
        raise ValueError(f'Attribute "{attribute}" is not present in the dataset schema')


def require_dataset_attribute(dataset: dict[str, Any], attribute: str) -> None:
    if attribute == "_id":
        return
    names = {
        str(field.get("name"))
        for field in (dataset.get("schema") or [])
        if isinstance(field, dict) and field.get("name")
    }
    if attribute not in names:
        raise ValueError(f'Attribute "{attribute}" is not present in the dataset schema')


def bounded_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return float(value)


def zoom_range(arguments: dict[str, Any]) -> tuple[float, float]:
    min_zoom = bounded_number(
        arguments.get("min_zoom", 0), "min_zoom", minimum=0, maximum=24
    )
    max_zoom = bounded_number(
        arguments.get("max_zoom", 24), "max_zoom", minimum=0, maximum=24
    )
    if min_zoom >= max_zoom:
        raise ValueError("min_zoom must be less than max_zoom")
    return min_zoom, max_zoom


def boolean_option(arguments: dict[str, Any], name: str, default: bool) -> bool:
    value = arguments.get(name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def label_action(
    arguments: dict[str, Any],
    dataset_id: str,
    attribute: str,
) -> dict[str, Any]:
    size = bounded_number(arguments.get("size", 14), "size", minimum=8, maximum=64)
    color = arguments.get("color", "#202124")
    if not isinstance(color, str) or not is_hex_color(color):
        raise ValueError("color must be a hexadecimal color")
    background = arguments.get("background", "light")
    if background not in {"none", "light", "dark"}:
        raise ValueError("background must be none, light, or dark")
    min_zoom, max_zoom = zoom_range(arguments)
    return {
        "type": "set_labels",
        "dataset_id": dataset_id,
        "attribute": attribute,
        "size": size,
        "color": color,
        "background": background,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "allow_overlap": boolean_option(arguments, "allow_overlap", False),
        "placement": "center",
    }


def unicode_symbol(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty Unicode symbol")
    value = value.strip()
    if len(value) > 16 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must be at most 16 characters without control characters")
    return value


def point_icon_action(
    arguments: dict[str, Any],
    dataset_id: str,
    attribute: str | None,
) -> dict[str, Any]:
    icons = arguments.get("icons", {})
    if not isinstance(icons, dict) or len(icons) > 100:
        raise ValueError("icons must be an object with at most 100 entries")
    normalized_icons: dict[str, str] = {}
    for raw_value, raw_icon in icons.items():
        if not isinstance(raw_value, str) or not raw_value or len(raw_value) > 256:
            raise ValueError("icon mapping keys must be non-empty strings of at most 256 characters")
        normalized_icons[raw_value] = unicode_symbol(
            raw_icon, f'icon for "{raw_value}"'
        ) or ""
    default_icon = unicode_symbol(arguments.get("default_icon"), "default_icon")
    if attribute is None and normalized_icons:
        raise ValueError("attribute is required when icons contains category mappings")
    if not normalized_icons and default_icon is None:
        raise ValueError("icons or default_icon is required")
    min_zoom, max_zoom = zoom_range(arguments)
    return {
        "type": "set_point_icons",
        "dataset_id": dataset_id,
        "attribute": attribute,
        "icons": normalized_icons,
        "default_icon": default_icon,
        "size": bounded_number(
            arguments.get("size", 24), "size", minimum=8, maximum=64
        ),
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "allow_overlap": boolean_option(arguments, "allow_overlap", False),
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
    if action_type == "apply_style":
        dataset_id = action.get("dataset_id")
        style = action.get("style")
        if (
            not isinstance(dataset_id, str)
            or not dataset_id
            or not isinstance(style, dict)
            or style.get("version") != 8
            or not isinstance(style.get("layers"), list)
        ):
            raise ValueError("Invalid apply_style action")
        return {"type": action_type, "dataset_id": dataset_id, "style": style}
    if action_type == "reset_style":
        dataset_id = action.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("Invalid reset_style action")
        return {"type": action_type, "dataset_id": dataset_id}
    if action_type == "highlight_feature":
        dataset_id = action.get("dataset_id")
        feature_id = action.get("feature_id")
        color = action.get("color")
        if (
            not isinstance(dataset_id, str)
            or not dataset_id
            or not isinstance(feature_id, int)
            or isinstance(feature_id, bool)
            or not -(2**53 - 1) <= feature_id <= 2**53 - 1
            or not isinstance(color, str)
            or not is_hex_color(color)
        ):
            raise ValueError("Invalid highlight_feature action")
        return {
            "type": action_type,
            "dataset_id": dataset_id,
            "feature_id": feature_id,
            "color": color,
        }
    if action_type == "clear_highlight":
        dataset_id = action.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("Invalid clear_highlight action")
        return {"type": action_type, "dataset_id": dataset_id}
    if action_type == "set_labels":
        dataset_id = action.get("dataset_id")
        attribute = action.get("attribute")
        if (
            not isinstance(dataset_id, str)
            or not dataset_id
            or not isinstance(attribute, str)
            or not attribute
        ):
            raise ValueError("Invalid set_labels action")
        return label_action(action, dataset_id, attribute)
    if action_type == "clear_labels":
        dataset_id = action.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("Invalid clear_labels action")
        return {"type": action_type, "dataset_id": dataset_id}
    if action_type == "set_point_icons":
        dataset_id = action.get("dataset_id")
        attribute = action.get("attribute")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("Invalid set_point_icons action")
        if attribute is not None and (not isinstance(attribute, str) or not attribute):
            raise ValueError("Invalid set_point_icons attribute")
        return point_icon_action(action, dataset_id, attribute)
    if action_type == "clear_point_icons":
        dataset_id = action.get("dataset_id")
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("Invalid clear_point_icons action")
        return {"type": action_type, "dataset_id": dataset_id}
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


def is_hex_color(value: str) -> bool:
    if not value.startswith("#") or len(value) not in {4, 7}:
        return False
    try:
        int(value[1:], 16)
    except ValueError:
        return False
    return True
