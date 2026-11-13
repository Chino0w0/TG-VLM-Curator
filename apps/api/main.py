from __future__ import annotations

from typing import TYPE_CHECKING

from .app import create_app

if TYPE_CHECKING:
    from fastapi import FastAPI

    app: FastAPI

_app: FastAPI | None = None


def get_app() -> FastAPI:
    global _app
    if _app is None:
        _app = create_app()
    return _app


def __getattr__(name: str) -> object:
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return ["app", "create_app", "get_app"]


__all__ = ["app", "create_app", "get_app"]
