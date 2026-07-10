from __future__ import annotations

from ucrstar.esri_hub import EsriHubClient, HubDataset, split_record_id


def test_split_record_id() -> None:
    assert split_record_id("ed1937ab15214b5d937ef4fe4cb55f44_0") == (
        "ed1937ab15214b5d937ef4fe4cb55f44",
        0,
    )
    assert split_record_id("ed1937ab15214b5d937ef4fe4cb55f44") == (
        "ed1937ab15214b5d937ef4fe4cb55f44",
        None,
    )
    assert split_record_id("not-an-item") == (None, None)


def test_search_datasets_uses_ogc_collection_endpoint() -> None:
    calls = []
    client = EsriHubClient("https://egis-lacounty.hub.arcgis.com/search")

    def fake_get_json(url, params=None):
        calls.append((url, params))
        return {"features": [], "numberMatched": 0, "numberReturned": 0}

    client.get_json = fake_get_json  # type: ignore[method-assign]

    client.search_datasets(q="address", item_type="Feature Service", limit=5, startindex=6)

    assert calls == [
        (
            "https://egis-lacounty.hub.arcgis.com/api/search/v1/collections/dataset/items",
            {
                "limit": 5,
                "startindex": 6,
                "q": "address",
                "type": "Feature Service",
            },
        )
    ]


def test_iter_datasets_pages_until_limit() -> None:
    client = EsriHubClient("https://egis-lacounty.hub.arcgis.com")
    starts = []

    def fake_search_datasets(**kwargs):
        starts.append(kwargs["startindex"])
        start = kwargs["startindex"]
        return {
            "numberMatched": 3,
            "numberReturned": 2 if start == 1 else 1,
            "features": [
                {"id": f"ed1937ab15214b5d937ef4fe4cb55f4{index}", "properties": {"title": f"Item {index}"}}
                for index in range(start, min(start + kwargs["limit"], 4))
            ],
        }

    client.search_datasets = fake_search_datasets  # type: ignore[method-assign]

    datasets = list(client.iter_datasets(page_size=2, max_items=3))

    assert starts == [1, 3]
    assert [dataset.title for dataset in datasets] == ["Item 1", "Item 2", "Item 3"]


def test_download_links_use_advertised_formats_and_layer() -> None:
    client = EsriHubClient("https://egis-lacounty.hub.arcgis.com")
    dataset = HubDataset(
        {
            "id": "ed1937ab15214b5d937ef4fe4cb55f44_0",
            "properties": {
                "title": "Address Points",
                "type": "Feature Layer",
                "url": "https://services.arcgis.com/example/FeatureServer",
                "properties": {
                    "downloads": {
                        "formats": [
                            {"key": "csv", "hidden": False},
                            {"key": "geojson", "hidden": False},
                            {"key": "kml", "hidden": True},
                        ]
                    }
                },
            },
        }
    )

    links = client.download_links(dataset)

    assert [link.format for link in links] == ["csv", "geojson"]
    assert links[0].url == (
        "https://egis-lacounty.hub.arcgis.com/api/download/v1/items/"
        "ed1937ab15214b5d937ef4fe4cb55f44/csv?redirect=false&layers=0"
    )


def test_metadata_combines_record_item_service_and_layer() -> None:
    client = EsriHubClient("https://egis-lacounty.hub.arcgis.com")
    dataset = HubDataset(
        {
            "id": "ed1937ab15214b5d937ef4fe4cb55f44_0",
            "properties": {
                "title": "Address Points",
                "type": "Feature Layer",
                "url": "https://services.arcgis.com/example/FeatureServer",
            },
        }
    )
    client.get_dataset = lambda record_id: dataset  # type: ignore[method-assign]
    client.arcgis_item = lambda value: {"id": dataset.item_id, "title": "Address Points"}  # type: ignore[method-assign]
    client.service_metadata = lambda value: {"layers": [{"id": 0, "name": "Address Points"}]}  # type: ignore[method-assign]
    client.layer_metadata = lambda value: {"id": 0, "geometryType": "esriGeometryPoint"}  # type: ignore[method-assign]

    metadata = client.metadata("ed1937ab15214b5d937ef4fe4cb55f44_0")

    assert metadata["arcgis_item"]["id"] == "ed1937ab15214b5d937ef4fe4cb55f44"
    assert metadata["service"]["layers"][0]["id"] == 0
    assert metadata["layer"]["geometryType"] == "esriGeometryPoint"
