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

Press `Ctrl+C` in the terminal to stop the server. The command runs without
Flask's auto-reloader by default so shutdown is a single process. Add `--reload`
only if you want automatic restarts while editing code.

## Add Datasets

```bash
.venv/bin/python src/ucrstar2/cli.py add-dataset path/to/source.geojson --name roads
```

This uses `starlet.add_dataset()` to build the dataset under `datasets/`, then
refreshes the SQLite catalog so the dataset is immediately available through
the REST API. Use `--overwrite` to replace an existing dataset with the same
name.

If you later install the project in editable mode, the module form also works:

```bash
.venv/bin/python -m pip install -e .
.venv/bin/python -m ucrstar2.cli serve --debug
```

## REST URLs

- `GET /datasets.json`
- `GET /datasets.json?name=roads&geometry_type=Polygon&min_size=1000`
- `GET /datasets/<ID>.json`
- `GET /datasets/<ID>/histogram.png?size=256`
- `GET /datasets/<ID>/download.geojson?MBR=x1,y1,x2,y2`
- `GET /datasets/<ID>/download.csv?MBR=x1,y1,x2,y2`
- `POST /datasets/<ID>/download.geojson` with a GeoJSON geometry body
- `GET /datasets/<ID>/sample.json?MBR=x1,y1,x2,y2`
- `GET /datasets/<ID>/sample.geojson?MBR=x1,y1,x2,y2`
- `GET /datasets/<ID>/tiles/<z>/<x>/<y>.mvt`
- `POST /admin/sync-datasets`

For backward compatibility, `<ID>` falls back to the dataset directory name if
no UUID matches.
