from pathlib import Path

from ucrstar2 import sources


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
