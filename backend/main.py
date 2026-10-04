from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .api.error_handlers import register_exception_handlers
from .api.routes import create_router
from .extraction.agent import extract_claims
from .verification.capabilities import VerificationCapabilities
from .storage.repository import StorageRepository
from .verification.service import VerificationService

ROOT = Path(__file__).resolve().parents[1]


def create_app(
    data_directory: Path = ROOT / "backend/data", extractor=extract_claims,
    *, subgraphs=None, capabilities: VerificationCapabilities | None = None,
) -> FastAPI:
    storage = StorageRepository(data_directory)
    verification = VerificationService(storage, extractor, subgraphs=subgraphs, capabilities=capabilities)

    @asynccontextmanager
    async def lifespan(app):
        storage.initialize()
        yield

    app = FastAPI(title="验一下 · Verification", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(create_router(storage, verification))
    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app


app = create_app()
