"""Small loopback health server for process supervision."""

from __future__ import annotations

from aiohttp import web


async def health(_: web.Request) -> web.Response:
    """Report liveness."""
    return web.json_response({"status": "ok"})


def create_health_app() -> web.Application:
    """Create the health HTTP application."""
    app = web.Application()
    app.router.add_get("/healthz", health)
    return app
