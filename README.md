# UCR Star

Flask backend for serving and exploring geospatial datasets built by
[`starlet`](https://github.com/ucr-bdlab/starlet).

## Layout

```text
datasets/
  <dataset-name>/
instance/
  ucrstar.sqlite
src/ucrstar/
```

Each dataset gets a stable UUID in the embedded SQLite catalog. The catalog
stores the dataset subdirectory name plus summary fields used by the REST API.

## Run

```bash
.venv/bin/python src/ucrstar/cli.py serve --debug
```

The SQLite catalog is created lazily the first time the server needs it. No
separate database setup command is required.

By default, the development server listens on `http://127.0.0.1:8000`. Use
`--port` to pick a different port.

Open `http://127.0.0.1:8000/` for a MapLibre test frontend with dataset search,
histogram thumbnails, MVT display, feature popups, and download links.
The frontend keeps the current center, zoom, selected dataset, and search query
in the URL so links can be copied and reopened into the same view. Browser URLs
use the dataset name and compact map state, for example
`/?roads@33.98,-117.33,11&q=highways`. The `q` parameter is included only when
search results are displayed.

Press `Ctrl+C` in the terminal to stop the server. The command runs without
Flask's auto-reloader by default so shutdown is a single process. Add `--reload`
only if you want automatic restarts while editing code.

CLI commands log progress to the console at `INFO` level by default. Use
`--log-level DEBUG` for more detail or `--log-level WARNING` for quieter output.

## Add Datasets

```bash
.venv/bin/python src/ucrstar/cli.py add-dataset path/to/source.geojson --name roads
.venv/bin/python src/ucrstar/cli.py add-dataset https://example.com/data/roads.geojson --name roads
.venv/bin/python src/ucrstar/cli.py add-dataset path/to/source.geojson --name roads --create-only
.venv/bin/python src/ucrstar/cli.py add-dataset https://egis-lacounty.hub.arcgis.com/search --create-only
.venv/bin/python src/ucrstar/cli.py add-datasets https://www.ezesri.com/catalog.json --create-only
```

The input can be a local path, a direct public download URL, an ArcGIS item URL,
an Esri Hub dataset URL, an Esri Hub repository/search URL, an ezesri
`catalog.json` URL, or a public ArcGIS FeatureServer layer URL. Local paths are
processed directly. Direct public URLs are downloaded to a temporary directory
and then passed to Starlet:

```bash
.venv/bin/python src/ucrstar/cli.py add-dataset \
  "https://hub.arcgis.com/datasets/example::roads/about" \
  --name roads
```

For remote Esri sources, the command resolves the dataset metadata. Public
FeatureServer layers are exported to a temporary GeoJSON file. Downloadable
ArcGIS items such as File Geodatabase, Shapefile, or GeoPackage downloads are
downloaded directly and passed to `starlet.add_dataset()` in their source
format. Source descriptions, attribute aliases, citations, and useful source
metadata are copied into the catalog where possible. Large downloadable Esri
items can take several minutes because they must be downloaded before Starlet
starts its own tiling step.

When `add-dataset` points to an Esri Hub repository URL such as
`https://egis-lacounty.hub.arcgis.com/search`, it scans the repository and
registers each eligible vector dataset as a separate catalog entry. With
`--create-only`, it stores the source URL, timestamps, Hub search record,
ArcGIS item metadata, service/layer metadata when available, and download links
without processing any datasets. Without `--create-only`, each registered
dataset is processed one at a time.

When `add-dataset` or its alias `add-datasets` points to
`https://www.ezesri.com/catalog.json`, it reads the directory feed directly and
registers each queryable ArcGIS service layer as a created dataset. Each record
stores the concrete FeatureServer or MapServer layer URL, category, owner, tags,
directory metadata, and a canonical ArcGIS item/service key. The ezesri Python
package is not required for this directory import.

By default, the command registers the dataset source, builds the dataset under
`datasets/`, refreshes the SQLite catalog, enriches metadata when configured,
and publishes the dataset so it is immediately available through the REST API.
Use `--create-only` to only insert the source record into the database in the
`created` state. Use `--overwrite` to replace an existing dataset with the same
name.

To process created datasets later:

```bash
.venv/bin/python src/ucrstar/cli.py process-dataset
.venv/bin/python src/ucrstar/cli.py process-dataset --limit 10
.venv/bin/python src/ucrstar/cli.py process-dataset roads
```

Datasets move through `created`, `downloaded`, `processed`, `ready`, and
`published`. If processing fails, the dataset is marked `error` and the error is
stored in the catalog. `/datasets.json` returns only `published` datasets by
default; pass `state=created`, `state=error`, or `state=all` to inspect other
states.

The catalog records how each dataset was added: source type, source URL or local
path, access time, source modified time when available, and source metadata. The
dataset details panel in the frontend shows a source link or local source path.

The command logs each major step, including Starlet build completion, catalog
sync, selected LLM provider/model, LLM enrichment, embedding creation, and any
LLM errors before falling back.

If LLM support is enabled, `add-dataset` also enriches the catalog entry:

- generates a short missing dataset description
- adds short descriptions for attributes from Starlet stats
- creates and stores a complete MapLibre Style Specification v8 document
- translates Esri renderer metadata into MapLibre expressions when an LLM is enabled
- stores a dataset embedding for semantic search

## Refresh Datasets

```bash
.venv/bin/python src/ucrstar/cli.py refresh
.venv/bin/python src/ucrstar/cli.py refresh-datasets
.venv/bin/python src/ucrstar/cli.py refresh roads
.venv/bin/python src/ucrstar/cli.py refresh roads --force
```

`refresh` checks source-backed datasets and rebuilds only those whose source has
a newer modification timestamp than the catalog entry. For local datasets, this
uses the local file or directory modification time. For Esri sources, this uses
ArcGIS item or layer modification metadata when available. For direct remote
files, this uses the HTTP `Last-Modified` header when available.

Refresh builds the replacement under a temporary dataset name first. If the
build succeeds, it swaps the new dataset directory into place while preserving
the existing dataset ID and name, then updates the catalog source metadata. If
the build fails, the existing dataset is left in place. Use `--force` to rebuild
even when the timestamp does not show a newer source.

## Delete Datasets

```bash
.venv/bin/python src/ucrstar/cli.py delete-dataset roads
```

`delete-dataset` accepts either a dataset ID or dataset name. It deletes the
dataset directory under `datasets/` using Starlet's safe delete helper, then
removes the dataset row and associated embeddings from the SQLite catalog. Use
`--missing-ok` to treat a missing dataset as a successful no-op.

## LLM Configuration

Copy the template and edit the copy:

```bash
cp ucrstar.config.template.json ucrstar.config.json
```

`ucrstar.config.json` is ignored by Git because it may contain API keys. The
template supports these providers:

- `openai`
- `gemini`
- `ollama`
- `integrated`

Choose the provider with:

```json
{
  "llm": {
    "enabled": true,
    "provider": "integrated"
  }
}
```

For OpenAI and Gemini, prefer environment variables in the config template:

```bash
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
```

The Gemini template uses `gemini-embedding-2` for embeddings, which is the
current Gemini API embedding model used with `embedContent`.

For Ollama, install Ollama, start its local server, and pull the models:

```bash
ollama serve
ollama pull llama3.1
ollama pull nomic-embed-text
```

### Integrated LLM

The template is configured to use the integrated provider immediately:

```json
{
  "llm": {
    "enabled": true,
    "provider": "integrated",
    "providers": {
      "integrated": {
        "backend": "llama-cpp",
        "model_dir": "models",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "model_file": "",
        "chat_model": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "embedding_model": "builtin-hash",
        "embedding_dimensions": 128
      }
    }
  }
}
```

This requires no API key and no separate model server. The first time the
integrated model is used, the server downloads the configured Hugging Face GGUF
model into `model_dir`, under a subfolder derived from `model_id`, and reuses
that local file afterward. `models/` is ignored by Git.

Install the optional runtime once:

```bash
.venv/bin/python -m pip install llama-cpp-python
```

The `model_file` setting can be left empty. In that case, the server queries
the Hugging Face model metadata and chooses a GGUF file, preferring `q4_k_m`
when available. Set `model_file` only when you want a specific quantization.

For a no-download fallback that still keeps everything inside this server
process, use the built-in backend:

```json
{
  "llm": {
    "enabled": true,
    "provider": "integrated",
    "providers": {
      "integrated": {
        "backend": "builtin",
        "chat_model": "builtin-heuristic",
        "embedding_model": "builtin-hash",
        "embedding_dimensions": 128
      }
    }
  }
}
```

The built-in backend creates deterministic local embeddings, simple dataset and
attribute descriptions from Starlet stats, and a default map style. It is useful
for offline testing. In both integrated modes, embeddings currently use the
built-in deterministic local embedding backend.

Semantic search is used when the frontend submits a search. The server embeds
the query with the currently selected provider/model and compares it with
stored dataset embeddings for that same provider/model. If no matching
embedding exists, search falls back to text matching.

### Chat Assistant

The browser discovers chat support from `GET /llm/capabilities.json`. The
response exposes only public provider/model labels and never returns API keys
or provider base URLs. The chat button remains disabled when chat is disabled
or its selected provider is missing required configuration.

Send a message with `POST /llm/chat.json`:

```json
{
  "message": "Explain the current map",
  "model_id": "ollama:llama3.1",
  "context": {
    "dataset_id": "DATASET_ID",
    "viewport": {
      "bounds": [-118, 33, -117, 34],
      "center": [-117.5, 33.5],
      "zoom": 9
    },
    "basemap": "street",
    "style": {}
  }
}
```

Do not send `session_id` on the first message. The server creates the session
and includes its UUID in the response. Send that ID with later messages. The
browser saves it automatically; Reset discards it, so the next message starts
a new server-side session. Sessions and individual user/assistant messages are
stored in `chat_sessions` and `chat_messages` in the catalog SQLite database.

The assistant uses server-side tools to ground requests in catalog and map data.
Dataset discovery combines text and embedding search. Ambiguous matches return a
`show_datasets` action that populates the normal search-results panel; a confident
match returns `select_dataset`. Named regions are resolved by the server-side
geocoder before a validated `fit_bounds` action is returned. The assistant can also
return `change_basemap` for the street and satellite basemaps.

For questions about the visible map, the browser sends viewport bounds rather than
feature summaries. The server runs a bounded Starlet range query over the selected
dataset and supplies aggregate statistics and a small record sample to the LLM.
The limits are configured under `llm.chat` with `viewport_max_features`,
`viewport_sample_size`, `viewport_max_attributes`, and `viewport_top_values`.
Geocoding endpoint, user agent, and timeout are configured under `llm.geocoding`.

All tool calls, results, and validated actions are stored with the assistant message
in `chat_messages.tool_calls_json` and `chat_messages.actions_json`, so later turns
can refer to real search results without trusting invented IDs from the model. Style
generation/editing and chat-initiated downloads remain unavailable in this phase.
The integrated `builtin` backend remains available for offline enrichment and
deterministic embeddings, but it does not provide conversational chat.

If you later install the project in editable mode, the module form also works:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m ucrstar.cli serve --debug
```

## REST URLs

- `GET /datasets.json`
- `GET /llm/capabilities.json`
- `POST /llm/chat.json` with a message, optional session ID, model ID, and map context
- `GET /datasets.json?name=roads&geometry_type=Polygon&min_size=1000`
- `GET /datasets.json?q=roads&semantic=1`
- `GET /datasets/<ID>.json`
- `GET /datasets/<ID>/style.json`
- `GET /datasets/<ID>/histogram.png?size=256`
- `GET /datasets/<ID>/download.geojson?MBR=x1,y1,x2,y2`
- `GET /datasets/<ID>/download.csv?MBR=x1,y1,x2,y2`
- `POST /datasets/<ID>/download.geojson` with a GeoJSON geometry body
- `GET /datasets/<ID>/sample.json?MBR=x1,y1,x2,y2`
- `GET /datasets/<ID>/sample.geojson?MBR=x1,y1,x2,y2`
- `GET /datasets/<ID>/tiles/<z>/<x>/<y>.mvt`

`GET /datasets/<ID>.json` includes a discriminated `visualization` object. Small
datasets smaller than 1 MB use `type: "GeoJSON"`; its `url` and
`download_url` point to the complete GeoJSON export. Larger datasets with MVT
output use `type: "VectorTile"` and include the tile URL template in both `url`
and `tiles`, plus the MVT `source_layer`. This object is intended to support
additional types such as raster tiles without clients guessing from URLs.

`GET /datasets/<ID>/style.json` returns a complete MapLibre v8 style document.
Data-driven `match`, `step`, and `interpolate` expressions identify styled
attributes and preserve category or range semantics for clients such as the
frontend legend renderer. The browser uses the same document to initialize
Styling Options and records user changes in a local MapLibre v8 document; it
never writes those changes back to the catalog. User-created ramps and
categories use the full-dataset attribute statistics from the dataset detail
response, never values sampled from the current map viewport. Automatically
generated categorical styles are kept only when their explicitly styled values
account for at least 80% of all dataset records according to the stored
statistics; otherwise the API returns a constant geometry color without a
categorical legend.

For backward compatibility, `<ID>` falls back to the dataset directory name if
no UUID matches.
