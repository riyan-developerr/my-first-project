from __future__ import annotations

from fastapi.testclient import TestClient

from main import app


def test_health_and_admin_dashboard_render():
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        admin = client.get("/admin", auth=("admin", "change-this-password"))
        assert admin.status_code == 200
        assert "PrintPilot Admin" in admin.text


def test_whatsapp_verification_route():
    with TestClient(app) as client:
        response = client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "change-this-webhook-token",
                "hub.challenge": "challenge-ok",
            },
        )
        assert response.status_code == 200
        assert response.text == "challenge-ok"
