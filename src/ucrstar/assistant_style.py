from __future__ import annotations

import copy
import json
import math
from typing import Any

from .catalog import (
    DATASET_STYLE_LAYER_TYPES,
    STYLE_SOURCE_ID,
    VECTOR_SOURCE_LAYER,
    normalize_style,
)


RESERVED_LAYER_IDS = {"basemap-street", "basemap-satellite"}

PAINT_PROPERTIES = {
    "fill": {
        "fill-antialias", "fill-opacity", "fill-color", "fill-outline-color",
        "fill-translate", "fill-translate-anchor", "fill-pattern",
    },
    "line": {
        "line-opacity", "line-color", "line-translate", "line-translate-anchor",
        "line-width", "line-gap-width", "line-offset", "line-blur",
        "line-dasharray", "line-pattern", "line-gradient", "line-trim-offset",
    },
    "circle": {
        "circle-radius", "circle-color", "circle-blur", "circle-opacity",
        "circle-translate", "circle-translate-anchor", "circle-pitch-scale",
        "circle-pitch-alignment", "circle-stroke-width", "circle-stroke-color",
        "circle-stroke-opacity",
    },
    "symbol": {
        "icon-opacity", "icon-color", "icon-halo-color", "icon-halo-width",
        "icon-halo-blur", "icon-translate", "icon-translate-anchor",
        "text-opacity", "text-color", "text-halo-color", "text-halo-width",
        "text-halo-blur", "text-translate", "text-translate-anchor",
    },
    "heatmap": {
        "heatmap-radius", "heatmap-weight", "heatmap-intensity", "heatmap-color",
        "heatmap-opacity",
    },
    "fill-extrusion": {
        "fill-extrusion-opacity", "fill-extrusion-color", "fill-extrusion-translate",
        "fill-extrusion-translate-anchor", "fill-extrusion-pattern",
        "fill-extrusion-height", "fill-extrusion-base",
        "fill-extrusion-vertical-gradient",
    },
}

LAYOUT_PROPERTIES = {
    "fill": {"visibility", "fill-sort-key"},
    "line": {
        "visibility", "line-cap", "line-join", "line-miter-limit",
        "line-round-limit", "line-sort-key",
    },
    "circle": {"visibility", "circle-sort-key"},
    "heatmap": {"visibility"},
    "fill-extrusion": {"visibility"},
    "symbol": {
        "visibility", "symbol-placement", "symbol-spacing", "symbol-avoid-edges",
        "symbol-sort-key", "symbol-z-order", "symbol-z-elevate", "icon-allow-overlap",
        "icon-ignore-placement", "icon-optional", "icon-rotation-alignment", "icon-size",
        "icon-text-fit", "icon-text-fit-padding", "icon-image", "icon-rotate",
        "icon-padding", "icon-keep-upright", "icon-offset", "icon-anchor",
        "icon-pitch-alignment", "text-pitch-alignment", "text-rotation-alignment",
        "text-field", "text-font", "text-size", "text-max-width", "text-line-height",
        "text-letter-spacing", "text-justify", "text-radial-offset", "text-variable-anchor",
        "text-anchor", "text-max-angle", "text-writing-mode", "text-rotate",
        "text-padding", "text-keep-upright", "text-transform", "text-offset",
        "text-allow-overlap", "text-ignore-placement", "text-optional",
    },
}

LITERAL_ARRAY_PROPERTIES = {
    "fill-translate", "line-translate", "line-dasharray", "circle-translate",
    "icon-translate", "text-translate", "icon-text-fit-padding", "icon-offset",
    "text-offset", "text-variable-anchor", "text-writing-mode",
}

# MapLibre GL JS 3.x expression operators accepted in generated styles. Signatures are
# argument counts after the operator; None means unbounded.
EXPRESSION_SIGNATURES: dict[str, tuple[int, int | None]] = {
    "literal": (1, 1), "get": (1, 2), "has": (1, 2), "properties": (0, 0),
    "geometry-type": (0, 0), "id": (0, 0), "feature-state": (1, 1),
    "zoom": (0, 0), "heatmap-density": (0, 0), "line-progress": (0, 0),
    "accumulated": (0, 0), "at": (2, 2), "at-interpolated": (2, 2),
    "in": (2, 2), "index-of": (2, 3), "slice": (2, 3), "length": (1, 1),
    "case": (3, None), "match": (4, None), "coalesce": (1, None),
    "step": (4, None), "interpolate": (6, None), "interpolate-hcl": (6, None),
    "interpolate-lab": (6, None), "linear": (0, 0), "exponential": (1, 1),
    "cubic-bezier": (4, 4), "let": (3, None), "var": (1, 1),
    "concat": (1, None), "downcase": (1, 1), "upcase": (1, 1),
    "is-supported-script": (1, 1), "resolved-locale": (1, 1),
    "format": (1, None), "number-format": (1, 2), "collator": (0, 1),
    "to-string": (1, 1), "to-boolean": (1, 1), "to-number": (1, None),
    "to-color": (1, None), "to-rgba": (1, 1), "typeof": (1, 1),
    "array": (1, 3), "boolean": (1, None), "number": (1, None),
    "object": (1, None), "string": (1, None), "image": (1, 1),
    "==": (2, 3), "!=": (2, 3), "<": (2, 3), "<=": (2, 3),
    ">": (2, 3), ">=": (2, 3), "all": (0, None), "any": (0, None),
    "!": (1, 1), "+": (2, None), "-": (1, 2), "*": (2, None),
    "/": (2, 2), "%": (2, 2), "^": (2, 2), "min": (1, None),
    "max": (1, None), "abs": (1, 1), "ceil": (1, 1), "floor": (1, 1),
    "round": (1, 1), "sqrt": (1, 1), "ln": (1, 1), "log10": (1, 1),
    "log2": (1, 1), "sin": (1, 1), "cos": (1, 1), "tan": (1, 1),
    "asin": (1, 1), "acos": (1, 1), "atan": (1, 2), "clamp": (3, 3),
    "e": (0, 0), "pi": (0, 0), "rgb": (3, 3), "rgba": (4, 4),
    "distance": (1, 1), "within": (1, 1),
}


def build_assistant_style(
    style: Any,
    dataset: dict[str, Any],
    *,
    max_chars: int = 40_000,
    max_layers: int = 40,
    max_nodes: int = 5_000,
) -> dict[str, Any]:
    """Validate an LLM-authored style and bind it to the selected dataset."""
    if not isinstance(style, dict) or not isinstance(style.get("layers"), list):
        raise ValueError("style must be a MapLibre style object with a layers array")
    if style.get("version", 8) != 8:
        raise ValueError("style.version must be 8")

    style = normalize_assistant_style_syntax(style)

    try:
        serialized = json.dumps(style, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("style must contain only JSON values") from exc
    if len(serialized) > max_chars:
        raise ValueError(f"style must be at most {max_chars} characters")

    layers = style["layers"]
    if not layers or len(layers) > max_layers:
        raise ValueError(f"style must contain between 1 and {max_layers} layers")
    validate_json_tree(style, max_nodes=max_nodes)
    validate_layers(layers, dataset)

    normalized = normalize_style(
        style,
        dataset.get("geometry_types"),
        dataset,
        enforce_inferred_category_safety=False,
    )
    ensure_vector_point_fallback(normalized, dataset)
    if len(normalized.get("layers") or []) > max_layers:
        raise ValueError(
            "style must leave room for the required low-zoom point fallback layer"
        )
    metadata = normalized.setdefault("metadata", {})
    metadata["ucrstar:assistant"] = {
        "dataset_id": dataset["id"],
        "point_fallback": (
            vector_point_fallback_required(dataset)
            and has_point_layer(normalized.get("layers") or [])
        ),
        "sampling_note": (
            "Vector tiles may omit sampled features at low zoom; zoom in to reveal more detail."
            if (dataset.get("visualization") or {}).get("type") == "VectorTile"
            else None
        ),
    }
    if len(json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)) > max_chars:
        raise ValueError(
            "validated style exceeds the size limit after adding required metadata"
        )
    return normalized


def client_safe_style(style: Any, dataset: dict[str, Any]) -> dict[str, Any]:
    """Return only server-validated layers for a style loaded from the catalog."""
    candidate = normalize_style(
        normalize_assistant_style_syntax(style) if isinstance(style, dict) else style,
        dataset.get("geometry_types"),
        dataset,
        enforce_inferred_category_safety=False,
    )
    candidate = normalize_assistant_style_syntax(candidate)
    safe_layers: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_ids: set[str] = set()
    for layer in candidate.get("layers") or []:
        layer_id = layer.get("id") if isinstance(layer, dict) else None
        if layer_id in seen_ids:
            warnings.append(f"Dropped duplicate style layer {layer_id}")
            continue
        try:
            validate_json_tree(layer, max_nodes=2_000)
            validate_layers([layer], dataset)
        except ValueError as exc:
            warnings.append(f"Dropped style layer {layer_id or '<unknown>'}: {exc}")
            continue
        seen_ids.add(str(layer_id))
        safe_layers.append(layer)

    if not safe_layers:
        fallback = normalize_style(None, dataset.get("geometry_types"), dataset)
        safe_layers = fallback["layers"]
    candidate["layers"] = safe_layers
    if warnings:
        metadata = candidate.setdefault("metadata", {})
        metadata["ucrstar:style_warnings"] = warnings[:20]
    return candidate


def normalize_assistant_style_syntax(style: dict[str, Any]) -> dict[str, Any]:
    """Normalize common LLM aliases and legacy filters to MapLibre expressions."""
    normalized = copy.deepcopy(style)
    for layer in normalized.get("layers") or []:
        if not isinstance(layer, dict):
            continue
        if "filter" in layer:
            layer["filter"] = normalize_legacy_filter(layer["filter"])
        for key in ("filter", "layout", "paint"):
            if key in layer:
                layer[key] = normalize_maplibre_expression(layer[key])
    return normalized


def normalize_maplibre_expression(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_maplibre_expression(child)
            for key, child in value.items()
        }
    if not isinstance(value, list):
        return value
    if value and value[0] == "literal":
        return copy.deepcopy(value)
    normalized = [normalize_maplibre_expression(child) for child in value]
    if normalized and normalized[0] == "includes":
        normalized[0] = "in"
    return normalized


def normalize_legacy_filter(value: Any) -> Any:
    if not isinstance(value, list) or len(value) < 2:
        return value
    operator = value[0]
    input_expression = value[1]
    if input_expression == "$type":
        input_expression = ["geometry-type"]
    elif (
        isinstance(input_expression, str)
        and operator in {"==", "!=", "<", "<=", ">", ">=", "in", "!in"}
    ):
        input_expression = ["get", input_expression]

    if input_expression == ["geometry-type"] and operator in {"==", "!=", "in", "!in"}:
        geometry_types = [canonical_geometry_type(item) for item in value[2:]]
        geometry_types = list(dict.fromkeys(item for item in geometry_types if item))
        if operator in {"==", "!="} and len(geometry_types) == 1:
            return [operator, ["geometry-type"], geometry_types[0]]
        if operator in {"in", "!in"} and geometry_types:
            comparisons = [
                ["==", ["geometry-type"], geometry_type]
                for geometry_type in geometry_types
            ]
            expression: Any = (
                comparisons[0] if len(comparisons) == 1 else ["any", *comparisons]
            )
            return ["!", expression] if operator == "!in" else expression

    if operator in {"in", "!in"} and len(value) > 3:
        expression = ["in", input_expression, ["literal", value[2:]]]
        return ["!", expression] if operator == "!in" else expression
    if operator == "!in" and len(value) == 3:
        return ["!", ["in", input_expression, value[2]]]
    if input_expression is not value[1]:
        return [operator, input_expression, *value[2:]]
    return value


def canonical_geometry_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return {
        "Point": "Point",
        "MultiPoint": "Point",
        "LineString": "LineString",
        "MultiLineString": "LineString",
        "Polygon": "Polygon",
        "MultiPolygon": "Polygon",
    }.get(value)


def validate_json_tree(value: Any, *, max_nodes: int, max_depth: int = 30) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"style must contain at most {max_nodes} JSON values")
        if depth > max_depth:
            raise ValueError(f"style expressions must be at most {max_depth} levels deep")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("style contains a non-finite number")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise ValueError("style object keys must be short strings")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is not None and not isinstance(item, str | int | float | bool):
            raise ValueError("style must contain only JSON values")

    visit(value, 0)


def validate_layers(layers: list[Any], dataset: dict[str, Any]) -> None:
    seen_ids: set[str] = set()
    available_fields = {
        str(field.get("name"))
        for field in (dataset.get("schema") or [])
        if isinstance(field, dict) and field.get("name")
    }
    available_fields.add("_id")

    for layer in layers:
        if not isinstance(layer, dict):
            raise ValueError("each style layer must be an object")
        layer_id = layer.get("id")
        if (
            not isinstance(layer_id, str)
            or not layer_id
            or len(layer_id) > 128
            or layer_id in RESERVED_LAYER_IDS
            or layer_id in seen_ids
        ):
            raise ValueError("style layer IDs must be unique, non-reserved strings")
        seen_ids.add(layer_id)

        layer_type = layer.get("type")
        if layer_type not in DATASET_STYLE_LAYER_TYPES:
            raise ValueError(f"unsupported dataset style layer type: {layer_type}")
        if "paint" in layer and not isinstance(layer["paint"], dict):
            raise ValueError(f"style layer {layer_id} paint must be an object")
        for property_name, property_value in (layer.get("paint") or {}).items():
            if property_name not in PAINT_PROPERTIES[layer_type]:
                raise ValueError(
                    f"unsupported paint property {property_name} for {layer_type} layer"
                )
            validate_style_property(
                property_value,
                property_name,
                f"layers.{layer_id}.paint.{property_name}",
            )
        if "layout" in layer and not isinstance(layer["layout"], dict):
            raise ValueError(f"style layer {layer_id} layout must be an object")
        for property_name, property_value in (layer.get("layout") or {}).items():
            if property_name not in LAYOUT_PROPERTIES[layer_type]:
                raise ValueError(
                    f"unsupported layout property {property_name} for {layer_type} layer"
                )
            if property_name == "text-field":
                raise ValueError(
                    f"style layer {layer_id} uses a MapLibre text-field, which is not "
                    "accepted in dataset styles; text labels remain available through "
                    "the set_labels canvas overlay"
                )
            if property_name == "icon-image":
                raise ValueError(
                    f"style layer {layer_id} uses MapLibre icon-image, which is not "
                    "accepted in dataset styles; Unicode point icons remain available "
                    "through the set_point_icons canvas overlay"
                )
            validate_style_property(
                property_value,
                property_name,
                f"layers.{layer_id}.layout.{property_name}",
            )
        if layer.get("filter") is not None:
            validate_expression(layer["filter"], f"layers.{layer_id}.filter")
        validate_zoom(layer, "minzoom")
        validate_zoom(layer, "maxzoom")
        if (
            isinstance(layer.get("minzoom"), int | float)
            and isinstance(layer.get("maxzoom"), int | float)
            and layer["minzoom"] >= layer["maxzoom"]
        ):
            raise ValueError(f"style layer {layer_id} minzoom must be below maxzoom")

        referenced_fields = style_attributes(
            {
                "filter": layer.get("filter"),
                "layout": layer.get("layout"),
                "paint": layer.get("paint"),
            }
        )
        if len(available_fields) > 1:
            unknown = referenced_fields - available_fields
            if unknown:
                names = ", ".join(sorted(unknown))
                raise ValueError(f"style references unknown dataset attribute(s): {names}")


def validate_style_property(value: Any, property_name: str, path: str) -> None:
    if not isinstance(value, list) or not value:
        return
    if property_name in LITERAL_ARRAY_PROPERTIES and not is_expression(value):
        return
    if property_name == "text-font" and all(isinstance(item, str) for item in value):
        return
    validate_expression(value, path)


def is_expression(value: list[Any]) -> bool:
    return bool(value and isinstance(value[0], str) and value[0] in EXPRESSION_SIGNATURES)


def validate_expression(value: Any, path: str) -> None:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise ValueError(f"{path} must be a valid MapLibre expression")
    operator = value[0]
    signature = EXPRESSION_SIGNATURES.get(operator)
    if signature is None:
        raise ValueError(f'{path} uses unsupported MapLibre operator "{operator}"')
    argument_count = len(value) - 1
    minimum, maximum = signature
    if argument_count < minimum or (maximum is not None and argument_count > maximum):
        expected = (
            str(minimum)
            if maximum == minimum
            else f"{minimum}..{maximum}" if maximum is not None else f"at least {minimum}"
        )
        raise ValueError(
            f'{path} operator "{operator}" expects {expected} arguments, '
            f"but received {argument_count}"
        )
    if operator == "case" and argument_count % 2 == 0:
        raise ValueError(f'{path} operator "case" requires condition/output pairs and a fallback')
    if operator == "match" and argument_count % 2 != 0:
        raise ValueError(f'{path} operator "match" requires label/output pairs and a fallback')
    if operator == "step" and argument_count % 2 != 0:
        raise ValueError(f'{path} operator "step" requires stop/output pairs')
    if operator in {"interpolate", "interpolate-hcl", "interpolate-lab"} and argument_count % 2 != 0:
        raise ValueError(f'{path} operator "{operator}" requires stop/output pairs')
    if operator == "let" and argument_count % 2 == 0:
        raise ValueError(f'{path} operator "let" requires name/value pairs and a result')

    if operator == "literal":
        return
    if operator == "match":
        validate_nested_expression(value[1], f"{path}[1]")
        for index in range(3, len(value) - 1, 2):
            validate_nested_expression(value[index], f"{path}[{index}]")
        validate_nested_expression(value[-1], f"{path}[{len(value) - 1}]")
        return
    if operator == "step":
        validate_nested_expression(value[1], f"{path}[1]")
        validate_nested_expression(value[2], f"{path}[2]")
        for index in range(4, len(value), 2):
            validate_nested_expression(value[index], f"{path}[{index}]")
        return
    if operator in {"interpolate", "interpolate-hcl", "interpolate-lab"}:
        validate_expression(value[1], f"{path}[1]")
        validate_nested_expression(value[2], f"{path}[2]")
        for index in range(4, len(value), 2):
            validate_nested_expression(value[index], f"{path}[{index}]")
        return
    if operator == "let":
        for index in range(1, len(value) - 1, 2):
            if not isinstance(value[index], str):
                raise ValueError(f"{path}[{index}] must be a variable name")
            validate_nested_expression(value[index + 1], f"{path}[{index + 1}]")
        validate_nested_expression(value[-1], f"{path}[{len(value) - 1}]")
        return

    for index, child in enumerate(value[1:], start=1):
        validate_nested_expression(child, f"{path}[{index}]")


def validate_nested_expression(value: Any, path: str) -> None:
    if isinstance(value, list) and value and isinstance(value[0], str):
        validate_expression(value, path)
    elif isinstance(value, dict):
        for key, child in value.items():
            validate_nested_expression(child, f"{path}.{key}")


def validate_zoom(layer: dict[str, Any], key: str) -> None:
    if key not in layer:
        return
    value = layer[key]
    if (
        not isinstance(value, int | float)
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 24
    ):
        raise ValueError(f"style layer {layer.get('id')} {key} must be between 0 and 24")


def style_attributes(value: Any) -> set[str]:
    attributes: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, list):
            if (
                len(item) >= 2
                and isinstance(item[0], str)
                and item[0] in {"get", "has"}
                and isinstance(item[1], str)
            ):
                attributes.add(item[1])
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    return attributes


def ensure_vector_point_fallback(
    style: dict[str, Any],
    dataset: dict[str, Any],
) -> None:
    visualization = dataset.get("visualization") or {}
    if not vector_point_fallback_required(dataset) or has_point_layer(
        style.get("layers") or []
    ):
        return

    color, stroke = representative_colors(style.get("layers") or [])
    fallback = {
        "id": "ucrstar-point-fallback",
        "type": "circle",
        "source": STYLE_SOURCE_ID,
        "source-layer": visualization.get("source_layer") or VECTOR_SOURCE_LAYER,
        "filter": ["==", ["geometry-type"], "Point"],
        "paint": {
            "circle-color": color,
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 0, 2, 12, 5],
            "circle-opacity": 0.9,
            "circle-stroke-color": stroke,
            "circle-stroke-width": 1,
        },
    }
    style.setdefault("layers", []).append(fallback)


def vector_point_fallback_required(dataset: dict[str, Any]) -> bool:
    visualization = dataset.get("visualization") or {}
    geometry_text = " ".join(dataset.get("geometry_types") or []).lower()
    return visualization.get("type") == "VectorTile" and (
        "polygon" in geometry_text or "line" in geometry_text
    )


def has_point_layer(layers: list[dict[str, Any]]) -> bool:
    return any(layer.get("type") in {"circle", "heatmap"} for layer in layers)


def representative_colors(layers: list[dict[str, Any]]) -> tuple[Any, Any]:
    color: Any = "#3b82f6"
    stroke: Any = "#1e3a5f"
    for layer in layers:
        paint = layer.get("paint") or {}
        candidate = paint.get("fill-color") or paint.get("line-color")
        if candidate is not None:
            color = copy.deepcopy(candidate)
            break
    for layer in layers:
        paint = layer.get("paint") or {}
        candidate = paint.get("line-color") or paint.get("fill-outline-color")
        if candidate is not None:
            stroke = copy.deepcopy(candidate)
            break
    return color, stroke
