from fastapi.testclient import TestClient

from app.main import app


def test_desktop_development_origin_can_call_authenticated_api():
    response = TestClient(app).options(
        "/api/auth/login",
        headers={
            "Origin": "http://127.0.0.1:1420",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "http://127.0.0.1:1420"
    )
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unknown_web_origin_is_not_allowed():
    response = TestClient(app).options(
        "/api/auth/login",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert "access-control-allow-origin" not in response.headers
