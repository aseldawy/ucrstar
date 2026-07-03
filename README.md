# UCR Star 2

Flask backend for serving and exploring geospatial datasets built by
[`starlet`](starlet_api.md).

## Layout

```text
datasets/
  <dataset-name>/
instance/
  ucrstar2.sqlite
src/ucrstar2/
```

Each dataset gets a stable UUID in the embedded SQLite catalog. The catalog
stores the dataset subdirectory name plus summary fields used by the REST API.

## Run

```bash
.venv/bin/python src/ucrstar2/cli.py serve --debug
```

The SQLite catalog is created lazily the first time the server needs it. No
separate database setup command is required.

By default, the development server listens on `http://127.0.0.1:8000`. Use
`--port` to pick a different port.

Open `http://127.0.0.1:8000/` for a MapLibre test frontend with dataset search,
histogram thumbnails, MVT display, feature popups, and download links.
The frontend keeps the current center, zoom, selected dataset, and search query
in the URL so links can be copied and reopened into the same view.

Press `Ctrl+C` in the terminal to stop the server. The command runs without
Flask's auto-reloader by default so shutdown is a single process. Add `--reload`
only if you want automatic restarts while editing code.

CLI commands log progress to the console at `INFO` level by default. Use
`--log-level DEBUG` for more detail or `--log-level WARNING` for quieter output.

## Add Datasets

```bash
.venv/bin/python src/ucrstar2/cli.py add-dataset path/to/source.geojson --name roads
```

This uses `starlet.add_dataset()` to build the dataset under `datasets/`, then
refreshes the SQLite catalog so the dataset is immediately available through
the REST API. Use `--overwrite` to replace an existing dataset with the same
name.

The command logs each major step, including Starlet build completion, catalog
sync, selected LLM provider/model, LLM enrichment, embedding creation, and any
LLM errors before falling back.

If LLM support is enabled, `add-dataset` also enriches the catalog entry:

- generates a short missing dataset description
- adds short descriptions for attributes from Starlet stats
- creates a default MapLibre style
- stores a dataset embedding for semantic search

## Delete Datasets

```bash
.venv/bin/python src/ucrstar2/cli.py delete-dataset roads
```

`delete-dataset` accepts either a dataset ID or dataset name. It deletes the
dataset directory under `datasets/` using Starlet's safe delete helper, then
removes the dataset row and associated embeddings from the SQLite catalog. Use
`--missing-ok` to treat a missing dataset as a successful no-op.

## LLM Configuration

Copy the template and edit the copy:

```bash
cp ucrstar2.config.template.json ucrstar2.config.json
```

`ucrstar2.config.json` is ignored by Git because it may contain API keys. The
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

If you later install the project in editable mode, the module form also works:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m ucrstar2.cli serve --debug
```

## REST URLs

- `GET /datasets.json`
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

For backward compatibility, `<ID>` falls back to the dataset directory name if
no UUID matches.
