from __future__ import annotations

from fastapi.testclient import TestClient

from moviexfone.api.app import create_app
from moviexfone.config import Settings


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    return TestClient(app)


def test_health_and_demo_page(settings: Settings) -> None:
    with _client(settings) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["voice_mode"] == "mock"
        page = client.get("/")
        assert page.status_code == 200
        assert "MovieXFone" in page.text
        assert "Moviefone" not in page.text
        assert "no-store" in page.headers["cache-control"]


def test_tool_secret_and_search(settings: Settings) -> None:
    with _client(settings) as client:
        denied = client.post("/v1/tools/search_showtimes", json={"location": "Union Square"})
        assert denied.status_code == 401
        ok = client.post(
            "/v1/tools/search_showtimes",
            json={"location": "Film Forum"},
            headers={"X-MovieXFone-Secret": "test-secret"},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["coverage_note"]
        bad = client.post(
            "/v1/tools/search_showtimes",
            json={"location": "Union Square", "format": "laser"},
            headers={"X-MovieXFone-Secret": "test-secret"},
        )
        assert bad.status_code == 422


def test_demo_ask_and_voice_socket(settings: Settings) -> None:
    with _client(settings) as client:
        asked = client.post("/v1/demo/ask", json={"text": "Times at Film Forum?"})
        assert asked.status_code == 200
        assert "Film Forum" in asked.json()["speak"]
        with client.websocket_connect("/ws/voice") as socket:
            ready = socket.receive_json()
            assert ready["mode"] == "mock"
            assert "MovieXFone" in ready["greeting"]
            socket.send_json({"type": "text", "text": "That's all, thanks."})
            user = socket.receive_json()
            assistant = socket.receive_json()
            payload = socket.receive_json()
            assert user["role"] == "user"
            assert assistant["text"] == "Enjoy the show."
            assert payload["payload"]["action"] == "end_call"


def test_builder_bundle(settings: Settings) -> None:
    with _client(settings) as client:
        bundle = client.get(
            "/v1/builder/bundle",
            headers={"X-MovieXFone-Secret": "test-secret"},
        ).json()
        names = [tool["name"] for tool in bundle["tools"]]
        assert names == ["search_showtimes", "get_movie_info", "end_call"]
        assert "web_search" not in bundle["prompt"]
        assert (
            "Do not announce" in bundle["prompt"]
            or "Do not announce the lookup" in bundle["prompt"]
        )
        assert bundle["header"] == "X-MovieXFone-Secret"


def test_rate_limit(settings: Settings, monkeypatch: object) -> None:
    tight = Settings(
        xai_api_key=settings.xai_api_key,
        live_voice=False,
        live_data=False,
        tool_secret=None,
        db_path=settings.db_path,
        data_dir=settings.data_dir,
        bind=settings.bind,
        port=settings.port,
        cors_origins=settings.cors_origins,
        rate_limit_per_minute=2,
        assemble_ttl_seconds=900,
        xai_model=settings.xai_model,
        max_session_seconds=900,
    )
    with _client(tight) as client:
        assert client.post("/v1/demo/ask", json={"text": "Times at Film Forum?"}).status_code == 200
        assert client.post("/v1/demo/ask", json={"text": "Times at Film Forum?"}).status_code == 200
        blocked = client.post("/v1/demo/ask", json={"text": "Times at Film Forum?"})
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "60"
