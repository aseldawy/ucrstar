var SOURCE_LAYER = 'layer0';
var DATASET_SOURCE = 'local';
var CLICKABLE_LAYERS = ['fill', 'points', 'lines'];
var DOWNLOAD_FORMATS = [
  {value:'geojson', label:'GeoJSON'},
  {value:'csv', label:'CSV'},
  {value:'parquet', label:'GeoParquet'},
  {value:'zip', label:'Shapefile'}
];
var FALLBACK_STYLE = {
  source_layer: SOURCE_LAYER,
  layers: {
    fill: {'fill-color':'#4285f4','fill-opacity':0.5},
    line: {'line-color':'#1a73e8','line-width':1.2,'line-opacity':0.9},
    circle: {'circle-radius':5,'circle-color':'#ea4335','circle-stroke-width':1,'circle-stroke-color':'#fff'}
  }
};

var map, currentDataset = null, currentDatasetInfo = null, allDatasets = [];
var currentGeometryType = null;
var currentAttributes = [];
var activePopup = null;
var activePopupState = null;
var urlUpdateTimer = null;
var quickTimer = null;
var sourceLayer = SOURCE_LAYER;
var activeDatasetStyle = clone(FALLBACK_STYLE);
var isApplyingUrl = false;
var skipNextUrlUpdate = false;
var basemapMode = 'street';
var currentDatasetBounds = null;
var currentTileAttributesKey = '';
var currentStyleColors = null;

var searchForm = document.getElementById('searchForm');
var searchInput = document.getElementById('searchInput');
var clearSearch = document.getElementById('clearSearch');
var quickResults = document.getElementById('quickResults');
var leftPanel = document.getElementById('leftPanel');
var panelTitle = document.getElementById('panelTitle');
var panelKicker = document.getElementById('panelKicker');
var panelContent = document.getElementById('panelContent');
var backToResults = document.getElementById('backToResults');
var stylePanelEl = document.getElementById('stylePanel');
var attributeSelect = document.getElementById('attributeSelect');
var labelSelect = document.getElementById('labelSelect');
var legendEl = document.getElementById('legend');
var legendContentEl = document.getElementById('legendContent');
var baseLayerBtn = document.getElementById('baseLayerBtn');
var zoomAllBtn = document.getElementById('zoomAllBtn');
var appEl = document.querySelector('.app');
var initialUrlState = parseUrlState();

function initMap(center, zoom) {
  map = new maplibregl.Map({
    container:'map',
    style:{
      version:8,
      sources:{
        basemap_street:{type:'raster',tiles:['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],tileSize:256,attribution:'OpenStreetMap'},
        basemap_satellite:{type:'raster',tiles:['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],tileSize:256,attribution:'Esri'}
      },
      layers:[
        {id:'basemap-street',type:'raster',source:'basemap_street',paint:{'raster-opacity':0.5}},
        {id:'basemap-satellite',type:'raster',source:'basemap_satellite',paint:{'raster-opacity':0}}
      ]
    },
    center:center || [-98,39],
    zoom:zoom || 4
  });
  ensureLabelCanvas();
  attachMapEvents();
  updateBasemapMode();
}

function attachMapEvents() {
  map.on('moveend', scheduleUrlUpdate);
  map.on('zoomend', scheduleUrlUpdate);
  map.on('click', handleMapClick);
  map.on('load', updateBasemapMode);
}

function waitForMapLoad() {
  if (map.loaded()) return Promise.resolve();
  return new Promise(function(resolve){ map.once('load', resolve); });
}

searchInput.addEventListener('input', function(){
  updateSearchControls();
  clearTimeout(quickTimer);
  quickTimer = setTimeout(runQuickSearch, 160);
});

searchInput.addEventListener('keydown', function(e){
  if (e.key === 'Escape') hideQuickResults();
});

searchForm.addEventListener('submit', function(e){
  e.preventDefault();
  hideQuickResults();
  runSemanticSearch(searchInput.value.trim());
});

clearSearch.addEventListener('click', function() {window.clearFilters();});
backToResults.addEventListener('click', showLastSearchResults);
baseLayerBtn.addEventListener('click', toggleBasemap);
zoomAllBtn.addEventListener('click', zoomToDataset);

window.addEventListener('popstate', function(){
  applyUrlState();
});

window.addEventListener('load', async function(){
  initMap(initialUrlState.center, initialUrlState.zoom);
  await waitForMapLoad();
  await loadDatasets();
  await applyUrlState();
});

async function loadDatasets() {
  try {
    var data = await fetchJson('/datasets.json');
    allDatasets = data.datasets || [];
  } catch(e) {
    allDatasets = [];
  }
}

var lastSearchQuery = '';
var lastSearchResults = [];

async function runQuickSearch() {
  var query = searchInput.value.trim();
  if (!query) {
    hideQuickResults();
    return;
  }
  var datasets = await fetchDatasets(query, {semantic:false});
  renderQuickResults(datasets.slice(0, 6));
}

function renderQuickResults(datasets) {
  if (!datasets.length) {
    quickResults.innerHTML = '<div class="panel-empty">No datasets found.</div>';
    quickResults.hidden = false;
    return;
  }
  quickResults.innerHTML = '';
  datasets.forEach(function(dataset){
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'quick-result';
    button.innerHTML =
      '<img class="quick-thumb" alt="" src="'+histogramUrl(dataset)+'" onerror="this.style.visibility=\'hidden\'">' +
      '<span>' +
        '<span class="quick-title">'+escapeHtml(dataset.name)+'</span>' +
        '<span class="quick-desc">'+escapeHtml(truncateText(dataset.description || describeDataset(dataset), 80))+'</span>' +
      '</span>';
    button.addEventListener('click', function(){
      searchInput.value = dataset.name;
      hideQuickResults();
      selectDataset(dataset.id || dataset.name);
    });
    quickResults.appendChild(button);
  });
  quickResults.hidden = false;
}

function hideQuickResults() {
  quickResults.hidden = true;
  quickResults.innerHTML = '';
}

async function runSemanticSearch(query) {
  lastSearchQuery = query;
  panelKicker.textContent = 'Search results';
  panelTitle.textContent = query ? 'Results for "'+query+'"' : 'Datasets';
  panelContent.innerHTML = '<div class="panel-loading">Searching...</div>';
  setDetailMode(false);
  showPanel();
  var datasets = await fetchDatasets(query, {semantic:true});
  lastSearchResults = datasets;
  renderSearchResults(datasets);
  updateSearchControls();
  updateUrl({history:'push', search:query});
}

function renderSearchResults(datasets) {
  setDetailMode(false);
  if (!datasets.length) {
    panelContent.innerHTML = '<div class="panel-empty">No datasets found.</div>';
    return;
  }
  var list = document.createElement('div');
  list.className = 'result-list';
  datasets.forEach(function(dataset){
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'result-card';
    button.innerHTML =
      '<img class="result-thumb" alt="" src="'+histogramUrl(dataset)+'" onerror="this.style.visibility=\'hidden\'">' +
      '<span>' +
        '<span class="result-title">'+escapeHtml(dataset.name)+'</span>' +
        '<span class="result-desc">'+escapeHtml(truncateText(dataset.description || describeDataset(dataset), 100))+'</span>' +
        '<span class="result-meta">'+renderDatasetChips(dataset)+'</span>' +
      '</span>';
    button.addEventListener('click', function(){ selectDataset(dataset.id || dataset.name); });
    list.appendChild(button);
  });
  panelContent.replaceChildren(list);
}

async function selectDataset(datasetRef, options) {
  options = options || {};
  var mapEl = document.getElementById('map');
  mapEl.classList.add('dataset-switching');
  setTimeout(function(){ mapEl.classList.remove('dataset-switching'); }, 400);

  currentDatasetInfo = await fetchJson('/datasets/'+encodeURIComponent(datasetRef)+'.json');
  currentDataset = currentDatasetInfo.id || currentDatasetInfo.name;
  currentAttributes = buildAttributeCatalog(currentDatasetInfo);
  searchInput.value = currentDatasetInfo.name || '';
  updateSearchControls();
  hideQuickResults();
  await loadMapDataset(currentDatasetInfo, {fitBounds: options.fitBounds !== false});
  renderDatasetDetails(currentDatasetInfo);
  updateUrl({history: options.history || 'push'});
}

async function loadMapDataset(dataset, options) {
  options = options || {};
  clearDatasetLayer();
  resetLegend();
  _detachLabelRenderer();
  activeDatasetStyle = await fetchDatasetStyle(dataset);
  var visualization = dataset.visualization || {};
  sourceLayer = visualization.source_layer || SOURCE_LAYER;
  currentTileAttributesKey = tileAttributesKey(tileAttributesForStyle(activeDatasetStyle.document));
  addDatasetSourceAndLayers(visualization, activeDatasetStyle.layers);

  attachClickableCursors();
  populateAttributeSelect();
  populateLabelSelect();
  syncStylePanelFromDocument(activeDatasetStyle.document);
  applyLabelsFromStylePanel();
  renderStyleLegend(activeDatasetStyle.document);

  var geomText = ((dataset.geometry_types || []).join(' ') || '').toUpperCase();
  var hasPoint = geomText.indexOf('POINT') !== -1;
  var hasLine = geomText.indexOf('LINE') !== -1;
  var hasPolygon = geomText.indexOf('POLYGON') !== -1;
  var typeCount = (hasPoint?1:0) + (hasLine?1:0) + (hasPolygon?1:0);
  currentGeometryType = typeCount === 1 ? (hasPoint ? 'point' : hasLine ? 'line' : 'polygon') : null;
  currentDatasetBounds = Array.isArray(dataset.mbr) && dataset.mbr.length === 4 ? dataset.mbr.slice() : null;

  if (options.fitBounds && Array.isArray(dataset.mbr) && dataset.mbr.length === 4) {
    map.fitBounds([[dataset.mbr[0], dataset.mbr[1]], [dataset.mbr[2], dataset.mbr[3]]], {padding:70, maxZoom:12, duration:600});
  }
  updateZoomAllState();
}

function visualizationSource(visualization) {
  if (visualization.type === 'GeoJSON') {
    return {type:'geojson', data:absoluteDatasetUrl(visualization.url)};
  }
  if (visualization.type === 'VectorTile') {
    var source = {type:'vector', tiles:[vectorTileUrl(visualization.url)]};
    var minZoom = numericZoom(visualization.min_zoom);
    var maxZoom = numericZoom(visualization.max_zoom);
    if (minZoom !== null) source.minzoom = minZoom;
    if (maxZoom !== null) source.maxzoom = maxZoom;
    return source;
  }
  throw new Error('Unsupported visualization type: ' + (visualization.type || 'missing'));
}

function addDatasetSourceAndLayers(visualization, paints) {
  map.addSource(DATASET_SOURCE, visualizationSource(visualization));
  var layerSource = visualization.type === 'VectorTile' ? {'source-layer':sourceLayer} : {};
  addStyledLayer(Object.assign({id:'fill', type:'fill', source:DATASET_SOURCE, filter:['==',['geometry-type'],'Polygon'], paint:clone(paints.fill)}, layerSource), FALLBACK_STYLE.layers.fill);
  addStyledLayer(Object.assign({id:'outline', type:'line', source:DATASET_SOURCE, filter:['==',['geometry-type'],'Polygon'], paint:clone(paints.line)}, layerSource), FALLBACK_STYLE.layers.line);
  addStyledLayer(Object.assign({id:'points', type:'circle', source:DATASET_SOURCE, filter:['==',['geometry-type'],'Point'], paint:clone(paints.circle)}, layerSource), FALLBACK_STYLE.layers.circle);
  addStyledLayer(Object.assign({id:'lines', type:'line', source:DATASET_SOURCE, filter:['==',['geometry-type'],'LineString'], paint:clone(paints.line)}, layerSource), FALLBACK_STYLE.layers.line);
}

function vectorTileUrl(url) {
  var absoluteUrl = absoluteDatasetUrl(url);
  if (!currentTileAttributesKey) return absoluteUrl;
  var separator = absoluteUrl.indexOf('?') === -1 ? '?' : '&';
  return absoluteUrl + separator + 'attributes=' + encodeURIComponent(currentTileAttributesKey);
}

function updateVectorTileAttributes(attributes) {
  if (!map || !currentDatasetInfo) return;
  var visualization = currentDatasetInfo.visualization || {};
  if (visualization.type !== 'VectorTile') return;
  var key = tileAttributesKey(attributes);
  if (key === currentTileAttributesKey) return;
  currentTileAttributesKey = key;
  var paints = currentMapPaints();
  clearDatasetLayer();
  addDatasetSourceAndLayers(visualization, paints);
  attachClickableCursors();
  _scheduleRender();
}

function currentMapPaints() {
  var paints = clone(activeDatasetStyle.layers);
  [['fill','fill'], ['outline','line'], ['points','circle'], ['lines','line']].forEach(function(pair){
    var layerId = pair[0], layerType = pair[1];
    if (!map.getLayer(layerId)) return;
    Object.keys(paints[layerType]).forEach(function(property){
      try {
        var value = map.getPaintProperty(layerId, property);
        if (value !== undefined) paints[layerType][property] = value;
      } catch(e) {}
    });
  });
  return paints;
}

function tileAttributesForStyle(style, extraAttributes) {
  var names = {};
  function add(name) {
    if (typeof name === 'string' && name && name !== 'geometry') names[name] = true;
  }
  collectStyleAttributes(style, add);
  (extraAttributes || []).forEach(add);
  return Object.keys(names).sort();
}

function tileAttributesKey(attributes) {
  var names = {};
  (attributes || []).forEach(function(value){
    if (typeof value === 'string' && value) names[value] = true;
  });
  return Object.keys(names).sort().join(',');
}

function collectStyleAttributes(value, add) {
  if (!value) return;
  if (Array.isArray(value)) {
    if ((value[0] === 'get' || value[0] === 'has') && typeof value[1] === 'string') add(value[1]);
    value.forEach(function(item){ collectStyleAttributes(item, add); });
    return;
  }
  if (typeof value === 'object') {
    Object.keys(value).forEach(function(key){ collectStyleAttributes(value[key], add); });
  }
}

function numericZoom(value) {
  var zoom = Number(value);
  return Number.isFinite(zoom) ? zoom : null;
}

function absoluteDatasetUrl(url) {
  if (typeof url !== 'string' || !url) throw new Error('Visualization URL is missing');
  if (/^[a-z][a-z0-9+.-]*:/i.test(url)) return url;
  if (url.indexOf('//') === 0) return window.location.protocol + url;
  if (url.charAt(0) === '/') return window.location.origin + url;
  return window.location.origin + '/' + url;
}

async function fetchDatasetStyle(dataset) {
  try {
    var style = await fetchJson('/datasets/'+encodeURIComponent(dataset.id || dataset.name)+'/style.json');
    return normalizeDatasetStyle(style);
  } catch(e) {
    return normalizeDatasetStyle(fallbackStyleDocument(dataset));
  }
}

function normalizeDatasetStyle(style, serverDocument) {
  var normalized = clone(FALLBACK_STYLE);
  normalized.document = clone(style);
  normalized.serverDocument = clone(serverDocument || style);
  if (!style || !Array.isArray(style.layers)) return normalized;
  style.layers.forEach(function(layer){
    if (!layer || ['fill','line','circle'].indexOf(layer.type) === -1) return;
    var paint = normalizeLayerPaint(layer.paint, layer.type);
    Object.keys(paint).forEach(function(key){ normalized.layers[layer.type][key] = paint[key]; });
  });
  return normalized;
}

function fallbackStyleDocument(dataset) {
  var visualization = dataset && dataset.visualization || {};
  var source = visualizationSource(visualization);
  var layerSource = visualization.type === 'VectorTile' ? {'source-layer':visualization.source_layer || SOURCE_LAYER} : {};
  return {
    version:8,
    name:(dataset && dataset.name) || 'UCR Star dataset',
    metadata:{},
    sources:{dataset:source},
    layers:[
      Object.assign({id:'fill',type:'fill',source:'dataset',filter:['==',['geometry-type'],'Polygon'],paint:clone(FALLBACK_STYLE.layers.fill)},layerSource),
      Object.assign({id:'outline',type:'line',source:'dataset',filter:['==',['geometry-type'],'Polygon'],paint:clone(FALLBACK_STYLE.layers.line)},layerSource),
      Object.assign({id:'lines',type:'line',source:'dataset',filter:['==',['geometry-type'],'LineString'],paint:clone(FALLBACK_STYLE.layers.line)},layerSource),
      Object.assign({id:'points',type:'circle',source:'dataset',filter:['==',['geometry-type'],'Point'],paint:clone(FALLBACK_STYLE.layers.circle)},layerSource)
    ]
  };
}

function renderStyleLegend(style) {
  resetLegend();
  if (!style || !Array.isArray(style.layers)) return;
  var metadata = style.metadata && style.metadata['ucrstar:legend'];
  var entry = findLegendExpression(style.layers);
  if (!entry && !metadata) return;
  var legend = legendFromExpression(entry && entry.expression, metadata || {});
  if (!legend || !legend.items.length) return;
  legendEl.querySelector('.legend-title').textContent = legend.title || 'Legend';
  legend.items.forEach(function(item){
    var row = document.createElement('div');
    row.className = 'legend-item';
    var swatch = document.createElement('div');
    swatch.className = 'legend-color';
    swatch.style.background = item.color;
    var label = document.createElement('span');
    label.textContent = item.label;
    row.appendChild(swatch);
    row.appendChild(label);
    legendContentEl.appendChild(row);
  });
  legendEl.classList.add('visible');
}

function findLegendExpression(layers) {
  var properties = ['fill-color','line-color','circle-color'];
  for (var i=0; i<layers.length; i++) {
    var paint = layers[i] && layers[i].paint || {};
    for (var j=0; j<properties.length; j++) {
      if (Array.isArray(paint[properties[j]])) return {expression:paint[properties[j]]};
    }
  }
  return null;
}

function legendFromExpression(expression, metadata) {
  var property = metadata.property || expressionProperty(expression);
  var labels = metadata.labels || {};
  var items = [];
  if (Array.isArray(metadata.stops) && metadata.stops.length) {
    items = metadata.stops.map(function(stop){
      return {label:String(stop.label || formatLegendValue(stop.value)), color:stop.color};
    });
  } else if (Array.isArray(expression) && expression[0] === 'match') {
    items = legendItemsFromMatch(expression, labels, '');
  } else if (Array.isArray(expression) && (expression[0] === 'interpolate' || expression[0] === 'step')) {
    var start = 3;
    if (expression[0] === 'step') items.push({label:'Below first break', color:expression[2]});
    for (var j=start; j<expression.length-1; j+=2) {
      if (typeof expression[j+1] === 'string') items.push({label:formatLegendValue(expression[j]), color:expression[j+1]});
    }
  }
  return {title:property || 'Legend', items:items.filter(function(item){ return typeof item.color === 'string'; })};
}

function legendItemsFromMatch(expression, labels, parentLabel) {
  var items = [];
  for (var i=2; i<expression.length-1; i+=2) {
    var value = expression[i];
    var output = expression[i+1];
    var valueLabel = String(labels[String(value)] || value);
    var label = parentLabel ? parentLabel + ' / ' + valueLabel : valueLabel;
    if (typeof output === 'string') {
      items.push({label:label, color:output});
    } else if (Array.isArray(output) && output[0] === 'match') {
      items = items.concat(legendItemsFromMatch(output, {}, label));
    }
  }
  var fallback = expression[expression.length-1];
  if (typeof fallback === 'string') {
    items.push({
      label:parentLabel ? parentLabel + ' / other' : 'Other / unspecified',
      color:fallback
    });
  }
  return items;
}

function expressionProperty(expression) {
  if (!Array.isArray(expression)) return null;
  if (expression[0] === 'get' && typeof expression[1] === 'string') return expression[1];
  for (var i=1; i<expression.length; i++) {
    var property = expressionProperty(expression[i]);
    if (property) return property;
  }
  return null;
}

function formatLegendValue(value) {
  return typeof value === 'number' ? Number(value.toFixed(2)).toLocaleString() : String(value);
}

function normalizeLayerPaint(layerStyle, layerType) {
  var paint = {};
  if (!layerStyle || typeof layerStyle !== 'object' || Array.isArray(layerStyle)) return paint;
  Object.keys(layerStyle).forEach(function(key){
    if (isSupportedPaintProperty(key, layerStyle[key], layerType)) paint[key] = layerStyle[key];
  });
  if (layerStyle.paint && typeof layerStyle.paint === 'object') {
    Object.keys(layerStyle.paint).forEach(function(key){
      if (isSupportedPaintProperty(key, layerStyle.paint[key], layerType)) paint[key] = layerStyle.paint[key];
    });
  }
  return paint;
}

function isSupportedPaintProperty(key, value, layerType) {
  return key.indexOf(layerType + '-') === 0 && (
    typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' ||
    Array.isArray(value) || (value !== null && typeof value === 'object')
  );
}

function addStyledLayer(layer, fallbackPaint) {
  try {
    map.addLayer(layer);
  } catch(e) {
    console.warn('Dataset style rejected for '+layer.id, e);
    layer.paint = fallbackPaint;
    map.addLayer(layer);
  }
}

function clearDatasetLayer() {
  CLICKABLE_LAYERS.concat(['outline']).forEach(function(layerId){
    if (map.getLayer(layerId)) map.removeLayer(layerId);
  });
  if (map.getSource(DATASET_SOURCE)) map.removeSource(DATASET_SOURCE);
  map._cursorBound = {};
}

function updateBasemapMode() {
  if (!map || !map.getLayer('basemap-street') || !map.getLayer('basemap-satellite')) return;
  map.setLayoutProperty('basemap-street', 'visibility', basemapMode === 'street' ? 'visible' : 'none');
  map.setLayoutProperty('basemap-satellite', 'visibility', basemapMode === 'satellite' ? 'visible' : 'none');
  map.setPaintProperty('basemap-street', 'raster-opacity', basemapMode === 'street' ? 0.5 : 0);
  map.setPaintProperty('basemap-satellite', 'raster-opacity', basemapMode === 'satellite' ? 0.9 : 0);
  if (baseLayerBtn) baseLayerBtn.textContent = basemapMode === 'street' ? '🗺' : '🛰';
}

function toggleBasemap() {
  basemapMode = basemapMode === 'street' ? 'satellite' : 'street';
  updateBasemapMode();
}

function zoomToDataset() {
  if (!map || !currentDatasetBounds) return;
  map.fitBounds(
    [[currentDatasetBounds[0], currentDatasetBounds[1]], [currentDatasetBounds[2], currentDatasetBounds[3]]],
    {padding:70, maxZoom:13, duration:600}
  );
}

function updateZoomAllState() {
  if (zoomAllBtn) zoomAllBtn.disabled = !currentDatasetBounds;
}

function attachClickableCursors() {
  CLICKABLE_LAYERS.forEach(function(layerId){
    if (!map.getLayer(layerId) || map._cursorBound && map._cursorBound[layerId]) return;
    map._cursorBound = map._cursorBound || {};
    map._cursorBound[layerId] = true;
    map.on('mouseenter', layerId, function(){ map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layerId, function(){ map.getCanvas().style.cursor = ''; });
  });
}

async function handleMapClick(e) {
  if (!currentDatasetInfo) return;
  var avail = CLICKABLE_LAYERS.filter(function(layerId){ return map.getLayer(layerId); });
  if (!avail.length) return;
  var features = map.queryRenderedFeatures(e.point, {layers:avail});
  if (!features || !features.length) return;
  var feature = features[0];
  var fallback = feature.properties || {};
  var sampleJsonUrl = sampleUrlForClick(currentDatasetInfo, e, 'json');
  var sampleGeojsonUrl = sampleUrlForClick(currentDatasetInfo, e, 'geojson');
  try {
    var response = await fetch(sampleJsonUrl);
    if (!response.ok) throw new Error(response.status + ' ' + response.statusText);
    var properties = await response.json();
    showFeaturePopup(properties, e.lngLat, sampleGeojsonUrl);
  } catch(error) {
    showFeaturePopup(fallback, e.lngLat, null);
  }
}

function sampleUrlForClick(dataset, event, format) {
  var mbr = clickMbr(event.point, 8).join(',');
  return '/datasets/'+encodeURIComponent(dataset.id || dataset.name)+'/sample.'+format+'?MBR='+encodeURIComponent(mbr);
}

function clickMbr(point, pixelRadius) {
  var sw = map.unproject([point.x - pixelRadius, point.y + pixelRadius]);
  var ne = map.unproject([point.x + pixelRadius, point.y - pixelRadius]);
  return [
    Math.min(sw.lng, ne.lng),
    Math.min(sw.lat, ne.lat),
    Math.max(sw.lng, ne.lng),
    Math.max(sw.lat, ne.lat)
  ].map(function(value){ return Number(value.toFixed(6)); });
}

function showFeaturePopup(properties, lngLat, geojsonUrl) {
  if (!properties || !Object.keys(properties).length) return;
  var rows = Object.keys(properties).filter(function(key){
    return key !== 'geometry' && key.indexOf('_') !== 0;
  }).map(function(key){
    return {
      key: key,
      value: properties[key],
      text: String(key) + ' ' + String(formatValue(properties[key]))
    };
  });
  var container = document.createElement('div');
  container.className = 'popup-shell';

  var header = document.createElement('div');
  header.className = 'popup-header';

  var title = document.createElement('span');
  title.className = 'popup-title';
  title.textContent = 'Feature';
  header.appendChild(title);

  var searchWrap = document.createElement('div');
  searchWrap.className = 'popup-search-wrap';

  var search = document.createElement('input');
  search.className = 'popup-search';
  search.type = 'text';
  search.placeholder = 'Filter fields';
  search.autocomplete = 'off';
  search.spellcheck = false;
  searchWrap.appendChild(search);

  var regexBtn = document.createElement('button');
  regexBtn.type = 'button';
  regexBtn.className = 'popup-regex-btn';
  regexBtn.title = 'Toggle regex search';
  regexBtn.setAttribute('aria-pressed', 'false');
  regexBtn.textContent = '.*';
  searchWrap.appendChild(regexBtn);

  header.appendChild(searchWrap);

  var actions = document.createElement('span');
  actions.className = 'popup-header-actions';
  if (geojsonUrl) {
    var download = document.createElement('a');
    download.className = 'popup-download';
    download.href = geojsonUrl;
    download.target = '_blank';
    download.rel = 'noopener';
    download.title = 'Download GeoJSON';
    download.textContent = '⇩';
    actions.appendChild(download);
  } else {
    var count = document.createElement('span');
    count.className = 'popup-count';
    count.textContent = String(rows.length);
    actions.appendChild(count);
  }
  header.appendChild(actions);
  container.appendChild(header);

  var options = document.createElement('label');
  options.className = 'popup-options';

  var hideNulls = document.createElement('input');
  hideNulls.type = 'checkbox';
  hideNulls.checked = true;
  options.appendChild(hideNulls);
  options.appendChild(document.createTextNode('Hide null attributes'));
  container.appendChild(options);

  var body = document.createElement('div');
  body.className = 'popup-body';
  container.appendChild(body);

  function renderRows() {
    var query = search.value.trim();
    var regexMode = regexBtn.getAttribute('aria-pressed') === 'true';
    var filtered = hideNulls.checked ? rows.filter(function(row){ return !isNullAttribute(row.value); }) : rows;
    var invalidRegex = false;
    if (query) {
      if (regexMode) {
        try {
          var re = new RegExp(query, 'i');
          filtered = filtered.filter(function(row){ return re.test(row.key) || re.test(String(formatValue(row.value))); });
        } catch(error) {
          invalidRegex = true;
          filtered = [];
        }
      } else {
        var needle = query.toLowerCase();
        filtered = filtered.filter(function(row){ return row.text.toLowerCase().indexOf(needle) !== -1; });
      }
    }
    body.innerHTML = '';
    if (invalidRegex) {
      var hint = document.createElement('div');
      hint.className = 'popup-empty-state';
      hint.textContent = 'Invalid regex pattern';
      body.appendChild(hint);
      return;
    }
    if (!filtered.length) {
      var empty = document.createElement('div');
      empty.className = 'popup-empty-state';
      empty.textContent = query ? 'No matching fields' : 'No visible fields';
      body.appendChild(empty);
      return;
    }
    filtered.forEach(function(row){
      var item = document.createElement('div');
      item.className = 'popup-row';

      var key = document.createElement('span');
      key.className = 'popup-key';
      key.title = row.key;
      key.textContent = row.key;
      item.appendChild(key);

      var val = document.createElement('span');
      val.className = 'popup-val';
      val.textContent = formatValue(row.value);
      item.appendChild(val);

      body.appendChild(item);
    });
  }

  search.addEventListener('input', renderRows);
  hideNulls.addEventListener('change', renderRows);
  regexBtn.addEventListener('click', function(){
    var pressed = regexBtn.getAttribute('aria-pressed') === 'true';
    regexBtn.setAttribute('aria-pressed', String(!pressed));
    regexBtn.classList.toggle('active', !pressed);
    renderRows();
    search.focus();
  });

  renderRows();
  if (activePopup) activePopup.remove();
  activePopupState = {search: search, regexBtn: regexBtn, hideNulls: hideNulls, body: body};
  activePopup = new maplibregl.Popup({maxWidth:'420px',closeButton:true}).setLngLat(lngLat).setDOMContent(container).addTo(map);
  activePopup.on('close', function(){ activePopupState = null; });
}

function isNullAttribute(value) {
  return value === null || value === undefined;
}

function renderDatasetDetails(dataset) {
  panelKicker.textContent = 'Dataset';
  panelTitle.textContent = '';
  setDetailMode(lastSearchResults.length > 0);
  showPanel();
  var sourceHtml = renderSource(dataset.source);
  panelContent.innerHTML =
    '<div class="detail-view">' +
      '<div class="detail-name">'+escapeHtml(dataset.name)+'</div>' +
      '<div class="detail-copy">'+escapeHtml(truncateText(dataset.description || 'No description available.', 300))+'</div>' +
      '<div class="detail-metrics">' +
        renderMetric('Size', formatMegabytes(dataset.size_bytes)) +
        renderMetric('Features', formatNumber(dataset.num_features)) +
        renderMetric('Coordinates', formatNumber(dataset.num_coordinates)) +
        renderGeometryMetric(dataset.geometry_types || []) +
      '</div>' +
      '<div class="detail-section-title">Schema</div>' +
      '<div class="schema-grid">'+renderSchemaCards(dataset.schema || [])+'</div>' +
      '<div class="detail-section-title">Source</div>' +
      (sourceHtml || '<div class="source-row">No source metadata available.</div>') +
      '<div class="detail-section-title">Download</div>' +
      renderDownloadControls(dataset) +
    '</div>';
  var select = document.getElementById('downloadFormat');
  if (select) select.addEventListener('change', function(){ updateDownloadLinks(dataset); });
  updateDownloadLinks(dataset);
}

function renderMetric(label, value) {
  return '<div class="metric-card"><span>'+escapeHtml(label)+'</span><strong>'+escapeHtml(value || 'Unknown')+'</strong></div>';
}

function renderGeometryMetric(types) {
  return '<div class="metric-card geometry-card"><span>Geometry</span><strong class="geometry-icons">'+renderGeometryIcons(types)+'</strong></div>';
}

function renderGeometryIcons(types) {
  if (!types.length) return '<span class="meta-chip">Unknown</span>';
  return types.map(function(type){
    return '<span class="geom-icon" title="'+escapeHtml(type)+'">'+geometryIcon(type)+'</span>';
  }).join('');
}

function geometryIcon(type) {
  var text = String(type).toLowerCase();
  if (text.indexOf('point') !== -1) return '&#9679;';
  if (text.indexOf('line') !== -1) return '&#9581;';
  if (text.indexOf('polygon') !== -1) return '&#9635;';
  return '&#9671;';
}

function renderSchemaCards(schema) {
  if (!schema.length) return '<div class="panel-empty">No schema available.</div>';
  return schema.filter(function(field){ return field.name && field.name !== 'geometry'; }).map(function(field){
    var description = field.description || field.alias || field.type || 'No description available';
    return '<div class="schema-card" title="'+escapeHtml(description)+'"><span>'+schemaIcon(field.type)+'</span><span>'+escapeHtml(field.name)+'</span></div>';
  }).join('');
}

function schemaIcon(type) {
  var text = String(type || '').toLowerCase();
  if (text.indexOf('date') !== -1 || text.indexOf('time') !== -1) return '&#128197;';
  if (text.indexOf('int') !== -1 || text.indexOf('float') !== -1 || text.indexOf('double') !== -1 || text.indexOf('number') !== -1 || text.indexOf('numeric') !== -1) return '123';
  if (text.indexOf('bool') !== -1) return '&#10003;';
  if (text.indexOf('geom') !== -1) return '&#9671;';
  return 'Aa';
}

function renderSource(source) {
  if (!source || source.type === 'local') return source ? '<div class="source-row"><span>Local file</span></div>' : '';
  if (!source.url) return '';
  var label = 'Source';
  var updated = source.modified_at ? 'Updated '+escapeHtml(formatDateTime(source.modified_at)) : 'Last update unknown';
  var href = /^https?:\/\//.test(source.url) ? source.url : '';
  if (href) {
    return '<div class="source-row"><a href="'+escapeHtml(href)+'" target="_blank" rel="noreferrer">'+escapeHtml(label)+'</a><span>'+updated+'</span></div>';
  }
  return '';
}

function renderDownloadControls(dataset) {
  var options = DOWNLOAD_FORMATS.map(function(fmt){
    return '<option value="'+fmt.value+'">'+escapeHtml(fmt.label)+'</option>';
  }).join('');
  return '<div class="download-row-new"><strong>Download</strong><select id="downloadFormat">'+options+'</select><a id="downloadView" class="download-link" href="#">Current view</a><a id="downloadAll" class="download-link" href="#">All</a></div>';
}

function updateDownloadLinks(dataset) {
  var select = document.getElementById('downloadFormat');
  var view = document.getElementById('downloadView');
  var all = document.getElementById('downloadAll');
  if (!select || !view || !all || !map) return;
  var ref = dataset.id || dataset.name;
  var base = '/datasets/'+encodeURIComponent(ref)+'/download.'+select.value;
  var bounds = map.getBounds();
  var mbr = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()].map(function(value){return value.toFixed(6);}).join(',');
  all.href = base;
  view.href = base + '?MBR=' + encodeURIComponent(mbr);
}

function downloadDataset(mode) {
  if (!currentDatasetInfo) return;
  updateDownloadLinks(currentDatasetInfo);
  var link = document.getElementById(mode === 'viewport' ? 'downloadView' : 'downloadAll');
  if (link) link.click();
}

function showPanel() {
  leftPanel.classList.add('visible');
  if (appEl) appEl.classList.add('panel-open');
  updateSearchControls();
  resizeMapSoon();
}
function hidePanel() {
  leftPanel.classList.remove('visible');
  if (appEl) appEl.classList.remove('panel-open');
  setDetailMode(false);
  updateSearchControls();
  resizeMapSoon();
}

function resizeMapSoon() {
  if (!map) return;
  setTimeout(function(){
    try { map.resize(); } catch(e) {}
  }, 280);
}

function setDetailMode(hasBack) {
  backToResults.hidden = !hasBack;
  leftPanel.classList.toggle('has-back', !!hasBack);
}

function showLastSearchResults() {
  if (!lastSearchResults.length) {
    hidePanel();
    return;
  }
  panelKicker.textContent = 'Search results';
  panelTitle.textContent = lastSearchQuery ? 'Results for "'+lastSearchQuery+'"' : 'Datasets';
  searchInput.value = lastSearchQuery;
  renderSearchResults(lastSearchResults);
  showPanel();
  updateSearchControls();
}

function updateSearchControls() {
  var hasPanel = leftPanel.classList.contains('visible');
  var hasSearch = searchInput.value.trim().length > 0;
  clearSearch.hidden = !(hasPanel || hasSearch || lastSearchResults.length);
}

function clearSearchAndResults() {
  searchInput.value = '';
  lastSearchQuery = '';
  lastSearchResults = [];
  hideQuickResults();
  panelContent.innerHTML = '';
  hidePanel();
  updateSearchControls();
}

async function fetchDatasets(query, options) {
  options = options || {};
  var params = new URLSearchParams();
  if (query) params.set('q', query);
  if (options.semantic) params.set('semantic', '1');
  var url = params.toString() ? '/datasets.json?' + params.toString() : '/datasets.json';
  var payload = await fetchJson(url);
  return payload.datasets || [];
}

function renderDatasetChips(dataset) {
  var chips = [];
  (dataset.geometry_types || []).slice(0, 3).forEach(function(type){
    chips.push('<span class="meta-chip">'+geometryIcon(type)+' '+escapeHtml(type)+'</span>');
  });
  if (dataset.num_features != null) chips.push('<span class="meta-chip">'+formatNumber(dataset.num_features)+' features</span>');
  return chips.join('');
}

function histogramUrl(dataset) {
  return '/datasets/'+encodeURIComponent(dataset.id || dataset.name)+'/histogram.png?size=128';
}

function scheduleUrlUpdate() {
  clearTimeout(urlUpdateTimer);
  urlUpdateTimer = setTimeout(function(){ updateUrl({history:'replace'}); }, 150);
}

function updateUrl(options) {
  options = options || {};
  if (isApplyingUrl || !map) return;
  if (skipNextUrlUpdate) {
    skipNextUrlUpdate = false;
    return;
  }
  var c = map.getCenter();
  var datasetName = currentDatasetInfo ? currentDatasetInfo.name : '';
  var token = encodeURIComponent(datasetName) + '@' + [c.lat.toFixed(coordinateDecimalsForZoom(map.getZoom())), c.lng.toFixed(coordinateDecimalsForZoom(map.getZoom())), map.getZoom().toFixed(2)].join(',');
  var params = new URLSearchParams();
  if (options.search) params.set('q', options.search);
  var suffix = params.toString();
  var url = window.location.pathname + '?' + token + (suffix ? '&' + suffix : '');
  if (url === window.location.pathname + window.location.search) return;
  if (options.history === 'push') window.history.pushState({}, '', url);
  else window.history.replaceState({}, '', url);
}

async function applyUrlState() {
  isApplyingUrl = true;
  try {
    var state = parseUrlState();
    if (state.center && map) {
      skipNextUrlUpdate = true;
      map.jumpTo({center:state.center, zoom:state.zoom || map.getZoom()});
    }
    if (state.dataset) {
      await selectDataset(state.dataset, {history:'replace', fitBounds:false});
    } else if (state.query) {
      searchInput.value = state.query;
      await runSemanticSearch(state.query);
    }
  } finally {
    isApplyingUrl = false;
  }
}

function parseUrlState() {
  var params = new URLSearchParams(window.location.search);
  var token = window.location.search.slice(1).split('&')[0] || '';
  var dataset = '';
  var center = null;
  var zoom = null;
  if (token && token.indexOf('=') === -1 && token.indexOf('@') !== -1) {
    var parts = token.split('@');
    dataset = safeDecodeURIComponent(parts[0] || '');
    var loc = (parts[1] || '').split(',').map(Number);
    if (loc.length === 3 && loc.every(function(v){return isFinite(v);})) {
      center = [loc[1], loc[0]];
      zoom = loc[2];
    }
  } else {
    dataset = params.get('dataset') || '';
    var hashParams = new URLSearchParams(window.location.hash.slice(1));
    var cs = hashParams.get('center');
    if (cs) {
      var pts = cs.split(',').map(Number);
      if (pts.length === 2 && isFinite(pts[0]) && isFinite(pts[1])) center = [pts[1], pts[0]];
    }
    zoom = parseNumber(params.get('z')) || parseNumber(hashParams.get('zoom'));
  }
  return {dataset:dataset, center:center, zoom:zoom, query:params.get('q') || ''};
}

function coordinateDecimalsForZoom(zoom) {
  if (zoom <= 3) return 1;
  if (zoom <= 6) return 2;
  if (zoom <= 9) return 3;
  if (zoom <= 12) return 4;
  if (zoom <= 15) return 5;
  return 6;
}

function populateAttributeSelect() {
  if (!attributeSelect) return;
  attributeSelect.innerHTML = '<option value="">Default style</option>';
  getAttributeNames().forEach(function(name){
    var option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    attributeSelect.appendChild(option);
  });
}

function populateLabelSelect() {
  if (!labelSelect) return;
  labelSelect.innerHTML = '<option value="">No labels</option>';
  getAttributeNames().forEach(function(name){
    var option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    labelSelect.appendChild(option);
  });
}

function syncStylePanelFromDocument(style) {
  if (!style || !Array.isArray(style.layers)) return;
  var state = styleControlState(style);
  setStyleSelectValue(attributeSelect, state.attribute, state.attribute);
  setStyleSelectValue(document.getElementById('vizType'), state.visualization);
  setColorSchemeControl(state.colors);

  setStyleSelectValue(labelSelect, '');
  setStyleSelectValue(document.getElementById('labelSize'), '12');
  document.getElementById('labelColor').value = '#202124';
  setStyleSelectValue(document.getElementById('labelMinZoom'), '13');
  setStyleSelectValue(document.getElementById('labelBg'), 'white');
  var symbol = style.layers.find(function(layer){ return layer && layer.type === 'symbol'; });
  var labelAttribute = symbol && symbolTextAttribute(symbol.layout && symbol.layout['text-field']);
  setStyleSelectValue(labelSelect, labelAttribute || '', labelAttribute);
  if (symbol) {
    var textSize = symbol.layout && symbol.layout['text-size'];
    if (typeof textSize === 'number') setStyleSelectValue(document.getElementById('labelSize'), String(textSize), String(textSize));
    var textColor = symbol.paint && symbol.paint['text-color'];
    if (typeof textColor === 'string' && isHexColor(textColor)) document.getElementById('labelColor').value = normalizeHexColor(textColor);
    if (typeof symbol.minzoom === 'number') setStyleSelectValue(document.getElementById('labelMinZoom'), String(symbol.minzoom), String(symbol.minzoom)+'+');
    setStyleSelectValue(document.getElementById('labelBg'), labelBackgroundFromSymbol(symbol));
  }
}

function styleControlState(style) {
  var metadata = style.metadata && style.metadata['ucrstar:legend'] || {};
  var preferredAttribute = typeof metadata.property === 'string' ? metadata.property : null;
  var colorEntries = [];
  var sizeEntries = [];
  style.layers.forEach(function(layer){
    var paint = layer && layer.paint || {};
    ['fill-color','line-color','circle-color'].forEach(function(property){
      var expression = paint[property], attribute = expressionProperty(expression);
      if (attribute) colorEntries.push({attribute:attribute, expression:expression});
    });
    ['line-width','circle-radius'].forEach(function(property){
      var expression = paint[property], attribute = expressionProperty(expression);
      if (attribute) sizeEntries.push({attribute:attribute, expression:expression});
    });
  });
  var sizeEntry = findStyleEntry(sizeEntries, preferredAttribute);
  var colorEntry = findStyleEntry(colorEntries, preferredAttribute);
  if (sizeEntry && (!colorEntry || sizeEntry.attribute === colorEntry.attribute || sizeEntry.attribute === preferredAttribute)) {
    return {attribute:sizeEntry.attribute, visualization:'size', colors:colorEntry && expressionColors(colorEntry.expression)};
  }
  if (!colorEntry) return {attribute:'', visualization:'choropleth', colors:null};
  return {
    attribute:colorEntry.attribute,
    visualization:colorEntry.expression[0] === 'match' ? 'categorical' : 'choropleth',
    colors:expressionColors(colorEntry.expression)
  };
}

function findStyleEntry(entries, preferredAttribute) {
  if (preferredAttribute) {
    for (var i=entries.length-1; i>=0; i--) {
      if (entries[i].attribute === preferredAttribute) return entries[i];
    }
  }
  return entries[entries.length-1] || null;
}

function expressionColors(expression) {
  if (!Array.isArray(expression)) return null;
  var colors = [];
  var start = expression[0] === 'step' ? 2 : expression[0] === 'interpolate' ? 4 : 3;
  if (expression[0] === 'match') {
    for (var i=3; i<expression.length-1; i+=2) {
      if (typeof expression[i] === 'string') colors.push(expression[i]);
      else colors = colors.concat(expressionColors(expression[i]) || []);
    }
    var fallback = expression[expression.length-1];
    if (typeof fallback === 'string') colors.push(fallback);
  } else if (expression[0] === 'interpolate' || expression[0] === 'step') {
    for (var j=start; j<expression.length; j+=2) if (typeof expression[j] === 'string') colors.push(expression[j]);
  }
  return colors.length ? colors : null;
}

function symbolTextAttribute(textField) {
  var attribute = expressionProperty(textField);
  if (attribute) return attribute;
  var match = typeof textField === 'string' && textField.match(/^\{([^{}]+)\}$/);
  return match ? match[1] : null;
}

function setColorSchemeControl(colors) {
  var select = document.getElementById('colorScheme');
  if (!select) return;
  Array.prototype.slice.call(select.querySelectorAll('option[data-current-style]')).forEach(function(option){ option.remove(); });
  currentStyleColors = colors && colors.length ? colors.slice(0, 5) : null;
  var scheme = matchingColorScheme(currentStyleColors);
  if (scheme) {
    select.value = scheme;
    return;
  }
  if (currentStyleColors) {
    var option = document.createElement('option');
    option.value = 'current';
    option.textContent = 'Current style';
    option.setAttribute('data-current-style', 'true');
    select.appendChild(option);
    select.value = 'current';
  } else {
    select.value = 'blues';
  }
}

function matchingColorScheme(colors) {
  if (!colors || !colors.length) return null;
  var schemes = ['blues','reds','greens','viridis','rainbow'];
  return schemes.find(function(scheme){
    var ramp = getColorRamp(scheme);
    return colors.slice(0, ramp.length).join('|').toLowerCase() === ramp.slice(0, colors.length).join('|').toLowerCase();
  }) || null;
}

function setStyleSelectValue(select, value, label) {
  if (!select || value === null || value === undefined) return;
  value = String(value);
  var exists = Array.prototype.some.call(select.options, function(option){ return option.value === value; });
  if (!exists && value) {
    var option = document.createElement('option');
    option.value = value;
    option.textContent = label || value;
    option.setAttribute('data-style-value', 'true');
    select.appendChild(option);
  }
  select.value = value;
}

function labelBackgroundFromSymbol(symbol) {
  var width = symbol.paint && Number(symbol.paint['text-halo-width']);
  if (!width) return 'none';
  var color = String(symbol.paint['text-halo-color'] || '').toLowerCase();
  return color.indexOf('0,0,0') !== -1 || color === '#000' || color === '#000000' ? 'dark' : 'white';
}

function isHexColor(value) {
  return /^#[0-9a-f]{3}([0-9a-f]{3})?$/i.test(value);
}

function normalizeHexColor(value) {
  if (value.length !== 4) return value;
  return '#' + value[1]+value[1] + value[2]+value[2] + value[3]+value[3];
}

function getAttributeNames() {
  return currentAttributes.map(function(field){ return field.name; });
}

function buildAttributeCatalog(dataset) {
  var schema = Array.isArray(dataset && dataset.schema) ? dataset.schema : [];
  var summaryAttrs = Array.isArray(dataset && dataset.summary_json && dataset.summary_json.attributes)
    ? dataset.summary_json.attributes
    : [];
  var byName = {};

  schema.forEach(function(field){
    if (!field || !field.name || field.name === 'geometry') return;
    byName[field.name] = clone(field);
  });

  summaryAttrs.forEach(function(field){
    if (!field || !field.name || field.name === 'geometry') return;
    if (byName[field.name]) {
      Object.keys(field).forEach(function(key){
        if (key !== 'name') byName[field.name][key] = field[key];
      });
    } else {
      byName[field.name] = clone(field);
    }
  });

  return Object.keys(byName).map(function(name){ return byName[name]; });
}

function resetToDefaultStyle() {
  if (!map || !currentDatasetInfo) return;
  try {
    if (map.getLayer('fill')) {
      map.setPaintProperty('fill','fill-color',activeDatasetStyle.layers.fill['fill-color']);
      map.setPaintProperty('fill','fill-opacity',activeDatasetStyle.layers.fill['fill-opacity']);
    }
    if (map.getLayer('outline')) {
      map.setPaintProperty('outline','line-color',activeDatasetStyle.layers.line['line-color']);
      map.setPaintProperty('outline','line-width',activeDatasetStyle.layers.line['line-width']);
    }
    if (map.getLayer('points')) {
      map.setPaintProperty('points','circle-color',activeDatasetStyle.layers.circle['circle-color']);
      map.setPaintProperty('points','circle-radius',activeDatasetStyle.layers.circle['circle-radius']);
    }
    if (map.getLayer('lines')) {
      map.setPaintProperty('lines','line-color',activeDatasetStyle.layers.line['line-color']);
      map.setPaintProperty('lines','line-width',activeDatasetStyle.layers.line['line-width']);
    }
  } catch(e) {}
}

function resetStyleToServerDefault() {
  if (!map || !currentDatasetInfo) return;
  var serverDocument = clone(activeDatasetStyle.serverDocument);
  activeDatasetStyle = normalizeDatasetStyle(serverDocument, serverDocument);
  syncStylePanelFromDocument(activeDatasetStyle.document);
  resetToDefaultStyle();
  applyLabelsFromStylePanel();
  renderStyleLegend(activeDatasetStyle.document);
  updateVectorTileAttributes(tileAttributesForStyle(activeDatasetStyle.document));
}

function applyLocalStyleDocument(style) {
  if (!style || style.version !== 8 || !Array.isArray(style.layers)) return false;
  var serverDocument = activeDatasetStyle.serverDocument;
  activeDatasetStyle = normalizeDatasetStyle(style, serverDocument);
  currentTileAttributesKey = tileAttributesKey(tileAttributesForStyle(activeDatasetStyle.document));
  clearDatasetLayer();
  addDatasetSourceAndLayers(currentDatasetInfo.visualization || {}, activeDatasetStyle.layers);
  attachClickableCursors();
  syncStylePanelFromDocument(activeDatasetStyle.document);
  applyLabelsFromStylePanel();
  renderStyleLegend(activeDatasetStyle.document);
  return true;
}

function resetLegend() {
  if (legendEl) {
    legendEl.classList.remove('visible');
    legendEl.scrollTop = 0;
    legendContentEl.innerHTML = '';
  }
}

var _labelAttr = null, _labelSize = 12, _labelColor = '#202124', _labelFrame = null;

function _getCanvas(){ return document.getElementById('label-canvas'); }
function _getCtx(){ var c=_getCanvas(); return c?c.getContext('2d'):null; }
function ensureLabelCanvas() {
  var mapEl = document.getElementById('map');
  if (!mapEl || document.getElementById('label-canvas')) return;
  var canvas = document.createElement('canvas');
  canvas.id = 'label-canvas';
  mapEl.appendChild(canvas);
}
function _resizeLabelCanvas(){ var c=_getCanvas(); if(!c)return; var el=document.getElementById('map'); c.width=el.offsetWidth; c.height=el.offsetHeight; }
function _clearLabels(){ var c=_getCanvas(), ctx=_getCtx(); if(ctx&&c) ctx.clearRect(0,0,c.width,c.height); }
function _scheduleRender(){ if(!_labelFrame) _labelFrame=requestAnimationFrame(_renderLabels); }
function applyLabels(attr, fontSize, color){
  _labelAttr = attr || null; _labelSize = parseInt(fontSize, 10) || 12; _labelColor = color || '#202124';
  if (!_labelAttr) {_clearLabels(); return;}
  _scheduleRender();
  if (map && !map._labelsBound) {
    map._labelsBound = true;
    map.on('render', _scheduleRender);
    map.on('zoomend', _scheduleRender);
  }
}

function applyLabelsFromStylePanel() {
  applyLabels(
    (labelSelect || {value:''}).value,
    (document.getElementById('labelSize') || {value:'12'}).value,
    (document.getElementById('labelColor') || {value:'#202124'}).value
  );
}
function _detachLabelRenderer(){
  if (map && map._labelsBound) {
    map.off('render', _scheduleRender);
    map.off('zoomend', _scheduleRender);
    map._labelsBound = false;
  }
  _clearLabels();
  _labelAttr = null;
}
function _renderLabels() {
  _labelFrame = null;
  var ctx = _getCtx(), canvas = _getCanvas();
  if (!ctx || !canvas || !map || !_labelAttr) {_clearLabels(); return;}
  var minZoomEl = document.getElementById('labelMinZoom');
  if (map.getZoom() < (minZoomEl ? parseFloat(minZoomEl.value) : 0)) {_clearLabels(); return;}
  _resizeLabelCanvas(); _clearLabels();
  var layers = CLICKABLE_LAYERS.filter(function(l){try{return map.getLayer(l);}catch(e){return false;}});
  var features; try { features = map.queryRenderedFeatures({layers:layers}); } catch(e) { return; }
  if (!features || !features.length) return;
  var fs = Math.round(_labelSize * Math.max(0.7, Math.min(2.0, 0.5 + map.getZoom()/12)));
  ctx.font = '600 '+fs+'px system-ui,-apple-system,sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  var seen = {}, placed = [], bgMode = (document.getElementById('labelBg') || {value:'white'}).value;
  features.forEach(function(f){
    var val = f.properties && f.properties[_labelAttr];
    if (val == null || val === '') return;
    var fid = f.id != null ? String(f.id) : JSON.stringify(f.properties).slice(0,60);
    if (seen[fid]) return;
    seen[fid] = true;
    var coord = featureLabelCoordinate(f.geometry);
    if (!coord) return;
    var pt = map.project(coord);
    if (pt.x < 0 || pt.y < 0 || pt.x > canvas.width || pt.y > canvas.height) return;
    var text = String(val), width = ctx.measureText(text).width;
    if (placed.some(function(p){ return !(pt.x+width/2 < p.x1 || pt.x-width/2 > p.x2 || pt.y+fs/2 < p.y1 || pt.y-fs/2 > p.y2); })) return;
    placed.push({x1:pt.x-width/2-4, x2:pt.x+width/2+4, y1:pt.y-fs/2-4, y2:pt.y+fs/2+4});
    if (bgMode !== 'none') {
      ctx.fillStyle = bgMode === 'dark' ? 'rgba(20,20,20,0.78)' : 'rgba(255,255,255,0.88)';
      ctx.fillRect(pt.x-width/2-4, pt.y-fs/2-3, width+8, fs+6);
    } else {
      ctx.strokeStyle = 'rgba(255,255,255,0.92)';
      ctx.lineWidth = 3;
      ctx.strokeText(text, pt.x, pt.y);
    }
    ctx.fillStyle = bgMode === 'dark' ? '#fff' : _labelColor;
    ctx.fillText(text, pt.x, pt.y);
  });
}
function featureLabelCoordinate(g) {
  if (!g) return null;
  if (g.type === 'Point') return g.coordinates;
  if (g.type === 'LineString') return g.coordinates[Math.floor(g.coordinates.length/2)];
  if (g.type === 'MultiLineString') return g.coordinates[0][Math.floor(g.coordinates[0].length/2)];
  var ring = null;
  if (g.type === 'Polygon') ring = g.coordinates[0];
  if (g.type === 'MultiPolygon') ring = g.coordinates[0] && g.coordinates[0][0];
  if (!ring) return null;
  var sx = 0, sy = 0;
  ring.forEach(function(c){ sx += c[0]; sy += c[1]; });
  return [sx/ring.length, sy/ring.length];
}

function applyStyle(){
  if (!map || !currentDatasetInfo) return;
  var attr = (document.getElementById('attributeSelect') || {value:''}).value;
  var viz = (document.getElementById('vizType') || {value:'choropleth'}).value;
  var scheme = (document.getElementById('colorScheme') || {value:'blues'}).value;
  var labelAttr = (document.getElementById('labelSelect') || {value:''}).value;
  if (!attr) {
    resetToDefaultStyle();
    renderStyleLegend(activeDatasetStyle.serverDocument);
  } else {
    var stats = viz === 'categorical' ? getAttributeStats(attr, {forceCategorical:true}) : getAttributeStats(attr);
    if (stats) {
      if (currentGeometryType === 'polygon') _applyPolygonStyle(attr, viz, stats, scheme);
      else if (currentGeometryType === 'point') _applyPointStyle(attr, viz, stats, scheme);
      else if (currentGeometryType === 'line') _applyLineStyle(attr, viz, stats, scheme);
      else {
        if (map.getLayer('fill')) _applyPolygonStyle(attr, viz, stats, scheme);
        if (map.getLayer('points')) _applyPointStyle(attr, viz, stats, scheme);
        if (map.getLayer('lines')) _applyLineStyle(attr, viz, stats, scheme);
      }
    }
  }
  applyLabels(labelAttr, (document.getElementById('labelSize') || {value:'12'}).value, (document.getElementById('labelColor') || {value:'#202124'}).value);
  captureLocalStyleDocument(labelAttr);
  updateVectorTileAttributes(tileAttributesForStyle(activeDatasetStyle.document));
}

function captureLocalStyleDocument(labelAttribute) {
  var documentStyle = clone(activeDatasetStyle.document);
  documentStyle.layers.forEach(function(layer){
    if (!layer || ['fill','line','circle'].indexOf(layer.type) === -1) return;
    var mapLayerId = layer.type === 'fill' ? 'fill' : layer.type === 'circle' ? 'points' : lineMapLayerId(layer);
    if (!map.getLayer(mapLayerId)) return;
    layer.paint = layer.paint || {};
    var properties = Object.keys(Object.assign({}, activeDatasetStyle.layers[layer.type], layer.paint));
    properties.forEach(function(property){
      try {
        var value = map.getPaintProperty(mapLayerId, property);
        if (value !== undefined) layer.paint[property] = clone(value);
      } catch(e) {}
    });
  });
  updateLabelStyleLayer(documentStyle, labelAttribute);
  if (documentStyle.metadata && documentStyle.metadata['ucrstar:legend']) {
    delete documentStyle.metadata['ucrstar:legend'];
  }
  activeDatasetStyle.document = documentStyle;
}

function lineMapLayerId(layer) {
  if (layer.id === 'outline') return 'outline';
  var filterText = JSON.stringify(layer.filter || []);
  return filterText.indexOf('Polygon') !== -1 ? 'outline' : 'lines';
}

function updateLabelStyleLayer(style, labelAttribute) {
  var symbolIndex = style.layers.findIndex(function(layer){ return layer && layer.type === 'symbol'; });
  if (!labelAttribute) {
    if (symbolIndex !== -1) style.layers.splice(symbolIndex, 1);
    return;
  }
  var symbol = symbolIndex === -1 ? {
    id:'labels',
    type:'symbol',
    source:'dataset',
    layout:{},
    paint:{}
  } : style.layers[symbolIndex];
  if (symbolIndex === -1) {
    var visualization = currentDatasetInfo.visualization || {};
    if (visualization.type === 'VectorTile') symbol['source-layer'] = visualization.source_layer || sourceLayer;
    style.layers.push(symbol);
  }
  symbol.minzoom = Number((document.getElementById('labelMinZoom') || {value:0}).value) || 0;
  symbol.layout = Object.assign({}, symbol.layout, {
    'text-field':['get', labelAttribute],
    'text-size':Number((document.getElementById('labelSize') || {value:12}).value) || 12
  });
  var color = (document.getElementById('labelColor') || {value:'#202124'}).value;
  var background = (document.getElementById('labelBg') || {value:'white'}).value;
  symbol.paint = Object.assign({}, symbol.paint, {'text-color':color});
  if (background === 'none') {
    symbol.paint['text-halo-width'] = 0;
    delete symbol.paint['text-halo-color'];
  } else {
    symbol.paint['text-halo-width'] = 2;
    symbol.paint['text-halo-color'] = background === 'dark' ? 'rgba(20,20,20,0.78)' : '#ffffff';
  }
}

function getAttributeStats(attrName, opts){
  opts = opts || {};
  var attr = currentAttributes.find(function(field){ return field.name === attrName; });
  if (!attr) return null;
  var stats = attr.stats && typeof attr.stats === 'object'
    ? Object.assign({}, attr, attr.stats)
    : attr;
  var topK = Array.isArray(stats.top_k) ? stats.top_k : [];
  var categories = topK.map(function(entry){
    return Array.isArray(entry) ? entry[0] : entry && entry.value;
  }).filter(function(value){
    return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean';
  });
  var min = finiteStatistic(stats.min);
  var max = finiteStatistic(stats.max);
  if (opts.forceCategorical) {
    return categories.length ? {
      min:null,
      max:null,
      count:stats.non_null_count || stats.count || categories.length,
      categories:categories
    } : null;
  }
  if (min === null && max === null && !categories.length) return null;
  return {
    min:min,
    max:max,
    count:stats.non_null_count || stats.count || categories.length,
    categories:categories
  };
}

function finiteStatistic(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
function getColorRamp(scheme){
  var ramps = {reds:['#fee5d9','#fcae91','#fb6a4a','#de2d26','#a50f15'],greens:['#e5f5e0','#a1d99b','#74c476','#31a354','#006d2c'],viridis:['#440154','#414487','#2a788e','#22a884','#7ad151'],rainbow:['#440154','#3b528b','#21918c','#5ec962','#fde725'],blues:['#eff3ff','#bdd7e7','#6baed6','#3182bd','#08519c']};
  if (scheme === 'current' && currentStyleColors && currentStyleColors.length) {
    var current = currentStyleColors.slice();
    while (current.length < 5) current.push(current[current.length-1]);
    return current;
  }
  return ramps[scheme] || ramps.blues;
}
function buildChoroplethExpression(attr, stats, scheme){
  var c = getColorRamp(scheme), mn = stats.min, mx = stats.max;
  if (!Number.isFinite(mn) || !Number.isFinite(mx) || mn === mx) return null;
  var s = (mx-mn)/4;
  return ['interpolate',['linear'],['to-number',['get',attr]],mn,c[0],mn+s,c[1],mn+2*s,c[2],mn+3*s,c[3],mx,c[4]];
}
function buildSizeExpression(attr, stats, base){
  if (!Number.isFinite(stats.min) || !Number.isFinite(stats.max) || stats.min === stats.max) return null;
  return ['interpolate',['linear'],['to-number',['get',attr]],stats.min,base*0.6,stats.max,base*2.2];
}
function buildCategoricalExpression(attr, stats, scheme){
  var c = getColorRamp(scheme), cats = stats.categories || [];
  if (!cats.length) return null;
  var e = ['match',['to-string',['get',attr]]];
  cats.forEach(function(x, i){ e.push(String(x), c[i%c.length]); });
  e.push('#9e9e9e');
  return e;
}
function _applyPolygonStyle(attr, viz, stats, scheme){
  if (!map.getLayer('fill')) return;
  var e = viz === 'categorical' ? buildCategoricalExpression(attr, stats, scheme) : buildChoroplethExpression(attr, stats, scheme);
  if (viz === 'size') e = buildChoroplethExpression(attr, stats, scheme);
  if (!e) return;
  map.setPaintProperty('fill','fill-color',e);
  map.setPaintProperty('fill','fill-opacity',0.8);
  if (map.getLayer('outline')) map.setPaintProperty('outline','line-color',e);
  viz === 'categorical' ? updateLegendCategorical(attr,stats,scheme) : updateLegendChoropleth(attr,stats,scheme);
}
function _applyPointStyle(attr, viz, stats, scheme){
  if (!map.getLayer('points')) return;
  if (viz === 'size') {
    var size = buildSizeExpression(attr, stats, 5);
    if (size) map.setPaintProperty('points','circle-radius',size);
    updateLegendChoropleth(attr,stats,scheme);
    return;
  }
  var e = viz === 'categorical' ? buildCategoricalExpression(attr, stats, scheme) : buildChoroplethExpression(attr, stats, scheme);
  if (!e) return;
  map.setPaintProperty('points','circle-color',e);
  viz === 'categorical' ? updateLegendCategorical(attr,stats,scheme) : updateLegendChoropleth(attr,stats,scheme);
}
function _applyLineStyle(attr, viz, stats, scheme){
  if (!map.getLayer('lines')) return;
  if (viz === 'size') {
    var size = buildSizeExpression(attr, stats, 3);
    if (size) map.setPaintProperty('lines','line-width',size);
    updateLegendChoropleth(attr,stats,scheme);
    return;
  }
  var e = viz === 'categorical' ? buildCategoricalExpression(attr, stats, scheme) : buildChoroplethExpression(attr, stats, scheme);
  if (!e) return;
  map.setPaintProperty('lines','line-color',e);
  viz === 'categorical' ? updateLegendCategorical(attr,stats,scheme) : updateLegendChoropleth(attr,stats,scheme);
}
function updateLegendChoropleth(attr, stats, scheme){
  if (!legendEl || !isFinite(stats.min) || !isFinite(stats.max)) return;
  legendContentEl.innerHTML = '';
  var c = getColorRamp(scheme), s = (stats.max-stats.min)/4;
  [stats.min,stats.min+s,stats.min+2*s,stats.min+3*s,stats.max].forEach(function(v, i){
    var el = document.createElement('div');
    el.className = 'legend-item';
    el.innerHTML = '<div class="legend-color" style="background:'+c[i]+'"></div><span>'+v.toFixed(2)+'</span>';
    legendContentEl.appendChild(el);
  });
  legendEl.classList.add('visible');
  legendEl.querySelector('.legend-title').textContent = attr;
}
function updateLegendCategorical(attr, stats, scheme){
  if (!legendEl) return;
  legendContentEl.innerHTML = '';
  var c = getColorRamp(scheme);
  (stats.categories || []).forEach(function(x, i){
    var el = document.createElement('div');
    el.className = 'legend-item';
    el.innerHTML = '<div class="legend-color" style="background:'+c[i%c.length]+'"></div><span>'+escapeHtml(x)+'</span>';
    legendContentEl.appendChild(el);
  });
  legendEl.classList.add('visible');
  legendEl.querySelector('.legend-title').textContent = attr;
}

var gotoInput = document.getElementById('gotoInput');
var gotoBtn = document.getElementById('gotoBtn');
var gotoResults = document.getElementById('gotoResults');
function goToLocation(){
  var input = gotoInput.value.trim();
  if (!input) return;
  var m = input.match(/^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/);
  if (m) {
    var la = parseFloat(m[1]), ln = parseFloat(m[2]);
    if (la >= -90 && la <= 90 && ln >= -180 && ln <= 180) {
      map.flyTo({center:[ln,la], zoom:12, speed:1.4, curve:1.42});
      gotoInput.value = '';
      return;
    }
  }
  gotoBtn.disabled = true;
  gotoBtn.textContent = '...';
  fetch('https://nominatim.openstreetmap.org/search?format=json&limit=6&q='+encodeURIComponent(input), {headers:{'Accept-Language':'en'}})
    .then(function(r){ return r.json(); })
    .then(function(data){
      gotoBtn.disabled = false; gotoBtn.textContent = 'Go';
      renderGeoResults(data || []);
    })
    .catch(function(){ gotoBtn.disabled = false; gotoBtn.textContent = 'Go'; });
}
function renderGeoResults(results){
  if (!gotoResults) return;
  if (!results.length) {
    gotoResults.innerHTML = '<div class="goto-no-result">No results found</div>';
    gotoResults.classList.add('visible');
    return;
  }
  gotoResults.innerHTML = results.slice(0,6).map(function(r){
    var label = r.display_name || (r.lat+', '+r.lon);
    return '<div class="goto-result-item" data-lat="'+escapeHtml(r.lat)+'" data-lon="'+escapeHtml(r.lon)+'" data-label="'+escapeHtml(label)+'"><span class="goto-result-icon">&#9679;</span><span class="goto-result-text">'+escapeHtml(truncateText(label, 60))+'</span></div>';
  }).join('');
  gotoResults.classList.add('visible');
  gotoResults.querySelectorAll('.goto-result-item').forEach(function(el){
    el.addEventListener('click', function(){
      map.flyTo({center:[parseFloat(el.dataset.lon), parseFloat(el.dataset.lat)], zoom:12, speed:1.4, curve:1.42});
      gotoInput.value = el.dataset.label;
      gotoResults.classList.remove('visible');
    });
  });
}
gotoBtn.addEventListener('click', goToLocation);
gotoInput.addEventListener('keypress', function(e){ if(e.key === 'Enter') goToLocation(); });

var darkToggle = document.getElementById('darkToggle');
if (localStorage.getItem('ucrstar-dark-mode') === 'on') { document.body.classList.add('dark'); darkToggle.innerHTML = '&#9728;'; }
darkToggle.addEventListener('click', function(){
  var dark = document.body.classList.toggle('dark');
  darkToggle.innerHTML = dark ? '&#9728;' : '&#127769;';
  localStorage.setItem('ucrstar-dark-mode', dark ? 'on' : 'off');
});

function toggleStylePanel() {
  if (!currentDatasetInfo) { alert('Select a dataset first'); return; }
  if (!stylePanelEl.classList.contains('visible')) syncStylePanelFromDocument(activeDatasetStyle.document);
  stylePanelEl.classList.toggle('visible');
}

window.selectDataset = selectDataset;
window.clearFilters = function(){
  currentDataset = null;
  currentDatasetInfo = null;
  currentDatasetBounds = null;
  currentAttributes = [];
  clearDatasetLayer();
  resetLegend();
  updateZoomAllState();
  panelKicker.textContent = 'UCR★STAR';
  panelTitle.textContent = '';
  panelContent.innerHTML = '';
  searchInput.value = '';
  lastSearchQuery = '';
  lastSearchResults = [];
  hideQuickResults();
  hidePanel();
  updateSearchControls();
  updateUrl({history:'replace'});
};
window.toggleStylePanel = toggleStylePanel;
window.applyStyle = applyStyle;
window.resetStyleToServerDefault = resetStyleToServerDefault;
window.downloadDataset = downloadDataset;

function fetchJson(url) {
  return fetch(url).then(function(response){
    if (!response.ok) throw new Error(response.status + ' ' + response.statusText);
    return response.json();
  });
}
function clone(value){ return JSON.parse(JSON.stringify(value)); }
function safeDecodeURIComponent(value){ try { return decodeURIComponent(value); } catch(e) { return value; } }
function parseNumber(value){ var n = Number(value); return isFinite(n) ? n : null; }
function truncateText(value, limit){ value = String(value || ''); return value.length > limit ? value.slice(0, Math.max(0, limit-3)).trimEnd() + '...' : value; }
function describeDataset(dataset) {
  var parts = [];
  if (dataset.num_features != null) parts.push(formatNumber(dataset.num_features)+' features');
  if (dataset.size_bytes != null) parts.push(formatMegabytes(dataset.size_bytes));
  return parts.join(' . ');
}
function formatNumber(value){ if (value == null || value === '') return ''; var n = Number(value); return isFinite(n) ? n.toLocaleString() : String(value); }
function formatMegabytes(value){ if (value == null || value === '') return ''; var mb = Number(value) / (1024*1024); return isFinite(mb) ? mb.toLocaleString(undefined, {maximumFractionDigits:1}) + ' MB' : String(value); }
function formatValue(value){ if (value == null) return ''; if (typeof value === 'object') return JSON.stringify(value); return String(value); }
function formatDateTime(value){ var d = new Date(value); return isNaN(d.getTime()) ? value : d.toLocaleString(); }
function escapeHtml(value){
  return String(value)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#039;');
}

(function(){
  var SESSION_STORAGE_KEY = 'ucrstar-ai-session-id';
  var _available = false;
  var _busy = false;
  var _sessionId = loadSavedSession();

  function addMsg(type, content){
    var box = document.getElementById('aiMsgs');
    if (!box) return null;
    var el = document.createElement('div');
    el.className = 'ai-msg ' + type;
    el.innerHTML = escapeHtml(content)
      .replace(/```json([\s\S]*?)```/g,'<pre>$1</pre>')
      .replace(/```([\s\S]*?)```/g,'<pre>$1</pre>')
      .replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>')
      .replace(/`([^`]+)`/g,'<code>$1</code>')
      .replace(/\n/g,'<br>');
    box.appendChild(el);
    box.scrollTop = box.scrollHeight;
    return el;
  }

  function replaceMessages(type, content){
    var box = document.getElementById('aiMsgs');
    if (box) box.innerHTML = '';
    addMsg(type, content);
  }

  function loadSavedSession(){
    try { return localStorage.getItem(SESSION_STORAGE_KEY); } catch(e) { return null; }
  }

  function saveSession(sessionId){
    _sessionId = sessionId || null;
    try {
      if (_sessionId) localStorage.setItem(SESSION_STORAGE_KEY, _sessionId);
      else localStorage.removeItem(SESSION_STORAGE_KEY);
    } catch(e) {}
  }

  function setAvailable(available, reason){
    _available = Boolean(available);
    var fab = document.getElementById('aiFab');
    var input = document.getElementById('aiIn');
    var send = document.getElementById('aiSendBtn');
    var reset = document.getElementById('aiResetBtn');
    var dot = document.getElementById('aiStatusDot');
    if (fab) {
      fab.disabled = !_available;
      fab.title = _available ? 'AI Assistant' : (reason || 'AI Assistant is unavailable');
    }
    if (input) input.disabled = !_available;
    if (send) send.disabled = !_available;
    if (reset) reset.disabled = !_available;
    document.querySelectorAll('.ai-chip').forEach(function(chip){ chip.disabled = !_available; });
    if (dot) {
      dot.className = 'ai-status-dot ' + (_available ? 'ok' : 'err');
      dot.title = _available ? 'Server LLM is configured' : (reason || 'Server LLM is unavailable');
    }
  }

  function setBusy(busy){
    _busy = busy;
    var input = document.getElementById('aiIn');
    var send = document.getElementById('aiSendBtn');
    if (input) input.disabled = busy || !_available;
    if (send) send.disabled = busy || !_available;
    document.querySelectorAll('.ai-chip').forEach(function(chip){ chip.disabled = busy || !_available; });
  }

  function populateModels(capabilities){
    var select = document.getElementById('aiModelSelect');
    if (!select) return;
    select.innerHTML = '';
    (capabilities.models || []).forEach(function(model){
      var option = document.createElement('option');
      option.value = model.id;
      option.textContent = model.label + (model.provider ? ' · ' + model.provider : '');
      select.appendChild(option);
    });
    if (capabilities.default_model) select.value = capabilities.default_model;
    select.disabled = !_available || select.options.length <= 1;
  }

  async function initialize(){
    try {
      var capabilities = await fetchJson('/llm/capabilities.json');
      setAvailable(capabilities.available, capabilities.reason);
      populateModels(capabilities);
      var sub = document.getElementById('aiHeadSub');
      var model = (capabilities.models || []).find(function(item){ return item.id === capabilities.default_model; });
      if (sub) sub.textContent = capabilities.available
        ? ((model && model.provider) || 'Server') + ' · server managed'
        : 'Unavailable';
      replaceMessages(
        capabilities.available ? 'info' : 'err',
        capabilities.available
          ? (_sessionId ? 'Continuing your saved chat session.' : 'Ask about datasets, maps, or visualization.')
          : (capabilities.reason || 'The server does not have LLM chat configured.')
      );
    } catch(e) {
      setAvailable(false, 'Could not read server LLM capabilities');
      populateModels({models:[]});
      replaceMessages('err', 'Could not read server LLM capabilities.');
    }
  }

  function getContext(){
    var context = {
      basemap:basemapMode,
      search_query:lastSearchQuery || searchInput.value.trim()
    };
    if (currentDatasetInfo) context.dataset_id = currentDatasetInfo.id || currentDatasetInfo.name;
    if (activeDatasetStyle && activeDatasetStyle.document) context.style = clone(activeDatasetStyle.document);
    if (map) {
      var bounds = map.getBounds();
      var center = map.getCenter();
      context.viewport = {
        bounds:[bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()],
        center:[center.lng, center.lat],
        zoom:map.getZoom()
      };
    }
    return context;
  }

  window.aiToggle = function(){
    if (!_available) return;
    var p = document.getElementById('aiPanel');
    p.classList.toggle('open');
  };

  window.aiQuick = function(type){
    if (!_available || _busy) return;
    var prompts = {
      describe:'What kind of data does this dataset contain?',
      suggest:'What is the best way to visualize this dataset?',
      best:'Which numeric attribute would make the best choropleth map and why?',
      insight:'Analyze the features currently visible on the map.',
      anomaly:'Are there any outliers or anomalies in the visible data?'
    };
    var msg = prompts[type] || type;
    addMsg('user', msg);
    callServer(msg);
  };

  window.aiSend = function(){
    var input = document.getElementById('aiIn');
    var msg = input ? input.value.trim() : '';
    if (!msg || !_available || _busy) return;
    input.value = '';
    addMsg('user', msg);
    callServer(msg);
  };

  async function callServer(userMsg){
    var model = (document.getElementById('aiModelSelect') || {value:''}).value;
    var thinking = addMsg('thinking', 'Thinking...');
    setBusy(true);
    try {
      var data = null;
      for (var attempt=0; attempt<2; attempt++) {
        var payload = {message:userMsg, model_id:model, context:getContext()};
        if (_sessionId) payload.session_id = _sessionId;
        var response = await fetch('/llm/chat.json', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify(payload)
        });
        data = await response.json().catch(function(){ return {}; });
        if (response.status === 404 && _sessionId && attempt === 0) {
          saveSession(null);
          continue;
        }
        if (!response.ok) throw new Error(data.error || (response.status + ' ' + response.statusText));
        break;
      }
      if (!data || !data.message) throw new Error('The server returned an invalid chat response');
      saveSession(data.session_id);
      addMsg('bot', data.message.content || '');
      handleActions(data.actions || []);
    } catch(e) {
      addMsg('err', 'Chat error: '+e.message);
    } finally {
      if (thinking) thinking.remove();
      setBusy(false);
    }
  }

  function handleActions(actions){
    actions.forEach(function(action){
      if (!action || !action.type) return;
      if (action.type === 'show_datasets' && Array.isArray(action.datasets)) {
        lastSearchQuery = action.query || '';
        lastSearchResults = action.datasets;
        if (action.query) searchInput.value = action.query;
        panelKicker.textContent = 'Search results';
        panelTitle.textContent = action.query ? 'Results for "'+action.query+'"' : 'Suggested datasets';
        renderSearchResults(lastSearchResults);
        showPanel();
        updateSearchControls();
      } else if (action.type === 'change_basemap' && ['street','satellite'].indexOf(action.basemap) !== -1) {
        basemapMode = action.basemap;
        updateBasemapMode();
      }
    });
  }

  function resetChat(){
    saveSession(null);
    replaceMessages('info', 'New chat. A session will be created with your next message.');
  }

  var fab = document.getElementById('aiFab');
  var aiIn = document.getElementById('aiIn');
  var reset = document.getElementById('aiResetBtn');
  var modelSelect = document.getElementById('aiModelSelect');
  if (fab) fab.addEventListener('click', window.aiToggle);
  if (aiIn) aiIn.addEventListener('keydown', function(e){ if (e.key === 'Enter') window.aiSend(); });
  if (reset) reset.addEventListener('click', resetChat);
  if (modelSelect) modelSelect.addEventListener('change', resetChat);
  initialize();
})();
