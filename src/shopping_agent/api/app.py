"""FastAPI application: REST API under /v1 and the web UI at /."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from shopping_agent import __version__
from shopping_agent.api.routes import chat, health, offers, products, profile, tracking
from shopping_agent.config import Settings, get_settings
from shopping_agent.errors import (
    ConfigurationError,
    LLMRefusalError,
    LLMUnavailableError,
    ToolInputError,
    ValidationFailedError,
)
from shopping_agent.logging import configure_logging
from shopping_agent.providers.cache import Cache
from shopping_agent.runtime import Runtime, open_runtime
from shopping_agent.tracking.actions import ActionNotFoundError
from shopping_agent.web import routes as web_routes
from shopping_agent.web.routes import STATIC_DIR


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    cache: Cache | None = None,
) -> FastAPI:
    """Create the app. Pass `runtime` to reuse an existing one (tests, embedding)."""
    settings = runtime.settings if runtime else settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.background = set()
        async with AsyncExitStack() as stack:
            app.state.runtime = runtime or await stack.enter_async_context(
                open_runtime(settings, cache=cache)
            )
            try:
                yield
            finally:
                pending: set[asyncio.Task[None]] = app.state.background
                if pending:
                    await asyncio.wait(pending, timeout=30)

    app = FastAPI(
        title="Shopping Agent",
        version=__version__,
        lifespan=lifespan,
        description="Shopping research and deal-intelligence agent for India.",
    )

    @app.exception_handler(ActionNotFoundError)
    async def _no_action(_: Request, exc: ActionNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ToolInputError)
    async def _bad_reference(_: Request, exc: ToolInputError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ConfigurationError)
    async def _config(_: Request, exc: ConfigurationError) -> JSONResponse:
        return JSONResponse({"detail": f"not configured: {exc}"}, status_code=503)

    @app.exception_handler(LLMUnavailableError)
    async def _llm_down(_: Request, exc: LLMUnavailableError) -> JSONResponse:
        return JSONResponse({"detail": f"language model unavailable: {exc}"}, status_code=503)

    @app.exception_handler(LLMRefusalError)
    async def _refused(_: Request, exc: LLMRefusalError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ValidationFailedError)
    async def _invalid_output(_: Request, exc: ValidationFailedError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    for router in (
        health.router,
        chat.router,
        products.router,
        offers.router,
        profile.router,
        tracking.router,
    ):
        app.include_router(router, prefix="/v1")
    app.include_router(web_routes.router)
    app.include_router(web_routes.pages)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
