"""Local PA Agent application; PAknow provides evidence retrieval internally."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from apps.api.pa_agent import router as pa_agent_router
from paknow.retrieval.pa_corpus import check_corpus
from paknow.retrieval.qdrant_store import check_store_ready


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    check_store_ready()
    check_corpus()
    yield


app = FastAPI(
    title="PA Agent",
    lifespan=_lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(pa_agent_router)


@app.get("/", include_in_schema=False)
def home() -> RedirectResponse:
    return RedirectResponse("/pa-agent")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
