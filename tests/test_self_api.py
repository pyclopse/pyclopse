"""Tests for the /api/v1/self REST API routes."""

import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """FastAPI test client with a mock gateway."""
    from pyclaw.api.app import create_app
    mock_gateway = MagicMock()
    mock_gateway.config = MagicMock()
    mock_gateway.config.gateway.cors_origins = ["*"]
    app = create_app(mock_gateway)
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /api/v1/self/topics
# ---------------------------------------------------------------------------

def test_get_topics_returns_200(client):
    """/topics returns 200 with topic index content."""
    response = client.get("/api/v1/self/topics")
    assert response.status_code == 200
    assert "overview" in response.text.lower()


def test_get_topics_returns_plain_text(client):
    """/topics returns plain text content type."""
    response = client.get("/api/v1/self/topics")
    assert "text/plain" in response.headers["content-type"]


# ---------------------------------------------------------------------------
# GET /api/v1/self/topic/{path}
# ---------------------------------------------------------------------------

def test_get_topic_overview(client):
    """/topic/overview returns the overview doc."""
    response = client.get("/api/v1/self/topic/overview")
    assert response.status_code == 200
    assert "pyclaw" in response.text.lower()


def test_get_topic_nested(client):
    """/topic/architecture/gateway returns a nested topic."""
    response = client.get("/api/v1/self/topic/architecture/gateway")
    assert response.status_code == 200
    assert "Gateway" in response.text


def test_get_topic_not_found(client):
    """/topic/{missing} returns 404."""
    response = client.get("/api/v1/self/topic/nonexistent/topic/xyz")
    assert response.status_code == 404


def test_get_topic_path_traversal_rejected(client):
    """/topic with path traversal returns 400."""
    response = client.get("/api/v1/self/topic/../../etc/passwd")
    assert response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# GET /api/v1/self/source/{module}
# ---------------------------------------------------------------------------

def test_get_source_existing_module(client):
    """/source/self/loader.py returns source with line numbers."""
    response = client.get("/api/v1/self/source/self/loader.py")
    assert response.status_code == 200
    assert "DocLoader" in response.text
    assert "\t" in response.text  # line numbers


def test_get_source_not_found(client):
    """/source/{missing} returns 404."""
    response = client.get("/api/v1/self/source/does/not/exist.py")
    assert response.status_code == 404


def test_get_source_path_traversal_rejected(client):
    """/source with path traversal returns 400."""
    response = client.get("/api/v1/self/source/../../../etc/passwd")
    assert response.status_code in (400, 404)


def test_get_source_returns_plain_text(client):
    """/source returns plain text content type."""
    response = client.get("/api/v1/self/source/self/loader.py")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
