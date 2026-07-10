const SOURCE_LAYER = "layer0";
const DATASET_SOURCE = "active-dataset";
const DATASET_LAYERS = ["dataset-fill", "dataset-line", "dataset-circle"];
const FORMATS = ["geojson", "csv", "parquet", "zip"];
const DEFAULT_DATASET_STYLE = {
  source_layer: SOURCE_LAYER,
  layers: {
    fill: {
      "fill-color": "#2a9d8f",
      "fill-opacity": 0.25,
    },
    line: {
      "line-color": "#0f6b99",
      "line-width": [
        "interpolate",
        ["linear"],
        ["zoom"],
        2,
        0.7,
        9,
        2.5,
      ],
      "line-opacity": 0.88,
    },
    circle: {
      "circle-color": "#d1495b",
      "circle-radius": [
        "interpolate",
        ["linear"],
        ["zoom"],
        2,
        2,
        10,
        5,
      ],
      "circle-stroke-color": "#ffffff",
      "circle-stroke-width": 0.8,
    },
  },
};

const state = {
  activeDataset: null,
  activeSearch: "",
  isApplyingUrl: false,
  skipNextMapUrlUpdate: false,
  suggestionsTimer: null,
};

const map = new maplibregl.Map({
  container: "map",
  center: [-98, 39],
  zoom: 3,
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: [
          "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        ],
        tileSize: 256,
        attribution: "OpenStreetMap",
      },
    },
    layers: [
      {
        id: "osm",
        type: "raster",
        source: "osm",
      },
    ],
  },
});

map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");

const searchForm = document.querySelector("#search-form");
const searchInput = document.querySelector("#search-input");
const suggestions = document.querySelector("#suggestions");
const sidePanel = document.querySelector("#side-panel");
const panelTitle = document.querySelector("#panel-title");
const panelContent = document.querySelector("#panel-content");
const closePanel = document.querySelector("#close-panel");

searchInput.addEventListener("input", () => {
  window.clearTimeout(state.suggestionsTimer);
  state.suggestionsTimer = window.setTimeout(loadSuggestions, 180);
});

searchInput.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    hideSuggestions();
  }
});

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  hideSuggestions();
  runSearch(searchInput.value.trim(), { history: "push" });
});

closePanel.addEventListener("click", () => {
  sidePanel.hidden = true;
});

map.on("click", async (event) => {
  if (!state.activeDataset) {
    return;
  }
  const features = map.queryRenderedFeatures(event.point, { layers: DATASET_LAYERS });
  if (!features.length) {
    return;
  }
  await requestFeaturePopup(event);
});

map.on("moveend", () => {
  if (state.activeDataset) {
    updateDownloadLinks(state.activeDataset);
  }
  if (state.skipNextMapUrlUpdate) {
    state.skipNextMapUrlUpdate = false;
    return;
  }
  updateUrl({ history: "replace" });
});

window.addEventListener("popstate", () => {
  applyUrlState();
});

map.once("load", async () => {
  await applyUrlState();
  updateUrl({ history: "replace" });
});

async function loadSuggestions() {
  const query = searchInput.value.trim();
  if (!query) {
    hideSuggestions();
    return;
  }

  const datasets = await fetchDatasets(query);
  renderSuggestions(datasets.slice(0, 5));
}

function renderSuggestions(datasets) {
  if (!datasets.length) {
    hideSuggestions();
    return;
  }

  suggestions.replaceChildren(...datasets.map((dataset) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.innerHTML = `
      <strong>${escapeHtml(dataset.name)}</strong>
      <span>${escapeHtml(dataset.description || describeDataset(dataset))}</span>
    `;
    button.addEventListener("click", () => {
      hideSuggestions();
      searchInput.value = dataset.name;
      selectDataset(dataset.id, { history: "push" });
    });
    return button;
  }));
  suggestions.hidden = false;
}

function hideSuggestions() {
  suggestions.hidden = true;
  suggestions.replaceChildren();
}

async function runSearch(query, options = {}) {
  state.activeSearch = query;
  state.activeDataset = null;
  clearDatasetLayer();
  const datasets = await fetchDatasets(query, { semantic: true });
  panelTitle.textContent = query ? `Results for "${query}"` : "Datasets";
  panelContent.replaceChildren(renderResults(datasets));
  sidePanel.hidden = false;
  searchInput.value = query;
  updateUrl({ history: options.history || "replace" });
}

function renderResults(datasets) {
  const container = document.createElement("div");
  if (!datasets.length) {
    const empty = document.createElement("div");
    empty.className = "detail-section";
    empty.textContent = "No datasets found.";
    container.append(empty);
    return container;
  }

  for (const dataset of datasets) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "result-card";
    button.innerHTML = `
      <img class="thumb" alt="" src="/datasets/${encodeURIComponent(dataset.id)}/histogram.png?size=256">
      <span>
        <span class="result-title">${escapeHtml(dataset.name)}</span>
        <span class="muted">${escapeHtml(dataset.description || describeDataset(dataset))}</span>
        <span class="result-meta">${renderPills(dataset)}</span>
      </span>
    `;
    button.addEventListener("click", () => selectDataset(dataset.id, { history: "push" }));
    container.append(button);
  }
  return container;
}

async function selectDataset(datasetId, options = {}) {
  const dataset = await fetchJson(`/datasets/${encodeURIComponent(datasetId)}.json`);
  state.activeDataset = dataset;
  state.activeSearch = "";
  searchInput.value = dataset.name;
  hideSuggestions();
  await showDatasetOnMap(dataset);
  renderDatasetDetails(dataset);
  updateUrl({ history: options.history || "replace" });
}

async function showDatasetOnMap(dataset, options = {}) {
  clearDatasetLayer();
  const style = await fetchDatasetStyle(dataset);
  const sourceLayer = style.source_layer;
  const layers = style.layers;

  map.addSource(DATASET_SOURCE, {
    type: "vector",
    tiles: [
      `${window.location.origin}/datasets/${encodeURIComponent(dataset.id)}/tiles/{z}/{x}/{y}.mvt`,
    ],
    minzoom: 0,
    maxzoom: 14,
  });

  addDatasetLayer({
    id: "dataset-fill",
    type: "fill",
    source: DATASET_SOURCE,
    "source-layer": sourceLayer,
    filter: ["==", ["geometry-type"], "Polygon"],
    paint: layers.fill,
  }, DEFAULT_DATASET_STYLE.layers.fill);

  addDatasetLayer({
    id: "dataset-line",
    type: "line",
    source: DATASET_SOURCE,
    "source-layer": sourceLayer,
    paint: layers.line,
  }, DEFAULT_DATASET_STYLE.layers.line);

  addDatasetLayer({
    id: "dataset-circle",
    type: "circle",
    source: DATASET_SOURCE,
    "source-layer": sourceLayer,
    filter: ["==", ["geometry-type"], "Point"],
    paint: layers.circle,
  }, DEFAULT_DATASET_STYLE.layers.circle);

  if (Array.isArray(dataset.mbr) && dataset.mbr.length === 4) {
    if (options.fitBounds === false) {
      return;
    }
    map.fitBounds(
      [
        [dataset.mbr[0], dataset.mbr[1]],
        [dataset.mbr[2], dataset.mbr[3]],
      ],
      { padding: 80, maxZoom: 12, duration: 600 },
    );
  }
}

async function fetchDatasetStyle(dataset) {
  try {
    const style = await fetchJson(`/datasets/${encodeURIComponent(dataset.id)}/style.json`);
    return normalizeDatasetStyle(style);
  } catch (error) {
    return normalizeDatasetStyle({});
  }
}

function normalizeDatasetStyle(style) {
  const normalized = cloneDefaultDatasetStyle();
  if (!style || typeof style !== "object" || !style.layers || typeof style.layers !== "object") {
    return normalized;
  }

  for (const layerType of ["fill", "line", "circle"]) {
    const paint = normalizeLayerPaint(style.layers[layerType], layerType);
    Object.assign(normalized.layers[layerType], paint);
  }
  return normalized;
}

function cloneDefaultDatasetStyle() {
  return JSON.parse(JSON.stringify(DEFAULT_DATASET_STYLE));
}

function normalizeLayerPaint(layerStyle, layerType) {
  if (!layerStyle || typeof layerStyle !== "object" || Array.isArray(layerStyle)) {
    return {};
  }

  const paint = {};
  for (const [key, value] of Object.entries(layerStyle)) {
    if (isSupportedPaintProperty(key, value, layerType)) {
      paint[key] = value;
    }
  }

  if (layerStyle.paint && typeof layerStyle.paint === "object" && !Array.isArray(layerStyle.paint)) {
    for (const [key, value] of Object.entries(layerStyle.paint)) {
      if (isSupportedPaintProperty(key, value, layerType)) {
        paint[key] = value;
      }
    }
  }
  return paint;
}

function isSupportedPaintProperty(key, value, layerType) {
  return (
    key.startsWith(`${layerType}-`)
    && (
      typeof value === "string"
      || typeof value === "number"
      || typeof value === "boolean"
      || Array.isArray(value)
      || (value !== null && typeof value === "object")
    )
  );
}

function addDatasetLayer(layer, fallbackPaint) {
  try {
    map.addLayer(layer);
  } catch (error) {
    console.warn(`Dataset style rejected for ${layer.id}; using fallback style.`, error);
    map.addLayer({ ...layer, paint: fallbackPaint });
  }
}

function clearDatasetLayer() {
  for (const layerId of DATASET_LAYERS) {
    if (map.getLayer(layerId)) {
      map.removeLayer(layerId);
    }
  }
  if (map.getSource(DATASET_SOURCE)) {
    map.removeSource(DATASET_SOURCE);
  }
}

function renderDatasetDetails(dataset) {
  panelTitle.textContent = dataset.name;
  const root = document.createElement("div");

  root.innerHTML = `
    <section class="detail-section">
      <p>${escapeHtml(dataset.description || "No description available.")}</p>
      ${renderSource(dataset.source)}
    </section>
    <section class="detail-section">
      <div class="stats-grid">
        ${renderStat("Size", formatBytes(dataset.size_bytes))}
        ${renderStat("Features", formatNumber(dataset.num_features))}
        ${renderStat("Coordinates", formatNumber(dataset.num_coordinates))}
        ${renderStat("Geometry", (dataset.geometry_types || []).join(", ") || "Unknown")}
      </div>
    </section>
    <section class="detail-section">
      <h2>Schema</h2>
      ${renderSchema(dataset.schema || [])}
    </section>
    <section class="detail-section">
      <h2>Download</h2>
      <div class="download-grid">
        <label class="download-row">
          <span>Format</span>
          <select id="download-format">
            ${FORMATS.map((format) => `<option value="${format}">.${format}</option>`).join("")}
          </select>
        </label>
        <div class="download-row">
          <span>All</span>
          <a id="download-all" href="#"></a>
        </div>
        <div class="download-row">
          <span>View</span>
          <a id="download-view" href="#"></a>
        </div>
      </div>
    </section>
  `;

  panelContent.replaceChildren(root);
  sidePanel.hidden = false;
  const select = document.querySelector("#download-format");
  select.addEventListener("change", () => updateDownloadLinks(dataset));
  updateDownloadLinks(dataset);
}

function renderSource(source) {
  if (!source || !source.url) {
    return "";
  }
  const label = source.type === "local" ? "Local source" : "Source";
  const href = source.url.startsWith("http://") || source.url.startsWith("https://")
    ? source.url
    : "";
  const modified = source.modified_at
    ? `<span class="muted">Updated ${escapeHtml(formatDateTime(source.modified_at))}</span>`
    : "";
  if (!href) {
    return `
      <div class="source-row">
        <span>${escapeHtml(label)}: ${escapeHtml(source.url)}</span>
        ${modified}
      </div>
    `;
  }
  return `
    <div class="source-row">
      <a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>
      ${modified}
    </div>
  `;
}

function updateDownloadLinks(dataset) {
  const select = document.querySelector("#download-format");
  const allLink = document.querySelector("#download-all");
  const viewLink = document.querySelector("#download-view");
  if (!select || !allLink || !viewLink) {
    return;
  }

  const format = select.value;
  const base = `/datasets/${encodeURIComponent(dataset.id)}/download.${format}`;
  const bounds = map.getBounds();
  const mbr = [
    bounds.getWest(),
    bounds.getSouth(),
    bounds.getEast(),
    bounds.getNorth(),
  ].map((value) => value.toFixed(6)).join(",");

  allLink.href = base;
  allLink.textContent = base;
  viewLink.href = `${base}?MBR=${encodeURIComponent(mbr)}`;
  viewLink.textContent = viewLink.href.replace(window.location.origin, "");
}

async function requestFeaturePopup(event) {
  const sampleJsonUrl = sampleUrlForClick(state.activeDataset, event, "json");
  const response = await fetch(sampleJsonUrl);
  if (!response.ok) {
    return;
  }

  const properties = await response.json();
  if (!properties || !Object.keys(properties).length) {
    return;
  }

  const sampleGeojsonUrl = sampleJsonUrl.replace("/sample.json?", "/sample.geojson?");
  showFeaturePopup(properties, sampleGeojsonUrl, event.lngLat);
}

function sampleUrlForClick(dataset, event, format) {
  const mbr = clickMbr(event.point, 8);
  return `/datasets/${encodeURIComponent(dataset.id)}/sample.${format}?MBR=${encodeURIComponent(mbr.join(","))}`;
}

function clickMbr(point, pixelRadius) {
  const sw = map.unproject([point.x - pixelRadius, point.y + pixelRadius]);
  const ne = map.unproject([point.x + pixelRadius, point.y - pixelRadius]);
  return [
    Math.min(sw.lng, ne.lng),
    Math.min(sw.lat, ne.lat),
    Math.max(sw.lng, ne.lng),
    Math.max(sw.lat, ne.lat),
  ].map((value) => Number(value.toFixed(6)));
}

function showFeaturePopup(properties, sampleGeojsonUrl, lngLat) {
  const rows = Object.entries(properties)
    .map(([key, value]) => `
      <tr>
        <th>${escapeHtml(key)}</th>
        <td>${escapeHtml(formatValue(value))}</td>
      </tr>
    `)
    .join("");

  new maplibregl.Popup()
    .setLngLat(lngLat)
    .setHTML(`
      <table class="popup-table">${rows || "<tr><td>No attributes</td></tr>"}</table>
      <a class="popup-action" href="${escapeHtml(sampleGeojsonUrl)}" target="_blank" rel="noopener">GeoJSON</a>
    `)
    .addTo(map);
}

async function fetchDatasets(query, options = {}) {
  const params = new URLSearchParams();
  if (query) {
    params.set("q", query);
  }
  if (options.semantic) {
    params.set("semantic", "1");
  }
  const url = params.toString() ? `/datasets.json?${params}` : "/datasets.json";
  const payload = await fetchJson(url);
  return payload.datasets || [];
}

async function applyUrlState() {
  state.isApplyingUrl = true;
  try {
    const urlState = readUrlState();
    applyMapUrlState(urlState);

    if (urlState.dataset) {
      const dataset = await fetchJson(`/datasets/${encodeURIComponent(urlState.dataset)}.json`);
      state.activeDataset = dataset;
      state.activeSearch = "";
      searchInput.value = dataset.name;
      hideSuggestions();
      await showDatasetOnMap(dataset, { fitBounds: false });
      renderDatasetDetails(dataset);
    } else if (urlState.q) {
      await runSearch(urlState.q, { history: "replace" });
    } else {
      state.activeDataset = null;
      state.activeSearch = "";
      clearDatasetLayer();
      searchInput.value = "";
      hideSuggestions();
      sidePanel.hidden = true;
    }
  } finally {
    state.isApplyingUrl = false;
  }
}

function applyMapUrlState(urlState) {
  if (urlState.lng == null || urlState.lat == null || urlState.z == null) {
    updateUrl({ history: "replace" });
    return;
  }
  state.skipNextMapUrlUpdate = true;
  map.jumpTo({
    center: [urlState.lng, urlState.lat],
    zoom: urlState.z,
  });
}

function readUrlState() {
  const params = new URLSearchParams(window.location.search);
  const compactState = readCompactUrlState(params);
  if (compactState) {
    return compactState;
  }
  return {
    lng: parseNumberParam(params.get("lng")),
    lat: parseNumberParam(params.get("lat")),
    z: parseNumberParam(params.get("z")),
    dataset: params.get("dataset") || "",
    q: params.get("q") || "",
  };
}

function readCompactUrlState(params) {
  const token = window.location.search.slice(1).split("&")[0] || "";
  if (!token || token.includes("=") || !token.includes("@")) {
    return null;
  }

  const [datasetToken, locationToken] = token.split("@", 2);
  const location = locationToken.split(",");
  if (location.length !== 3) {
    return null;
  }

  const lat = parseNumberParam(location[0]);
  const lng = parseNumberParam(location[1]);
  const z = parseNumberParam(location[2]);
  return {
    lng,
    lat,
    z,
    dataset: datasetToken ? safeDecodeURIComponent(datasetToken) : "",
    q: params.get("q") || "",
  };
}

function updateUrl(options = {}) {
  if (state.isApplyingUrl) {
    return;
  }

  const center = map.getCenter();
  const z = Math.round(map.getZoom());
  const decimals = coordinateDecimalsForZoom(z);
  const datasetName = state.activeDataset ? state.activeDataset.name : "";
  const locationToken = [
    center.lat.toFixed(decimals),
    center.lng.toFixed(decimals),
    z,
  ].join(",");
  const compactToken = `${encodeURIComponent(datasetName)}@${locationToken}`;

  const params = new URLSearchParams();
  if (state.activeSearch) {
    params.set("q", state.activeSearch);
  }

  const suffix = params.toString();
  const url = `${window.location.pathname}?${compactToken}${suffix ? `&${suffix}` : ""}`;
  if (url === `${window.location.pathname}${window.location.search}`) {
    return;
  }

  if (options.history === "push") {
    window.history.pushState({}, "", url);
  } else {
    window.history.replaceState({}, "", url);
  }
}

function coordinateDecimalsForZoom(zoom) {
  if (zoom <= 3) {
    return 1;
  }
  if (zoom <= 6) {
    return 2;
  }
  if (zoom <= 9) {
    return 3;
  }
  if (zoom <= 12) {
    return 4;
  }
  if (zoom <= 15) {
    return 5;
  }
  return 6;
}

function safeDecodeURIComponent(value) {
  try {
    return decodeURIComponent(value);
  } catch (error) {
    return value;
  }
}

function parseNumberParam(value) {
  if (value == null || value === "") {
    return null;
  }
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return response.json();
}

function renderPills(dataset) {
  const values = [
    ...(dataset.geometry_types || []),
    formatBytes(dataset.size_bytes),
  ].filter(Boolean);
  return values.map((value) => `<span class="pill">${escapeHtml(value)}</span>`).join("");
}

function renderStat(label, value) {
  return `
    <div class="stat">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value || "Unknown")}</strong>
    </div>
  `;
}

function renderSchema(schema) {
  if (!schema.length) {
    return "<p class=\"muted\">No schema available.</p>";
  }
  return `
    <ul class="schema-list">
      ${schema.map((field) => `
        <li>
          <span>${escapeHtml(field.name || "field")}</span>
          <span class="muted">${escapeHtml(field.type || "unknown")}</span>
        </li>
      `).join("")}
    </ul>
  `;
}

function describeDataset(dataset) {
  const parts = [];
  if (dataset.num_features != null) {
    parts.push(`${formatNumber(dataset.num_features)} features`);
  }
  if (dataset.size_bytes != null) {
    parts.push(formatBytes(dataset.size_bytes));
  }
  return parts.join(" . ");
}

function formatNumber(value) {
  if (value == null) {
    return "";
  }
  return Number(value).toLocaleString();
}

function formatBytes(value) {
  if (value == null) {
    return "";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function formatValue(value) {
  if (value == null) {
    return "";
  }
  if (typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function formatDateTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
