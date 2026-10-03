from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .agent import extract_claims
from .api import create_router
from .storage.repository import StorageRepository
from .verification.service import VerificationService

ROOT = Path(__file__).resolve().parents[1]


def create_app(data_directory: Path = ROOT / "backend/data", extractor=extract_claims) -> FastAPI:
    storage = StorageRepository(data_directory)
    verification = VerificationService(storage, extractor)

    @asynccontextmanager
    async def lifespan(app):
        storage.initialize()
        yield

    app = FastAPI(title="验一下 · Claim Extraction", lifespan=lifespan)
    app.include_router(create_router(storage, verification))
    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app


app = create_app()
