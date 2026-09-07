from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from apps.api import create_app
from tgcurator.application import Settings


class FakeDatabase:
    def __init__(self, ready: bool = True, error: Exception | None = None) -> None:
        self.ready = ready
        self.error = error
        self.disposed = False

    async def ping(self) -> bool:
        if self.error is not None:
            raise self.error
        return self.ready

    async def dispose(self) -> None:
        self.disposed = True


class ApiHealthTests(unittest.TestCase):
    def test_liveness_and_readiness_when_postgresql_is_ready(self) -> None:
        database = FakeDatabase(ready=True)
        app = create_app(Settings(environment="test", app_name="curator-test"), database)

        with TestClient(app) as client:
            live = client.get("/health/live")
            ready = client.get("/health/ready")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["checks"], {"process": "ok"})
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["checks"], {"database": "ok"})
        self.assertTrue(database.disposed)

    def test_readiness_returns_503_when_postgresql_is_unavailable(self) -> None:
        database = FakeDatabase(ready=False)
        app = create_app(Settings(environment="test"), database)

        with TestClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(response.json()["checks"], {"database": "not_ready"})
        self.assertTrue(database.disposed)

    def test_readiness_contains_probe_exceptions_and_still_disposes(self) -> None:
        database = FakeDatabase(error=RuntimeError("database credentials must not leak"))
        app = create_app(Settings(environment="test"), database)

        with TestClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"], {"database": "not_ready"})
        self.assertTrue(database.disposed)

    def test_api_readiness_does_not_require_an_inference_provider(self) -> None:
        app = create_app(Settings(environment="test"), FakeDatabase(ready=True))

        with TestClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("inference", response.json()["checks"])


if __name__ == "__main__":
    unittest.main()
