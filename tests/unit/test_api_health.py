import unittest

from fastapi.testclient import TestClient

from apps.api import create_app
from tgcurator.application import Settings


class FakeDatabase:
    def __init__(self, ready: bool) -> None:
        self.ready = ready
        self.disposed = False

    async def ping(self) -> bool:
        return self.ready

    async def dispose(self) -> None:
        self.disposed = True


class ApiHealthTests(unittest.TestCase):
    def test_liveness_and_readiness_when_database_is_ready(self) -> None:
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

    def test_readiness_returns_503_when_database_is_unavailable(self) -> None:
        app = create_app(Settings(environment="test"), FakeDatabase(ready=False))
        with TestClient(app) as client:
            response = client.get("/health/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")


if __name__ == "__main__":
    unittest.main()
