from pathlib import Path

from ucrstar import sources


def test_esri_attribute_metadata_normalizes_types_and_keeps_originals() -> None:
    attributes = sources.esri_attribute_metadata(
        {
            "fields": [
                {"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "Object ID"},
                {"name": "CODE", "type": "esriFieldTypeString", "alias": "Code"},
                {"name": "HEIGHT", "type": "esriFieldTypeDouble", "alias": "Height"},
                {"name": "COUNT", "type": "esriFieldTypeSmallInteger", "alias": "Count"},
            ]
        }
    )

    assert attributes == [
        {"name": "OBJECTID", "type": "OID", "esri_type": "esriFieldTypeOID", "description": "Object ID"},
        {"name": "CODE", "type": "String", "esri_type": "esriFieldTypeString", "description": "Code"},
        {"name": "HEIGHT", "type": "Double", "esri_type": "esriFieldTypeDouble", "description": "Height"},
        {"name": "COUNT", "type": "Integer", "esri_type": "esriFieldTypeSmallInteger", "description": "Count"},
    ]


def test_source_reference_for_remote_file_uses_http_timestamp(monkeypatch) -> None:
    monkeypatch.setattr(
        sources,
        "head_url",
        lambda url: {
            "Last-Modified": "Tue, 30 Jun 2026 06:24:35 GMT",
            "Content-Type": "application/geo+json",
            "Content-Length": "42",
        },
    )

    source = sources.source_reference("https://example.com/data/roads.geojson")

    assert source["type"] == "remote_file"
    assert source["url"] == "https://example.com/data/roads.geojson"
    assert source["modified_at"] == "2026-06-30T06:24:35+00:00"
    assert source["metadata"]["filename"] == "roads.geojson"
    assert source["metadata"]["content_type"] == "application/geo+json"
    assert source["metadata"]["content_length"] == "42"


def test_hub_dataset_item_url_downloads_arcgis_item_data(monkeypatch) -> None:
    url = "https://egis-lacounty.hub.arcgis.com/datasets/cdd4c011519849caa62286044f1d31c9/about"
    calls = {}

    monkeypatch.setattr(
        sources,
        "fetch_arcgis_item",
        lambda item_id: {
            "id": item_id,
            "title": "CAMS Export Data",
            "name": "Export.gdb.zip",
            "type": "File Geodatabase",
            "typeKeywords": ["File Geodatabase", "zip"],
            "size": 608900556,
            "modified": 1782800675000,
            "description": "<p>Downloadable Address Points</p>",
            "url": None,
        },
    )

    def fake_download(download_url, target):
        calls["download_url"] = download_url
        Path(target).write_bytes(b"fake geodatabase zip")

    monkeypatch.setattr(sources, "download_url", fake_download)

    with sources.prepare_remote_source(url) as prepared:
        assert prepared.path.name == "Export.gdb.zip"
        assert prepared.path.read_bytes() == b"fake geodatabase zip"
        assert prepared.source["type"] == "esri_hub"
        assert prepared.source["url"] == url
        assert prepared.source["modified_at"] == "2026-06-30T06:24:35+00:00"
        assert prepared.source["metadata"]["title"] == "CAMS Export Data"
        assert prepared.source["metadata"]["description"] == "Downloadable Address Points"
        assert prepared.source["metadata"]["prepared_path"] == str(prepared.path)
        assert prepared.source["metadata"]["conversion"] == {"required": False, "format": "source"}

    assert calls["download_url"] == (
        "https://www.arcgis.com/sharing/rest/content/items/"
        "cdd4c011519849caa62286044f1d31c9/data"
    )


def test_prepare_arcgis_service_keeps_original_schema(monkeypatch) -> None:
    layer = {
        "id": 0,
        "name": "Buildings",
        "geometryType": "esriGeometryPolygon",
        "fields": [
            {"name": "HEIGHT", "type": "esriFieldTypeDouble", "alias": "Height"},
        ],
    }

    monkeypatch.setattr(sources, "fetch_json", lambda url, params=None, **kwargs: layer)

    def fake_export(layer_url, layer_metadata, target):
        Path(target).write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    monkeypatch.setattr(sources, "export_arcgis_layer_geojson", fake_export)

    with sources.prepare_arcgis_service(
        "https://services.example.com/FeatureServer/0",
        original_url="https://hub.example.com/datasets/buildings/about",
    ) as prepared:
        metadata = prepared.source["metadata"]
        assert metadata["attributes"] == [
            {
                "name": "HEIGHT",
                "type": "Double",
                "esri_type": "esriFieldTypeDouble",
                "description": "Height",
            }
        ]
        assert metadata["original_schema"] == layer["fields"]
