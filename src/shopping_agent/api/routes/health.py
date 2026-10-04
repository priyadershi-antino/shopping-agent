from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel

from shopping_agent import __version__
from shopping_agent.api.deps import get_runtime
from shopping_agent.db.session import ping

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    database: bool
    cache: bool
    demo_data: bool
    product_providers: list[str]
    web_providers: list[str]


@router.get("/health")
async def health(request: Request, response: Response) -> HealthResponse:
    runtime = get_runtime(request)
    db_ok = await ping(runtime.engine)
    cache_ok = await runtime.cache.ping()
    ok = db_ok and cache_ok
    if not ok:
        response.status_code = 503
    return HealthResponse(
        status="ok" if ok else "degraded",
        version=__version__,
        database=db_ok,
        cache=cache_ok,
        demo_data=runtime.providers.demo_mode,
        product_providers=[p.name for p in runtime.providers.products],
        web_providers=[w.name for w in runtime.providers.web],
    )
