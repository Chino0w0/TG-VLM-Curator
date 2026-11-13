from __future__ import annotations

import asyncio
import unittest
from typing import Any

import httpx

from apps.api import create_app
from tgcurator.application import Settings


class SyncAsgiClient:
    def __init__(self, app: Any, *, base_url: str = "http://testserver") -> None:
        self.app = app
        self._base_url = base_url
        self._cookies = httpx.Cookies()
        self._runner: asyncio.Runner | None = None
        self._lifespan_cm: Any = None
        self._client: httpx.AsyncClient | None = None

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies if self._client is not None else self._cookies

    def __enter__(self) -> SyncAsgiClient:
        if self._runner is None:
            self._runner = asyncio.Runner()
            self._lifespan_cm = self.app.router.lifespan_context(self.app)
            self._runner.run(self._lifespan_cm.__aenter__())
            self._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self._base_url,
                cookies=self._cookies,
            )
            self._runner.run(self._client.__aenter__())
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._runner is not None:
            try:
                if self._client is not None:
                    self._cookies = self._client.cookies
                    self._runner.run(self._client.__aexit__(exc_type, exc_val, exc_tb))
                if self._lifespan_cm is not None:
                    self._runner.run(self._lifespan_cm.__aexit__(exc_type, exc_val, exc_tb))
            finally:
                self._client = None
                self._lifespan_cm = None
                self._runner.close()
                self._runner = None

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        if self._runner is not None and self._client is not None:
            return self._runner.run(self._client.request(method, url, **kwargs))

        async def _oneshot() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url=self._base_url,
                cookies=self._cookies,
            ) as client:
                resp = await client.request(method, url, **kwargs)
                self._cookies = client.cookies
                return resp

        return asyncio.run(_oneshot())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)


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

        with SyncAsgiClient(app) as client:
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

        with SyncAsgiClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(response.json()["checks"], {"database": "not_ready"})
        self.assertTrue(database.disposed)

    def test_readiness_contains_probe_exceptions_and_still_disposes(self) -> None:
        database = FakeDatabase(error=RuntimeError("database credentials must not leak"))
        app = create_app(Settings(environment="test"), database)

        with SyncAsgiClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"], {"database": "not_ready"})
        self.assertTrue(database.disposed)

    def test_api_readiness_does_not_require_an_inference_provider(self) -> None:
        app = create_app(Settings(environment="test"), FakeDatabase(ready=True))

        with SyncAsgiClient(app) as client:
            response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("inference", response.json()["checks"])


if __name__ == "__main__":
    unittest.main()
