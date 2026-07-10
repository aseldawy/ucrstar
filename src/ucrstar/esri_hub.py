from __future__ import annotations

import json
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterator

try:
    import certifi
except ImportError:  # pragma: no cover - certifi is a project dependency
    certifi = None


ARCGIS_ITEM_ID_RE = re.compile(r"^([0-9a-fA-F]{32})(?:_(\d+))?$")
ARCGIS_ITEM_URL = "https://www.arcgis.com/sharing/rest/content/items/{item_id}"


@dataclass(frozen=True)
class HubDataset:
    """A dataset record returned by an ArcGIS Hub OGC search collection."""

    record: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.record.get("id") or "")

    @property
    def properties(self) -> dict[str, Any]:
        return self.record.get("properties") or {}

    @property
    def item_id(self) -> str:
        item_id, _layer_id = split_record_id(self.id)
        return item_id or str(self.properties.get("id") or self.id)

    @property
    def layer_id(self) -> int | None:
        _item_id, layer_id = split_record_id(self.id)
        return layer_id

    @property
    def title(self) -> str:
        return str(self.properties.get("title") or self.id)

    @property
    def item_type(self) -> str:
        return str(self.properties.get("type") or "")

    @property
    def service_url(self) -> str | None:
        return self.properties.get("url")

    @property
    def download_formats(self) -> list[str]:
        downloads = self.properties.get("properties") or {}
        downloads = downloads.get("downloads") or {}
        formats = []
        for entry in downloads.get("formats") or []:
            if entry.get("hidden"):
                continue
            key = entry.get("key")
            if key:
                formats.append(str(key))
        return formats


@dataclass(frozen=True)
class DownloadLink:
    format: str
    url: str
    method: str = "GET"
    note: str | None = None


class EsriHubClient:
    """Small helper for exploring an ArcGIS Hub site's search and download APIs."""

    def __init__(self, site_url: str, *, timeout: int = 60) -> None:
        parsed = urllib.parse.urlparse(site_url)
        if not parsed.scheme:
            site_url = f"https://{site_url}"
            parsed = urllib.parse.urlparse(site_url)
        if not parsed.netloc:
            raise ValueError(f"Invalid Esri Hub URL: {site_url}")
        self.site_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")
        self.timeout = timeout

    @property
    def search_base_url(self) -> str:
        return f"{self.site_url}/api/search/v1"

    @property
    def download_base_url(self) -> str:
        return f"{self.site_url}/api/download/v1"

    def landing(self) -> dict[str, Any]:
        return self.get_json(self.search_base_url)

    def openapi_definition(self) -> dict[str, Any]:
        return self.get_json(f"{self.site_url}/api/search/definition/", {"f": "json"})

    def collections(self) -> list[dict[str, Any]]:
        payload = self.get_json(f"{self.search_base_url}/collections")
        return payload.get("collections") or []

    def collection(self, collection_id: str = "dataset") -> dict[str, Any]:
        return self.get_json(f"{self.search_base_url}/collections/{collection_id}")

    def queryables(self, collection_id: str = "dataset") -> dict[str, Any]:
        return self.get_json(f"{self.search_base_url}/collections/{collection_id}/queryables")

    def search_datasets(
        self,
        *,
        q: str | None = None,
        item_type: str | None = None,
        title: str | None = None,
        record_id: str | None = None,
        bbox: str | tuple[float, float, float, float] | None = None,
        cql_filter: str | None = None,
        sort_by: str | None = None,
        limit: int = 10,
        startindex: int = 1,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit, "startindex": startindex}
        if q:
            params["q"] = q
        if item_type:
            params["type"] = item_type
        if title:
            params["title"] = title
        if record_id:
            params["recordId"] = record_id
        if bbox:
            params["bbox"] = ",".join(str(value) for value in bbox) if isinstance(bbox, tuple) else bbox
        if cql_filter:
            params["filter"] = cql_filter
        if sort_by:
            params["sortBy"] = sort_by
        return self.get_json(f"{self.search_base_url}/collections/dataset/items", params)

    def list_datasets(self, **kwargs: Any) -> list[HubDataset]:
        payload = self.search_datasets(**kwargs)
        return [HubDataset(feature) for feature in payload.get("features") or []]

    def iter_datasets(
        self,
        *,
        page_size: int = 100,
        max_items: int | None = None,
        **kwargs: Any,
    ) -> Iterator[HubDataset]:
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")

        returned = 0
        startindex = int(kwargs.pop("startindex", 1))
        while True:
            payload = self.search_datasets(limit=page_size, startindex=startindex, **kwargs)
            features = payload.get("features") or []
            if not features:
                return
            for feature in features:
                yield HubDataset(feature)
                returned += 1
                if max_items is not None and returned >= max_items:
                    return
            number_returned = int(payload.get("numberReturned") or len(features))
            number_matched = payload.get("numberMatched")
            startindex += number_returned
            if number_returned < page_size:
                return
            if number_matched is not None and startindex > int(number_matched):
                return

    def get_dataset(self, record_id: str) -> HubDataset:
        payload = self.get_json(f"{self.search_base_url}/collections/dataset/items/{record_id}")
        return HubDataset(payload)

    def related(self, record_id: str, *, limit: int = 10, startindex: int = 1) -> list[HubDataset]:
        payload = self.get_json(
            f"{self.search_base_url}/collections/dataset/items/{record_id}/related",
            {"limit": limit, "startindex": startindex},
        )
        return [HubDataset(feature) for feature in payload.get("features") or []]

    def connected(self, record_id: str, *, limit: int = 10, startindex: int = 1) -> list[HubDataset]:
        payload = self.get_json(
            f"{self.search_base_url}/collections/dataset/items/{record_id}/connected",
            {"limit": limit, "startindex": startindex},
        )
        return [HubDataset(feature) for feature in payload.get("features") or []]

    def arcgis_item(self, dataset_or_item_id: HubDataset | str) -> dict[str, Any]:
        item_id = dataset_or_item_id.item_id if isinstance(dataset_or_item_id, HubDataset) else split_record_id(dataset_or_item_id)[0]
        if not item_id:
            raise ValueError(f"Could not determine ArcGIS item id: {dataset_or_item_id}")
        return self.get_json(ARCGIS_ITEM_URL.format(item_id=item_id), {"f": "json"})

    def service_metadata(self, dataset: HubDataset) -> dict[str, Any] | None:
        if not dataset.service_url:
            return None
        return self.get_json(dataset.service_url, {"f": "json"})

    def layer_metadata(self, dataset: HubDataset) -> dict[str, Any] | None:
        if not dataset.service_url:
            return None
        layer_id = dataset.layer_id
        if layer_id is None:
            layers = (self.service_metadata(dataset) or {}).get("layers") or []
            if not layers:
                return None
            layer_id = int(layers[0]["id"])
        return self.get_json(f"{dataset.service_url.rstrip('/')}/{layer_id}", {"f": "json"})

    def metadata(self, record_id: str, *, include_arcgis_item: bool = True) -> dict[str, Any]:
        dataset = self.get_dataset(record_id)
        metadata: dict[str, Any] = {
            "record": dataset.record,
            "properties": dataset.properties,
            "download_links": [link.__dict__ for link in self.download_links(dataset)],
        }
        if include_arcgis_item:
            metadata["arcgis_item"] = self.arcgis_item(dataset)
        service = self.service_metadata(dataset)
        if service is not None:
            metadata["service"] = service
        layer = self.layer_metadata(dataset)
        if layer is not None:
            metadata["layer"] = layer
        return metadata

    def download_links(self, dataset: HubDataset, *, layer_id: int | None = None) -> list[DownloadLink]:
        links: list[DownloadLink] = []
        formats = dataset.download_formats
        effective_layer_id = layer_id if layer_id is not None else dataset.layer_id
        if effective_layer_id is None and dataset.service_url and formats:
            effective_layer_id = 0

        for format_key in formats:
            query = {"redirect": "false"}
            if effective_layer_id is not None:
                query["layers"] = str(effective_layer_id)
            url = self.url(
                f"{self.download_base_url}/items/{dataset.item_id}/{format_key}",
                query,
            )
            links.append(
                DownloadLink(
                    format=format_key,
                    url=url,
                    note="Hub may return an export status first; poll this URL until a download is ready.",
                )
            )

        if dataset.properties.get("size") and not dataset.service_url:
            links.append(
                DownloadLink(
                    format="item-data",
                    url=f"https://www.arcgis.com/sharing/rest/content/items/{dataset.item_id}/data",
                    note="Direct ArcGIS item data download.",
                )
            )
        return links

    def download_status(self, link: DownloadLink | str) -> dict[str, Any]:
        url = link.url if isinstance(link, DownloadLink) else link
        return self.get_json(url)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            self.url(url, params),
            headers={"User-Agent": "ucrstar/0.1"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout, context=ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def url(url: str, params: dict[str, Any] | None = None) -> str:
        if not params:
            return url
        filtered = {key: value for key, value in params.items() if value is not None}
        if not filtered:
            return url
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        return f"{url}{separator}{urllib.parse.urlencode(filtered)}"


def split_record_id(record_id: str) -> tuple[str | None, int | None]:
    match = ARCGIS_ITEM_ID_RE.match(record_id or "")
    if not match:
        return None, None
    item_id, layer_id = match.groups()
    return item_id, int(layer_id) if layer_id is not None else None


def ssl_context() -> ssl.SSLContext:
    if certifi is None:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())
