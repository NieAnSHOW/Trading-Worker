"""News API paths must not be consumed by the SPA catch-all mount."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api_server import SPAStaticFiles


def test_news_api_unknown_path_is_json_404_but_news_is_spa_html(tmp_path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!DOCTYPE html><html><body>SPA</body></html>", encoding="utf-8")

    app = FastAPI()
    app.mount("/", SPAStaticFiles(directory=str(dist), html=True), name="frontend")
    client = TestClient(app)

    api_response = client.get("/news-api/unknown", headers={"Accept": "text/html"})
    spa_response = client.get("/news", headers={"Accept": "text/html"})

    assert api_response.status_code == 404
    assert api_response.headers["content-type"].startswith("application/json")
    assert not api_response.text.lstrip().startswith("<")
    assert spa_response.status_code == 200
    assert spa_response.text.lstrip().startswith("<")
