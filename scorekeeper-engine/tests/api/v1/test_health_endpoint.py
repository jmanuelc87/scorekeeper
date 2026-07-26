"""The liveness probe is reachable both unversioned and under the v1 prefix."""

import pytest
from fastapi.testclient import TestClient

from scorekeeper.main import app


@pytest.mark.parametrize("path", ["/health", "/api/v1/health"])
def test_health(path: str) -> None:
    with TestClient(app) as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
