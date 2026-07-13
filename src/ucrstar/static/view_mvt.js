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
  sourceLayer = activeDatasetStyle.source_layer || SOURCE_LAYER;

  var tileRef = dataset.id || dataset.name;
  map.addSource(DATASET_SOURCE, {
    type:'vector',
    tiles:[window.location.origin+'/datasets/'+encodeURIComponent(tileRef)+'/tiles/{z}/{x}/{y}.mvt'],
    minzoom:0,
    maxzoom:14
  });
  addStyledLayer({id:'fill', type:'fill', source:DATASET_SOURCE, 'source-layer':sourceLayer, filter:['any',['==',['geometry-type'],'Polygon'],['==',['geometry-type'],'MultiPolygon']], paint:activeDatasetStyle.layers.fill}, FALLBACK_STYLE.layers.fill);
  addStyledLayer({id:'outline', type:'line', source:DATASET_SOURCE, 'source-layer':sourceLayer, filter:['any',['==',['geometry-type'],'Polygon'],['==',['geometry-type'],'MultiPolygon']], paint:activeDatasetStyle.layers.line}, FALLBACK_STYLE.layers.line);
  addStyledLayer({id:'points', type:'circle', source:DATASET_SOURCE, 'source-layer':sourceLayer, filter:['==',['geometry-type'],'Point'], paint:activeDatasetStyle.layers.circle}, FALLBACK_STYLE.layers.circle);
  addStyledLayer({id:'lines', type:'line', source:DATASET_SOURCE, 'source-layer':sourceLayer, filter:['any',['==',['geometry-type'],'LineString'],['==',['geometry-type'],'MultiLineString']], paint:activeDatasetStyle.layers.line}, FALLBACK_STYLE.layers.line);

  attachClickableCursors();
  populateAttributeSelect();
  populateLabelSelect();

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

async function fetchDatasetStyle(dataset) {
  try {
    var style = await fetchJson('/datasets/'+encodeURIComponent(dataset.id || dataset.name)+'/style.json');
    return normalizeDatasetStyle(style);
  } catch(e) {
    return clone(FALLBACK_STYLE);
  }
}

function normalizeDatasetStyle(style) {
  var normalized = clone(FALLBACK_STYLE);
  if (style && style.source_layer) normalized.source_layer = style.source_layer;
  if (!style || !style.layers) return normalized;
  ['fill','line','circle'].forEach(function(type){
    var paint = normalizeLayerPaint(style.layers[type], type);
    Object.keys(paint).forEach(function(key){ normalized.layers[type][key] = paint[key]; });
  });
  return normalized;
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

  var body = document.createElement('div');
  body.className = 'popup-body';
  container.appendChild(body);

  function renderRows() {
    var query = search.value.trim();
    var regexMode = regexBtn.getAttribute('aria-pressed') === 'true';
    var filtered = rows;
    var invalidRegex = false;
    if (query) {
      if (regexMode) {
        try {
          var re = new RegExp(query, 'i');
          filtered = rows.filter(function(row){ return re.test(row.key) || re.test(String(formatValue(row.value))); });
        } catch(error) {
          invalidRegex = true;
          filtered = [];
        }
      } else {
        var needle = query.toLowerCase();
        filtered = rows.filter(function(row){ return row.text.toLowerCase().indexOf(needle) !== -1; });
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
  regexBtn.addEventListener('click', function(){
    var pressed = regexBtn.getAttribute('aria-pressed') === 'true';
    regexBtn.setAttribute('aria-pressed', String(!pressed));
    regexBtn.classList.toggle('active', !pressed);
    renderRows();
    search.focus();
  });

  renderRows();
  if (activePopup) activePopup.remove();
  activePopupState = {search: search, regexBtn: regexBtn, body: body};
  activePopup = new maplibregl.Popup({maxWidth:'420px',closeButton:true}).setLngLat(lngLat).setDOMContent(container).addTo(map);
  activePopup.on('close', function(){ activePopupState = null; });
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
  if (attributeSelect) attributeSelect.value = '';
  if (labelSelect) labelSelect.value = '';
  if (document.getElementById('vizType')) document.getElementById('vizType').value = 'choropleth';
  if (document.getElementById('colorScheme')) document.getElementById('colorScheme').value = 'blues';
  if (document.getElementById('labelSize')) document.getElementById('labelSize').value = '12';
  if (document.getElementById('labelColor')) document.getElementById('labelColor').value = '#202124';
  if (document.getElementById('labelMinZoom')) document.getElementById('labelMinZoom').value = '13';
  if (document.getElementById('labelBg')) document.getElementById('labelBg').value = 'white';
  resetToDefaultStyle();
  resetLegend();
}

function resetLegend() {
  if (legendEl) {
    legendEl.classList.remove('visible');
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
    resetLegend();
  } else {
    var stats = viz === 'categorical' ? getAttributeStats(attr, {forceCategorical:true}) : getAttributeStats(attr);
    if (!stats || (stats.min === null && (!stats.categories || !stats.categories.length))) stats = _buildSyntheticStats(attr, viz);
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
}

function _buildSyntheticStats(attrName, viz){
  if (!map) return null;
  try {
    var features = map.queryRenderedFeatures({layers:CLICKABLE_LAYERS.filter(function(l){return map.getLayer(l);})});
    var nums = [], cats = [], seen = {};
    features.forEach(function(f){
      var v = f.properties && f.properties[attrName];
      if (v == null || v === '') return;
      var s = String(v);
      if (!seen[s]) { seen[s] = true; cats.push(s); }
      var n = parseFloat(v);
      if (!isNaN(n)) nums.push(n);
    });
    if (!cats.length) return null;
    if (viz === 'categorical' || nums.length < cats.length*0.6) return {min:null,max:null,count:cats.length,categories:cats.slice(0,8)};
    var mn = Math.min.apply(null, nums), mx = Math.max.apply(null, nums);
    if (mn === mx) { mn = mn*0.9 || 0; mx = mx*1.1 || 1; }
    return {min:mn,max:mx,count:nums.length,categories:[]};
  } catch(e) { return null; }
}

function getAttributeStats(attrName, opts){
  opts = opts || {};
  var attr = currentAttributes.find(function(field){ return field.name === attrName; });
  if (attr && attr.stats) {
    var stats = attr.stats;
    var topK = stats.top_k || [];
    if (opts.forceCategorical) return {
      min: null,
      max: null,
      count: stats.non_null_count || topK.length,
      categories: topK.map(function(t){
        return String(Array.isArray(t) ? t[0] : t && t.value);
      }).filter(function(value){ return value && value !== 'undefined'; }).slice(0, 8)
    };
    if (isFinite(stats.min) || isFinite(stats.max) || topK.length) {
      return {
        min: isFinite(stats.min) ? stats.min : null,
        max: isFinite(stats.max) ? stats.max : null,
        count: stats.non_null_count || stats.count || topK.length,
        categories: topK.map(function(t){
          return String(Array.isArray(t) ? t[0] : t && t.value);
        }).filter(function(value){ return value && value !== 'undefined'; }).slice(0, 8)
      };
    }
  }
  return _buildSyntheticStats(attrName, opts.forceCategorical ? 'categorical' : 'choropleth');
}
function getColorRamp(scheme){
  var ramps = {reds:['#fee5d9','#fcae91','#fb6a4a','#de2d26','#a50f15'],greens:['#e5f5e0','#a1d99b','#74c476','#31a354','#006d2c'],viridis:['#440154','#414487','#2a788e','#22a884','#7ad151'],rainbow:['#440154','#3b528b','#21918c','#5ec962','#fde725'],blues:['#eff3ff','#bdd7e7','#6baed6','#3182bd','#08519c']};
  return ramps[scheme] || ramps.blues;
}
function buildChoroplethExpression(attr, stats, scheme){
  var c = getColorRamp(scheme), mn = stats.min, mx = stats.max;
  if (!isFinite(mn) || !isFinite(mx) || mn === mx) return null;
  var s = (mx-mn)/4;
  return ['interpolate',['linear'],['to-number',['get',attr]],mn,c[0],mn+s,c[1],mn+2*s,c[2],mn+3*s,c[3],mx,c[4]];
}
function buildSizeExpression(attr, stats, base){
  if (!isFinite(stats.min) || !isFinite(stats.max) || stats.min === stats.max) return null;
  return ['interpolate',['linear'],['to-number',['get',attr]],stats.min,base*0.6,stats.max,base*2.2];
}
function buildCategoricalExpression(attr, stats, scheme){
  var c = getColorRamp(scheme), cats = stats.categories || [];
  if (!cats.length) return null;
  var e = ['match',['get',attr]];
  cats.forEach(function(x, i){ e.push(x, c[i%c.length]); });
  e.push('#d3d3d3');
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
  var OLLAMA = 'http://localhost:11434';
  var _hist = [];
  var _pending = null;
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
  function ping(){
    fetch(OLLAMA+'/api/tags').then(function(r){
      var dot = document.getElementById('aiStatusDot');
      if (!dot) return;
      dot.className = r.ok ? 'ai-status-dot ok' : 'ai-status-dot err';
      dot.title = r.ok ? 'Ollama is running' : 'Ollama not responding';
    }).catch(function(){
      var dot = document.getElementById('aiStatusDot');
      if (dot) { dot.className = 'ai-status-dot err'; dot.title = 'Ollama not found at localhost:11434'; }
    });
  }
  function getCtx(){
    if (!currentDatasetInfo) return 'No dataset selected.';
    return 'Dataset: '+currentDatasetInfo.name+'\nGeometry type: '+(currentGeometryType || 'unknown')+'\nAttributes: '+getAttributeNames().join(', ');
  }
  window.aiToggle = function(){
    var p = document.getElementById('aiPanel');
    var wasOpen = p.classList.contains('open');
    p.classList.toggle('open');
    if (!wasOpen) ping();
  };
  window.aiQuick = function(type){
    var prompts = {
      describe:'What kind of data does this dataset contain?',
      suggest:'What is the best way to visualize this dataset? Include style JSON.',
      best:'Which numeric attribute would make the best choropleth map and why? Include style JSON.',
      insight:'Analyze the features currently visible on the map.',
      anomaly:'Are there any outliers or anomalies in the visible data?'
    };
    var msg = prompts[type] || type;
    addMsg('user', msg);
    callOllama(msg);
  };
  window.aiSend = function(){
    var input = document.getElementById('aiIn');
    var msg = input ? input.value.trim() : '';
    if (!msg) return;
    input.value = '';
    addMsg('user', msg);
    callOllama(msg);
  };
  async function callOllama(userMsg){
    var model = (document.getElementById('aiModelSelect') || {value:'llama3.2'}).value || 'llama3.2';
    var thinking = addMsg('thinking', 'Thinking with '+model+'...');
    _hist.push({role:'user', content:userMsg});
    try {
      var res = await fetch(OLLAMA+'/api/chat', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({
          model:model,
          messages:[{role:'system', content:'You are a GIS visualization assistant. Suggest JSON style blocks when useful.'},{role:'user', content:'Current map context:\n'+getCtx()}].concat(_hist.slice(-10)),
          stream:false
        })
      });
      if (thinking) thinking.remove();
      if (!res.ok) throw new Error(res.status + ' ' + res.statusText);
      var data = await res.json();
      var reply = data.message && data.message.content || '';
      _hist.push({role:'assistant', content:reply});
      addMsg('bot', reply);
      var match = reply.match(/```json([\s\S]*?)```/);
      _pending = match ? JSON.parse(match[1].trim()) : null;
      var apply = document.getElementById('aiApplyBtn');
      if (apply) apply.style.display = _pending ? 'block' : 'none';
    } catch(e) {
      if (thinking) thinking.remove();
      addMsg('err', 'Ollama error: '+e.message);
      _hist.pop();
    }
  }
  window.aiApplyStyle = function(){
    if (!_pending || !currentDatasetInfo) return;
    function setSelect(id, value){
      var el = document.getElementById(id);
      if (!el || !value) return;
      for (var i=0; i<el.options.length; i++) {
        if (el.options[i].value.toLowerCase() === String(value).toLowerCase()) {
          el.selectedIndex = i;
          return;
        }
      }
    }
    setSelect('attributeSelect', _pending.attribute);
    setSelect('vizType', _pending.viz);
    setSelect('colorScheme', _pending.scheme);
    setSelect('labelSelect', _pending.labelAttr);
    setSelect('labelBg', _pending.labelBg);
    applyStyle();
    addMsg('info', 'Style applied.');
    _pending = null;
  };
  var fab = document.getElementById('aiFab');
  var aiIn = document.getElementById('aiIn');
  if (fab) fab.addEventListener('click', window.aiToggle);
  if (aiIn) aiIn.addEventListener('keydown', function(e){ if (e.key === 'Enter') window.aiSend(); });
})();
